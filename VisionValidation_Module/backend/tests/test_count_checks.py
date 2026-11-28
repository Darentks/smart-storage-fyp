"""Trusted native background counts: preserve failed observations and never create actions."""
import copy
import unittest
from unittest.mock import Mock,patch
import test_runtime_inventory as inventory_tests
import test_autonomous_edge as coordinator_tests
from src.runtime_validation import validate_runtime_request

SID='SHELF_R1_C1'


class CountCheckTests(unittest.TestCase):
    def setUp(self):
        self.f=inventory_tests.RuntimeTests();self.f.setUp();self.runtime=self.f.runtime

    def check(self,front='Front_111',back='Back_11X',**changes):
        samples,receipts=self.f.fresh(front,back)
        args=dict(shelf=SID,run_id='run',event_id='check',expected_version=self.runtime.snapshot(SID)['version'],
            samples=samples,captures=receipts,lease='lease',source='COUNT_CHECK',transport_ok=False)
        args.update(changes)
        return self.runtime.accept(**args)

    def test_count_change_is_not_a_borrow_or_return_and_needs_no_cctv(self):
        self.f.baseline();self.runtime.set_cctv_enabled(False)
        result=self.check('Front_010','Back_000')
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],1)
        self.assertEqual(result['count_check']['status'],'Unattributed change')
        self.assertEqual(result['comparison']['state_delta'],'COUNT_CHECK')
        self.assertFalse(self.runtime.snapshot(SID)['action_capable'])
        self.assertNotIn('check',self.runtime.shelves[SID]['events'])

    def test_unchanged_counts_and_many_background_checks_do_not_fill_action_event_limit(self):
        self.f.baseline()
        for _ in range(205):result=self.check()
        self.assertEqual(result['count_check']['status'],'Count unchanged')
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],5)
        self.assertEqual(len(self.runtime.shelves[SID]['events']),1)

    def test_hidden_visible_match_verifies_range_and_keeps_exact_history_separate(self):
        self.f.baseline('Front_011','Back_X01')
        baseline=self.runtime.shelves[SID]['baseline']
        baseline.update(total=3,precision='EXACT',count_range=[3,3],slots=[0,1,1,1,0])
        before=self.runtime.snapshot(SID)
        result=self.check('Front_011','Back_X01',transport_ok=True)
        current=self.runtime.snapshot(SID)
        self.assertIsNone(current['current_quantity']);self.assertEqual(current['count_range'],[2,3])
        self.assertGreater(current['observed_at'],before['observed_at'])
        self.assertEqual(current['historical']['total'],3)
        self.assertEqual(current['historical']['observed_at'],before['observed_at'])
        self.assertIsNone(current['latest_observation']['slots'][3])
        self.assertIsNone(current['baseline']['slots'][3])
        self.assertEqual(result['count_check']['status'],'B1 hidden — range')
        self.assertFalse(current['count_check_unverified']);self.assertTrue(current['action_capable'])
        self.assertTrue(current['authoritative'],'the fresh range is verified without verifying B1')

    def test_repeated_hidden_checks_advance_range_timestamp_without_refreshing_history(self):
        self.f.baseline()
        original=self.runtime.snapshot(SID)['observed_at']
        self.check('Front_111','Back_X01',transport_ok=True)
        first=self.runtime.snapshot(SID)
        self.check('Front_111','Back_X01',transport_ok=True)
        second=self.runtime.snapshot(SID)
        self.assertEqual(second['historical']['observed_at'],original)
        self.assertGreater(second['observed_at'],first['observed_at'])
        self.assertEqual(second['count_range'],[3,4]);self.assertTrue(second['action_capable'])
        self.check()
        self.assertIsNone(self.runtime.snapshot(SID)['historical'])

    def test_every_invalid_x01_pair_retains_count_and_can_retry_next_check(self):
        for front in ('Front_000','Front_010','Front_100','Front_110'):
            with self.subTest(front=front):
                self.setUp();self.f.baseline();before=self.runtime.snapshot(SID)
                with self.assertRaisesRegex(ValueError,'M2 conflict'):self.check(front,'Back_X01')
                self.runtime.count_check_failed(SID,'M2_CONFLICT','conflict',before['version'])
                current=self.runtime.snapshot(SID)
                self.assertEqual(current['current_quantity'],before['current_quantity'])
                self.assertEqual(current['observed_at'],before['observed_at'])
                self.assertEqual(current['state'],'Ready')
                self.assertIn('Unclear — recheck',current['count_check']['message'])
                self.assertEqual(self.check()['count_check']['status'],'Count unchanged')

    def test_hidden_visible_change_shows_range_instead_of_a_guessed_exact_count(self):
        self.f.baseline()
        result=self.check('Front_011','Back_X01')
        self.assertEqual(result['count_range'],[2,3]);self.assertIsNone(self.runtime.snapshot(SID)['current_quantity'])
        self.assertIsNone(result['occupancy'][3]);self.assertIn('range',result['count_check']['message'])

    def test_failure_retains_count_but_does_not_overwrite_newer_inventory(self):
        self.f.baseline();before=self.runtime.snapshot(SID)
        self.assertTrue(self.runtime.count_check_failed(SID,'LOW_CONFIDENCE','Low confidence',before['version']))
        after=self.runtime.snapshot(SID)
        self.assertEqual(after['current_quantity'],before['current_quantity'])
        self.assertEqual(after['observed_at'],before['observed_at'])
        self.assertEqual(after['count_check']['status'],'Check failed')
        self.assertFalse(after['authoritative'])
        self.assertFalse(self.runtime.count_check_failed(SID,'MISSING_CAMERA','Missing camera',before['version']-1))

    def test_deferred_check_keeps_a_valid_baseline_available_for_the_pending_action(self):
        self.f.baseline();before=self.runtime.snapshot(SID)
        self.runtime.count_check_failed(SID,'COUNT_CHECK_SKIPPED','CCTV check pending',before['version'])
        after=self.runtime.snapshot(SID)
        self.assertTrue(after['action_capable']);self.assertTrue(after['authoritative'])
        self.assertEqual(after['count_check']['status'],'Check deferred')

    def test_failed_confirmation_and_reused_receipts_cannot_change_count(self):
        self.f.baseline();samples,receipts=self.f.fresh('Front_010','Back_000')
        for issue in ('confidence','missing','unstable'):
            bad=copy.deepcopy(samples)
            if issue=='confidence':bad[1]['front_confidence']=79
            elif issue=='missing':bad=bad[:1]
            else:bad[1]['front_class']='Front_000'
            with self.assertRaises(ValueError):
                self.runtime.accept(SID,run_id='run',event_id='check',expected_version=self.runtime.snapshot(SID)['version'],
                    samples=bad,captures=copy.deepcopy(receipts),lease='lease',source='COUNT_CHECK')
            self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],5)


