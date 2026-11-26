"""Update the local Front/Back Teachable Machine models in two phases.

prepare  (bridge may keep running): read the links from deploy/tm-model-sources.json, download both exports
         into a staging folder, check that they load, and compare each role with the active models.
         Unchanged -> staging is deleted and nothing else happens. Changed -> the staged candidate is kept.
install  (bridge must be stopped): back up the active models and swap in the prepared candidate.

Every outcome is written to .runtime/model-update-result.json (the bridge shows it on the dashboard and
checks it against the models it actually loaded) and appended to the bounded .runtime/model-update.log.
The pose model is never replaced by this tool."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.bridge_runtime import InstanceLock, redact, TM_SOURCE_RULE as SOURCE_RULE  # noqa: E402

SOURCES_FILE='deploy/tm-model-sources.json'
ROLES=('Front','Back')
CANDIDATE_RULE=re.compile(r'^model-update-[A-Za-z0-9_-]+$')
LOG_LINES=200


class UpdateFailure(Exception):
    """A failure the operator can act on: which part (stage) and which model (role) failed."""
    def __init__(self,stage,message,role=None):
        super().__init__(message);self.stage=stage;self.role=role;self.message=message
    def as_dict(self):return {'stage':self.stage,'role':self.role,'message':redact(self.message)[:300]}


def node_environment():
    allowed={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','HOME','USERPROFILE','LOCALAPPDATA','APPDATA'}
    return {key:value for key,value in os.environ.items() if key.upper() in allowed}


def load_sources(root):
    path=Path(root)/SOURCES_FILE
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):raise UpdateFailure('settings',f'Cannot read the model settings file {SOURCES_FILE}')
    if not isinstance(data,dict) or data.get('version')!=1:raise UpdateFailure('settings',f'{SOURCES_FILE} must contain "version": 1')
    for role in ROLES:
        if not isinstance(data.get(role),str) or not SOURCE_RULE.fullmatch(data[role]):
            raise UpdateFailure('settings',f'Please input the correct Teachable Machine link for the {role} model in {SOURCES_FILE} (https://teachablemachine.withgoogle.com/models/<id>/)',role)
    if data['Front']==data['Back']:raise UpdateFailure('settings','Front and Back links must be different models')
    return {role:data[role] for role in ROLES}


def stage(root,destination,node):
    try:
        result=subprocess.run([node,str(root/'VisionValidation_Module/edge_service/stage_models.cjs'),str(destination),'--sources',str(root/SOURCES_FILE)],
            cwd=root,env=node_environment(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=300)
    except FileNotFoundError:raise UpdateFailure('download','Node.js was not found; install Node or set SMART_STORAGE_NODE')
    except subprocess.TimeoutExpired:raise UpdateFailure('download','Downloading the models took longer than 5 minutes')
    if result.returncode!=0:
        try:report=json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError,IndexError):report={}
        raise UpdateFailure(report.get('stage') or 'download',report.get('reason') or 'Model download failed',report.get('role'))


def verify(root,manifest,node):
    """Load both models exactly as the bridge does; returns their loaded versions."""
    script=("const {Inference}=require(process.argv[1]);new Inference().init(process.argv[2],'pair')"
            ".then(v=>console.log(JSON.stringify({ok:true,versions:v})))"
            ".catch(e=>{console.log(JSON.stringify({ok:false,role:e&&e.role||null,error:String(e&&e.message||e).slice(0,200)}));process.exitCode=1;});")
    try:
        result=subprocess.run([node,'-e',script,str(root/'VisionValidation_Module/edge_service/inference.cjs'),str(manifest)],
            cwd=root,env=node_environment(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=180)
    except FileNotFoundError:raise UpdateFailure('check','Node.js was not found; install Node or set SMART_STORAGE_NODE')
    except subprocess.TimeoutExpired:raise UpdateFailure('check','Loading the models took longer than 3 minutes')
    try:report=json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError,IndexError):report={'ok':False,'error':'The model check produced no result'}
    if not report.get('ok'):raise UpdateFailure('check','Model did not load: '+str(report.get('error')),report.get('role'))
    return report['versions']


def role_fingerprint(folder,manifest,role):
    """Sorted (path, sha256) of one role's files, with every file re-hashed on disk; None if anything is off."""
    try:
        spec=manifest[role];entries=[spec['model'],spec['metadata'],*spec['weights']]
        rows=[]
        for entry in entries:
            path=(folder/entry['path']).resolve()
            if not path.is_relative_to(folder.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:return None
            rows.append((entry['path'],entry['sha256']))
        return tuple(sorted(rows))
    except (OSError,KeyError,TypeError,ValueError):return None


def bundle_fingerprint(folder):
    """Fingerprint of a whole bundle (both roles and pose), or None if missing or corrupt."""
    try:manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return None
    roles=[role_fingerprint(folder,manifest,role) for role in ROLES]
    try:
        pose=manifest['pose'];data=(folder/pose['path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=pose['sha256']:return None
    except (OSError,KeyError,TypeError):return None
    return None if None in roles else (tuple(roles),pose['sha256'])


def file_hashes(folder):
    hashes=set()
    for path in folder.rglob('*'):
        try:
            if path.is_file():hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:hashes.add('unreadable:'+str(path))  # never matches a backup, so its folder is kept
    return hashes


def sweep_staging(runtime,active):
    """Delete leftover staging folders only when nothing in them is unique: every file still inside must be
    byte-identical to a file in the active models or a backup. This also clears half-deleted folders left by
    an interrupted cleanup. Returns what is still pending."""
    leftovers=[folder for folder in sorted(runtime.glob('model-update-*')) if folder.is_dir()]
    if not leftovers:return []
    known=set()
    for bundle in [active,*runtime.glob('edge-models-backup-*')]:
        if bundle.is_dir():known|=file_hashes(bundle)
    pending=[]
    for folder in leftovers:
        if not file_hashes(folder)<=known:
            pending.append(folder.name+': kept, it holds model files with no identical backup');continue
        try:shutil.rmtree(folder)
        except OSError:pending.append(folder.name+': locked (for example by OneDrive); will retry on the next update')
    return pending


def promote(candidate,active,previous):
    had_active=active.exists()
    if had_active:active.rename(previous)
    try:candidate.rename(active)
    except BaseException:
        if had_active:
            try:previous.rename(active)
            except OSError:shutil.copytree(previous,active)
        raise


def check_layout(root):
    root=Path(root).resolve();runtime=root/'.runtime';runtime.mkdir(exist_ok=True)
    active=runtime/'edge-models'
    if not runtime.resolve().is_relative_to(root) or not active.resolve().is_relative_to(runtime.resolve()):
        raise UpdateFailure('settings','Model directories must stay inside this project folder')
    configured=os.getenv('SMART_STORAGE_EDGE_MANIFEST')
    if configured and (root/configured).resolve()!=(active/'manifest.json').resolve():
        raise UpdateFailure('settings','SMART_STORAGE_EDGE_MANIFEST points elsewhere; this tool only updates .runtime/edge-models')
    return root,runtime,active


def update_lock(runtime):
    try:return InstanceLock(runtime/'model-update.lock').__enter__()
    except RuntimeError:raise UpdateFailure('settings','Another model update is already running')


def short(version):
    value=(version or {}).get('weights_sha256') if isinstance(version,dict) else None
    return value if isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value) else None


def prepare(root=ROOT,node='node'):
    root,runtime,active=check_layout(root)
    lock=update_lock(runtime)
    try:
        cleanup=sweep_staging(runtime,active)
        sources=load_sources(root)
        temporary=Path(tempfile.mkdtemp(prefix='model-update-',dir=runtime)).resolve()
        keep=False
        try:
            candidate=temporary/'candidate'
            stage(root,candidate,node)
            if (active/'manifest.json').exists():
                # This tool updates the two TM models, not the independently staged pose model.
                try:
                    old=json.loads((active/'manifest.json').read_text(encoding='utf-8'))
                    pose=(active/old['pose']['path']).resolve()
                    if not pose.is_relative_to(active.resolve()):raise ValueError
                    data=pose.read_bytes()
                    if hashlib.sha256(data).hexdigest()!=old['pose']['sha256']:raise ValueError
                except (OSError,ValueError,KeyError,TypeError):raise UpdateFailure('check','The installed pose model is missing or changed; it was not touched')
                (candidate/'yolo11n-pose.onnx').write_bytes(data)
                manifest=json.loads((candidate/'manifest.json').read_text(encoding='utf-8'))
                manifest['pose']={'path':'yolo11n-pose.onnx','sha256':old['pose']['sha256']}
                (candidate/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
            new_versions=verify(root,candidate/'manifest.json',node)
            try:old_versions=verify(root,active/'manifest.json',node) if (active/'manifest.json').exists() else {}
            except UpdateFailure:old_versions={}  # an unloadable active bundle simply counts as changed
            try:active_manifest=json.loads((active/'manifest.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):active_manifest=None
            candidate_manifest=json.loads((candidate/'manifest.json').read_text(encoding='utf-8'))
            roles={}
            for role in ROLES:
                before=role_fingerprint(active,active_manifest,role) if active_manifest else None
                after=role_fingerprint(candidate,candidate_manifest,role)
                roles[role]=dict(old=short(old_versions.get(role)),new=short(new_versions.get(role)),
                    changed=before is None or before!=after,source=sources[role])
            changed=any(row['changed'] for row in roles.values())
            result=dict(outcome='prepared' if changed else 'unchanged',roles=roles,cleanup_pending=cleanup)
            if changed:
                result['candidate']=temporary.name
                (temporary/'prepared.json').write_text(json.dumps(result),encoding='utf-8')
                keep=True
            return result
        finally:
            if not keep:
                try:shutil.rmtree(temporary)
                except OSError:pass  # swept on the next update
    finally:lock.__exit__(None,None,None)


def install(name,root=ROOT):
    root,runtime,active=check_layout(root)
    if not isinstance(name,str) or not CANDIDATE_RULE.fullmatch(name):raise UpdateFailure('install','Invalid prepared update name')
    temporary=(runtime/name).resolve();candidate=temporary/'candidate'
    if not temporary.is_relative_to(runtime.resolve()) or not (temporary/'prepared.json').is_file():
        raise UpdateFailure('install','The prepared update is missing; run the update again')
    prepared=json.loads((temporary/'prepared.json').read_text(encoding='utf-8'))
    if bundle_fingerprint(candidate) is None:raise UpdateFailure('install','The prepared models changed on disk; run the update again')
    lock=update_lock(runtime)
    try:
        try:bridge=InstanceLock(runtime/'bridge.lock').__enter__()
        except RuntimeError:raise UpdateFailure('install','The bridge is still running, so the models were not replaced')
        try:
            backup=None
            if active.exists():
                backup=runtime/('edge-models-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
                try:shutil.copytree(active,backup)
                except OSError:raise UpdateFailure('install','Could not back up the current models; nothing was replaced')
            try:promote(candidate,active,temporary/'previous')
            except OSError:raise UpdateFailure('install','Could not replace the model folder (it may be locked); the previous models were restored')
        finally:bridge.__exit__(None,None,None)
        cleanup=list(prepared.get('cleanup_pending') or [])
        try:shutil.rmtree(temporary)
        except OSError:cleanup.append(name+': locked (for example by OneDrive); will retry on the next update')
        return dict(prepared,outcome='installed',candidate=None,backup=backup.name if backup else None,cleanup_pending=cleanup)
    finally:lock.__exit__(None,None,None)


def update_models(root=ROOT,node='node'):
    """Both phases in one call (bridge must be stopped for the install). Used by tests and manual runs."""
    result=prepare(root,node)
    return install(result['candidate'],root) if result['outcome']=='prepared' else result


def record(root,result):
    """Write the latest result for the dashboard and append one redacted line to the bounded log."""
    runtime=Path(root)/'.runtime'
    text=json.dumps(result)
    try:
        temporary=runtime/'model-update-result.json.tmp';temporary.write_text(text,encoding='utf-8')
        os.replace(temporary,runtime/'model-update-result.json')
    except OSError:pass
    try:
        log=runtime/'model-update.log'
        lines=log.read_text(encoding='utf-8').splitlines()[-(LOG_LINES-1):] if log.exists() else []
        log.write_text('\n'.join(lines+[redact(text)])+'\n',encoding='utf-8')
    except OSError:pass


def main(argv=None):
    from src.config_loader import find_and_load_env
    find_and_load_env()
    argv=list(sys.argv[1:] if argv is None else argv)
    phase=argv[0] if argv else 'both'
    attempt=argv[2] if phase=='install' and len(argv)>2 else (argv[1] if phase=='prepare' and len(argv)>1 else str(uuid.uuid4()))
    base=dict(id=attempt,at=time.time(),phase=phase)
    try:
        if phase=='prepare':result=prepare(ROOT,os.getenv('SMART_STORAGE_NODE','node'))
        elif phase=='install':result=install(argv[1] if len(argv)>1 else '',ROOT)
        elif phase=='both':result=update_models(ROOT,os.getenv('SMART_STORAGE_NODE','node'))
        else:raise UpdateFailure('settings','Unknown phase '+phase[:20])
        result=dict(base,ok=True,**result)
    except UpdateFailure as failure:
        result=dict(base,ok=False,outcome='failed',failure=failure.as_dict())
    except Exception:
        result=dict(base,ok=False,outcome='failed',failure={'stage':phase,'role':None,'message':'Unexpected error; existing models were kept'})
    record(ROOT,result)
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
