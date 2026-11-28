"""Direction is from trusted ESP32 observations; unmatched changes are RAM-only."""
import copy
import ast
import threading
import types
import urllib.parse
import unittest
from unittest.mock import Mock,patch
import test_runtime_inventory as fixtures
import consensus_fixture as consensus_fixtures
import test_autonomous_edge as coordinator_fixtures
from src.runtime_inventory import RuntimeInventory
from src.runtime_validation import prepare_runtime_retry,validate_runtime_request,discard_runtime_attempt

SID='SHELF_R1_C1'


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RuntimeTests();self.f.setUp();self.f.baseline();self.runtime=self.f.runtime

    def test_wrong_cctv_direction_cannot_override_picture_direction(self):
        result=self.runtime.accept(SID,**self.f.action(expected_action='RETURN'))
        self.assertEqual(result['action'],'BORROW');self.assertTrue(result['action_authorized'])
        self.assertEqual(result['comparison']['delta'],-1)
        args=self.f.action('Front_111','Back_11X',expected_action='BORROW')
        args['event_id']='return';args['event']['trigger_id']='return'
        result=self.runtime.accept(SID,**args)
        self.assertEqual(result['action'],'RETURN');self.assertTrue(result['action_authorized'])

    def test_multi_box_change_updates_baseline_without_creating_borrow(self):
        result=self.runtime.accept(SID,**self.f.action('Front_010','Back_000'))
        self.assertEqual(result['action'],'UNATTRIBUTED');self.assertFalse(result['action_authorized'])
        self.assertEqual(self.runtime.snapshot(SID)['baseline']['total'],1)
        self.assertEqual(self.runtime.shelves[SID]['events']['event'],'UNATTRIBUTED')

    def test_background_change_records_time_before_after_and_pictures_in_ram(self):
        samples,receipts=self.f.fresh('Front_011','Back_11X')
        self.runtime.accept(SID,run_id='run',event_id='background',expected_version=1,samples=samples,captures=receipts,
            lease='lease',source='COUNT_CHECK',images={'image_back':'back','image_front':'front'})
        entry=self.runtime.latest_changes(include_images=True)[0]
        self.assertEqual(entry['label'],'Unattributed change');self.assertEqual(entry['timestamp'],self.f.now)
        self.assertEqual((entry['before']['total'],entry['after']['total']),(5,4))
        self.assertEqual(entry['pictures']['after'],{'image_back':'back','image_front':'front'})
        self.assertNotIn('pictures',self.runtime.latest_changes()[0])
        self.assertEqual(self.runtime.snapshot(SID)['baseline']['total'],4)

    def test_overlapping_event_and_old_queued_event_cannot_become_transactions(self):
        first=self.f.action();first['event']['overlap']=True
        result=self.runtime.accept(SID,**first)
        self.assertEqual(result['action'],'UNATTRIBUTED');self.assertFalse(result['action_authorized'])
        second=self.f.action('Front_111','Back_11X')
        second.update(event_id='second');second['event'].update(trigger_id='second',overlap=True,timestamp=first['event']['timestamp'])
        result=self.runtime.accept(SID,**second)
        self.assertEqual(result['action'],'UNATTRIBUTED');self.assertFalse(result['action_authorized'])
        self.assertEqual(self.runtime.snapshot(SID)['current_quantity'],5)

    def test_hidden_b1_unprovable_change_keeps_range_and_is_unattributed(self):
        self.f.setUp();self.f.baseline('Front_111','Back_X01');self.runtime=self.f.runtime
        result=self.runtime.accept(SID,**self.f.action('Front_011','Back_X01'))
        self.assertEqual(result['action'],'UNATTRIBUTED');self.assertFalse(result['action_authorized'])
        self.assertEqual(result['count_range'],[2,3]);self.assertIsNone(result['occupancy'][3])

    def test_bounded_ram_log_is_not_restored_after_restart(self):
        for number in range(7):
            samples,receipts=self.f.fresh('Front_011' if number%2==0 else 'Front_111','Back_11X')
            self.runtime.accept(SID,run_id='run',event_id=str(number),expected_version=self.runtime.snapshot(SID)['version'],
                samples=samples,captures=receipts,lease='lease',source='COUNT_CHECK')
        self.assertEqual(len(self.runtime.latest_changes()),5)
        self.assertEqual(RuntimeInventory('new').latest_changes(),[])

    def test_untrusted_or_single_capture_cannot_update_or_log_a_change(self):
        samples,receipts=self.f.fresh('Front_011','Back_11X');before=self.runtime.snapshot(SID)
        for bad in (samples[:1],[dict(samples[0],front_confidence=79),samples[1]]):
            with self.assertRaises(ValueError):
                self.runtime.accept(SID,run_id='run',event_id='bad',expected_version=before['version'],samples=bad,captures=copy.deepcopy(receipts),lease='lease',source='COUNT_CHECK')
        self.assertEqual(self.runtime.snapshot(SID)['baseline'],before['baseline']);self.assertEqual(self.runtime.latest_changes(),[])


class QueueAndRetryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RuntimeTests();self.f.setUp();self.f.baseline();self.args=self.f.action()
        self.context=self.f.adapter_context();self.event=self.args['event'];self.context['pending']=[self.event]

    def retry(self,code='LOW_CONFIDENCE'):
        with patch('src.runtime_validation.time.time',return_value=self.f.now):
            return prepare_runtime_retry(SID,'lease',code,**{key:self.context[key] for key in ('authority','coordinator','consensus_lock','pending')})

    def test_nonterminal_retry_is_once_only_and_keeps_the_original_evidence_time(self):
        before=self.f.runtime.snapshot(SID);timestamp=self.event['timestamp']
        self.assertTrue(self.retry());self.assertTrue(self.retry(),'repeat notification does not consume a second retry')
        self.assertEqual(self.event['retry_count'],1);self.assertEqual(self.event['timestamp'],timestamp)
        self.assertEqual(self.f.runtime.snapshot(SID)['baseline'],before['baseline'])
        self.event['retry_pending']=False
        self.assertFalse(self.retry())

    def test_expired_or_terminal_held_events_are_never_retried(self):
        self.f.now+=56;self.assertFalse(self.retry())
        self.f.now-=56;self.f.runtime.hold(SID,'event');self.assertFalse(self.retry())
        samples,receipts=self.f.fresh()
        self.f.runtime.accept(SID,run_id='run',event_id='new-baseline',expected_version=self.f.runtime.snapshot(SID)['version'],samples=samples,captures=receipts,lease='lease',source='STARTUP')
        self.assertTrue(self.f.runtime.event_terminal(SID,'event'));self.assertFalse(self.retry())
        self.assertEqual(self.f.runtime.shelves[SID]['events']['event'],'HELD')

    def test_adapter_low_confidence_is_retry_pending_without_terminal_hold(self):
        samples=self.args['samples'];samples[0]['front_confidence']=79
        context=self.context;context['captures']=self.args['captures']
        for record in context['captures'].values():record.update(ip1='back',ip2='front')
        context['load_mappings'].return_value=[dict(shelf_id=SID,ip_address='back',secondary_ip_address='front')]
        body=dict(shelf_id=SID,validation_token='lease',source='CCTV',trigger_id='event',ip='back',ip2='front',image_back='b',image_front='f',
            run_id='run',expected_version=1,dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples})
        with patch('src.runtime_validation.time.time',return_value=self.f.now):status,result=validate_runtime_request(body,**context)
        self.assertEqual((status,result['status']),(409,'retry_pending'))
        self.assertEqual(self.f.runtime.snapshot(SID)['state'],'Ready');self.assertFalse(self.f.runtime.event_terminal(SID,'event'))
        context['readiness'].set.assert_not_called()

    def test_second_same_shelf_agreement_queues_even_while_first_is_claimed(self):
        f=consensus_fixtures.ConsensusFixture();f.setUp();vote=f.ns['record_cctv_consensus']
        def consensus(prefix,ready):
            for cid in (1,2,3):
                result=vote('ANY_SHELF' if cid==3 else SID,f'CCTV_0{cid}','RETURN' if cid==2 else 'BORROW',.99,
                    trigger_id=f'{prefix}{cid}',queue_ready=lambda sid:ready)
            return result
        first=consensus('a',True);self.assertTrue(first['consensus_met'])
        f.ns['PENDING_CCTV_TRIGGERS'][0].update(claimed=True,validation_token='first')
        f.now+=2
        second=consensus('b',False)
        self.assertTrue(second['inventory_eligible']);self.assertEqual(second['expected_action'],'RECHECK')
        self.assertEqual(len(f.ns['PENDING_CCTV_TRIGGERS']),2)
        self.assertTrue(all(event['overlap'] for event in f.ns['PENDING_CCTV_TRIGGERS']))

    def test_finishing_one_attempt_preserves_the_second_pending_action(self):
        other=dict(self.event,trigger_id='second',claimed=False);other.pop('validation_token')
        self.context['pending'].append(other)
        discard_runtime_attempt(SID,'lease',**{key:self.context[key] for key in ('coordinator','consensus_lock','pending','capture_lock','captures')})
        self.assertEqual([event['trigger_id'] for event in self.context['pending']],['second'])

    def test_retry_release_discards_old_pairs_then_reclaims_original_event_with_new_lease(self):
        self.assertTrue(self.retry());capability=object()
        captures=self.args['captures'];old=copy.deepcopy(captures)
        body=dict(shelf_id=SID,token='lease',source='CCTV',trigger_id='event')
        readiness=types.SimpleNamespace(ready=lambda sid:True,get=lambda sid:{'ready_at':self.f.now},set=Mock())
        ns=dict(urllib=types.SimpleNamespace(parse=urllib.parse),RUNTIME_INVENTORY=self.f.runtime,
            PROCESSOR_AUTHORITY='service',_PROCESSOR_CAPABILITY=capability,RECHECKS=self.context['coordinator'],READINESS=readiness,
            DUAL_CAM_CAPTURE_LOCK=threading.Lock(),DUAL_CAM_CAPTURES=captures,CCTV_CONSENSUS_LOCK=self.context['consensus_lock'],
            PENDING_CCTV_TRIGGERS=self.context['pending'],esp32_ref_manager=types.SimpleNamespace(baseline_freeze_until={}),
            time=types.SimpleNamespace(time=lambda:self.f.now))
        for name in ('do_POST','claim_cctv_trigger'):
            exec(compile(ast.Module(body=[self.f.bridge_function(name)],type_ignores=[]),'real bridge','exec'),ns)
        handler=types.SimpleNamespace(path='/validation_end',_processor_capability=capability,_read_json_body=lambda:body,_send_json=Mock())
        ns['do_POST'](handler)
        self.assertEqual(captures,{});self.assertEqual(len(self.context['pending']),1)
        self.assertFalse(self.event['claimed']);self.assertNotIn('validation_token',self.event)
        self.f.now+=2
        self.assertTrue(ns['claim_cctv_trigger']('event',SID,'retry-lease'))
        self.assertFalse(self.event['retry_pending'])
        for cid in ('CCTV_01','CCTV_02','CCTV_03'):self.f.runtime.camera(cid,self.f.now,True)
        with self.assertRaisesRegex(ValueError,'another validation event'):
            self.f.runtime.accept(SID,run_id='run',event_id='event',expected_version=1,samples=self.args['samples'],captures=old,lease='retry-lease',source='CCTV',event=self.event)
        samples,receipts=self.f.fresh('Front_011','Back_11X',lease='retry-lease')
        result=self.f.runtime.accept(SID,run_id='run',event_id='event',expected_version=1,samples=samples,captures=receipts,lease='retry-lease',source='CCTV',event=self.event)
        self.assertTrue(result['action_authorized']);self.assertEqual(result['action'],'BORROW')

    def test_second_event_cannot_overtake_the_first_event_retry(self):
        f=coordinator_fixtures.SchedulerTests();head=dict(shelf_id=SID,trigger_id='first',retry_after=101)
        second=dict(shelf_id=SID,trigger_id='second')
        processor=f.make({SID:{'state':'Ready','runtime':{'action_capable':True}}},pending=[head,second])
        processor.actions_blocker=Mock(return_value=None)
        with patch('src.autonomous_edge.time.time',return_value=100):processor._validations()
        processor.validate.assert_not_called()
        processor.stop.is_set.side_effect=[False,True]
        with patch('src.autonomous_edge.time.time',return_value=102):processor._validations()
        processor.validate.assert_called_once_with(f.C1,head)
