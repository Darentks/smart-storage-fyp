"""Run one bridge with finite crash backoff; no secrets on the command line."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.bridge_runtime import write_process_record, remove_process_record  # noqa: E402
RECORD=ROOT/'.runtime/bridge-supervisor.json'

def main():
    failures=0
    while failures<5:
        child=subprocess.Popen([sys.executable,str(ROOT/'VisionValidation_Module/camera_service.py')],cwd=ROOT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:code=child.wait()
        except KeyboardInterrupt:
            child.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGTERM)
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()
            return 0
        if code==0:return 0
        failures+=1
        if failures<5:time.sleep(min(30,2**failures))
    return 1

if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    if hasattr(signal,'SIGBREAK'):signal.signal(signal.SIGBREAK,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # The launcher stops a recorded supervisor first so it cannot respawn the bridge during a restart.
    write_process_record(RECORD,'supervisor')
    try:code=main()
    finally:remove_process_record(RECORD)
    raise SystemExit(code)
