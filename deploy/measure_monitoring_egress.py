"""Compare actual HEAD/current readiness handlers using a deterministic cloud stub."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import time
import types
import urllib.parse
ROOT=Path(__file__).resolve().parents[1]
SERVICE='VisionValidation_Module/camera_service.py'

def measure(source):
    tree=ast.parse(source);method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='do_GET')
    metrics={'inventory_reads':0,'simulated_response_body_bytes':0}
    def load(shelf):
        row=dict(shelf_id=shelf,revision=1)
        metrics['inventory_reads']+=1;metrics['simulated_response_body_bytes']+=len(json.dumps(row).encode())
        return row
    readiness=types.SimpleNamespace(run_id='fixture',get=lambda shelf:dict(state='Ready',revision=1),set=lambda *a:None)
    ns=dict(urllib=types.SimpleNamespace(parse=urllib.parse),time=time,PROCESSOR_AUTHORITY='browser',READINESS=readiness,
        get_cached_compartments=lambda:[dict(shelf_id=f'SHELF_R1_C{i}') for i in range(1,7)],load_inventory_state=load,
        mqtt_bridge=types.SimpleNamespace(is_connected=True),RECHECKS=types.SimpleNamespace(busy=lambda shelf:False))
    exec(compile(ast.Module(body=[method],type_ignores=[]),'<readiness source>','exec'),ns)
    responses=[];handler=types.SimpleNamespace(path='/shelf_readiness',_send_json=lambda code,value:responses.append(code))
    for _ in range(10):ns['do_GET'](handler)
    if responses!=[200]*10:raise ValueError('Readiness fixture failed')
    return metrics

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='.runtime/phase6-egress-fixture.json');args=parser.parse_args()
    before=subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix(),'show','HEAD:'+SERVICE],cwd=ROOT).decode('utf8')
    report=dict(scope='Ten healthy status polls, six shelves, deterministic cloud stub; not actual network traffic or whole-application egress',
        baseline_revision=subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix(),'rev-parse','HEAD'],cwd=ROOT).decode().strip(),
        before=measure(before),after=measure((ROOT/SERVICE).read_text(encoding='utf8')))
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
