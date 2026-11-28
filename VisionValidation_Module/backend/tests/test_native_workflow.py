"""Bridge-level behaviour of the simplified native workflow (Retry scan, consensus view, readiness, startup defaults)."""
import ast
import re
import sys
import threading
import time
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).parents[1]))
from src.bridge_access import BridgeAccessError
from src.runtime_inventory import RuntimeInventory
from src.shelves import SHELF_SET

BRIDGE=Path(__file__).parents[2]/'camera_service.py'
SOURCE=BRIDGE.read_text(encoding='utf8')


def extract(name,**globals_):
    node=next(n for n in ast.walk(ast.parse(SOURCE)) if isinstance(n,(ast.FunctionDef)) and n.name==name)
    ns=dict(globals_);exec(compile(ast.Module(body=[node],type_ignores=[]),str(BRIDGE),'exec'),ns)
    return ns[name]


class RetryScanTests(unittest.TestCase):
    def setUp(self):
        self.runtime=RuntimeInventory('run',2);self.processor=Mock()
        self.readiness=Mock();self.pending=[dict(shelf_id='SHELF_R1_C1',trigger_id='old'),dict(shelf_id='SHELF_R1_C2',trigger_id='other')]
        self.captures={'a':dict(shelf_id='SHELF_R1_C1'),'b':dict(shelf_id='SHELF_R1_C2')}
        self.busy=set();self.auth=Mock()

    def call(self,body,client='127.0.0.1',processor=True,authority=True):
        handler=types.SimpleNamespace(client_address=(client,0),headers={'Authorization':'Bearer x'},_send_json=Mock())
        method=extract('_request_scan',AUTONOMOUS=self.processor if processor else None,RUNTIME_INVENTORY=self.runtime if authority else None,
            RECHECKS=types.SimpleNamespace(global_lock=threading.RLock(),busy=lambda sid:sid in self.busy),READINESS=self.readiness,
            SHELF_SET=SHELF_SET,CCTV_CONSENSUS_LOCK=threading.RLock(),PENDING_CCTV_TRIGGERS=self.pending,
            DUAL_CAM_CAPTURE_LOCK=threading.Lock(),DUAL_CAM_CAPTURES=self.captures,BridgeAccessError=BridgeAccessError,
            require_application_user=self.auth)
        method(handler,body);return handler._send_json.call_args.args

    def make_ready(self,sid,action_capable=True):
        row=self.runtime._state(sid);row.update(state='Ready',ready_at=1,baseline=dict(slots=[1,1,1,1,1],total=5,precision='EXACT',count_range=[5,5],action_capable=action_capable))

    def test_cctv_control_uses_application_access_and_strict_boolean(self):
        handler=types.SimpleNamespace(client_address=('192.168.0.50',0),headers={},_send_json=Mock())
        method=extract('_control_cctv',AUTONOMOUS=self.processor,require_application_user=self.auth,
            BridgeAccessError=BridgeAccessError,_PERSISTENT_STREAMS_LOCK=threading.Lock(),_PERSISTENT_STREAMS={})
        self.auth.side_effect=BridgeAccessError(401,'Sign in')
        method(handler,{'enabled':True});self.assertEqual(handler._send_json.call_args.args[0],401)
        self.processor.set_cctv_enabled.assert_not_called()
        self.auth.side_effect=None
        for body in ({'enabled':'false'},{},[]):
            method(handler,body);self.assertEqual(handler._send_json.call_args.args[0],400)
        method(handler,{'enabled':True});self.assertEqual(handler._send_json.call_args.args[0],200)
        self.processor.set_cctv_enabled.assert_called_once_with(True)

    def test_remote_callers_need_an_application_session(self):
        self.auth.side_effect=BridgeAccessError(401,'Sign in to configure the shared bridge')
        status,payload=self.call({'shelf_id':'SHELF_R1_C1'},client='192.168.0.50')
        self.assertEqual(status,401);self.readiness.set.assert_not_called();self.processor.reset_scan_budget.assert_not_called()
        self.auth.side_effect=None;self.assertEqual(self.call({'shelf_id':'SHELF_R1_C1'},client='192.168.0.50')[0],200)
        self.auth.reset_mock();self.call({'shelf_id':'SHELF_R1_C1'});self.auth.assert_not_called()

    def test_unknown_shelf_and_missing_processor_are_rejected_without_side_effects(self):
        self.assertEqual(self.call({'shelf_id':'SHELF_R1_C6'})[0],400)
        self.assertEqual(self.call({},processor=False)[0],409);self.assertEqual(self.call({},authority=False)[0],409)
        self.readiness.set.assert_not_called()

    def test_single_shelf_retry_resets_only_that_shelf_and_purges_its_stale_evidence(self):
        self.make_ready('SHELF_R1_C1');self.make_ready('SHELF_R1_C2')
        status,payload=self.call({'shelf_id':'SHELF_R1_C1'})
        self.assertEqual((status,payload['shelves']),(200,['SHELF_R1_C1']))
        self.assertEqual(self.runtime.snapshot('SHELF_R1_C1')['state'],'Starting');self.assertEqual(self.runtime.snapshot('SHELF_R1_C2')['state'],'Ready')
        self.assertEqual([t['trigger_id'] for t in self.pending],['other']);self.assertEqual(list(self.captures),['b'])
        self.processor.reset_scan_budget.assert_called_once_with('SHELF_R1_C1')
        self.readiness.set.assert_called_once_with('SHELF_R1_C1','Starting','Fresh count requested')

    def test_scan_all_keeps_healthy_counts_and_skips_busy_shelves(self):
        self.make_ready('SHELF_R1_C1',action_capable=True);self.make_ready('SHELF_R1_C2',action_capable=False);self.busy.add('SHELF_R1_C3')
        status,payload=self.call({})
        self.assertEqual(status,200);self.assertEqual(payload['shelves'],['SHELF_R1_C2']);self.assertEqual(payload['busy'],['SHELF_R1_C3'])
        self.assertEqual(self.runtime.snapshot('SHELF_R1_C1')['state'],'Ready','a healthy action-ready count is never discarded by scan-all')

    def test_request_is_routed_with_the_same_exemption_as_roi_install_and_never_reads_inventory_from_the_browser(self):
        self.assertIn("'/local_rois','/scan_request','/cctv_control','/training_session'}",SOURCE)
        self.assertIn("if path == '/scan_request':",SOURCE)


