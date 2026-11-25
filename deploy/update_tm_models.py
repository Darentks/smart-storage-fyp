"""Back up and replace the local Front/Back bundle only after isolated validation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.bridge_runtime import InstanceLock


def node_environment():
    allowed={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','HOME','USERPROFILE','LOCALAPPDATA','APPDATA'}
    return {key:value for key,value in os.environ.items() if key.upper() in allowed}


def stage(root,destination,node):
    subprocess.run([node,str(root/'VisionValidation_Module/edge_service/stage_models.cjs'),str(destination)],
        cwd=root,env=node_environment(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=180)


def verify(root,manifest,node):
    script="const {Inference}=require(process.argv[1]);new Inference().init(process.argv[2],'pair').then(v=>console.log(JSON.stringify(v))).catch(()=>process.exit(1));"
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


def update_models(root=ROOT,node='node'):
    root=Path(root).resolve();runtime=root/'.runtime';runtime.mkdir(exist_ok=True)
    active=runtime/'edge-models'
    if not runtime.resolve().is_relative_to(root) or not active.resolve().is_relative_to(runtime.resolve()):
        raise ValueError('Model directories must stay inside this checkout')
    configured=os.getenv('SMART_STORAGE_EDGE_MANIFEST')
    if configured and (root/configured).resolve()!=(active/'manifest.json').resolve():
        raise ValueError('This launcher requires the standard local model directory')
    with InstanceLock(runtime/'model-update.lock'),InstanceLock(runtime/'bridge.lock'):
        backup=None
        if active.exists():
            backup=runtime/('edge-models-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            shutil.copytree(active,backup)
        temporary=Path(tempfile.mkdtemp(prefix='model-update-',dir=runtime)).resolve()
        if not temporary.is_relative_to(runtime.resolve()):raise ValueError('Invalid staging directory')
        completed=False
        try:
            candidate=temporary/'candidate'
            stage(root,candidate,node)
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
            promote(candidate,active,temporary/'previous')
            completed=True
        finally:
            # Never erase the recovery copy if Windows also prevented rollback.
            if completed or not (temporary/'previous').exists():
                try:shutil.rmtree(temporary)
                except OSError:pass  # A cleanup lock must not misreport a committed update as a failed one.
        return {'ok':True,'backup':backup.name if backup else None,'versions':versions,
            'cleanup_pending':temporary.name if temporary.exists() else None}


def main():
    from src.config_loader import find_and_load_env
    find_and_load_env()
    try:
        result=update_models(node=os.getenv('SMART_STORAGE_NODE','node'))
    except Exception:
        print(json.dumps({'ok':False,'message':'Model update failed. Existing models were retained; check internet, Node and the staged model configuration.'}))
        return 1
    print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())
