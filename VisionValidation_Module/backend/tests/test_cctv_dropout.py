"""CCTV failures isolate one tracker and retain context without stale commits."""
import ast
import copy
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_autonomous_edge as coordinator_tests
import test_runtime_inventory as inventory_tests
import consensus_fixture as consensus_tests
from src.autonomous_edge import ActionTracker
from src.runtime_validation import reset_runtime_evidence

SID='SHELF_R1_C1'


class DropoutTests(unittest.TestCase):
    def setUp(self):
        self.f=inventory_tests.RuntimeTests();self.f.setUp();self.f.baseline()
        self.runtime=self.f.runtime
        self.p=coordinator_tests.SchedulerTests().make({})
        self.p.runtime=self.runtime
        self.p.cameras=[dict(id=cid,tracker=Mock()) for cid in ('CCTV_01','CCTV_02','CCTV_03')]
        self.p.health={cam['id']:dict(state='Online',at=self.f.now) for cam in self.p.cameras}

    def recover(self,cid='CCTV_02',after=1):
        self.f.now+=after
        for camera in ('CCTV_01','CCTV_02','CCTV_03'):self.runtime.camera(camera,self.f.now,True)

    def test_short_failure_resets_only_its_tracker_without_invalidating_context_or_retry_limits(self):
        before=self.runtime.snapshot(SID)
        self.p.attempts[SID]=2;self.p.exhausted.add(SID);self.p.retry_at[SID]=200
        self.p._frame_failed(self.p.cameras[1],'FRAME_UNAVAILABLE',self.f.now)
        for index,cam in enumerate(self.p.cameras):self.assertEqual(cam['tracker'].reset.call_count,1 if index==1 else 0)
        self.p.invalidate.assert_not_called()
        after=self.runtime.snapshot(SID)
        for key in ('baseline','current_quantity','version','observed_at','ready_at'):self.assertEqual(after[key],before[key])
        self.assertEqual(self.p.attempts[SID],2);self.assertIn(SID,self.p.exhausted);self.assertEqual(self.p.retry_at[SID],200)
        self.assertTrue(after['movement_warnings'][0]['possible_missed_movement'])
        self.assertEqual(after['movement_warnings'][0]['camera_id'],'CCTV_02')

    def test_grace_preserves_count_at_exactly_five_seconds_but_long_outage_invalidates(self):
        before=self.runtime.snapshot(SID);start=self.f.now
        self.assertFalse(self.runtime.camera('CCTV_03',start,False))
        self.f.now=start+5
        self.assertFalse(self.runtime.camera('CCTV_03',self.f.now,False))
        self.assertEqual(self.runtime.snapshot(SID)['baseline'],before['baseline'])
        self.assertEqual(self.runtime.snapshot(SID)['version'],before['version'])
        self.f.now+=.001
        self.assertTrue(self.runtime.camera('CCTV_03',self.f.now,False))
        after=self.runtime.snapshot(SID)
        self.assertEqual(after['state'],'Offline');self.assertIsNone(after['current_quantity'])
        self.assertEqual(after['historical']['total'],5)

    def test_repeated_failures_do_not_restart_the_outage_clock(self):
        start=self.f.now
        self.p._frame_failed(self.p.cameras[0],'FRAME_UNAVAILABLE',start)
        epoch=self.runtime.movement_generation()
        self.f.now+=2
        self.p._frame_failed(self.p.cameras[0],'STALE_FRAME',self.f.now)
        self.assertEqual(self.runtime.movement_generation(),epoch)
        self.assertEqual(self.p.health['CCTV_01']['down_since'],start)
        self.assertEqual(self.p.health['CCTV_01']['duration_ms'],2000)
        self.p.invalidate.assert_not_called()

    def test_pre_gap_pairs_cannot_commit_and_cross_gap_changes_are_unattributed(self):
        args=self.f.action();before=self.runtime.snapshot(SID)
        self.runtime.camera('CCTV_02',self.f.now,False)
        self.recover()
        with self.assertRaisesRegex(ValueError,'fresh paired captures'):self.runtime.accept(SID,**args)
        self.assertEqual(self.runtime.snapshot(SID)['baseline'],before['baseline'])
        result=self.runtime.accept(SID,**self.f.action())
        self.assertEqual(result['action'],'UNATTRIBUTED');self.assertFalse(result['action_authorized'])
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],4)
        record=self.runtime.latest_changes()[0]
        self.assertEqual(record['movement_warnings'][0]['camera_id'],'CCTV_02')
        self.assertEqual(self.runtime.snapshot(SID)['movement_warnings'],[])
        next_action=self.f.action('Front_111','Back_11X',expected_action='RETURN')
        next_action['event_id']='return';next_action['event']['trigger_id']='return'
        self.assertTrue(self.runtime.accept(SID,**next_action)['action_authorized'])

    def test_a_pair_from_during_the_outage_cannot_commit_after_recovery(self):
        self.runtime.camera('CCTV_01',self.f.now,False)
        samples,receipts=self.f.fresh('Front_011','Back_11X')  # Recovery occurs after these pictures were taken.
        event=dict(trigger_id='e',shelf_id=SID,consensus_met=True,claimed=True,validation_token='lease',timestamp=self.f.now-6,claimed_at=self.f.now-5.9)
        with self.assertRaisesRegex(ValueError,'fresh paired captures'):
            self.runtime.accept(SID,run_id='run',event_id='e',expected_version=1,samples=samples,captures=receipts,lease='lease',source='CCTV',event=event)

    def test_offline_side_camera_still_blocks_actions_but_counts_can_continue(self):
        args=self.f.action();self.runtime.camera('CCTV_03',self.f.now,False)
        with self.assertRaisesRegex(ValueError,'Fresh CCTV'):self.runtime.accept(SID,**args)
        result=self.runtime.accept(SID,run_id='run',event_id='count',expected_version=1,samples=args['samples'],captures=args['captures'],lease='lease',source='COUNT_CHECK')
        self.assertEqual(result['status'],'success');self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],4)
        self.assertFalse(self.runtime.snapshot(SID)['action_capable'])
        self.assertTrue(self.runtime.snapshot(SID)['movement_warnings'])

    def test_no_synthetic_withdrawal_from_failed_camera_and_healthy_tracker_survives(self):
        for cam in self.p.cameras[:2]:cam['tracker']=ActionTracker('GRID_6_ROI',{SID:coordinator_tests.ROI})
        start=self.f.now
        for number in range(14):
            for cam in self.p.cameras[:2]:cam['tracker'].update([coordinator_tests.person()],start+number*.1)
        self.f.now=start+1.4
        self.p._frame_failed(self.p.cameras[0],'FRAME_UNAVAILABLE',self.f.now)
        self.assertEqual(self.p.cameras[0]['tracker'].update([],start+1.7),[])
        self.assertEqual(len(self.p.cameras[1]['tracker'].update([],start+1.7)),1)

    def test_votes_keep_original_times_and_expire_normally_through_a_dropout(self):
        f=consensus_tests.ConsensusFixture();f.setUp();vote=f.ns['record_cctv_consensus']
        vote(SID,'CCTV_01','RECHECK',.99)
        original=copy.deepcopy(f.ns['CCTV_ALIGN_STATE'])
        self.p._frame_failed(self.p.cameras[0],'FRAME_UNAVAILABLE',self.f.now)
        self.assertEqual(f.ns['CCTV_ALIGN_STATE'],original)
        f.now+=4.1
        result=vote(SID,'CCTV_02','RECHECK',.99)
        self.assertFalse(result['consensus_met']);self.assertNotIn('CCTV_01',f.ns['CCTV_ALIGN_STATE'])
        vote('ANY_SHELF','CCTV_03',None,.99)
        result=vote(SID,'CCTV_01','RECHECK',.99)
        self.assertTrue(result['consensus_met']);self.assertEqual(result['evidence_started_at'],104.1)

    def test_long_outage_recovery_does_not_reset_scan_budgets_or_other_trackers(self):
        tree=ast.parse((Path(__file__).parents[2]/'camera_service.py').read_text(encoding='utf-8'))
        function=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='invalidate')
        self.p.reset_scan_budget=Mock()
        coordinator=types.SimpleNamespace(global_lock=threading.RLock(),leases={SID:('lease',999)},waiting={'request':{}})
        pending=[{'shelf_id':SID,'trigger_id':'event'}];captures={'capture':{'shelf_id':SID}};votes={'CCTV_01':{'ts':self.f.now}}
        readiness=types.SimpleNamespace(states={SID:{}},set=Mock())
        ns=dict(reset_runtime_evidence=reset_runtime_evidence,RUNTIME_INVENTORY=self.runtime,RECHECKS=coordinator,
            CCTV_CONSENSUS_LOCK=threading.RLock(),PENDING_CCTV_TRIGGERS=pending,DUAL_CAM_CAPTURE_LOCK=threading.Lock(),
            DUAL_CAM_CAPTURES=captures,CCTV_ALIGN_STATE=votes,READINESS=readiness,LOCAL_INTENTS=None,
            esp32_ref_manager=types.SimpleNamespace(baseline_freeze_until={}),AUTONOMOUS=self.p)
        exec(compile(ast.Module(body=[function],type_ignores=[]),'real invalidation','exec'),ns)
        ns['invalidate']('CCTV outage exceeded five seconds')
        self.assertEqual((pending,captures,votes),([],{},{}));self.assertEqual(coordinator.leases,{})
        self.p.reset_scan_budget.assert_not_called()
        for camera in self.p.cameras:camera['tracker'].reset.assert_not_called()