class ConsensusAndReadinessTests(unittest.TestCase):
    def test_consensus_view_shows_live_votes_pending_events_and_drops_old_ones(self):
        now=time.time()
        view=extract('consensus_view',time=time,CONSENSUS_WINDOW_SEC=4.0,CCTV_CONSENSUS_LOCK=threading.RLock(),
            CCTV_ALIGN_STATE={'CCTV_01':dict(ts=now-1,action='BORROW',shelf_id='SHELF_R1_C2',camera_role='GRID_6_ROI',conf=.9),
                'CCTV_02':dict(ts=now-9,action='RETURN',shelf_id='SHELF_R1_C1',camera_role='GRID_6_ROI',conf=.9)},
            PENDING_CCTV_TRIGGERS=[dict(shelf_id='SHELF_R1_C2',expected_action='BORROW',claimed=False,timestamp=now-2),dict(shelf_id='SHELF_R1_C3',timestamp=now-300)])()
        self.assertEqual(sorted(view['votes']),['CCTV_01']);self.assertEqual(view['votes']['CCTV_01']['shelf_id'],'SHELF_R1_C2')
        self.assertEqual([(p['shelf_id'],p['action'],p['claimed']) for p in view['pending']],[('SHELF_R1_C2','BORROW',False)])
        self.assertNotIn('conf',view['votes']['CCTV_01'])

    def readiness_response(self,authority,processor,mqtt):
        readiness=types.SimpleNamespace(run_id='r',get=lambda s:dict(state='Ready',revision=1),set=Mock(),recovery_retries={})
        ns=dict(urllib=types.SimpleNamespace(parse=urllib.parse),READINESS=readiness,PROCESSOR_AUTHORITY='service',RUNTIME_INVENTORY=authority,AUTONOMOUS=processor,
            get_cached_compartments=lambda:[dict(shelf_id='SHELF_R1_C1')],mqtt_bridge=types.SimpleNamespace(is_connected=mqtt),RECHECKS=types.SimpleNamespace(busy=lambda s:False),
            SHELF_SET=SHELF_SET,time=time)
        method=next(n for n in ast.walk(ast.parse(SOURCE)) if isinstance(n,ast.FunctionDef) and n.name=='do_GET')
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(BRIDGE),'exec'),ns)
        handler=types.SimpleNamespace(path='/shelf_readiness',_send_json=Mock());ns['do_GET'](handler)
        return readiness,handler._send_json.call_args.args[1]

    def test_service_mode_keeps_counts_visible_when_mqtt_drops_and_reports_native_view(self):
        authority=RuntimeInventory('r',2);processor=Mock();processor.shelf_status.return_value={'actions_ready':False,'actions_paused_reason':'MQTT unavailable'}
        readiness,payload=self.readiness_response(authority,processor,mqtt=False)
        self.assertFalse(any(call.args[1]=='Offline' for call in readiness.set.call_args_list),'MQTT loss must not erase counts')
        row=payload['shelves']['SHELF_R1_C1'];self.assertFalse(payload['mqtt_connected'])
        self.assertEqual(row['native']['actions_paused_reason'],'MQTT unavailable');self.assertIn('action_capable',row['runtime'])

    def test_non_service_modes_still_report_offline_when_mqtt_drops(self):
        readiness=types.SimpleNamespace(run_id='r',get=lambda s:dict(state='Ready',revision=1),set=Mock())
        ns=dict(urllib=types.SimpleNamespace(parse=urllib.parse),READINESS=readiness,PROCESSOR_AUTHORITY='browser',get_cached_compartments=lambda:[dict(shelf_id='SHELF_R1_C1')],
            mqtt_bridge=types.SimpleNamespace(is_connected=False),RECHECKS=types.SimpleNamespace(busy=lambda s:False))
        method=next(n for n in ast.walk(ast.parse(SOURCE)) if isinstance(n,ast.FunctionDef) and n.name=='do_GET')
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(BRIDGE),'exec'),ns)
        ns['do_GET'](types.SimpleNamespace(path='/shelf_readiness',_send_json=Mock()))
        readiness.set.assert_called_with('SHELF_R1_C1','Offline','MQTT unavailable')


