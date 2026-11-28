"""Side-only presence never stops scanning; changed counts require a fresh clear recheck."""
import ast
import copy
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock, patch
import test_runtime_inventory as inventory_tests
import test_count_checks as count_tests
import consensus_fixture as replay_tests
from src.runtime_validation import validate_runtime_request

SID='SHELF_R1_C1'


class SidePresenceTests(unittest.TestCase):
    def setUp(self):
        self.f=inventory_tests.RuntimeTests();self.f.setUp();self.f.baseline()
        self.runtime=self.f.runtime

    def advance(self,seconds,present=False):
        while seconds>1e-8:
            step=min(.5,seconds);self.f.now+=step;seconds-=step
            for cid in ('CCTV_01','CCTV_02'):self.runtime.camera(cid,self.f.now,True)
            if present is not None:self.runtime.camera('CCTV_03',self.f.now,True,present)

    def pairs(self,front='Front_011',back='Back_11X',present=True):
        samples=[];receipts={}
        for index,pause in enumerate((.2,2)):
            self.advance(pause,present[index] if isinstance(present,tuple) else present)
            self.f.sequence+=1;identity=str(self.f.sequence)
            receipts[identity]=dict(run_id='run',shelf_id=SID,validation_token='lease',complete=True,
                completed_at=self.f.now,duration_ms=100,ip1='back',ip2='front')
            samples.append(dict(front_class=front,back_class=back,front_confidence=95,back_confidence=95,capture={'id':identity}))
        return samples,receipts

    def accept(self,pair,**changes):
        samples,receipts=pair
        args=dict(run_id='run',event_id='background',expected_version=self.runtime.snapshot(SID)['version'],
            samples=samples,captures=receipts,lease='lease',source='COUNT_CHECK')
        args.update(changes)
        return self.runtime.accept(SID,**args)

    def deferred(self,pair):
        before=self.runtime.snapshot(SID)
        changes=self.runtime.latest_changes(include_images=True)
        result=self.accept(pair)
        self.assertEqual(result['code'],'SIDE_PRESENCE_DEFERRED')
        after=self.runtime.snapshot(SID)
        for key in ('baseline','version','current_quantity','observed_at','ready_at','born_at','historical','action_capable','authoritative'):
            self.assertEqual(after[key],before[key],key)
        self.assertTrue(after['side_count_deferred'])
        self.assertFalse(after['count_check_unverified'])
        self.assertEqual(self.runtime.latest_changes(include_images=True),changes,'a deferral must not log an applied change')
        return result

    def test_continuous_side_presence_allows_scanning_and_unchanged_counts(self):
        quiet=self.runtime.last_interaction
        pair=self.pairs('Front_111','Back_11X')
        self.assertEqual(self.runtime.last_interaction,quiet)
        self.assertIsNone(self.runtime.environment_wait_reason(require_cameras=False))
        self.assertTrue(self.runtime.environment_ready())
        self.assertEqual(self.accept(pair)['count_check']['status'],'Count unchanged')

    def test_initial_scan_can_finish_during_continuous_side_presence(self):
        self.f=inventory_tests.RuntimeTests();self.f.setUp();self.runtime=self.f.runtime
        self.advance(3,True)
        result=self.accept(self.pairs(),source='STARTUP')
        self.assertEqual(result['status'],'success');self.assertEqual(result['comparison']['quantity_after'],4)

    def test_changed_count_defers_then_needs_clear_period_and_two_new_pairs(self):
        pair=self.pairs();old=copy.deepcopy(pair)
        self.deferred(pair)
        self.assertEqual(pair[1],{},'deferred images cannot be reused')
        self.assertFalse(self.runtime.count_recheck_ready(SID))
        self.advance(1.9,False);self.assertFalse(self.runtime.count_recheck_ready(SID))
        self.advance(.6,False);self.assertTrue(self.runtime.count_recheck_ready(SID))
        # Even restored unconsumed copies predate the clear boundary.
        self.deferred(old)
        pair=self.pairs(present=False)
        with self.assertRaisesRegex(ValueError,'consecutive matching'):
            self.accept((pair[0][:1],copy.deepcopy(pair[1])))
        self.assertEqual(self.accept(pair)['comparison']['quantity_after'],4)
        self.assertFalse(self.runtime.snapshot(SID)['side_count_deferred'])

    def test_stale_clear_evidence_or_a_gap_cannot_release_deferral(self):
        self.deferred(self.pairs());self.advance(2.5,False)
        self.assertTrue(self.runtime.count_recheck_ready(SID))
        self.advance(1.6,None);self.assertFalse(self.runtime.count_recheck_ready(SID))
        self.deferred(self.pairs(present=None))
        self.advance(.1,False);self.assertFalse(self.runtime.count_recheck_ready(SID))
        self.advance(2,False);self.assertTrue(self.runtime.count_recheck_ready(SID))

    def test_presence_arriving_between_pairs_or_at_commit_defers_changed_count(self):
        self.deferred(self.pairs(present=(False,True)))
        self.advance(2.5,False)
        pair=self.pairs(present=False)
        self.runtime.camera('CCTV_03',self.f.now,True,True)
        self.deferred(pair)
        self.advance(2.5,False)
        pair=self.pairs(present=False)
        # Presence followed by less than a full clear window must still block.
        self.runtime.camera('CCTV_03',self.f.now,True,True)
        self.advance(.5,False)
        self.deferred(pair)

    def test_a_pair_started_before_two_seconds_of_clearance_cannot_commit(self):
        self.deferred(self.pairs());self.advance(1,False)
        pair=self.pairs(present=False)
        self.assertTrue(self.runtime.count_recheck_ready(SID))
        self.deferred(pair)
        self.assertEqual(self.accept(self.pairs(present=False))['status'],'success')

    def test_hidden_b1_stays_unknown_through_deferred_recheck(self):
        self.f=inventory_tests.RuntimeTests();self.f.setUp();self.f.baseline('Front_111','Back_X01');self.runtime=self.f.runtime
        self.deferred(self.pairs('Front_011','Back_X01'))
        self.assertIsNone(self.runtime.snapshot(SID)['baseline']['slots'][3])
        self.advance(2.5,False)
        self.accept(self.pairs('Front_011','Back_X01',False))
        current=self.runtime.snapshot(SID)
        self.assertEqual(current['count_range'],[2,3]);self.assertIsNone(current['current_quantity'])
        self.assertIsNone(current['baseline']['slots'][3])

    def test_visibility_only_changes_are_not_treated_as_movement(self):
        # A previously known physical B1 becomes occluded; no known slot changes.
        self.runtime.shelves[SID]['baseline'].update(slots=[1,1,1,1,0],total=4,count_range=[4,4])
        result=self.accept(self.pairs('Front_111','Back_X01'))
        self.assertEqual(result['status'],'success');self.assertEqual(result['count_range'],[3,4])
        self.assertIsNone(self.runtime.snapshot(SID)['baseline']['slots'][3])

    def test_explicit_cctv_stop_allows_the_normal_changed_count(self):
        self.deferred(self.pairs());self.runtime.set_cctv_enabled(False)
        self.assertTrue(self.runtime.count_recheck_ready(SID))
        self.assertEqual(self.accept(self.pairs(present=None))['comparison']['quantity_after'],4)

    def test_automatic_rebaseline_after_invalidation_cannot_bypass_a_deferral(self):
        self.deferred(self.pairs());self.runtime.invalidate()
        self.advance(3,True);before=self.runtime.snapshot(SID)
        result=self.accept(self.pairs(),source='STARTUP')
        self.assertEqual(result['status'],'deferred')
        current=self.runtime.snapshot(SID)
        for key in ('baseline','observed_at','version','state'):self.assertEqual(current[key],before[key])
        self.advance(2.5,False)
        result=self.accept(self.pairs(present=False),source='STARTUP')
        self.assertEqual(result['status'],'success');self.assertEqual(result['comparison']['quantity_after'],4)

    def test_shelf_activity_still_restarts_quiet_wait(self):
        for cid in ('CCTV_01','CCTV_02'):
            self.runtime.camera(cid,self.f.now,True,True)
            self.assertIn(cid,self.runtime.environment_wait_reason(require_cameras=False))
            with self.assertRaisesRegex(ValueError,'Quiet'):self.runtime.environment_ready(require_cameras=False)

    def test_valid_actions_can_commit_during_side_presence(self):
        args=self.f.action()
        self.runtime.camera('CCTV_03',self.f.now,True,True)
        self.assertEqual(self.runtime.accept(SID,**args)['comparison']['delta'],-1)

    def test_stale_or_offline_side_camera_still_blocks_actions(self):
        for stale in (True,False):
            self.setUp();args=self.f.action()
            self.runtime.camera('CCTV_03',self.f.now-2 if stale else self.f.now,stale)
            with self.assertRaisesRegex(ValueError,'Fresh CCTV'):self.runtime.accept(SID,**args)

    def test_adapter_deferral_preserves_readiness_and_does_not_become_failed_check(self):
        pair=self.pairs();samples,receipts=pair;context=self.f.adapter_context()
        context['captures']=receipts;context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        body=dict(shelf_id=SID,validation_token='lease',source='COUNT_CHECK',ip='back',ip2='front',image_back='b',image_front='f',
            run_id='run',expected_version=self.runtime.snapshot(SID)['version'],runtime_event_id='check',
            dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples})
        status,result=validate_runtime_request(body,**context)
        self.assertEqual((status,result['status']),(200,'deferred'))
        context['readiness'].set.assert_not_called()
        self.assertTrue(self.runtime.snapshot(SID)['action_capable'])

    def test_adapter_side_votes_allow_counts_but_shelf_votes_and_pending_actions_preempt(self):
        for vote,pending,expected in [({'CCTV_03':{'ts':100}},[],200),({'CCTV_01':{'ts':100}},[],409),
                ({'CCTV_02':{'ts':100}},[],409),({'CCTV_03':{'ts':100}},[{'source':'CCTV','timestamp':100}],409)]:
            self.setUp();samples,receipts=self.pairs('Front_111','Back_11X')
            context=self.f.adapter_context();context.update(captures=receipts,votes=vote,pending=pending)
            context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
            body=dict(shelf_id=SID,validation_token='lease',source='COUNT_CHECK',ip='back',ip2='front',image_back='b',image_front='f',
                run_id='run',expected_version=self.runtime.snapshot(SID)['version'],runtime_event_id='check',
                dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples})
            with patch('src.runtime_validation.time.time',return_value=100):status,_=validate_runtime_request(body,**context)
            self.assertEqual(status,expected)


