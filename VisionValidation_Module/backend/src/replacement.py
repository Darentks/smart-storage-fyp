"""Local replacement orchestration. No inventory, pictures or credentials are persisted here."""
import configparser
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid

from .camera_slots import SLOTS, slot_for

# Shared with every bridge firmware upload/build entry point.
FIRMWARE_LOCK=threading.Lock()


class MaintenanceStore:
    def __init__(self,path):
        self.path=Path(path);self.records=[];self.error=None;self.lock=threading.RLock()
        try:
            if self.path.exists():
                value=json.loads(self.path.read_text(encoding='utf8'))
                if not isinstance(value,list):raise ValueError()
                seen=set()
                for row in value:
                    slot=slot_for(row.get('camera_slot'))
                    if (set(row)!={'camera_slot','shelf_id','state'} or not slot or
                        row['shelf_id']!=slot['shelf_id'] or row['state']!='Maintenance' or row['shelf_id'] in seen):raise ValueError()
                    seen.add(row['shelf_id'])
                self.records=value
        except (OSError,ValueError,TypeError,AttributeError):
            self.error='Maintenance record unreadable or invalid; local operator recovery required'

    def blocked(self):
        return {s['shelf_id'] for s in SLOTS} if self.error else {r['shelf_id'] for r in self.records}

    def save(self,records):
        from .local_operations import atomic_json
        atomic_json(self.path,records)
        self.records=records

    def begin(self,slot):
        with self.lock:
            if self.error:raise ValueError(self.error)
            if any(r['shelf_id']==slot['shelf_id'] and r['camera_slot']!=slot['label'] for r in self.records):
                raise ValueError('Another replacement already owns this shelf')
            records=[r for r in self.records if r['shelf_id']!=slot['shelf_id']]
            self.save(records+[dict(camera_slot=slot['label'],shelf_id=slot['shelf_id'],state='Maintenance')])

    def finish(self,shelf):
        with self.lock:
            if self.error:raise ValueError(self.error)
            self.save([r for r in self.records if r['shelf_id']!=shelf])


def validate_browser(peer,headers,origin,mutation=False):
    if peer!='127.0.0.1':raise ValueError('Wired replacement is available only on the bridge laptop at 127.0.0.1')
    # Same-origin GET may omit Origin; mutations always require it.
    supplied=headers.get('Origin')
    if supplied!=origin and (mutation or supplied is not None):raise ValueError('Open the trusted local dashboard')
    if mutation and headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
        raise ValueError('JSON request required')


def firmware_tools(root):
    repo=Path(os.getenv('EDGE_RELAY_REPO_PATH') or Path(root).parent/'EdgeHardwareRelay').resolve()
    config=configparser.ConfigParser(interpolation=None)
    try:
        config.read(repo/'platformio.ini')
        valid=all('env:'+s['environment'] in config and config['env:'+s['environment']].get('custom_cam_id')==s['device_id'] and
            config['env:'+s['environment']].get('custom_shelf_id')==s['shelf_id'] and
            config['env:'+s['environment']].get('custom_ip_octet')==s['ip_address'].split('.')[-1] for s in SLOTS)
        valid=valid and (repo/'src/main.cpp').is_file() and (repo/'http_ota_provisioner.py').is_file()
    except (OSError,configparser.Error):valid=False
    executable=os.getenv('SMART_STORAGE_PLATFORMIO') or shutil.which('pio') or shutil.which('platformio')
    if not executable:
        executable=next((str(p) for p in (Path.home()/'.platformio/penv/Scripts/pio.exe',
            Path.home()/'.platformio/penv/Scripts/platformio.exe') if p.is_file()),None)
    return repo,executable,bool(valid)


def serial_ports(pio):
    if not pio:return []
    result=subprocess.run([pio,'device','list','--json-output'],capture_output=True,text=True,timeout=15)
    if result.returncode:raise ValueError('Serial enumeration failed; check PlatformIO and USB drivers')
    rows=json.loads(result.stdout)
    return [dict(port=r['port'],description=r.get('description','USB serial')) for r in rows
        if isinstance(r,dict) and isinstance(r.get('port'),str) and r['port'].upper().startswith('COM') and r['port'][3:].isdigit()]


def camera_status(ip):
    try:
        with urllib.request.urlopen('http://'+ip+'/status',timeout=2) as response:
            value=json.load(response)
            return value if isinstance(value,dict) else None
    except (OSError,ValueError):return None