class StartupContractTests(unittest.TestCase):
    def test_native_processor_is_the_default_and_no_approval_file_or_dev_flag_is_needed(self):
        self.assertIn("os.getenv('SMART_STORAGE_PROCESSOR_MODE','service')",SOURCE)
        for text in ("os.environ['SMART_STORAGE_EDGE_ACCEPTANCE']","os.environ['SMART_STORAGE_EDGE_CONFIG']","SERVICE_UNACCEPTED_DEV"):
            self.assertNotIn(text,SOURCE)
        engine=(Path(__file__).parents[1]/'src'/'autonomous_edge.py').read_text(encoding='utf8')
        self.assertNotIn('UNACCEPTED_DEV',engine);self.assertIn("'NOT_FIELD_ACCEPTED'",engine)

    def test_one_startup_failure_mode_is_a_visible_setup_issue_not_a_crash(self):
        self.assertIn("except (ValueError,OSError,KeyError,TypeError) as exc:cameras=[];setup_issue=str(exc)[:300]",SOURCE)
        self.assertIn("if setup_issue:AUTONOMOUS.calibration_issue=setup_issue",SOURCE)

    def test_saved_camera_records_supply_sources_without_exposing_credentials(self):
        from src import config_loader
        rows=[dict(cctv_id='CCTV_02',rtsp_url='rtsp://u:p@10.0.0.2/s'),dict(cctv_id='CCTV_01',rtsp_url=' rtsp://10.0.0.1/s '),dict(cctv_id='CCTV_09',rtsp_url='x'),dict(cctv_id='CCTV_03',rtsp_url='')]
        original=config_loader._supabase_rest
        try:
            captured={}
            def fake(**kwargs):captured.update(kwargs);return rows
            config_loader._supabase_rest=fake
            sources=config_loader.get_cctv_sources()
        finally:config_loader._supabase_rest=original
        self.assertEqual(sources,{'CCTV_02':'rtsp://u:p@10.0.0.2/s','CCTV_01':'rtsp://10.0.0.1/s'})
        self.assertEqual(captured['table'],'cctv_cameras','device administration table only')


