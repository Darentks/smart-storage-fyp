import ast
import copy
import os
from pathlib import Path
import sys
import threading
import types
import unittest
import tempfile
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from src.runtime_inventory import RuntimeInventory,runtime_enabled

SID='SHELF_R1_C1'

class RuntimeTests(unittest.TestCase):
    def bridge_function(self,name):
        path=Path(__file__).parents[2]/'camera_service.py'
        tree=ast.parse(path.read_text(encoding='utf8'))
        return next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)

    def test_runtime_default_and_legacy_release_cannot_select_cloud_inventory(self):
        with patch.dict(os.environ,{},clear=True):self.assertTrue(runtime_enabled())
        for mode in ('persistent','invalid',''):
            with patch.dict(os.environ,{'SMART_STORAGE_INVENTORY_MODE':mode,'SMART_STORAGE_RELEASE_PHASE':'5'}):
                with self.assertRaisesRegex(ValueError,'Only runtime inventory'):runtime_enabled()

    def test_browser_shadow_and_service_reject_forged_writers_before_reading_body(self):
        import urllib.parse
        capability=object()
        for mode in ('browser','shadow','service'):
            ns=dict(urllib=types.SimpleNamespace(parse=urllib.parse),PROCESSOR_AUTHORITY=mode,RUNTIME_INVENTORY=self.runtime,_PROCESSOR_CAPABILITY=capability)
            exec(compile(ast.Module(body=[self.bridge_function('do_POST')],type_ignores=[]),'bridge','exec'),ns)
            for path in ('/esp32_validate','/esp32_trigger','/validation_begin','/validation_progress','/readiness_recovery','/cctv_trigger_claim','/cctv_alignment_config','/run_pipeline'):
                handler=types.SimpleNamespace(path=path,_read_json_body=Mock(side_effect=AssertionError('Must reject before evidence')),_send_json=Mock(),headers={'X-Processor-Capability':'forged'})
                ns['do_POST'](handler)
                self.assertEqual(handler._send_json.call_args.args[0],403);handler._read_json_body.assert_not_called()
            exec(compile(ast.Module(body=[self.bridge_function('do_GET')],type_ignores=[]),'bridge','exec'),ns)
            handler=types.SimpleNamespace(path='/esp32_preview',_send_json=Mock())
            ns['do_GET'](handler);self.assertEqual(handler._send_json.call_args.args[0],403)

    def test_device_database_offline_keeps_bindings_and_local_action_working(self):
        from src import config_loader
        from src.runtime_validation import validate_runtime_request
        self.baseline();args=self.action()
        cache=dict(data=[],fetched_at=0)
        ns=dict(runtime_enabled=runtime_enabled,time=types.SimpleNamespace(time=lambda:self.now),SUPABASE_COMPARTMENTS_CACHE=cache,SUPABASE_CACHE_TTL_SEC=0,RUNTIME_INVENTORY=self.runtime)
        exec(compile(ast.Module(body=[self.bridge_function('get_cached_compartments')],type_ignores=[]),'bridge','exec'),ns)
        devices=[dict(shelf_id=SID,angle_index=i,ip_address=ip,device_id=ip) for i,ip in ((1,'back'),(2,'front'))]
        with patch.object(config_loader,'_supabase_rest',side_effect=[devices,None,None]) as transport,patch.object(config_loader,'create_client',side_effect=AssertionError('No SDK bypass')):
            self.assertIsNone(config_loader.get_supabase_client())
            self.assertEqual(ns['get_cached_compartments']()[0]['ip_address'],'back')
            self.assertEqual(ns['get_cached_compartments']()[0]['secondary_ip_address'],'front')
            with tempfile.TemporaryDirectory() as directory:
                context,body=self.range_adapter_fixture(args,directory)
                context['load_mappings']=ns['get_cached_compartments']
                status,result=validate_runtime_request(body,**context)
            self.assertEqual(status,200);self.assertEqual(result['comparison']['delta'],-1)
            self.assertTrue(all(call.kwargs['table']=='esp32_cameras' for call in transport.call_args_list))

    def test_confirmed_empty_device_database_clears_saved_bindings(self):
        from src import config_loader
        cache=dict(data=[dict(shelf_id=SID,ip_address='old')],fetched_at=0)
        ns=dict(runtime_enabled=runtime_enabled,time=types.SimpleNamespace(time=lambda:self.now),SUPABASE_COMPARTMENTS_CACHE=cache,SUPABASE_CACHE_TTL_SEC=0,RUNTIME_INVENTORY=self.runtime)
        exec(compile(ast.Module(body=[self.bridge_function('get_cached_compartments')],type_ignores=[]),'bridge','exec'),ns)
        with patch.object(config_loader,'_supabase_rest',return_value=[]):
            self.assertEqual(ns['get_cached_compartments'](),[])
        self.assertEqual(cache['data'],[])

    def test_mqtt_disconnect_invalidates_without_browser_and_reconnect_needs_new_pairs(self):
        from src.runtime_validation import reset_runtime_evidence
        from src.mqtt_client import MQTTClientNode
        from src.shelf_readiness import ShelfReadiness
        from src.periodic_recheck import RecheckCoordinator
        self.baseline();args=self.action();old_receipts=copy.deepcopy(args['captures'])
        with tempfile.TemporaryDirectory() as directory:
            context,body=self.range_adapter_fixture(args,directory)
            readiness=ShelfReadiness();readiness.set(SID,'Ready','test')
            coordinator=RecheckCoordinator();coordinator.begin(SID)
            mqtt=object.__new__(MQTTClientNode);mqtt.is_connected=True
            mqtt.on_connection_lost=Mock(side_effect=lambda:reset_runtime_evidence(authority=self.runtime,coordinator=coordinator,
                consensus_lock=context['consensus_lock'],pending=context['pending'],capture_lock=context['capture_lock'],captures=context['captures'],
                votes=context['votes'],readiness=readiness,intents=context['intents'],reason='MQTT unavailable'))
            mqtt._on_disconnect(None,None,1);mqtt._on_disconnect(None,None,1)
            mqtt.on_connection_lost.assert_called_once()
            self.assertEqual(self.runtime.snapshot(SID)['state'],'Offline');self.assertEqual(readiness.get(SID)['state'],'Offline')
            self.assertFalse(coordinator.leases);self.assertFalse(context['pending']);self.assertFalse(context['captures']);self.assertFalse(context['votes'])
            self.assertEqual(context['intents'].snapshot()[0]['status'],'HELD')
            self.assertEqual(context['intents'].snapshot()[0]['reason'],'MQTT_UNAVAILABLE')
            mqtt.is_connected=True
            with self.assertRaises(ValueError):self.runtime.accept(SID,run_id='run',event_id='recovery',expected_version=self.runtime.snapshot(SID)['version'],samples=args['samples'],captures=old_receipts,lease='lease',source='STARTUP')
            samples,receipts=self.fresh('Front_011','Back_11X')
            result=self.runtime.accept(SID,run_id='run',event_id='recovery',expected_version=self.runtime.snapshot(SID)['version'],samples=samples,captures=receipts,lease='lease',source='STARTUP')
            self.assertEqual(result['comparison']['state_delta'],'BASELINE');self.assertIsNone(result['comparison']['delta'])
            self.assertEqual(context['intents'].snapshot()[0]['status'],'HELD')

    def test_recovery_discards_evidence_even_if_intent_metadata_disk_fails(self):
        from src.runtime_validation import reset_runtime_evidence
        from src.periodic_recheck import RecheckCoordinator
        from src.shelf_readiness import ShelfReadiness
        self.baseline();readiness=ShelfReadiness();readiness.set(SID,'Ready','test')
        coordinator=RecheckCoordinator();coordinator.begin(SID)
        pending=[dict(trigger_id='pending',shelf_id=SID)];captures={'old':dict(shelf_id=SID)};votes={'old':True}
        intents=Mock();intents.update.side_effect=OSError('Disk unavailable')
        with self.assertRaises(OSError):reset_runtime_evidence(authority=self.runtime,coordinator=coordinator,
            consensus_lock=threading.RLock(),pending=pending,capture_lock=threading.Lock(),captures=captures,
            votes=votes,readiness=readiness,intents=intents)
        self.assertFalse(pending);self.assertFalse(votes);self.assertFalse(captures);self.assertFalse(coordinator.leases)
        self.assertEqual(readiness.get(SID)['state'],'Offline');self.assertIsNone(self.runtime.snapshot(SID)['current_quantity'])
        # A failure-file write on one shelf must not leave other shelves Ready.
        from src.local_operations import LocalFailures
        with tempfile.TemporaryDirectory() as directory:
            readiness.failures=LocalFailures(Path(directory)/'failures')
            other='SHELF_R1_C2';readiness.set(other,'Ready','test');self.runtime.snapshot(other)
            with patch('src.local_operations.atomic_json',side_effect=OSError('Disk unavailable')):
                with self.assertRaises(OSError):reset_runtime_evidence(authority=self.runtime,coordinator=coordinator,
                    consensus_lock=threading.RLock(),pending=[],capture_lock=threading.Lock(),captures={},votes={},readiness=readiness)
            self.assertTrue(all(readiness.get(sid)['state']=='Offline' for sid in (SID,other)))

    def test_shared_raw_confidence_floor_rejects_rounding_bool_and_nonfinite(self):
        from src.dual_cam_inventory import validate_confidences
        validate_confidences(dict(front_confidence=80,back_confidence=100))
        for value in (79.999,True,float('nan'),float('inf'),101):
            for role in ('front_confidence','back_confidence'):
                sample=dict(front_confidence=95,back_confidence=95);sample[role]=value
                with self.assertRaises(ValueError):validate_confidences(sample)

    def test_shared_receipt_check_is_pure_and_generation_bound(self):
        from src.dual_cam_inventory import validate_capture
        samples,receipts=self.fresh();before=copy.deepcopy(receipts);seen=set()
        timing=validate_capture(samples[0],self.now,receipts,SID,'lease',seen,run_id='run')
        self.assertEqual(timing.capture_id,'1');self.assertEqual(timing.started_at,timing.completed_at-.1)
        self.assertEqual(receipts,before);self.assertEqual(seen,set())
        with self.assertRaises(ValueError):validate_capture(samples[0],self.now,receipts,SID,'lease',seen,run_id='old')
        with self.assertRaises(ValueError):validate_capture(samples[0],self.now,receipts,SID,'lease',{'1'},run_id='run')

    def adapter_context(self):
        return dict(authority=self.runtime,coordinator=types.SimpleNamespace(global_lock=threading.RLock(),owns=lambda *a:True,end=Mock(return_value=True)),
            mqtt=types.SimpleNamespace(is_connected=True),load_mappings=Mock(return_value=[]),consensus_lock=threading.RLock(),
            pending=[],capture_lock=threading.Lock(),captures={},readiness=types.SimpleNamespace(set=Mock()))

    def test_extracted_adapter_rejects_expired_lease_before_mapping_or_mutation(self):
        from src.runtime_validation import validate_runtime_request
        context=self.adapter_context();context['coordinator'].owns=lambda *a:False
        before=self.runtime.snapshot(SID)
        status,_=validate_runtime_request(dict(shelf_id=SID,validation_token='expired'),**context)
        self.assertEqual(status,409);context['load_mappings'].assert_not_called()
        self.assertEqual(self.runtime.snapshot(SID),before)

    def test_extracted_adapter_rejects_wrong_camera_before_inventory_accept(self):
        from src.runtime_validation import validate_runtime_request
        context=self.adapter_context()
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        with patch.object(self.runtime,'accept',side_effect=AssertionError('Wrong cameras must not reach inventory')) as accept:
            status,_=validate_runtime_request(dict(shelf_id=SID,validation_token='lease',image_back='b',image_front='f',ip='front',ip2='back'),**context)
        self.assertEqual(status,409);accept.assert_not_called()

    def setUp(self):
        self.now=1000.;self.runtime=RuntimeInventory('run',2,clock=lambda:self.now,monotonic=lambda:self.now)
        self.runtime.snapshot(SID);self.runtime.models_ready=True;self.sequence=0

    def fresh(self,front='Front_111',back='Back_11X',lease='lease',down=()):
        born=self.now;self.now+=7
        for c in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(c,self.now,c not in down)
        samples=[];receipts={}
        from src.dual_cam_inventory import REQUIRED_MATCHING_PAIRS
        for i in range(REQUIRED_MATCHING_PAIRS):
            self.sequence+=1;identity=str(self.sequence)
            receipts[identity]=dict(run_id='run',shelf_id=SID,validation_token=lease,complete=True,completed_at=born+2+i*2,duration_ms=100)
            samples.append(dict(front_class=front,back_class=back,front_confidence=95,back_confidence=94,capture={'id':identity}))
        return samples,receipts

    def baseline(self,front='Front_111',back='Back_11X'):
        samples,receipts=self.fresh(front,back)
        return self.runtime.accept(SID,run_id='run',event_id='baseline',expected_version=self.runtime.snapshot(SID)['version'],samples=samples,captures=receipts,lease='lease',source='STARTUP')

    def test_exact_and_range_baselines_do_not_fill_unknown_slots(self):
        for front,back,precision,quantity,bounds in [('Front_111','Back_11X','EXACT',5,[5,5]),('Front_011','Back_X01','RANGE',None,[2,3]),('Front_111','Back_X01','RANGE',None,[3,4])]:
            self.setUp();result=self.baseline(front,back)
            self.assertEqual(result['readiness'],'Ready');self.assertEqual(result['precision'],precision)
            self.assertEqual(result['comparison']['quantity_after'],quantity);self.assertEqual(result['count_range'],bounds)
            if precision=='RANGE':self.assertIsNone(result['occupancy'][3])

    def test_restart_has_no_restore_and_old_run_is_rejected(self):
        self.baseline();restarted=RuntimeInventory('new-run',2,clock=lambda:self.now,monotonic=lambda:self.now)
        self.assertIsNone(restarted.snapshot(SID)['baseline']);self.assertIsNone(restarted.snapshot(SID)['current_quantity'])
        with self.assertRaisesRegex(ValueError,'Stale run'):restarted.accept(SID,run_id='run',event_id='old',expected_version=0,samples=[],captures={},lease='lease',source='STARTUP')

    def test_quiet_window_and_native_health_are_required(self):
        samples,receipts=self.fresh();self.runtime.camera('CCTV_01',self.now,True,True)
        args=dict(run_id='run',event_id='baseline',expected_version=0,samples=samples,captures=receipts,lease='lease',source='STARTUP')
        with self.assertRaisesRegex(ValueError,'Quiet'):self.runtime.accept(SID,**args)
        self.runtime.models_ready=False
        with self.assertRaisesRegex(ValueError,'models'):self.runtime.accept(SID,**args)

    def test_initial_missing_cctvs_do_not_interrupt_count_evidence(self):
        version=self.runtime.snapshot(SID)['version']
        quiet_since=self.runtime.last_interaction
        self.now+=3
        for cid in ('CCTV_01','CCTV_02','CCTV_03'):
            self.runtime.camera(cid,self.now,False)
        self.assertEqual(self.runtime.snapshot(SID)['version'],version)
        self.assertEqual(self.runtime.last_interaction,quiet_since)
        self.assertTrue(self.runtime.environment_ready(require_cameras=False))
        samples,receipts=self.fresh(down=('CCTV_01','CCTV_02','CCTV_03'))
        result=self.runtime.accept(SID,run_id='run',event_id='count-only',expected_version=version,
            samples=samples,captures=receipts,lease='lease',source='STARTUP')
        self.assertEqual(result['readiness'],'Ready')
        self.assertFalse(self.runtime.snapshot(SID)['action_capable'])

    def test_camera_outage_invalidates_baseline_and_old_capture_authority(self):
        self.baseline();version=self.runtime.snapshot(SID)['version'];self.runtime.camera('CCTV_02',self.now,False)
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],5)
        with self.assertRaises(ValueError):self.runtime.environment_ready()
        self.now+=5
        self.runtime.camera('CCTV_02',self.now,False)
        self.assertEqual(self.runtime.snapshot(SID)['version'],version)
        self.now+=.01
        self.runtime.camera('CCTV_02',self.now,False)
        state=self.runtime.snapshot(SID);self.assertEqual(state['state'],'Offline');self.assertIsNone(state['current_quantity'])
        self.assertGreater(state['version'],version);self.assertIsNotNone(state['baseline'])
        with self.assertRaises(ValueError):self.runtime.environment_ready()
        samples,receipts=self.fresh('Front_011','Back_11X')
        result=self.runtime.accept(SID,run_id='run',event_id='camera-recovery',expected_version=state['version'],samples=samples,captures=receipts,lease='lease',source='STARTUP')
        self.assertEqual(result['comparison']['state_delta'],'BASELINE');self.assertIsNone(result['comparison']['delta'])
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],4)

    def test_cctv_stop_retains_count_timestamp_but_revokes_action_authority(self):
        self.baseline();before=self.runtime.snapshot(SID)
        self.runtime.interrupt_actions(disable_actions=True)
        self.runtime.set_cctv_enabled(False)
        stopped=self.runtime.snapshot(SID)
        self.assertEqual(stopped['current_quantity'],before['current_quantity'])
        self.assertEqual(stopped['observed_at'],before['observed_at'])
        self.assertFalse(stopped['action_capable']);self.assertFalse(stopped['cctv_enabled'])
        self.assertGreater(stopped['version'],before['version'])
        with self.assertRaisesRegex(ValueError,'CCTV'):self.runtime.environment_ready()
        self.runtime.set_cctv_enabled(True)
        self.assertFalse(self.runtime.cameras_fresh(),'old feeds cannot become live through Start')
        self.assertFalse(self.runtime.snapshot(SID)['action_capable'],'fresh count required after Start')

    def test_short_cctv_dropout_recovers_count_without_using_stale_frames(self):
        self.baseline();before=self.runtime.snapshot(SID)
        self.runtime.camera('CCTV_02',self.now,False)
        with self.assertRaisesRegex(ValueError,'CCTV'):self.runtime.environment_ready()
        self.now+=4.9
        for cid in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(cid,self.now,True)
        after=self.runtime.snapshot(SID)
        self.assertEqual(after['current_quantity'],before['current_quantity'])
        self.assertEqual(after['observed_at'],before['observed_at'])
        self.assertEqual(after['version'],before['version'])
        self.assertTrue(self.runtime.environment_ready())

    def action(self,front='Front_011',back='Back_11X',**changes):
        version=self.runtime.snapshot(SID)['version'];timestamp=self.now+.2;samples,receipts=self.fresh(front,back)
        event=dict(trigger_id='event',shelf_id=SID,claimed=True,consensus_met=True,validation_token='lease',timestamp=timestamp,claimed_at=timestamp+.1,expected_action='BORROW')
        event.update(changes)
        args=dict(run_id='run',event_id='event',expected_version=version,samples=samples,captures=receipts,lease='lease',source='CCTV',event=event)
        return args

    def test_exact_borrow_and_duplicate_rejection(self):
        self.baseline();args=self.action();result=self.runtime.accept(SID,**args)
        self.assertEqual(result['comparison']['delta'],-1);self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],4)
        args['expected_version']=result['version']
        with self.assertRaisesRegex(ValueError,'Duplicate'):self.runtime.accept(SID,**args)

    def test_exact_return_remains_supported_after_shared_validation(self):
        self.baseline();self.runtime.accept(SID,**self.action())
        args=self.action('Front_111','Back_11X',expected_action='RETURN')
        args['event_id']='return-event';args['event']['trigger_id']='return-event'
        result=self.runtime.accept(SID,**args)
        self.assertEqual(result['comparison']['delta'],1)
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],5)

    def test_visible_change_with_hidden_b1_is_unattributed_not_an_accepted_action(self):
        self.baseline('Front_111','Back_X01');args=self.action('Front_011','Back_X01')
        result=self.runtime.accept(SID,**args)
        self.assertEqual(result['action'],'UNATTRIBUTED');self.assertFalse(result['action_authorized'])
        state=self.runtime.snapshot(SID);self.assertEqual(state['baseline']['slots'],[0,1,1,None,0])
        self.assertEqual(state['state'],'Ready');self.assertIsNone(state['current_quantity'])

    def test_stale_event_wrong_action_and_relocation_do_not_change_baseline(self):
        for overrides in ({'timestamp':1},{'shelf_id':'SHELF_R1_C2'},{'validation_token':'other'}):
            self.setUp();self.baseline();args=self.action(**overrides)
            with self.assertRaises(ValueError):self.runtime.accept(SID,**args)
            self.assertEqual(self.runtime.snapshot(SID)['baseline']['total'],5)

    def test_two_consecutive_distinct_fresh_samples_and_confidence(self):
        for issue in ('confidence','duplicate','stale','partial','conflict','run','lease','separation','unstable','too_few','too_many'):
            self.setUp();samples,receipts=self.fresh()
            if issue=='confidence':samples[0]['back_confidence']=79.99
            if issue=='duplicate':samples[1]['capture']=samples[0]['capture']
            if issue=='stale':receipts['1']['completed_at']=self.now-61
            if issue=='partial':receipts['1']['complete']=False
            if issue=='conflict':samples[0]['back_class']='Back_000'
            if issue=='run':receipts['1']['run_id']='old'
            if issue=='lease':receipts['1']['validation_token']='old'
            if issue=='separation':receipts['2']['completed_at']=receipts['1']['completed_at']+.5
            if issue=='unstable':samples[1]['front_class']='Front_011'
            if issue=='too_few':samples=samples[:1]
            if issue=='too_many':samples=samples+[dict(samples[0])]
            with self.assertRaises(ValueError,msg=issue):self.runtime.accept(SID,run_id='run',event_id='baseline',expected_version=0,samples=samples,captures=receipts,lease='lease',source='STARTUP')
            self.assertIsNone(self.runtime.snapshot(SID)['baseline'])

    def test_concurrent_updates_have_only_one_winner(self):
        self.baseline();args=self.action();outcomes=[]
        def attempt():
            try:self.runtime.accept(SID,**args);outcomes.append('accepted')
            except ValueError:outcomes.append('rejected')
        workers=[threading.Thread(target=attempt) for _ in range(2)]
        for w in workers:w.start()
        for w in workers:w.join()
        self.assertEqual(sorted(outcomes),['accepted','rejected'])

    def test_runtime_cloud_guard_blocks_inventory_before_network(self):
        from src import config_loader
        with patch.dict(os.environ,{'SMART_STORAGE_INVENTORY_MODE':'runtime'}),patch.object(config_loader,'get_resolved_supabase_url') as url,patch.object(config_loader.urllib.request,'urlopen') as network:
            for table in ('compartments','rpc/commit_inventory_observation','rpc/reconcile_inventory'):
                with self.assertRaises(ValueError):config_loader._supabase_rest(table=table)
            url.assert_not_called();network.assert_not_called()

    def range_adapter_fixture(self,args,directory):
        from src.local_operations import LatestIntents
        context=self.adapter_context()
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        context['pending']=[dict(args['event'])];context['captures']=args['captures'];context['votes']={'CCTV_01':{'old':True}}
        def end(*_):context['coordinator'].owns=lambda *a:False;return True
        context['coordinator'].end=Mock(side_effect=end)
        for receipt in context['captures'].values():receipt.update(ip1='back',ip2='front')
        context['intents']=LatestIntents(Path(directory)/'intents.json')
        context['intents'].add(dict(id='event',timestamp=self.now,camera_id='CCTV_03',shelf_id=SID,action='BORROW',status='CONSENSUS'))
        body=dict(shelf_id=SID,validation_token='lease',source='CCTV',trigger_id='event',image_back='b',image_front='f',ip='back',ip2='front',
            run_id='run',expected_version=args['expected_version'],dual_cam_evidence=dict(version='stacked-visibility-v4-x01',samples=args['samples']))
        return context,body

    def test_hold_recovery_uses_new_pairs_and_never_accepts_or_replays_old_intent(self):
        from src.runtime_validation import validate_runtime_request
        self.baseline('Front_111','Back_X01');args=self.action('Front_111','Back_X01');old_receipts=copy.deepcopy(args['captures'])
        with tempfile.TemporaryDirectory() as directory:
            context,body=self.range_adapter_fixture(args,directory)
            status,result=validate_runtime_request(body,**context)
            self.assertEqual(status,409);self.assertEqual(result['code'],'UNKNOWN_B1')
            self.assertEqual(self.runtime.snapshot(SID)['baseline']['slots'],[1,1,1,None,0])
            self.assertFalse(self.runtime.snapshot(SID)['latest_observation']['action_authorized'])
            self.assertEqual(context['intents'].snapshot()[0]['status'],'HELD')
            self.assertEqual(context['pending'],[]);self.assertEqual(context['votes'],{});self.assertEqual(context['captures'],{})
            self.assertFalse(context['coordinator'].owns(SID,'lease'))
            version=self.runtime.snapshot(SID)['version']
            with self.assertRaises(ValueError):self.runtime.accept(SID,run_id='run',event_id='recovery',expected_version=version,
                samples=args['samples'],captures=old_receipts,lease='lease',source='STARTUP')
            samples,receipts=self.fresh('Front_011','Back_X01');context['captures']=receipts
            for receipt in receipts.values():receipt.update(ip1='back',ip2='front')
            context['coordinator'].owns=lambda *a:True
            recovery=dict(body,source='STARTUP',trigger_id=None,expected_version=self.runtime.snapshot(SID)['version'],
                runtime_event_id='recovery',dual_cam_evidence=dict(version='stacked-visibility-v4-x01',samples=samples))
            status,result=validate_runtime_request(recovery,**context)
            self.assertEqual(status,200);self.assertEqual(result['comparison']['state_delta'],'BASELINE')
            self.assertIsNone(result['comparison']['delta']);self.assertEqual(result['count_range'],[2,3])
            self.assertFalse(self.runtime.snapshot(SID)['baseline']['action_authorized'])
            self.assertEqual(context['intents'].snapshot()[0]['status'],'HELD')
            replay=self.action('Front_011','Back_X01')
            with self.assertRaisesRegex(ValueError,'Duplicate'):self.runtime.accept(SID,**replay)
            self.assertFalse(self.runtime.hold(SID,'event'))
            self.assertEqual(self.runtime.snapshot(SID)['state'],'Ready')

    def test_only_proven_exact_action_is_marked_accepted_in_latest_five(self):
        from src.runtime_validation import validate_runtime_request
        self.baseline();args=self.action()
        with tempfile.TemporaryDirectory() as directory:
            context,body=self.range_adapter_fixture(args,directory)
            status,result=validate_runtime_request(body,**context)
            self.assertEqual(status,200);self.assertEqual(result['comparison']['delta'],-1)
            self.assertEqual(context['intents'].snapshot()[0]['status'],'ACCEPTED')
            context['intents'].update('event','HELD','UNKNOWN_B1')
            self.assertEqual(context['intents'].snapshot()[0]['status'],'ACCEPTED')

    def test_native_range_action_collects_two_matching_pairs_before_server_proof(self):
        from test_autonomous_edge import CoordinatorTests
        fixture=CoordinatorTests();candidate=dict(front_class='Front_011',back_class='Back_X01',front_confidence=95,back_confidence=96,
            slots=[0,1,1,None,0],count_range=[2,3],hold='UNKNOWN_B1')
        processor=fixture.make(iter([copy.deepcopy(candidate) for _ in range(3)]))
        processor.runtime=Mock();processor.runtime.snapshot.return_value={'version':4}
        processor.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'),dict(trigger_id='event',source='CCTV',expected_action='BORROW'))
        payload=next(body for path,body in fixture.calls if path=='/esp32_validate')
        self.assertEqual(payload['source'],'CCTV');self.assertEqual(len(payload['dual_cam_evidence']['samples']),2)
        processor.runtime.accept.assert_not_called()

    def test_native_scheduler_automatically_selects_held_shelf_for_rebaseline(self):
        from test_autonomous_edge import CoordinatorTests
        fixture=CoordinatorTests();processor=fixture.make()
        shelf=dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')
        processor.shelves.return_value=[shelf];processor.pending.return_value=[];processor.models_loaded=True
        processor.request=Mock(return_value={'shelves':{SID:{'state':'Held'}}});processor.validate=Mock()
        processor.stop.is_set.side_effect=[False,True]
        processor._validations();processor.validate.assert_called_once_with(shelf,None)

    def test_native_controller_can_submit_stable_range_for_startup_only(self):
        from test_autonomous_edge import CoordinatorTests
        fixture=CoordinatorTests();candidate=dict(front_class='Front_011',back_class='Back_X01',front_confidence=95,back_confidence=96,
            slots=[0,1,1,None,0],count_range=[2,3],hold='UNKNOWN_B1')
        processor=fixture.make(iter([copy.deepcopy(candidate) for _ in range(3)]))
        processor.runtime=Mock();processor.runtime.snapshot.return_value={'version':4}
        processor.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'))
        payload=next(body for path,body in fixture.calls if path=='/esp32_validate')
        self.assertIsNone(payload['predicted_box_count']);self.assertEqual(payload['expected_version'],4)
        self.assertEqual(len(payload['dual_cam_evidence']['samples']),2)

    def test_real_runtime_handler_uses_ram_without_cloud_commit(self):
        path=Path(__file__).parents[2]/'camera_service.py';tree=ast.parse(path.read_text(encoding='utf8'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_runtime_validate')
        samples,receipts=self.fresh('Front_011','Back_X01')
        for receipt in receipts.values():receipt.update(ip1='back',ip2='front')
        cloud=Mock(side_effect=AssertionError('Inventory cloud path must not run'))
        readiness=types.SimpleNamespace(set=Mock())
        from src.runtime_validation import validate_runtime_request
        ns=dict(CONSENSUS_WINDOW_SEC=4.0,LOCAL_INTENTS=None,CCTV_ALIGN_STATE={},validate_runtime_request=validate_runtime_request, CCTV_CONSENSUS_LOCK=threading.RLock(),PENDING_CCTV_TRIGGERS=[],RUNTIME_INVENTORY=self.runtime,RECHECKS=types.SimpleNamespace(global_lock=threading.RLock(),owns=lambda *args:True),
            mqtt_bridge=types.SimpleNamespace(is_connected=True),DUAL_CAM_CAPTURE_LOCK=threading.Lock(),DUAL_CAM_CAPTURES=receipts,
            READINESS=readiness,failure_code=lambda exc:'VALIDATION_REJECTED',load_inventory_state=cloud,commit_inventory_state=cloud,
            get_cached_compartments=lambda:[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')])
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(path),'exec'),ns)
        handler=types.SimpleNamespace(_send_json=Mock())
        body=dict(shelf_id=SID,validation_token='lease',source='STARTUP',image_back='back-image',image_front='front-image',ip='back',ip2='front',
            dual_cam_evidence=dict(version='stacked-visibility-v4-x01',samples=samples),run_id='run',expected_version=0,runtime_event_id='baseline')
        ns['_runtime_validate'](handler,body)
        self.assertEqual(handler._send_json.call_args.args[0],200)
        self.assertEqual(handler._send_json.call_args.args[1]['precision'],'RANGE');cloud.assert_not_called()

    # ---- count readiness vs action readiness -------------------------------------------------
    def degraded_baseline(self,front='Front_111',back='Back_11X',transport_ok=True):
        """Count taken while CCTV is down (or MQTT is down): valid as a count, never as an action baseline."""
        self.runtime.camera('CCTV_02',self.now,False)  # outage first: the quiet window restarts, then the rack is scanned
        samples,receipts=self.fresh(front,back,down=('CCTV_02',))
        return self.runtime.accept(SID,run_id='run',event_id='degraded',expected_version=self.runtime.snapshot(SID)['version'],samples=samples,
            captures=receipts,lease='lease',source='STARTUP',transport_ok=transport_ok)

    def test_initial_count_is_accepted_without_cctv_but_is_not_action_capable(self):
        result=self.degraded_baseline();self.assertEqual(result['readiness'],'Ready');self.assertEqual(result['comparison']['quantity_after'],5)
        snapshot=self.runtime.snapshot(SID);self.assertTrue(snapshot['authoritative']);self.assertFalse(snapshot['action_capable'])
        with self.assertRaisesRegex(ValueError,'rescan required'):self.runtime.accept(SID,**self.action())
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],5,'a refused action never changes the count')

    def test_cctv_actions_still_require_live_cctv_even_with_an_action_capable_baseline(self):
        self.baseline();self.assertTrue(self.runtime.snapshot(SID)['action_capable'])
        args=self.action();self.runtime.camera('CCTV_02',self.now,False)
        with self.assertRaises(ValueError):self.runtime.accept(SID,**args)

    def test_mqtt_outage_also_makes_the_count_non_actionable(self):
        samples,receipts=self.fresh()
        self.runtime.accept(SID,run_id='run',event_id='b',expected_version=0,samples=samples,captures=receipts,lease='lease',source='STARTUP',transport_ok=False)
        self.assertFalse(self.runtime.snapshot(SID)['action_capable'])

    def test_count_only_baseline_is_replaced_by_a_fresh_scan_once_everything_is_live(self):
        self.degraded_baseline();samples,receipts=self.fresh('Front_011','Back_11X')  # all CCTV healthy again
        result=self.runtime.accept(SID,run_id='run',event_id='rescan',expected_version=self.runtime.snapshot(SID)['version'],samples=samples,captures=receipts,lease='lease',source='STARTUP')
        self.assertEqual(result['comparison']['state_delta'],'BASELINE');self.assertIsNone(result['comparison']['delta'],'a recount is never a historical BORROW/RETURN')
        self.assertTrue(self.runtime.snapshot(SID)['action_capable']);self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],4)
        samples,receipts=self.fresh()
        with self.assertRaisesRegex(ValueError,'cannot be silently rebaselined'):
            self.runtime.accept(SID,run_id='run',event_id='again',expected_version=self.runtime.snapshot(SID)['version'],samples=samples,captures=receipts,lease='lease',source='STARTUP')

    def test_environment_ready_separates_count_from_action_requirements(self):
        self.now+=7
        with self.assertRaisesRegex(ValueError,'CCTV'):self.runtime.environment_ready()
        self.assertTrue(self.runtime.environment_ready(require_cameras=False))
        self.runtime.models_ready=False
        with self.assertRaisesRegex(ValueError,'models'):self.runtime.environment_ready(require_cameras=False)
        self.runtime.models_ready=True;self.runtime.camera('CCTV_01',self.now,True,True)
        with self.assertRaisesRegex(ValueError,'Quiet'):self.runtime.environment_ready(require_cameras=False)

    def test_wait_reason_names_only_fresh_interacting_cameras_and_then_quiet_delay(self):
        self.runtime.camera('CCTV_01',self.now,True,True)
        self.runtime.camera('CCTV_02',self.now,True,False)
        self.runtime.camera('CCTV_03',self.now,True,True)
        reason=self.runtime.environment_wait_reason(require_cameras=False)
        self.assertIn('CCTV_01 detects someone in a shelf region',reason)
        self.assertNotIn('CCTV_03',reason)
        self.assertNotIn('CCTV_02',reason)
        self.now+=1.6
        reason=self.runtime.environment_wait_reason(require_cameras=False)
        self.assertNotIn('detects someone',reason,'stale sightings must not be reported as current people')
        self.assertIn('another 0.4 s',reason)
        self.now+=.5
        self.assertIsNone(self.runtime.environment_wait_reason(require_cameras=False))
        self.assertTrue(self.runtime.environment_ready(require_cameras=False))

    def test_wait_reason_distinguishes_models_and_action_camera_requirements(self):
        self.runtime.models_ready=False
        self.assertIn('models',self.runtime.environment_wait_reason(require_cameras=False))
        self.runtime.models_ready=True;self.now+=3
        self.assertIsNone(self.runtime.environment_wait_reason(require_cameras=False))
        self.assertIn('CCTV_02',self.runtime.environment_wait_reason())
        self.runtime.set_cctv_enabled(False)
        self.assertEqual(self.runtime.environment_wait_reason(),'Waiting for CCTV to start')
        self.assertIsNone(self.runtime.environment_wait_reason(require_cameras=False))

    def test_rescan_resets_only_the_requested_shelf_and_keeps_a_historical_count(self):
        self.baseline();other=RuntimeInventory('run',2,clock=lambda:self.now,monotonic=lambda:self.now);other.snapshot('SHELF_R1_C2')
        before=self.runtime.snapshot(SID)['version'];self.runtime.reset_shelf(SID)
        snapshot=self.runtime.snapshot(SID);self.assertEqual(snapshot['state'],'Starting');self.assertGreater(snapshot['version'],before)
        self.assertFalse(snapshot['authoritative']);self.assertIsNone(snapshot['current_quantity']);self.assertFalse(snapshot['action_capable'])
        self.assertEqual(snapshot['historical']['total'],5,'last count stays visible only as history')
        with self.assertRaises(ValueError):self.runtime.reset_shelf('SHELF_R1_C9')
        samples,receipts=self.fresh()
        with self.assertRaisesRegex(ValueError,'Stale run or inventory version'):  # in-flight evidence from before the reset is rejected
            self.runtime.accept(SID,run_id='run',event_id='late',expected_version=before,samples=samples,captures=receipts,lease='lease',source='STARTUP')

    def test_runtime_adapter_takes_the_initial_count_without_mqtt_but_actions_need_it(self):
        from src.runtime_validation import validate_runtime_request
        samples,receipts=self.fresh()
        for receipt in receipts.values():receipt.update(ip1='back',ip2='front')
        context=self.adapter_context();context['mqtt']=types.SimpleNamespace(is_connected=False);context['captures']=receipts
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        body=dict(shelf_id=SID,validation_token='lease',source='STARTUP',image_back='b',image_front='f',ip='back',ip2='front',run_id='run',
            expected_version=0,runtime_event_id='baseline',dual_cam_evidence=dict(version='stacked-visibility-v4-x01',samples=samples))
        status,result=validate_runtime_request(body,**context)
        self.assertEqual(status,200);self.assertEqual(result['comparison']['quantity_after'],5)
        self.assertFalse(self.runtime.snapshot(SID)['action_capable'],'counted without the action transport')
        self.setUp();samples,receipts=self.fresh()
        context=self.adapter_context();context['mqtt']=types.SimpleNamespace(is_connected=False)
        status,result=validate_runtime_request(dict(body,source='CCTV',trigger_id='event'),**context)
        self.assertEqual(status,409);self.assertIn('MQTT',result['error'])

if __name__=='__main__':unittest.main()