class InFlightDropoutTests(unittest.TestCase):
    def test_fresh_frame_after_an_unreported_gap_is_flagged_and_long_gaps_invalidate(self):
        from unittest.mock import patch
        for duration in (2,6):
            with self.subTest(duration=duration):
                f=inventory_tests.RuntimeTests();f.setUp();f.baseline()
                processor=coordinator_tests.SchedulerTests().make({});processor.runtime=f.runtime
                tracker=ActionTracker('GRID_6_ROI',{})
                processor.cameras[0]['tracker']=tracker
                processor.health['CCTV_01']['at']=f.now-duration
                processor.capture.return_value={'status':'success','image':'image','seq':1}
                processor.pose_native.infer.return_value={'persons':[],'metrics':{}}
                processor.stop.is_set.side_effect=[False,False,True]
                with patch('src.autonomous_edge.time.time',return_value=f.now):processor._poses()
                self.assertTrue(f.runtime.snapshot(SID)['movement_warnings'])
                self.assertEqual(processor.log.call_args.kwargs['reason'],'FRAME_GAP')
                self.assertEqual(processor.log.call_args.kwargs['duration_ms'],duration*1000)
                if duration>5:processor.invalidate.assert_called_once_with('CCTV outage exceeded five seconds')
                else:processor.invalidate.assert_not_called()

    def test_gap_during_first_pair_uses_remaining_budget_with_same_lease(self):
        f=coordinator_tests.CoordinatorTests();processor=f.make();processor.runtime=Mock()
        processor.runtime.snapshot.return_value={'version':1}
        processor.runtime.environment_ready.side_effect=[True,True,ValueError('Fresh CCTV'),True,True,True]
        processor.runtime.movement_generation.side_effect=[0,0,1,1,1,1,1,1]
        result=processor.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'),dict(trigger_id='e',expected_action='RECHECK',source='CCTV'))
        self.assertEqual(result['status'],'accepted');self.assertEqual(f.capture_number,3)
        payload=next(body for path,body in f.calls if path=='/esp32_validate')
        self.assertEqual([sample['capture']['id'] for sample in payload['dual_cam_evidence']['samples']],['2','3'])
        self.assertEqual(sum(path=='/validation_begin' for path,_ in f.calls),1)
        self.assertEqual(sum(path=='/validation_end' for path,_ in f.calls),1)
        self.assertFalse(any(path=='/validation_progress' and body.get('failed') for path,body in f.calls))

    def test_gap_after_the_second_pair_cannot_submit_old_evidence(self):
        f=coordinator_tests.CoordinatorTests();processor=f.make();processor.runtime=Mock()
        processor.runtime.snapshot.return_value={'version':1}
        processor.runtime.environment_ready.side_effect=[True,True,True,ValueError('Fresh CCTV'),True]
        processor.runtime.movement_generation.side_effect=[0,0,0,0,0,1]
        with self.assertRaises(coordinator_tests.ScanFailure) as caught:
            processor.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'),dict(trigger_id='e',expected_action='RECHECK',source='CCTV'))
        self.assertEqual(caught.exception.code,'STALE_CAPTURE')
        self.assertEqual(f.capture_number,2)
        self.assertFalse(any(path=='/esp32_validate' for path,_ in f.calls))

    def test_commit_health_race_is_nonterminal_with_the_original_retry_budget(self):
        from src.runtime_validation import validate_runtime_request
        f=inventory_tests.RuntimeTests();f.setUp();f.baseline();args=f.action()
        context=f.adapter_context();context.update(pending=[args['event']],captures=args['captures'])
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        for receipt in context['captures'].values():receipt.update(ip1='back',ip2='front')
        f.runtime.camera('CCTV_02',f.now,False)
        body=dict(shelf_id=SID,validation_token='lease',source='CCTV',trigger_id='event',ip='back',ip2='front',image_back='b',image_front='f',
            run_id='run',expected_version=1,dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':args['samples']})
        from unittest.mock import patch
        with patch('src.runtime_validation.time.time',return_value=f.now):status,result=validate_runtime_request(body,**context)
        self.assertEqual((status,result['status'],result['code']),(409,'retry_pending','CCTV_UNAVAILABLE'))
        self.assertEqual(f.runtime.snapshot(SID)['state'],'Ready');self.assertFalse(f.runtime.event_terminal(SID,'event'))
        self.assertEqual(context['pending'][0]['retry_count'],1)
