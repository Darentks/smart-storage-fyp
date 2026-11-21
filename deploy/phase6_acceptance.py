"""Repeatable isolated acceptance runner. Never labels hardware as passed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default=str(ROOT/'.runtime/phase6-acceptance.json'))
    args=parser.parse_args();started=time.monotonic()
    env=dict(os.environ)
    manifest=ROOT/'.runtime/edge-models/manifest.json'
    if manifest.exists():env['EDGE_TEST_MANIFEST']=str(manifest)
    completed=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','VisionValidation_Module/backend/tests','-p','test_*.py'],cwd=ROOT,text=True,capture_output=True,env=env)
    matrix=[dict(case='isolated_python_safety_suite',status='PASS' if completed.returncode==0 else 'FAIL',
        evidence=completed.stderr[-2000:])]
    matrix += [dict(case=name,status='PASS' if completed.returncode==0 else 'FAIL',scope='isolated fixtures only',evidence=file) for name,file in {
        'startup_restart_baseline_guards':'test_periodic_recheck.py / test_inventory_persistence.py',
        'trigger_lease_consensus_direction':'test_cctv_dispatch.py / test_autonomous_edge.py',
        'atomic_receipt_revision_replay':'test_inventory_persistence.py; PostgreSQL WASM scripts run separately',
        'uncertainty_unknown_m2_relocation':'test_dual_cam_inventory.py',
        'local_five_retention_crash_restart':'test_phase6_local.py',
        'roi_identity_overlap_reboot':'test_phase6_local.py / test_autonomous_edge.py',
        'zero_cloud_reads_for_healthy_status':'test_phase6_local.py',
        'operator_authorization':'test_periodic_recheck.py; SQL tests run separately',
        'single_writer_denial':'test_autonomous_edge.py'
    }.items()]
    matrix += [dict(case=name,status='BLOCKED',reason=reason) for name,reason in {
        'strict_count_device_only_cloud':'Current atomic RPC still uses cloud slots/revisions/receipts and reconciliation history; migration not performed',
        'fresh_startup_restart':'Requires physical shelf/three fresh pairs',
        'physical_borrow_return_browser_closed':'No real hardware transaction proof supplied',
        'camera_mqtt_database_outage_recovery':'Only mocks tested; field outage proof outstanding',
        'real_frame_roi_model_parity':'Requires genuine recorded frames and bound camera calibrations',
        'physical_reconciliation_without_cloud_audit':'Existing reconciliation RPC still writes cloud audit; replacement blocked by migration',
        'real_egress_measurements':'REST instrumentation available; no live before/after traffic sample, SDK/browser not covered',
        'sustained_soak':'No hardware soak duration/conditions measured',
        'host_migration_reboot':'Local files tested; actual deployment reboot/volume persistence not tested'
    }.items()]
    report=dict(version=1,created_at=time.time(),duration_seconds=time.monotonic()-started,
        scope='isolated tests only; no live hardware/cloud requests intentionally performed',release='BLOCKED',matrix=matrix,
        invariants='Existing cloud atomic quantity+slots+revision+event retained; not equivalent to count-only cloud storage')
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2))
    print(json.dumps({'release':'BLOCKED','isolated_tests':matrix[0]['status'],'report':str(path)}))
    return 1 if completed.returncode else 2  # CI must never treat blocked hardware/storage as release PASS.
if __name__=='__main__':raise SystemExit(main())
