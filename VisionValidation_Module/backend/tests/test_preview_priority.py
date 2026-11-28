"""Diagnostic previews never make a scan wait or time out; pictures are always shown. No cameras or workers."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import test_count_checks as count_tests
from src.autonomous_edge import NativeClient, PREVIEW_INFERENCE_TTL
from src.periodic_recheck import RecheckCoordinator
from src.preview_panel import InferenceBusy, NativeObservations, PreviewPictures, diagnostic_preview, diagnostic_classify

C1,C2='SHELF_R1_C1','SHELF_R1_C2'
QUERY={k:[v] for k,v in dict(run_mode='diagnostic',shelf_id=C1,ip='192.168.0.201',ip2='192.168.0.202').items()}


def fake_client():
    """A NativeClient whose worker answers instantly; only its lock/priority logic is real."""
    client=object.__new__(NativeClient)
    lines=[]
    owner=SimpleNamespace(startup_id='run',proc=SimpleNamespace(stdin=SimpleNamespace(write=lambda text:lines.append(json.loads(text)),flush=lambda:None)),
        _reply=lambda replies,timeout:{'id':lines[-1]['id'],'startup_id':'run','result':{'candidate':{'hold':None}}},_stop_child=lambda:None)
    client.owner=owner;client.lock=threading.Lock();client.replies=object();client.versions={};client.failures=0
    client.accepted_versions=None;client.restart_count=0;client.retry_at=0;client.holder=None
    return client


class InferencePriorityTests(unittest.TestCase):
    def test_preview_never_waits_and_never_restarts_the_worker(self):
        client=fake_client();client.lock.acquire();client.holder='scan'
        started=time.monotonic()
        with self.assertRaises(InferenceBusy):client.infer({'kind':'pair'},preview=True)
        self.assertLess(time.monotonic()-started,.1);self.assertEqual(client.restart_count,0)
        client.lock.release();client.holder=None
        self.assertEqual(client.infer({'kind':'pair'},preview=True),{'candidate':{'hold':None}})
        self.assertIsNone(client.holder);self.assertFalse(client.lock.locked())

    def test_scan_outlasts_a_preview_holding_the_model_instead_of_timing_out(self):
        client=fake_client();client.lock.acquire();client.holder='preview'
        threading.Timer(2.4,lambda:(setattr(client,'holder',None),client.lock.release())).start()  # one preview pair ~2.4 s
        started=time.monotonic();result=client.infer({'kind':'pair'})
        self.assertEqual(result,{'candidate':{'hold':None}});self.assertGreater(time.monotonic()-started,2.0)
        self.assertLessEqual(PREVIEW_INFERENCE_TTL,5)

    def test_scan_behind_another_scan_keeps_the_two_second_limit(self):
        client=fake_client();client.lock.acquire();client.holder='scan'
        started=time.monotonic()
        with self.assertRaises(TimeoutError):client.infer({'kind':'pair'})
        self.assertLess(time.monotonic()-started,2.5)
        client.lock.release()


class PreviewReservationTests(unittest.TestCase):
    def test_preview_reserves_only_an_idle_shelf_and_blocks_only_its_scan(self):
        c=RecheckCoordinator()
        token=c.begin_preview(C1,5);self.assertTrue(token);self.assertEqual(c.preview_shelf(),C1)
        self.assertIsNone(c.begin(C1,'COUNT_CHECK'),'no scan captures from the same cameras');self.assertIsNotNone(c.begin(C2,'COUNT_CHECK'))
        self.assertFalse(c.begin_preview(C2,5),'one preview at a time')
        c.end_preview(token);self.assertIsNone(c.preview_shelf())
        busy=RecheckCoordinator();busy.begin(C1,'COUNT_CHECK');self.assertFalse(busy.begin_preview(C1,5))
        queued=RecheckCoordinator();queued.enqueue(C1,'CCTV','r1');self.assertFalse(queued.begin_preview(C1,5),'a queued scan has priority')
        expiring=RecheckCoordinator();expiring.begin_preview(C1,0);self.assertIsNone(expiring.preview_shelf())

    def test_capture_runs_without_the_shared_lock_and_releases_the_reservation(self):
        c=RecheckCoordinator();seen={}
        def fetch(**kwargs):
            done=[]
            thread=threading.Thread(target=lambda:done.append(c.global_lock.acquire(timeout=.5)) or c.global_lock.release());thread.start();thread.join(1)
            seen.update(lock_free=done==[True],shelf=c.preview_shelf(),budget=kwargs['budget'])
            return None,'combined','back','front'
        status,row=diagnostic_preview(QUERY,SimpleNamespace(fetch_dual_esp32_frame=fetch),c,None)
        self.assertEqual(status,200);self.assertEqual(seen,dict(lock_free=True,shelf=C1,budget=5.0))
        self.assertIsNone(c.preview_shelf());self.assertFalse(row['authorizes_write'])

    def test_a_shelf_being_scanned_or_queued_answers_busy_for_the_browser_to_retry(self):
        manager=Mock()
        c=RecheckCoordinator();c.begin(C1,'COUNT_CHECK')
        self.assertEqual(diagnostic_preview(QUERY,manager,c,None)[1]['code'],'BUSY')
        c=RecheckCoordinator();c.enqueue(C1,'CCTV','r1')
        self.assertEqual(diagnostic_preview(QUERY,manager,c,None)[1]['code'],'BUSY')
        manager.fetch_dual_esp32_frame.assert_not_called()


class ClassificationTests(unittest.TestCase):
    def native(self):
        native=SimpleNamespace(models_loaded=True,active_shelf=None,native=Mock(),panel_observations=NativeObservations())
        native.native.infer.return_value={'candidate':{'front_class':'Front_111','hold':None}};native.native.versions={'Front':{}}
        return native

    def manager(self,back='back',front='front'):
        manager=Mock();manager.fetch_dual_esp32_frame.return_value=(None,'combined',back,front);return manager

    def test_classification_waits_for_scans_and_runs_later_without_a_new_capture(self):
        c=RecheckCoordinator();native=self.native();store=PreviewPictures();manager=self.manager()
        token=c.begin(C2,'COUNT_CHECK')  # another shelf is being scanned: its inference has priority
        status,row=diagnostic_preview(QUERY,manager,c,native,store=store)
        self.assertEqual((status,row['classification'],row['image_back']),(200,'deferred','back'),'pictures shown, classification deferred')
        native.native.infer.assert_not_called()
        self.assertEqual(diagnostic_classify({'preview_id':[row['preview_id']]},c,native,store)[1]['classification'],'deferred')
        c.end(C2,token)
        status,result=diagnostic_classify({'preview_id':[row['preview_id']]},c,native,store)
        self.assertEqual((status,result['classification'],result['candidate']['front_class']),(200,'done','Front_111'))
        native.native.infer.assert_called_once();self.assertEqual(manager.fetch_dual_esp32_frame.call_count,1,'no second capture')
        self.assertEqual(diagnostic_classify({'preview_id':['missing']},c,native,store)[0],404)

    def test_busy_model_defers_instead_of_failing(self):
        native=self.native();native.native.infer.side_effect=InferenceBusy('busy')
        status,row=diagnostic_preview(QUERY,self.manager(),RecheckCoordinator(),native,store=PreviewPictures())
        self.assertEqual((status,row['classification']),(200,'deferred'))
        native=self.native();native.active_shelf=C2
        self.assertEqual(diagnostic_preview(QUERY,self.manager(),RecheckCoordinator(),native,store=PreviewPictures())[1]['classification'],'deferred')
        native.native.infer.assert_not_called()

    def test_capture_errors_name_the_camera(self):
        cases=[(None,'front','back','The Back camera (192.168.0.201) did not return a picture; the Front camera worked.'),
               ('back',None,'front','The Front camera (192.168.0.202) did not return a picture; the Back camera worked.'),
               (None,None,'both','Neither camera returned a picture (Back 192.168.0.201, Front 192.168.0.202).')]
        for back,front,which,message in cases:
            status,row=diagnostic_preview(QUERY,self.manager(back,front),RecheckCoordinator(),None)
            self.assertEqual((status,row['camera']),(502,which));self.assertTrue(row['error'].startswith(message),row['error'])

    def test_training_pictures_reach_the_panel_without_classification(self):
        c=RecheckCoordinator();native=self.native()
        token=c.request_training(C1,('192.168.0.201','192.168.0.202'))['token']
        status,row=diagnostic_preview(dict(QUERY,purpose=['training']),self.manager(),c,native,training_token=token)
        self.assertEqual(status,200);native.native.infer.assert_not_called()
        shown=native.panel_observations.get(C1)
        self.assertEqual((shown['source'],shown['candidate'],shown['image_back']),('training',None,'back'))


class SchedulerTests(unittest.TestCase):
    def test_previewed_shelf_waits_and_other_shelves_continue(self):
        p=count_tests.CountCheckSchedulingTests().make();p.preview_shelf=lambda:count_tests.SID
        p._validations();p.validate.assert_not_called()
        p=count_tests.CountCheckSchedulingTests().make();p.preview_shelf=lambda:C2
        p._validations();p.validate.assert_called_once()


if __name__=='__main__':unittest.main()
