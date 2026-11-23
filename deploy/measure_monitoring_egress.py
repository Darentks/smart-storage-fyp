"""Compare actual HEAD/current readiness handlers using a deterministic cloud stub."""
import argparse
import ast
import json
import math
from pathlib import Path
import subprocess
import time
import types
import urllib.parse
import urllib.request
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

def live_python_metrics(base_url,seconds,interval):
    """Observe bridge counters; probes go only to the local bridge."""
    def snapshot():
        with urllib.request.urlopen(base_url.rstrip('/')+'/local_operations',timeout=5) as response:value=json.load(response)
        if value.get('inventory_contract')!='runtime-v1':raise ValueError('Runtime contract required')
        return value['run_id'],value['cloud_metrics']
    try:
        run_id,before=snapshot();start=time.monotonic()
        while time.monotonic()-start<seconds:
            time.sleep(min(interval,max(0,seconds-(time.monotonic()-start))))
            latest_id,after=snapshot()
            if latest_id!=run_id:raise ValueError('Bridge restarted during measurement')
        delta={key:after[key]-before[key] for key in before}
        if any(value<0 for value in delta.values()):raise ValueError('Counters reset')
        return dict(status='MEASURED',duration_seconds=time.monotonic()-start,counters=delta,
            scope='Python PostgREST adapter only; request bytes estimated, response body bytes counted; not wire egress')
    except (OSError,ValueError,KeyError,TypeError):
        return dict(status='BLOCKED',requests=None,reason='Bridge unavailable, wrong contract, restarted or counters invalid; no live egress claim')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='.runtime/phase6-egress-fixture.json')
    parser.add_argument('--bridge-url');parser.add_argument('--seconds',type=float,default=10);parser.add_argument('--interval',type=float,default=2)
    args=parser.parse_args()
    if not all(math.isfinite(v) for v in (args.seconds,args.interval)) or not 0<args.seconds<=86400 or not .1<=args.interval<=60:parser.error('Finite duration >0..86400 and interval .1..60 required')
    before=subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix(),'show','HEAD:'+SERVICE],cwd=ROOT).decode('utf8')
    report=dict(scope='Ten healthy status polls, six shelves, deterministic cloud stub; not actual network traffic or whole-application egress',
        baseline_revision=subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix(),'rev-parse','HEAD'],cwd=ROOT).decode().strip(),
        before=measure(before),after=measure((ROOT/SERVICE).read_text(encoding='utf8')),
        actual_network=dict(python_rest=live_python_metrics(args.bridge_url,args.seconds,args.interval) if args.bridge_url else dict(status='NOT_MEASURED',requests=None),
            python_sdk=dict(status='NOT_MEASURED',requests=None,code_policy='Disabled in runtime contract'),
            browser_rest=dict(status='NOT_MEASURED',requests=None,how='Read window.cloudRequestMetrics for this window; decoded body bytes are not wire egress'),
            browser_sdk=dict(status='NOT_MEASURED',requests=None,code_policy='Removed from dashboards and optional React service')),
        release='BLOCKED',note='Fixture counts are simulated. Do not add them to network totals or infer zero total Supabase egress.')
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
