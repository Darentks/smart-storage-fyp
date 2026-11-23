"""Isolated regression runner and explicit field blockers; never auto-approve hardware."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]


def run_check(name,command,env):
    try:
        result=subprocess.run(command,cwd=ROOT,text=True,capture_output=True,env=env,timeout=120)
        output=result.stdout+'\n'+result.stderr
        skips=len(re.findall(r'\.\.\. skipped ',output)) + len(re.findall(r'^SKIP:',output,re.M))
        return dict(case=name,status='FAIL' if result.returncode else 'SKIP' if skips else 'PASS',
            skipped=skips,scope='isolated fixtures only',evidence=output[-2000:])
    except (OSError,subprocess.TimeoutExpired):
        return dict(case=name,status='BLOCKED',reason='Required local executable unavailable or test exceeded 120 seconds')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default=str(ROOT/'.runtime/phase6-acceptance.json'))
    args=parser.parse_args();started=time.monotonic();env=dict(os.environ)
    manifest=ROOT/'.runtime/edge-models/manifest.json'
    if manifest.exists():env['EDGE_TEST_MANIFEST']=str(manifest)
    matrix=[run_check('isolated_python_safety_suite',[sys.executable,'-B','-m','unittest','discover','-s','VisionValidation_Module/backend/tests','-p','test_*.py','-v'],env)]
    for script in sorted((ROOT/'VisionValidation_Module/backend/tests').glob('*.cjs')):
        matrix.append(run_check(script.name,['node',str(script)],env))
    matrix.append(run_check('native_unit_contract',['node','VisionValidation_Module/edge_service/test.cjs'],env))
    matrix += [dict(case=name,status='BLOCKED',reason=reason) for name,reason in {
        'fresh_exact_startup_restart':'Physical quiet three-pair baseline/restart proof required',
        'real_frame_browser_native_parity':'Genuine same-frame TM and pose parity with bound camera geometry required',
        'physical_three_cctv_consensus':'Physical shelf/action agreement and side-presence proof required',
        'physical_borrow_return_browser_closed':'Real removal and return while dashboard is closed required',
        'range_2_3_and_3_4_x01':'Both physical X01 ranges without guessed B1 or accepted ambiguous action required',
        'quiet_rebaseline_after_hold':'Physical HOLD to quiet recovery proof; unresolved intent must stay HELD',
        'stale_duplicate_events':'Physical delayed/duplicate event rejection without second update required',
        'bridge_crash_mid_action':'Actual termination/restart must discard evidence and infer no historical action',
        'camera_mqtt_device_database_outage':'Field reconnect proof; cached device bindings must permit local inventory during DB outage',
        'roi_source_swap':'Physical swapped-source rejection plus accepted source-bound ROI deployment required',
        'service_ownership_rollback':'Real single writer and monitoring-only browser/shadow rollback required',
        'real_egress_measurements':'Python, SDK and browser live counts/bytes require separate evidence; fixtures are insufficient',
        'sustained_cpu_ram_latency':'Actual bridge/native soak and measured latency/action outcomes required',
        'host_migration_reboot':'Final host restart, protected data directory and local ROI persistence unproven',
        'operator_field_approval':'No real operator-approved runtime-v1 evidence supplied'
    }.items()]
    report=dict(version=2,created_at=time.time(),duration_seconds=time.monotonic()-started,
        scope='isolated tests only; no intentional live camera/cloud operations',release='BLOCKED',matrix=matrix,
        inventory_contract='runtime-v1',invariants='Per-process RAM only; quiet fresh baseline on reboot/recovery; cloud restricted to device administration')
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2))
    print(json.dumps({'release':'BLOCKED','isolated_tests':matrix[0]['status'],'report':str(path)}))
    return 1 if any(row['status']=='FAIL' for row in matrix) else 2

if __name__=='__main__':raise SystemExit(main())