class CountCheckSchedulingTests(unittest.TestCase):
    def make(self,pending=None):
        f=coordinator_tests.SchedulerTests()
        p=f.make({SID:{'state':'Ready','runtime':{'action_capable':False}}},mqtt=False,cameras_live=False,pending=pending)
        p.cctv_enabled=False;p.runtime=Mock();p.runtime.snapshot.return_value={'version':1};p.count_check_due[SID]=0
        return p

    def test_runs_when_due_with_cctv_and_mqtt_off(self):
        p=self.make();p._validations()
        p.validate.assert_called_once_with(coordinator_tests.SchedulerTests.C1,count_check=True)

    def test_ready_shelf_is_checked_at_thirty_seconds_not_on_every_poll(self):
        p=self.make();p.count_check_due.clear()
        for now in (100,129.9):
            p.stop.is_set.side_effect=[False,True]
            with patch('src.autonomous_edge.time.monotonic',return_value=now):p._validations()
            p.validate.assert_not_called()
        self.assertEqual(p.count_check_due[SID],130)
        p.stop.is_set.side_effect=[False,True]
        with patch('src.autonomous_edge.time.monotonic',return_value=130):p._validations()
        p.validate.assert_called_once();self.assertEqual(p.count_check_due[SID],160)

    def test_native_count_check_uses_two_paired_captures_and_shows_its_pictures(self):
        f=coordinator_tests.CoordinatorTests();p=f.make();p.runtime=Mock()
        p.runtime.snapshot.return_value={'version':1};p.runtime.environment_wait_reason.return_value=None;p.pending.return_value=[]
        p.panel_observations.put(SID,{'image_back':'previous CCTV picture'},{'previous_cctv':True})
        p.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'),count_check=True)
        self.assertEqual(sum(path.startswith('/esp32_preview') for path,_ in f.calls),2)
        payload=next(body for path,body in f.calls if path=='/esp32_validate')
        self.assertEqual(payload['source'],'COUNT_CHECK');self.assertEqual(len(payload['dual_cam_evidence']['samples']),2)
        self.assertFalse(any(path=='/validation_progress' for path,_ in f.calls))
        shown=p.panel_observations.get(SID)  # display only: the panel shows the latest picture, labelled by source
        self.assertEqual(shown['source'],'count_check');self.assertNotIn('previous_cctv',shown['candidate']);self.assertFalse(shown['authorizes_write'])

    def test_new_cctv_event_between_pairs_preempts_without_holding_shelf(self):
        f=coordinator_tests.CoordinatorTests();p=f.make();p.runtime=Mock()
        p.runtime.snapshot.return_value={'version':1};p.runtime.environment_wait_reason.return_value=None;p.pending.return_value=[]
        def wait(_):
            p.pending.return_value=[{'source':'CCTV'}]
            return False
        p.stop.wait.side_effect=wait
        with self.assertRaises(coordinator_tests.ScanFailure):p.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'),count_check=True)
        self.assertEqual(sum(path.startswith('/esp32_preview') for path,_ in f.calls),1)
        self.assertFalse(any(path in ('/validation_progress','/esp32_validate') for path,_ in f.calls))

    def test_not_due_and_pending_cctv_checks_prevent_background_capture(self):
        p=self.make();p.count_check_due[SID]=float('inf');p._validations();p.validate.assert_not_called()
        p=self.make(pending=[{'shelf_id':SID,'source':'CCTV'}]);p._validations();p.validate.assert_not_called()
        p=self.make();p.cctv_busy=lambda:True;p._validations();p.validate.assert_not_called()

    def test_last_known_count_is_not_overwritten_by_an_unrequested_startup_scan(self):
        f=coordinator_tests.SchedulerTests()
        p=f.make({SID:{'state':'Ready','runtime':{'action_capable':False,'count_check_unverified':True}}})
        p.runtime=Mock();p._validations();p.validate.assert_not_called()

    def test_oldest_due_shelf_runs_first_so_slow_checks_cannot_starve_other_shelves(self):
        ids=[SID,'SHELF_R1_C2','SHELF_R1_C3']
        shelves=[dict(coordinator_tests.SchedulerTests.C1,shelf_id=sid) for sid in ids]
        p=coordinator_tests.SchedulerTests().make({sid:{'state':'Ready','runtime':{'action_capable':False}} for sid in ids},
            mqtt=False,cameras_live=False,shelves=shelves)
        p.runtime=Mock();p.runtime.snapshot.return_value={'version':1}
        p.count_check_due=dict(zip(ids,[10,0,5]))
        p._validations();p.validate.assert_called_once_with(shelves[1],count_check=True)

    def test_preemption_does_not_send_failure_progress_or_overwrite_native_images(self):
        f=coordinator_tests.CoordinatorTests();p=f.make();p.runtime=Mock()
        p.runtime.snapshot.return_value={'version':1};p.pending.return_value=[{'source':'CCTV'}]
        with self.assertRaises(coordinator_tests.ScanFailure):p.validate(dict(shelf_id=SID,ip_address='back',secondary_ip_address='front'),count_check=True)
        self.assertFalse(any(path in ('/validation_progress','/esp32_validate') or path.startswith('/esp32_preview') for path,_ in f.calls))
        self.assertEqual(f.calls[-1][0],'/validation_end')

    def test_http_adapter_failure_preserves_count_and_cctv_votes(self):
        f=inventory_tests.RuntimeTests();f.setUp();f.baseline();context=f.adapter_context()
        # Invalid paired evidence is a background-check failure, not a shelf HOLD.
        before=f.runtime.snapshot(SID)
        body=dict(shelf_id=SID,validation_token='lease',source='COUNT_CHECK',expected_version=before['version'])
        status,_=validate_runtime_request(body,**context)
        self.assertEqual(status,409);self.assertEqual(f.runtime.snapshot(SID)['current_quantity'],5)
        self.assertEqual(f.runtime.snapshot(SID)['state'],'Ready')
        self.assertEqual(f.runtime.snapshot(SID)['count_check']['status'],'Check failed')

    def test_adapter_defers_for_partial_cctv_votes_and_never_clears_them(self):
        f=inventory_tests.RuntimeTests();f.setUp();f.baseline()
        samples,receipts=f.fresh('Front_010','Back_000');context=f.adapter_context()
        for receipt in receipts.values():receipt.update(ip1='back',ip2='front')
        context['captures']=receipts;context['mqtt'].is_connected=False
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        votes={'CCTV_01':{'ts':f.now}}
        body=dict(shelf_id=SID,validation_token='lease',source='COUNT_CHECK',ip='back',ip2='front',image_back='back',image_front='front',
            run_id='run',expected_version=f.runtime.snapshot(SID)['version'],runtime_event_id='check',
            dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples})
        with patch('src.runtime_validation.time.time',return_value=f.now):
            status,result=validate_runtime_request(body,**context,votes=votes)
        self.assertEqual((status,result['code']),(409,'COUNT_CHECK_SKIPPED'))
        self.assertEqual(votes,{'CCTV_01':{'ts':f.now}});self.assertEqual(f.runtime.snapshot(SID)['current_quantity'],5)
        self.assertEqual(len(receipts),2,'deferred check cannot consume evidence as an accepted update')

    def test_adapter_accepts_count_change_without_mqtt_or_action_metadata_and_rejects_replay(self):
        f=inventory_tests.RuntimeTests();f.setUp();f.baseline()
        samples,receipts=f.fresh('Front_011','Back_11X');context=f.adapter_context()
        for receipt in receipts.values():receipt.update(ip1='back',ip2='front')
        context['captures']=receipts;context['mqtt'].is_connected=False;context['intents']=Mock()
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        body=dict(shelf_id=SID,validation_token='lease',source='COUNT_CHECK',ip='back',ip2='front',image_back='back',image_front='front',
            run_id='run',expected_version=f.runtime.snapshot(SID)['version'],runtime_event_id='check',
            dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples})
        status,result=validate_runtime_request(body,**context)
        self.assertEqual((status,result['count_check']['status']),(200,'Unattributed change'))
        self.assertEqual(f.runtime.snapshot(SID)['current_quantity'],4)
        context['intents'].update.assert_not_called();context['readiness'].set.assert_not_called()
        self.assertEqual(len(receipts),0)
        self.assertEqual(validate_runtime_request(body,**context)[0],409)
        self.assertEqual(f.runtime.snapshot(SID)['count_check']['status'],'Unattributed change')


if __name__=='__main__':unittest.main()
