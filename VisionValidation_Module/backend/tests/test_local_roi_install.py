import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).parents[1]))
from src.local_operations import RoiStore
from src.runtime_inventory import RuntimeInventory
from src.preview_panel import NativeObservations
from src.autonomous_edge import AutonomousProcessor, ActionTracker
from src.bridge_access import BridgeAccessError
from test_phase6_local import rois

BRIDGE=Path(__file__).parents[2]/'camera_service.py'


def bridge_method(name,**globals_):
    tree=ast.parse(BRIDGE.read_text(encoding='utf8'))
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
    ns=dict(Path=Path,tempfile=tempfile,os=os,subprocess=subprocess,sys=sys,json=json,__file__=str(BRIDGE),
        READINESS=types.SimpleNamespace(run_id='run-1'),**globals_)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'bridge','exec'),ns)
    return ns[name]


class LocalRoiInstallTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.store=RoiStore(Path(self.folder.name)/'data'/'camera-rois.json')

    def tearDown(self):self.folder.cleanup()

    def call(self,body,reload=None,client='127.0.0.1',authorization=None):
        def verify(header):
            if header!='Bearer user-session':raise BridgeAccessError(401,'Sign in to configure the shared bridge')
        handler=types.SimpleNamespace(client_address=(client,0),headers={'Authorization':authorization},_send_json=Mock())
        bridge_method('_install_local_rois',LOCAL_ROIS=self.store,NATIVE_CALIBRATION_RELOAD=reload,
            BridgeAccessError=BridgeAccessError,require_application_user=verify)(handler,body)
        return handler._send_json.call_args.args

    def test_remote_client_is_rejected_without_writing(self):
        status,_=self.call(rois(),client='192.168.0.50')
        self.assertEqual(status,401);self.assertFalse(self.store.path.exists())

    def test_remote_application_user_without_admin_claim_can_install(self):
        status,payload=self.call(rois(),client='192.168.0.50',authorization='Bearer user-session')
        self.assertEqual(status,200);self.assertEqual(payload['status'],'installed')
        self.assertEqual(len(self.store.load()['cameras']),3)

    def test_invalid_calibration_returns_422_with_cli_output_and_leaves_store(self):
        value=rois();value['cameras'][0]['camera_role']='SIDE_PRESENCE'
        status,payload=self.call(value)
        self.assertEqual(status,422);self.assertIn('mismatch',payload['output'])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.call({})[0],400)

    def test_install_without_native_service_writes_store_and_cleans_temp(self):
        before=set(Path(tempfile.gettempdir()).glob('roi-upload-*.json'))
        status,payload=self.call(rois())
        self.assertEqual(status,200);self.assertEqual(payload['native']['status'],'not_running')
        self.assertEqual(len(self.store.load()['cameras']),3)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob('roi-upload-*.json')),before)

    def test_install_triggers_native_reload_or_reports_blocked(self):
        reload=Mock()
        status,payload=self.call(rois(),reload)
        self.assertEqual(status,200);self.assertEqual(payload['native']['status'],'verifying');reload.assert_called_once()
        status,payload=self.call(rois(),Mock(side_effect=ValueError('SERVICE_CUTOVER_BLOCKED: acceptance missing, expired or mismatched')))
        self.assertEqual(status,200);self.assertEqual(payload['native']['status'],'blocked')
        self.assertIn('SERVICE_CUTOVER_BLOCKED',payload['native']['message'])

    def test_reload_clears_baselines_and_schedules_fresh_verification(self):
        runtime=RuntimeInventory('run-1')
        row=runtime._state('SHELF_R1_C1');row.update(state='Ready',ready_at=1,baseline=dict(slots=[1,1,1,0,0],total=3,precision='EXACT',count_range=[3,3]))
        runtime.invalidate();runtime.clear_baselines()
        snapshot=runtime.snapshot('SHELF_R1_C1')
        self.assertEqual(snapshot['state'],'Offline');self.assertIsNone(snapshot['baseline']);self.assertIsNone(snapshot['current_quantity'])
        grid={f'SHELF_R1_C{j}':dict(x_min=(j-1)/6,x_max=j/6,y_min=0,y_max=1) for j in range(1,7)}
        from unittest.mock import Mock as M
        processor=AutonomousProcessor('run-1',M(),[],[],M(),M(),M(),M(),M(),M())
        processor.last_frames={'CCTV_01':5};processor.health={'CCTV_01':{'state':'Online','at':1}}
        processor.retry_at={'SHELF_R1_C1':10**12};processor.exhausted={'SHELF_R1_C1'};processor.attempts={'SHELF_R1_C1':3}
        processor.calibration_issue='old problem'
        fresh=[dict(id='CCTV_01',role='GRID_6_ROI',source='0',tracker=ActionTracker('GRID_6_ROI',grid))]
        processor.reload_calibration(fresh)
        self.assertIs(processor.cameras,fresh);self.assertEqual(processor.retry_at,{})
        self.assertEqual(processor.exhausted,set());self.assertEqual(processor.attempts,{});self.assertIsNone(processor.calibration_issue)
        self.assertEqual(processor.last_frames,{});self.assertEqual(processor.health,{})


class RetiredColumnTests(unittest.TestCase):
    def test_only_columns_one_to_three_are_active(self):
        from src.shelves import SHELF_IDS
        from src.local_operations import validate_rois
        self.assertEqual(SHELF_IDS,('SHELF_R1_C1','SHELF_R1_C2','SHELF_R1_C3'))
        runtime=RuntimeInventory('run-1')
        for retired in ('SHELF_R1_C4','SHELF_R1_C5','SHELF_R1_C6'):
            with self.assertRaisesRegex(ValueError,'Unknown shelf'):runtime.snapshot(retired)
        # Older six-shelf exports still install; retired regions are dropped, never stored.
        installed=validate_rois(rois())
        for camera in installed['cameras'][:2]:self.assertEqual(sorted(camera['rois']),list(SHELF_IDS))
        value=rois()
        for camera in value['cameras'][:2]:camera['rois']={s:r for s,r in camera['rois'].items() if s in SHELF_IDS}
        self.assertEqual(validate_rois(value),installed)


if __name__=='__main__':unittest.main()