class EndToEndContractTests(unittest.TestCase):
    """Real coordinator + real validate_runtime_request + real RuntimeInventory: the layers must agree on the pair contract."""
    SID='SHELF_R1_C1'

    def world(self):
        from src.autonomous_edge import AutonomousProcessor
        from src.runtime_validation import validate_runtime_request
        self.clock=[1000.0];clock=lambda:self.clock[0]
        self.runtime=RuntimeInventory('run',2,clock=clock,monotonic=clock);self.runtime.models_ready=True
        self.captures={};self.pending=[];self.previews=0;self.cctv_live=True;self.mqtt=types.SimpleNamespace(is_connected=True)
        self.coordinator=types.SimpleNamespace(global_lock=threading.RLock(),owns=lambda *a:True,end=Mock(return_value=True))
        mapping=[dict(shelf_id=self.SID,ip_address='back',secondary_ip_address='front')]
        self.candidates=[]
        def request(path,body):
            if path=='/validation_begin':return {'token':'lease'}
            if path in ('/validation_renew',):return {'renewed':True}
            if path.startswith('/validation_'):return {}
            if path=='/cctv_trigger_claim':
                for event in self.pending:event.update(claimed=True,validation_token='lease',claimed_at=self.clock[0])
                return {'claimed':True}
            if path.startswith('/esp32_preview'):
                self.previews+=1
                for _ in range(4):
                    self.clock[0]+=.5
                    if self.cctv_live:
                        for cid in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(cid,self.clock[0],True)
                identity=f'cap{self.previews}'
                self.captures[identity]=dict(shelf_id=self.SID,validation_token='lease',complete=True,run_id='run',ip1='back',ip2='front',completed_at=self.clock[0],duration_ms=100)
                return dict(image='i',image_back='b',image_front='f',capture=dict(id=identity,completed_at=self.clock[0],duration_ms=100))
            if path=='/esp32_validate':
                if self.cctv_live:  # the live pose loop keeps feeds fresh while the cameras are up
                    for cid in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(cid,self.clock[0],True)
                status,payload=validate_runtime_request(body,authority=self.runtime,coordinator=self.coordinator,mqtt=self.mqtt,load_mappings=lambda:mapping,
                    consensus_lock=threading.RLock(),pending=self.pending,capture_lock=threading.Lock(),captures=self.captures,readiness=Mock(),votes={})
                if status!=200:raise ValueError(payload.get('error','held'))
                return payload
            raise AssertionError(path)
        native=Mock();native.infer.side_effect=lambda job:dict(candidate=dict(self.candidates.pop(0)),metrics={})
        processor=AutonomousProcessor('run',native,[],[],request,Mock(),Mock(),Mock(),Mock(),Mock())
        processor.runtime=self.runtime;processor.cameras_ready=lambda:True
        processor.stop=Mock();processor.stop.is_set.return_value=False;processor.stop.wait.return_value=False
        real,calls=self.runtime.environment_ready,[0]
        def guarded(**kwargs):  # a stuck quiet-window loop must fail the test fast instead of spinning
            calls[0]+=1;assert calls[0]<200,'quiet/health wait never satisfied'
            return real(**kwargs)
        self.runtime.environment_ready=guarded
        self.clock[0]=1010.0
        return processor,mapping[0]

    def pair(self,front,back,slots,total):
        return dict(front_class=front,back_class=back,front_confidence=95,back_confidence=96,slots=slots,total=total,hold=None)

    def count(self,processor,shelf,*pairs):
        self.candidates[:]=list(pairs)
        for cid in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(cid,self.clock[0],True)
        return processor.validate(shelf)

    def borrow(self,processor,shelf,*pairs):
        self.candidates[:]=list(pairs);self.clock[0]+=1
        for cid in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(cid,self.clock[0],True)
        self.pending[:]=[dict(trigger_id='t1',shelf_id=self.SID,consensus_met=True,expected_action='BORROW',source='CCTV',timestamp=self.clock[0])]
        self.clock[0]+=1
        return processor.validate(shelf,dict(self.pending[0]))

    def test_count_then_borrow_pass_through_every_real_layer_with_two_pairs(self):
        processor,shelf=self.world();full=self.pair('Front_111','Back_11X',[1,1,1,1,1],5);four=self.pair('Front_011','Back_11X',[0,1,1,1,1],4)
        result=self.count(processor,shelf,full,full)
        self.assertEqual(result['comparison']['state_delta'],'BASELINE');self.assertEqual(self.previews,2)
        self.assertEqual(self.runtime.snapshot(self.SID)['current_quantity'],5);self.assertTrue(self.runtime.snapshot(self.SID)['action_capable'])
        result=self.borrow(processor,shelf,four,four)
        self.assertEqual((result['comparison']['quantity_before'],result['comparison']['quantity_after'],result['comparison']['delta']),(5,4,-1))
        self.assertEqual(self.previews,4,'two pairs per attempt, no extra captures');self.assertEqual(self.runtime.snapshot(self.SID)['current_quantity'],4)

    def test_a_transient_bad_pair_is_absorbed_and_a_range_count_stays_a_range(self):
        processor,shelf=self.world();low=dict(hold='LOW_CONFIDENCE');x01=dict(self.pair('Front_011','Back_X01',[0,1,1,None,0],None),hold='UNKNOWN_B1',count_range=[2,3])
        result=self.count(processor,shelf,low,x01,x01)
        self.assertEqual(self.previews,3);self.assertEqual(result['precision'],'RANGE');self.assertEqual(result['count_range'],[2,3])
        self.assertIsNone(self.runtime.snapshot(self.SID)['current_quantity'],'B1 is never guessed into an integer')

    def test_unchanged_shelf_after_a_gesture_holds_and_never_changes_the_count(self):
        processor,shelf=self.world();full=self.pair('Front_111','Back_11X',[1,1,1,1,1],5)
        self.count(processor,shelf,full,full)
        result=self.borrow(processor,shelf,full,full)
        self.assertEqual(result['action'],'NOTHING_CHANGED');self.assertFalse(result['action_authorized'])
        snapshot=self.runtime.snapshot(self.SID);self.assertEqual(snapshot['state'],'Ready');self.assertTrue(snapshot['authoritative'])
        self.assertEqual(snapshot['current_quantity'],5)

    def test_count_while_cctv_is_down_is_accepted_but_blocks_actions_until_recounted(self):
        processor,shelf=self.world();full=self.pair('Front_111','Back_11X',[1,1,1,1,1],5)
        self.candidates[:]=[full,full];self.cctv_live=False
        result=processor.validate(shelf)  # no CCTV feed has ever reported: counting still works
        self.assertEqual(result['comparison']['quantity_after'],5);self.assertFalse(self.runtime.snapshot(self.SID)['action_capable'])
        four=self.pair('Front_011','Back_11X',[0,1,1,1,1],4);self.cctv_live=True  # cameras come back, but the old count is still count-only
        with self.assertRaisesRegex(ValueError,'rescan required|Count was taken'):self.borrow(processor,shelf,four,four)
        self.assertNotEqual(self.runtime.snapshot(self.SID)['current_quantity'],4)


if __name__=='__main__':unittest.main()
