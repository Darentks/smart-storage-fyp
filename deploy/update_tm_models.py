"""Back up and replace the local Front/Back bundle only after isolated validation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.bridge_runtime import InstanceLock
from src.tm_sources import configured_sources, installed_sources

BACKUP_NAME=re.compile(r'edge-models-backup-[0-9-]{1,40}')
LOADED_TIMEOUT_SECONDS=150


def node_environment():
    allowed={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','HOME','USERPROFILE','LOCALAPPDATA','APPDATA'}
    return {key:value for key,value in os.environ.items() if key.upper() in allowed}


def stage(root,destination,node):
    subprocess.run([node,str(root/'VisionValidation_Module/edge_service/stage_models.cjs'),str(destination)],
        cwd=root,env=node_environment(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=180)


def verify(root,manifest,node):
    script=("const {Inference}=require(process.argv[1]);const engine=new Inference();"
        "engine.init(process.argv[2],'pair').then(async v=>{await engine.selfTest(process.argv[2]);console.log(JSON.stringify(v))})"
        ".catch(()=>process.exit(1));")
    result=subprocess.run([node,'-e',script,str(root/'VisionValidation_Module/edge_service/inference.cjs'),str(manifest)],
        cwd=root,env=node_environment(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,check=True,timeout=180)
    return json.loads(result.stdout.strip().splitlines()[-1])


def promote(candidate,active,previous):
    had_active=active.exists()
    if had_active:active.rename(previous)
    try:candidate.rename(active)
    except BaseException:
        if had_active:
            try:previous.rename(active)
            except OSError:shutil.copytree(previous,active)
        raise


def model_directories(root):
    root=Path(root).resolve();runtime=root/'.runtime';runtime.mkdir(exist_ok=True)
    active=runtime/'edge-models'
    if not runtime.resolve().is_relative_to(root) or not active.resolve().is_relative_to(runtime.resolve()):
        raise ValueError('Model directories must stay inside this checkout')
    configured=os.getenv('SMART_STORAGE_EDGE_MANIFEST')
    if configured and (root/configured).resolve()!=(active/'manifest.json').resolve():
        raise ValueError('This launcher requires the standard local model directory')
    return root,runtime,active


def fingerprint(sources):
    return hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()


def show_sources(root=ROOT):
    """What the launcher shows before anything is stopped or downloaded."""
    root,_,active=model_directories(root)
    sources,origin=configured_sources(root)
    return {'ok':True,'sources':sources,'origin':origin,'installed':installed_sources(active/'manifest.json'),
        'fingerprint':fingerprint(sources)}


def update_models(root=ROOT,node='node',expected=None,progress=None):
    root,runtime,active=model_directories(root)
    progress={} if progress is None else progress
    with InstanceLock(runtime/'model-update.lock'),InstanceLock(runtime/'bridge.lock'):
        if expected is not None and fingerprint(configured_sources(root)[0])!=expected:
            raise ValueError('The saved model links changed after they were confirmed')
        progress['stage']='backup'
        backup=None
        if active.exists():
            backup=runtime/('edge-models-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            shutil.copytree(active,backup)
        temporary=Path(tempfile.mkdtemp(prefix='model-update-',dir=runtime)).resolve()
        if not temporary.is_relative_to(runtime.resolve()):raise ValueError('Invalid staging directory')
        completed=False
        try:
            candidate=temporary/'candidate'
            progress['stage']='download'
            stage(root,candidate,node)
            if expected is not None:
                built=installed_sources(candidate/'manifest.json')
                if built is None or fingerprint(built)!=expected:
                    raise ValueError('Downloaded models do not come from the confirmed links')
            progress['stage']='verify'
            # This action updates two TM models, not the independently staged pose model.
            if active.exists():
                old=json.loads((active/'manifest.json').read_text(encoding='utf-8'))
                pose=(active/old['pose']['path']).resolve()
                if not pose.is_relative_to(active.resolve()):raise ValueError('Invalid pose path')
                data=pose.read_bytes()
                if hashlib.sha256(data).hexdigest()!=old['pose']['sha256']:raise ValueError('Existing pose hash mismatch')
                (candidate/'yolo11n-pose.onnx').write_bytes(data)
                manifest=json.loads((candidate/'manifest.json').read_text(encoding='utf-8'))
                manifest['pose']={'path':'yolo11n-pose.onnx','sha256':old['pose']['sha256']}
                (candidate/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
            versions=verify(root,candidate/'manifest.json',node)
            progress['stage']='activate'
            promote(candidate,active,temporary/'previous')
            completed=True
        finally:
            # Never erase the recovery copy if Windows also prevented rollback.
            if completed or not (temporary/'previous').exists():
                try:shutil.rmtree(temporary)
                except OSError:pass  # A cleanup lock must not misreport a committed update as a failed one.
        return {'ok':True,'backup':backup.name if backup else None,'versions':versions,
            'cleanup_pending':temporary.name if temporary.exists() else None}


def restore_backup(backup_name,root=ROOT,node='node'):
    """Put a backup back after new models were activated but did not load. The backup itself is kept."""
    if not isinstance(backup_name,str) or not BACKUP_NAME.fullmatch(backup_name):raise ValueError('Invalid backup name')
    root,runtime,active=model_directories(root)
    backup=runtime/backup_name
    if not (backup/'manifest.json').is_file():raise ValueError('Backup is missing')
    with InstanceLock(runtime/'model-update.lock'),InstanceLock(runtime/'bridge.lock'):
        temporary=Path(tempfile.mkdtemp(prefix='model-update-',dir=runtime)).resolve()
        completed=False
        try:
            candidate=temporary/'candidate'
            shutil.copytree(backup,candidate)
            versions=verify(root,candidate/'manifest.json',node)
            promote(candidate,active,temporary/'previous')
            completed=True
        finally:
            if completed or not (temporary/'previous').exists():
                try:shutil.rmtree(temporary)
                except OSError:pass
        return {'ok':True,'restored':backup_name,'versions':versions}


def tree_digest(folder):
    if not folder.is_dir():return None
    digest=hashlib.sha256()
    for path in sorted(p for p in folder.rglob('*') if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode()+b'\0'+hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def safe_update(root=ROOT,node='node',expected=None):
    """update_models with an honest failure report: say 'retained' only after comparing the files."""
    root,runtime,active=model_directories(root)
    before=tree_digest(active)
    existing={p.name for p in runtime.iterdir()}
    progress={}
    try:return update_models(root,node,expected,progress)
    except Exception:
        try:retained=tree_digest(active)==before
        except OSError:retained=False
        created=sorted(p for p in runtime.iterdir() if p.name not in existing)
        backup=next((p.name for p in created if BACKUP_NAME.fullmatch(p.name)),None)
        recovery=[p.name+'/previous' for p in created if (p/'previous').is_dir()]
        return {'ok':False,'stage':progress.get('stage','start'),'retained':retained,'backup':backup,
            'recovery':recovery[0] if recovery else None}


def read_edge_status(port=8008,timeout=3):
    # Loopback only. The local development certificate is not in Python's trust store.
    for scheme in ('http','https'):
        try:
            context=ssl._create_unverified_context() if scheme=='https' else None
            with urllib.request.urlopen(f'{scheme}://127.0.0.1:{port}/edge_status',timeout=timeout,context=context) as response:
                return json.loads(response.read(1_000_000))
        except (OSError,ValueError):continue
    return None


def confirm_loaded(front,back,read=read_edge_status,timeout=LOADED_TIMEOUT_SECONDS,clock=time.monotonic,sleep=time.sleep):
    """loaded: the running bridge reports exactly these weights. failed: it reports others or cannot load.
    unconfirmed: it never said, so success must not be claimed."""
    expected={'Front':front,'Back':back};deadline=clock()+timeout;problem=False
    while True:
        status=read()
        if isinstance(status,dict):
            versions=status.get('model_versions') or {}
            loaded={role:(versions.get(role) or {}).get('weights_sha256') for role in expected}
            if all(loaded.values()):return {'state':'loaded' if loaded==expected else 'failed'}
            # The bridge retries loading, so only a problem that persists until the deadline counts.
            problem=str(status.get('setup_issue') or '').startswith('Box-reading models unavailable')
        if clock()>=deadline:return {'state':'failed' if problem else 'unconfirmed'}
        sleep(2)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--show-sources',action='store_true')
    action.add_argument('--restore',metavar='BACKUP')
    action.add_argument('--confirm-loaded',nargs=2,metavar=('FRONT_SHA256','BACK_SHA256'))
    parser.add_argument('--expect',metavar='FINGERPRINT',help='fingerprint of the sources the user confirmed')
    args=parser.parse_args(argv)
    from src.config_loader import find_and_load_env
    find_and_load_env()
    node=os.getenv('SMART_STORAGE_NODE','node')
    try:
        if args.show_sources:result=show_sources()
        elif args.restore:result=restore_backup(args.restore,node=node)
        elif args.confirm_loaded:result=dict(confirm_loaded(*args.confirm_loaded),ok=True)
        else:result=safe_update(node=node,expected=args.expect)
    except Exception:
        result={'ok':False}
    print(json.dumps(result));return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
