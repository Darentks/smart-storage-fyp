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
  self.assertEqual(self.manager.method_calls[0].kwargs['flash'],0)
  self.assertEqual(len(self.manager.method_calls),1)
 def test_native_or_busy_capture_rejected_before_hardware(self):
  for native in (object(),None):
   self.coordinator.busy.return_value=True
   self.assertEqual(diagnostic_preview(self.query,self.manager,self.coordinator,native)[0],409)
  self.manager.fetch_dual_esp32_frame.assert_not_called()
 def test_explicit_mode_and_complete_pair_required(self):
  self.assertEqual(diagnostic_preview({},self.manager,self.coordinator,None)[0],400)
  self.manager.fetch_dual_esp32_frame.return_value=(None,None,'back',None)
  self.assertEqual(diagnostic_preview(self.query,self.manager,self.coordinator,None)[0],502)
 def test_native_cache_is_bounded_copied_expiring_and_clearable(self):
  store=NativeObservations()
  for i in range(1,7):store.put(f'SHELF_R1_C{i}',{'image_back':'back'},{'hold':'LOW_CONFIDENCE'})
  store.put('unknown',{},{});self.assertEqual(len(store.rows),6)
  row=store.get('SHELF_R1_C1');row['candidate']['hold']='changed'
  self.assertEqual(store.get('SHELF_R1_C1')['candidate']['hold'],'LOW_CONFIDENCE')
  with patch('src.preview_panel.time.time',return_value=time.time()+31):self.assertIsNone(store.get('SHELF_R1_C1'))
  store.clear();self.assertEqual(store.rows,{})
if __name__=='__main__':unittest.main()
