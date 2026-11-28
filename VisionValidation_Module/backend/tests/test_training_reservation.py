"""Training capture reserves one shelf only; other shelves keep counting. No cameras, network or MQTT."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_count_checks as count_tests
from src.periodic_recheck import RecheckCoordinator, TrainingUnavailable
from src.preview_panel import diagnostic_preview
from src.runtime_validation import reset_shelf_evidence

C1,C2='SHELF_R1_C1','SHELF_R1_C2'
IPS=('192.168.0.201','192.168.0.202')


class CoordinatorReservationTests(unittest.TestCase):
    def setUp(self):self.c=RecheckCoordinator()

    def test_reservation_blocks_only_the_target_shelf(self):
        reply=self.c.request_training(C1,IPS)
        self.assertEqual(reply['state'],'ready');self.assertTrue(reply['newly_ready'])
        self.assertIsNone(self.c.begin(C1,'COUNT_CHECK'),'the training shelf gets no native lease')
        self.assertIsNotNone(self.c.begin(C2,'COUNT_CHECK'),'other shelves keep counting')
        self.assertEqual((self.c.training_shelf(),self.c.training_capturing()),(C1,C1))

    def test_waits_for_the_target_shelf_lease_but_not_other_shelves(self):
        token=self.c.begin(C2,'COUNT_CHECK');self.assertIsNotNone(token)
        self.assertEqual(self.c.request_training(C1,IPS)['state'],'ready','a lease on another shelf does not block')
        c=RecheckCoordinator();lease=c.begin(C1,'COUNT_CHECK')
        first=c.request_training(C1,IPS);self.assertEqual(first['state'],'waiting');self.assertFalse(first['newly_ready'])
        self.assertIsNone(c.training_capturing(),'no CCTV votes are dropped while waiting')
        self.assertEqual(c.request_training(C1,IPS,first['token'],idle=False)['state'],'waiting','native still working on the shelf')
        c.end(C1,lease)
        ready=c.request_training(C1,IPS,first['token']);self.assertEqual(ready['state'],'ready');self.assertTrue(ready['newly_ready'])
        self.assertFalse(c.request_training(C1,IPS,first['token'])['newly_ready'],'the authority reset happens only once')

    def test_capture_admission_is_per_shelf(self):
        token=self.c.request_training(C1,IPS)['token']
        self.assertIsNone(self.c.training_capture_allowed(None,C2,('192.168.0.203','192.168.0.204')),'ordinary preview of another shelf')
        self.assertIs(self.c.training_capture_allowed(None,C1,IPS),False,'ordinary preview of the reserved shelf')
        self.assertIs(self.c.training_capture_allowed('other',C1,IPS),False)
        self.assertIs(self.c.training_capture_allowed(token,C1,('192.168.0.201','192.168.0.209')),False,'camera assignment changed')
        self.c.begin(C2,'COUNT_CHECK')
        self.assertIs(self.c.training_capture_allowed(token,C1,IPS),True,'another shelf lease does not block')

    def test_end_expiry_and_restart_release_the_shelf_without_a_global_resume(self):
        token=self.c.request_training(C1,IPS)['token']
        self.assertTrue(self.c.end_training(token));self.assertIsNone(self.c.training_shelf())
        self.assertIsNotNone(self.c.begin(C1,'COUNT_CHECK'),'the shelf is schedulable again straight away')
        c=RecheckCoordinator();c.request_training(C1,IPS)
        with patch('src.periodic_recheck.time.monotonic',return_value=time.monotonic()+31):self.assertIsNone(c.training_shelf())
        c=RecheckCoordinator();c.request_training(C1,IPS);c.clear_training();self.assertIsNone(c.training_capturing())
        with self.assertRaises(ValueError):c.request_training(C1,IPS,'stale-token')

    def test_second_batch_and_changed_cameras_are_rejected(self):
        token=self.c.request_training(C1,IPS)['token']
        with self.assertRaisesRegex(TrainingUnavailable,'Another training batch is running on SHELF_R1_C1'):self.c.request_training(C2,IPS)
        with self.assertRaisesRegex(TrainingUnavailable,'camera addresses changed'):self.c.request_training(C1,('192.168.0.201','192.168.0.209'),token)
        with self.assertRaisesRegex(TrainingUnavailable,'another batch'):self.c.end_training('other')
        self.c.clear_training()
        with self.assertRaisesRegex(TrainingUnavailable,'expired'):self.c.request_training(C1,IPS,token)


class ShelfEvidenceResetTests(unittest.TestCase):
    def test_only_the_trained_shelf_loses_authority_and_evidence(self):
        c=RecheckCoordinator();c.leases[C2]=('lease',time.time()+60)
        c.waiting={'a':dict(shelf=C1,expires=time.time()+60),'b':dict(shelf=C2,expires=time.time()+60)}
        pending=[dict(shelf_id=C1,trigger_id='t1'),dict(shelf_id=C2,trigger_id='t2')]
        votes={'CCTV_01':dict(shelf_id=C1),'CCTV_02':dict(shelf_id=C2),'CCTV_03':dict(shelf_id='ANY_SHELF')}
        captures={'x':dict(shelf_id=C1),'y':dict(shelf_id=C2)}
        authority,readiness,intents=Mock(),Mock(),Mock()
        reset_shelf_evidence(C1,authority=authority,coordinator=c,consensus_lock=threading.RLock(),pending=pending,
            capture_lock=threading.Lock(),captures=captures,votes=votes,readiness=readiness,intents=intents,reason='Training capture on this shelf')
        authority.reset_shelf.assert_called_once_with(C1);authority.invalidate.assert_not_called()
        self.assertIn(C2,c.leases);self.assertEqual(list(c.waiting),['b'])
        self.assertEqual(pending,[dict(shelf_id=C2,trigger_id='t2')])
        self.assertEqual(set(votes),{'CCTV_02','CCTV_03'});self.assertEqual(list(captures),['y'])
        readiness.set.assert_called_once();self.assertEqual(readiness.set.call_args.args[:2],(C1,'Offline'))
        intents.update.assert_called_once();self.assertEqual(intents.update.call_args.args[:2],('t1','HELD'))


class TrainingCaptureLockTests(unittest.TestCase):
    def test_reserved_training_capture_does_not_hold_the_shared_lock(self):
        c=RecheckCoordinator();token=c.request_training(C1,IPS)['token']
        seen=[]
        def fetch(**_):
            # Another shelf's native scan must be able to take the lock while this capture is in flight.
            result=[]
            thread=threading.Thread(target=lambda:result.append(c.global_lock.acquire(timeout=.5)) or c.global_lock.release())
            thread.start();thread.join(1);seen.append(result==[True])
            return None,'combined','back','front'
        manager=SimpleNamespace(fetch_dual_esp32_frame=fetch)
        query={k:[v] for k,v in dict(run_mode='diagnostic',purpose='training',shelf_id=C1,ip=IPS[0],ip2=IPS[1]).items()}
        status,row=diagnostic_preview(query,manager,c,SimpleNamespace(cctv_busy=lambda:True),training_token=token)
        self.assertEqual(status,200);self.assertEqual(seen,[True]);self.assertFalse(row['authorizes_write'])
        self.assertTrue(c.global_lock.acquire(blocking=False),'lock is not released twice or leaked');c.global_lock.release()

    def test_ordinary_preview_of_reserved_shelf_is_refused_before_hardware(self):
        c=RecheckCoordinator();c.request_training(C1,IPS)
        manager=Mock()
        query={k:[v] for k,v in dict(run_mode='diagnostic',shelf_id=C1,ip=IPS[0],ip2=IPS[1]).items()}
        status,row=diagnostic_preview(query,manager,c,None)
        self.assertEqual(status,409);self.assertEqual(row['code'],'TRAINING_SESSION_UNAVAILABLE')
        manager.fetch_dual_esp32_frame.assert_not_called()


class SchedulerReservationTests(unittest.TestCase):
    def test_training_shelf_is_skipped_and_other_shelves_still_counted(self):
        p=count_tests.CountCheckSchedulingTests().make();p.training_paused=lambda:count_tests.SID
        p._validations();p.validate.assert_not_called()
        status=p.shelf_status(count_tests.SID)
        self.assertIn('Training capture',status['scan']['message']);self.assertFalse(status['actions_ready'])
        p=count_tests.CountCheckSchedulingTests().make();p.training_paused=lambda:C2
        p._validations();p.validate.assert_called_once()
        self.assertNotIn('Training',str(p.actions_blocker()),'training on another shelf does not pause actions globally')


if __name__=='__main__':unittest.main()
