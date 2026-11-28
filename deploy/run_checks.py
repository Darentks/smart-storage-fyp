"""Isolated regression runner and explicit field blockers; never auto-approve hardware."""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
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
    # Load the pure capture contract without importing optional camera dependencies.
    policy=runpy.run_path(str(ROOT/'VisionValidation_Module/backend/src/dual_cam_inventory.py'))
    required,maximum=policy['REQUIRED_MATCHING_PAIRS'],policy['MAX_PAIRS_PER_ATTEMPT']
    matrix=[run_check('isolated_python_safety_suite',[sys.executable,'-B','-m','unittest','discover','-s','VisionValidation_Module/backend/tests','-p','test_*.py','-v'],env)]
    for script in sorted((ROOT/'VisionValidation_Module/backend/tests').glob('*.cjs')):
        matrix.append(run_check(script.name,['node',str(script)],env))
    matrix.append(run_check('native_unit_contract',['node','VisionValidation_Module/edge_service/test.cjs'],env))
    matrix += [dict(case=name,status='BLOCKED',reason=reason) for name,reason in {
        'fresh_exact_startup_restart':f'Physical quiet startup/restart proof required: {required} consecutive matching ESP32 pairs, at most {maximum} pairs per attempt, CCTV off, and no restored RAM inventory',
        'real_frame_browser_native_parity':'Genuine same-frame TM and pose parity with bound camera geometry required',
        'physical_three_cctv_consensus':'Physical shelf/action agreement and side-presence proof required',
        'physical_borrow_return_browser_closed':'Real removal and return while dashboard is closed required',
        'range_2_3_and_3_4_x01':'Both physical X01 ranges without guessed B1 or accepted ambiguous action required',
        'quiet_rebaseline_after_hold':'Physical HOLD to quiet recovery proof; unresolved intent must stay HELD',
        'stale_duplicate_events':'Physical delayed/duplicate event rejection without second update required',
        'bridge_crash_mid_action':'Actual termination/restart must discard evidence and infer no historical action',
        'camera_mqtt_device_database_outage':'Physical outage/reconnect proof required: CCTV gaps up to 5 seconds preserve counts while actions require current frames; longer gaps require fresh baselines; pausing CCTV (/cctv_control) retains counts/timestamps, resuming it or a bridge start recounts; MQTT gates actions, not initial counts; cached device bindings permit local inventory during DB outage',
        'roi_source_swap':'Physical swapped-source rejection plus accepted source-bound ROI deployment required',
        'service_ownership_rollback':'Real single writer and monitoring-only browser/shadow rollback required',
        'real_egress_measurements':'Python, SDK and browser live counts/bytes require separate evidence; fixtures are insufficient',
        'sustained_cpu_ram_latency':'Actual bridge plus separate box-count and pose worker soak required, including worker failure recovery, CPU/RAM, latency and missed/wrongly accepted actions',
        'host_migration_reboot':'Final host restart, protected data directory and local ROI persistence unproven',
        'operator_field_approval':'No real operator-approved runtime-v1 evidence supplied'
    }.items()]
    report=dict(version=2,created_at=time.time(),duration_seconds=time.monotonic()-started,
        scope='isolated tests only; no intentional live camera/cloud operations',release='BLOCKED',matrix=matrix,
        status_meanings={'PASS':'Executed check passed within its stated scope; fixtures do not establish hardware acceptance',
            'FAIL':'Executed check failed','SKIP':'Check was not fully executed, for example because a test-only dependency is missing',
            'BLOCKED':'Required execution or field evidence is unavailable; field checklist entries are not automatically satisfied by this runner'},
        inventory_contract='runtime-v1',invariants='Per-process RAM only; quiet fresh baseline on reboot/recovery; cloud restricted to device administration')
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2))
    print(json.dumps({'release':'BLOCKED','isolated_tests':matrix[0]['status'],'report':str(path)}))
    return 1 if any(row['status']=='FAIL' for row in matrix) else 2

if __name__=='__main__':raise SystemExit(main())
