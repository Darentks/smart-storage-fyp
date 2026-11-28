import sys,time,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parents[1]))
from src.preview_panel import diagnostic_preview,NativeObservations
class PanelTests(unittest.TestCase):
 def setUp(self):
  self.manager=Mock();self.manager.fetch_dual_esp32_frame.return_value=(None,'combined','back','front')
  self.coordinator=SimpleNamespace(global_lock=threading.RLock(),busy=Mock(return_value=False))
  self.query={k:[v] for k,v in dict(run_mode='diagnostic',shelf_id='SHELF_R1_C1',ip='192.168.0.201',ip2='192.168.0.202').items()}
 def test_diagnostic_capture_has_no_authority_or_receipt(self):
  status,row=diagnostic_preview(self.query,self.manager,self.coordinator,None)
  self.assertEqual(status,200);self.assertFalse(row['authorizes_write']);self.assertNotIn('capture',row)
  self.assertEqual(self.manager.method_calls[0].kwargs['flash'],1)
  self.assertEqual(self.manager.method_calls[0].kwargs['duty'],140)
  self.assertEqual(len(self.manager.method_calls),1)
 def test_native_or_busy_capture_rejected_before_hardware(self):
  for native in (object(),None):
   self.coordinator.busy.return_value=True
   self.assertEqual(diagnostic_preview(self.query,self.manager,self.coordinator,native)[0],409)
  self.manager.fetch_dual_esp32_frame.assert_not_called()
 def test_quick_preview_runs_beside_native_but_not_on_a_shelf_it_is_scanning(self):
  self.coordinator.busy.side_effect=lambda shelf=None:shelf=='SHELF_R1_C2'
  native=SimpleNamespace(models_loaded=True,native=Mock())
  native.native.infer.return_value={'candidate':{'front_confidence':42,'hold':'LOW_CONFIDENCE'}}
  native.native.versions={'Front':{'weights_sha256':'fixture'}}
  status,row=diagnostic_preview(self.query,self.manager,self.coordinator,native)
  self.assertEqual(status,200);self.assertFalse(row['authorizes_write']);self.assertEqual(self.manager.fetch_dual_esp32_frame.call_count,1)
  self.assertEqual(row['inference_source'],'native')
  self.assertEqual(row['candidate']['front_confidence'],42)
  native.native.infer.assert_called_once_with(dict(kind='pair',image_back='back',image_front='front',boost='off'),preview=True)
  self.assertEqual(row['classification'],'done')
  status,row=diagnostic_preview(dict(self.query,shelf_id=['SHELF_R1_C2']),self.manager,self.coordinator,native)
  self.assertEqual(status,409);self.assertIn('scanned',row['error']);self.assertEqual(self.manager.fetch_dual_esp32_frame.call_count,1,'no camera request for a busy shelf')
 def test_native_failure_never_falls_back_to_different_browser_models(self):
  native=SimpleNamespace(models_loaded=False,native=Mock())
  status,row=diagnostic_preview(self.query,self.manager,self.coordinator,native)
  self.assertEqual((status,row['classification'],row['image_back']),(200,'unavailable','back'),'pictures are shown while models load')
  native.native.infer.assert_not_called()
  native.models_loaded=True;native.native.infer.side_effect=RuntimeError('private detail')
  status,row=diagnostic_preview(self.query,self.manager,self.coordinator,native)
  self.assertEqual((status,row['classification'],row['image_front']),(200,'failed','front'),'a classification failure still returns the pictures')
  self.assertNotIn('private detail',str(row));self.assertNotIn('candidate',row);self.assertTrue(row['preview_id'])
 def test_native_scanner_and_quick_preview_share_one_bright_setting(self):
  from src import autonomous_edge,preview_panel
  self.assertEqual((preview_panel.LIVE_CAPTURE_BOOST,preview_panel.LIVE_CAPTURE_DUTY),('off',140))
  self.assertEqual((autonomous_edge.CAPTURE_BOOST,autonomous_edge.CAPTURE_DUTY),('off',140),'native scans use the same flash brightness as the quick preview')
  diagnostic_preview(self.query,self.manager,self.coordinator,None)
  kwargs=self.manager.fetch_dual_esp32_frame.call_args.kwargs
  self.assertEqual((kwargs['flash'],kwargs['duty'],kwargs['boost_mode']),(1,preview_panel.LIVE_CAPTURE_DUTY,preview_panel.LIVE_CAPTURE_BOOST))
 def test_capture_duty_override_is_clamped_and_falls_back_safely(self):
  from src.preview_panel import _capture_duty
  for value,expected in (('200',200),('999',255),('-5',0),('abc',140),('',140)):
   with patch.dict('os.environ',{'SMART_STORAGE_CAPTURE_DUTY':value}):self.assertEqual(_capture_duty(),expected,value)
  with patch.dict('os.environ',{},clear=True):self.assertEqual(_capture_duty(),140)
 def test_explicit_mode_and_complete_pair_required(self):
  self.assertEqual(diagnostic_preview({},self.manager,self.coordinator,None)[0],400)
  self.manager.fetch_dual_esp32_frame.return_value=(None,None,'back',None)
  self.assertEqual(diagnostic_preview(self.query,self.manager,self.coordinator,None)[0],502)
 def test_training_capture_runs_with_native_without_models_inference_or_receipts(self):
  native=SimpleNamespace(models_loaded=False,native=Mock(),cctv_busy=Mock(return_value=False))
  status,row=diagnostic_preview(dict(self.query,purpose=['training']),self.manager,self.coordinator,native)
  self.assertEqual(status,200);self.assertEqual(row['purpose'],'training')
  self.assertFalse(row['authorizes_write']);self.assertNotIn('capture',row);self.assertNotIn('candidate',row)
  native.native.infer.assert_not_called();self.coordinator.busy.assert_called_once_with()
 def test_training_yields_to_any_native_lease_waiter_or_cctv_check(self):
  native=SimpleNamespace(cctv_busy=Mock(return_value=False))
  for reason in ('lease','waiting','cctv'):
   self.coordinator.busy.return_value=reason=='lease'
   self.coordinator.waiting={'request':{}} if reason=='waiting' else {}
   native.cctv_busy.return_value=reason=='cctv'
   self.assertEqual(diagnostic_preview(dict(self.query,purpose=['training']),self.manager,self.coordinator,native)[0],409)
  self.manager.fetch_dual_esp32_frame.assert_not_called()
 def test_contended_global_lock_returns_busy_without_waiting_or_capturing(self):
  acquired=threading.Event();release=threading.Event()
  def owner():
   with self.coordinator.global_lock:acquired.set();release.wait(2)
  worker=threading.Thread(target=owner);worker.start();self.assertTrue(acquired.wait(1))
  try:
   started=time.monotonic();status,row=diagnostic_preview(self.query,self.manager,self.coordinator,object())
   self.assertEqual(status,409);self.assertIn('Another capture',row['error'])
   self.assertLess(time.monotonic()-started,.5);self.manager.fetch_dual_esp32_frame.assert_not_called()
  finally:release.set();worker.join(2)
 def test_failed_hardware_capture_releases_lock(self):
  lock=threading.Lock();self.coordinator.global_lock=lock
  self.manager.fetch_dual_esp32_frame.side_effect=OSError('fixture capture failure')
  self.assertEqual(diagnostic_preview(self.query,self.manager,self.coordinator,None)[0],502)
  self.assertTrue(lock.acquire(blocking=False));lock.release()
 def test_native_cache_is_bounded_copied_expiring_and_clearable(self):
  store=NativeObservations()
  for i in range(1,7):store.put(f'SHELF_R1_C{i}',{'image_back':'back'},{'hold':'LOW_CONFIDENCE'})  # C4-C6 are retired and ignored
  store.put('unknown',{},{});self.assertEqual(len(store.rows),3)
  row=store.get('SHELF_R1_C1');assert row is not None;row['candidate']['hold']='changed'
  row2=store.get('SHELF_R1_C1');assert row2 is not None
  self.assertEqual(row2['candidate']['hold'],'LOW_CONFIDENCE')
  with patch('src.preview_panel.time.time',return_value=time.time()+31):self.assertEqual(store.get('SHELF_R1_C1')['age_seconds'],31)
  with patch('src.preview_panel.time.time',return_value=time.time()+601):self.assertIsNone(store.get('SHELF_R1_C1'))
  store.clear();self.assertEqual(store.rows,{})
if __name__=='__main__':unittest.main()