class ReplacementService:
    def __init__(self,root,store,enter,verify,recover):
        self.root=Path(root);self.store=store;self.enter=enter;self.verify=verify;self.recover=recover
        self.job=None;self.guard=threading.RLock()

    def status(self):
        repo,pio,valid=firmware_tools(self.root)
        with self.guard:
            return dict(repository_available=valid,platformio_available=bool(pio),maintenance=list(self.store.records),
                blocker=self.store.error,job=dict(self.job) if self.job else None)

    def ports(self):return serial_ports(firmware_tools(self.root)[1])

    def update(self,**values):
        with self.guard:self.job.update(values)

    def begin(self,label):
        slot=slot_for(label)
        if not slot:raise ValueError('Invalid camera slot')
        with self.guard:
            if self.job and self.job.get('busy'):raise ValueError('Replacement operation busy')
            self.enter(slot)  # callback persists first, then invalidates under coordinator lock
            self.job=dict(id=str(uuid.uuid4()),camera_slot=label,shelf_id=slot['shelf_id'],stage='Maintenance active',busy=False,verified=False)
        return self.status()

    def start(self,label,port,confirmed,retry=False,repair=False):
        slot=slot_for(label)
        with self.guard:
            if not slot or not self.job or self.job['camera_slot']!=label:raise ValueError('Begin replacement first')
            if self.job.get('busy'):raise ValueError('Replacement operation busy')
            if not any(r['camera_slot']==label for r in self.store.records):raise ValueError('Maintenance is required')
            repo,pio,valid=firmware_tools(self.root)
            if not retry or not self.job.get('physical_mac'):
                if confirmed is not True:raise ValueError('Confirm physical disconnection first')
                if not valid or not pio:raise ValueError('Install PlatformIO and the configured firmware repository')
                if port not in [r['port'] for r in self.ports()]:raise ValueError('Refresh and select a detected COM port')
                if repair and not self.job.get('physical_mac'):raise ValueError('Flash and identify the physical replacement before identity repair')
            if not FIRMWARE_LOCK.acquire(blocking=False):raise ValueError('Another firmware operation is running')
            self.job.update(busy=True,verified=False,error=None)
            # Ordinary application exit waits for an upload; the UI offers no unsafe cancel.
            try:threading.Thread(target=self._run,args=(slot,repo,pio,port,retry,repair),daemon=False).start()
            except RuntimeError:
                self.job['busy']=False;FIRMWARE_LOCK.release();raise ValueError('Unable to start firmware operation') from None
        return self.status()

    def _command(self,args,repo,timeout,repair_nonce=None):
        # Never publish compiler output: command lines may contain deployment credentials.
        environment=dict(os.environ)
        environment.pop('SMART_STORAGE_REPAIR_IDENTITY',None)
        if repair_nonce:environment['SMART_STORAGE_REPAIR_IDENTITY']=repair_nonce
        process=subprocess.Popen(args,cwd=repo,env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
        try:code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=='nt':
                # A timed-out pio parent must not leave esptool running past the operation lock.
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,timeout=15)
            else:process.kill()
            process.wait(timeout=15)
            raise ValueError('Firmware operation timed out; maintenance retained. Check USB/power before retrying.') from None
        if code:raise ValueError('PlatformIO operation failed; check USB wiring, bootloader mode and local configuration')

    def _physical_mac(self,pio,port,repo):
        # Confirmed against PlatformIO's installed package exec implementation: no --call/shell.
        result=subprocess.run([pio,'pkg','exec','--package','tool-esptoolpy','--',
            'esptool.py','--chip','esp32','--port',port,'read_mac'],cwd=repo,
            capture_output=True,text=True,timeout=30)
        matches=re.findall(r'MAC:\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})',result.stdout)
        if result.returncode or len(set(matches))!=1:raise ValueError('Cannot identify the physical ESP32 over USB. Enter bootloader mode and retry; no identity repair is authorized.')
        return matches[0].lower()

    def _run(self,slot,repo,pio,port,retry,repair=False):
        try:
            if retry and not self.job.get('physical_mac'):
                self.update(stage='Identifying replacement over USB; reset into normal mode afterwards')
                self.update(physical_mac=self._physical_mac(pio,port,repo))
            if not retry:
                self.update(stage='Checking disconnected camera')
                physical_mac=self._physical_mac(pio,port,repo)
                if repair and self.job.get('physical_mac')!=physical_mac:raise ValueError('Physical replacement changed; identity repair rejected')
                self.update(physical_mac=physical_mac)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    responses=list(pool.map(camera_status,[s['ip_address'] for s in SLOTS]))
                for configured,status in zip(SLOTS,responses):
                    if status and (configured['ip_address']==slot['ip_address'] or status.get('device_id')==slot['device_id']):
                        if str(status.get('mac_address','')).lower()!=physical_mac:
                            raise ValueError('A different physical device responds with the selected IP or identity; disconnect the old camera before flashing')
                self.update(stage='Building firmware')
                repair_nonce=uuid.uuid4().hex if repair else None
                self._command([pio,'run','-e',slot['environment'],'-t','clean'],repo,60)
                self._command([pio,'run','-e',slot['environment']],repo,300,repair_nonce=repair_nonce)
                artifact=repo/'.pio/build'/slot['environment']/'firmware.bin'
                if not artifact.is_file() or artifact.stat().st_size<10000:raise ValueError('Selected environment firmware artifact missing')
                self.update(stage='Uploading firmware')
                self._command([pio,'run','-e',slot['environment'],'-t','upload','--upload-port',port],repo,180,repair_nonce=repair_nonce)
                self.update(stage='USB upload complete')
            boundary=time.time();self.update(stage='Waiting for camera reboot')
            deadline=time.monotonic()+75;reason='Camera not reachable'
            while time.monotonic()<deadline:
                try:
                    self.update(stage='Camera verification in progress')
                    status=camera_status(slot['ip_address'])
                    if not self.job.get('physical_mac') or not status or str(status.get('mac_address','')).lower()!=self.job['physical_mac']:
                        raise ValueError('Waiting for the physically identified replacement; check power and reset into normal mode')
                    self.verify(slot,boundary)
                    break
                except ValueError as exc:reason=str(exc);time.sleep(2)
            else:raise ValueError(reason+'; retry verification without reflashing. For NVS mismatch, use verified physical serial recovery; no assumed-IP writes are made.')
            self.update(stage='Camera verified',verified=True)
            self.update(stage='Shelf baseline pending')
            self.recover(slot)
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            message=str(exc) if isinstance(exc,ValueError) else 'Build/upload failed. Check PlatformIO, USB driver, power and GPIO0 bootloader wiring. Retry after correction.'
            self.update(stage='Failed / Maintenance required',error=message)
        finally:
            self.update(busy=False);FIRMWARE_LOCK.release()
