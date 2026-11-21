"""Failure classification and bounded, metadata-only diagnostics."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]))
from src.validation_diagnostics import failure_code
from src.shelf_readiness import ShelfReadiness

class DiagnosticsTests(unittest.TestCase):
    def test_actionable_codes(self):
        for text,code in [('Low confidence; quantity retained','LOW_CONFIDENCE'),('M2 conflict','M2_CONFLICT'),
          ('B1 unknown (Back_X01)','UNKNOWN_B1'),('Occupancy changed between captures','OCCUPANCY_CHANGED'),
          ('Missing, stale or mismatched camera frames','STALE_CAPTURE'),('Fresh CCTV consensus bound to this lease is required','MISSING_CCTV_CONSENSUS'),
          ('Atomic inventory commit not confirmed','DATABASE_FAILURE'),('capture/inference time limit exceeded','VALIDATION_TIMEOUT')]:
            self.assertEqual(failure_code(text),code)

    def test_bounded_metadata_excludes_images_and_secrets(self):
        state=ShelfReadiness()
        for i in range(250):
            state.record('s','lease','trigger',attempt=i,stage='capture',image='forbidden',api_key='forbidden')
        self.assertEqual(len(state.diagnostics),200)
        first=state.diagnostics[0]
        self.assertEqual(first['attempt'],50)
        self.assertEqual(first['run_id'],state.run_id)
        self.assertNotIn('image',first);self.assertNotIn('api_key',first)

    def test_hold_code_and_reason_survive_until_resolution(self):
        state=ShelfReadiness();state.set('s','Held','B1 unknown; observability limit')
        self.assertEqual(state.get('s')['code'],'UNKNOWN_B1')
        self.assertFalse(state.get('s')['retryable'])
        self.assertFalse(state.ready('s'))

if __name__=='__main__':unittest.main()