class SideVotePriorityTests(unittest.TestCase):
    def test_bridge_busy_filter_ignores_only_side_votes(self):
        tree=ast.parse((Path(__file__).parents[2]/'camera_service.py').read_text(encoding='utf-8'))
        fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='cctv_busy')
        ns=dict(CCTV_CONSENSUS_LOCK=threading.RLock(),time=types.SimpleNamespace(time=lambda:100),CONSENSUS_WINDOW_SEC=4,
            CCTV_ALIGN_STATE={},PENDING_CCTV_TRIGGERS=[])
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'busy','exec'),ns)
        for cid,expected in [('CCTV_03',False),('CCTV_01',True),('CCTV_02',True)]:
            ns['CCTV_ALIGN_STATE']={cid:{'ts':100}}
            self.assertEqual(ns['cctv_busy'](),expected)
        ns['CCTV_ALIGN_STATE']={'CCTV_01':{'ts':95},'CCTV_03':{'ts':100}}
        self.assertFalse(ns['cctv_busy']())
        ns['PENDING_CCTV_TRIGGERS']=[{'source':'CCTV','timestamp':100}]
        self.assertTrue(ns['cctv_busy']())

    def test_side_only_votes_create_no_activity_and_still_support_three_camera_consensus(self):
        f=replay_tests.ConsensusFixture();f.setUp();store=Mock();f.ns['LOCAL_INTENTS']=store
        vote=f.ns['record_cctv_consensus']
        vote('ANY_SHELF','CCTV_03',None,.99,trigger_id='side')
        store.add.assert_not_called()
        vote(SID,'CCTV_01','BORROW',.99,trigger_id='grid1')
        self.assertEqual(store.add.call_args.args[0]['reason'],'MISSING_CCTV_CONSENSUS')
        result=vote(SID,'CCTV_02','BORROW',.99,trigger_id='grid2')
        self.assertTrue(result['consensus_met']);self.assertEqual(result['confirmed_cameras'],['CCTV_01','CCTV_02','CCTV_03'])
        self.assertEqual(len(f.ns['PENDING_CCTV_TRIGGERS']),1)

    def test_scheduler_waits_for_clear_evidence_then_schedules_fresh_capture(self):
        f=count_tests.CountCheckSchedulingTests();p=f.make()
        p.runtime.count_recheck_ready.return_value=False
        p._validations();p.validate.assert_not_called()
        p.stop.is_set.side_effect=[False,True]
        p.runtime.count_recheck_ready.return_value=True
        p.validate.return_value={'status':'deferred','code':'SIDE_PRESENCE_DEFERRED'}
        with patch('src.autonomous_edge.time.monotonic',return_value=100):p._validations()
        p.validate.assert_called_once_with(count_tests.coordinator_tests.SchedulerTests.C1,count_check=True)
        self.assertEqual(p.count_check_due[SID],102)
        p.runtime.count_check_failed.assert_not_called()

    def test_rebaseline_deferral_does_not_consume_the_scan_retry_budget(self):
        f=count_tests.coordinator_tests.SchedulerTests();p=f.make({})
        p.validate.return_value={'status':'deferred','code':'SIDE_PRESENCE_DEFERRED'}
        with patch('src.autonomous_edge.time.monotonic',return_value=100):p._attempt(f.C1,None)
        self.assertEqual(p.attempts[SID],0);self.assertEqual(p.retry_at[SID],102)
        self.assertNotIn(SID,p.exhausted);self.assertEqual(p.scan[SID]['state'],'Waiting')
