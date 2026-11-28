"""Wired replacement is removed; leftover maintenance records must still hold shelves (fail-closed)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.camera_slots import SLOTS
from src.local_operations import legacy_maintenance_holds

ALL = {s['shelf_id'] for s in SLOTS}


class LegacyMaintenanceHolds(unittest.TestCase):
    def check(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replacement-maintenance.json'
            if content is not None:
                path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf8')
            return legacy_maintenance_holds(path)

    def test_missing_or_empty_record_holds_nothing(self):
        self.assertEqual(self.check(None), (set(), None))
        self.assertEqual(self.check([]), (set(), None))

    def test_valid_record_holds_only_its_shelves(self):
        slot = SLOTS[2]
        held, message = self.check([dict(camera_slot=slot['label'], shelf_id=slot['shelf_id'], state='Maintenance')])
        self.assertEqual(held, {slot['shelf_id']})
        self.assertIn(slot['shelf_id'], message); self.assertIn('previous bridge version', message)

    def test_unreadable_or_invalid_record_holds_every_shelf(self):
        slot = SLOTS[0]
        for bad in ('{not json', {'camera_slot': slot['label']}, [dict(camera_slot=slot['label'], shelf_id='SHELF_R1_C9', state='Maintenance')],
                    [dict(camera_slot=slot['label'], shelf_id=slot['shelf_id'], state='Done')],
                    [dict(camera_slot=slot['label'], shelf_id=slot['shelf_id'], state='Maintenance', extra=1)], ['row'],
                    [dict(camera_slot=slot['label'], shelf_id=slot['shelf_id'], state='Maintenance')] * 2):
            held, message = self.check(bad)
            self.assertEqual(held, ALL, bad); self.assertIn('every shelf is held', message)

    def test_bridge_loads_holds_from_the_helper(self):
        source = (Path(__file__).parents[2] / 'camera_service.py').read_text(encoding='utf8')
        self.assertIn("legacy_maintenance_holds(data_dir/'replacement-maintenance.json')", source)
        self.assertIn('for sid in held:RUNTIME_INVENTORY.enter_maintenance(sid)', source)


if __name__ == '__main__':
    unittest.main()
