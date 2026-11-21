"""Exercise the real preview handler without starting cameras or the bridge."""
import ast
from pathlib import Path
import threading
import time
import types
import unittest
from unittest.mock import Mock
import urllib.parse
import uuid


class RawPreviewTests(unittest.TestCase):
    def test_tm_raw_bypasses_boost_preserving_capture_settings(self):
        service = Path(__file__).parents[2] / 'camera_service.py'
        tree = ast.parse(service.read_text(encoding='utf-8'))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'do_GET')
        manager = types.SimpleNamespace(fetch_dual_esp32_frame=Mock(return_value=(None, 'combined', 'back', 'front')))
        captures = {}
        coordinator = types.SimpleNamespace(global_lock=threading.RLock(), leases={'shelf': ('active-event',)}, busy=lambda shelf: True)
        shadow=Mock()
        ns = dict(EDGE_SHADOW=shadow, RECHECKS=coordinator, urllib=types.SimpleNamespace(parse=urllib.parse), time=time, uuid=uuid,
                  esp32_ref_manager=manager, ACTIVE_ESP32_IPS={}, DUAL_CAM_CAPTURES=captures,
                  DUAL_CAM_CAPTURE_LOCK=threading.Lock())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<real handler>', 'exec'), ns)
        for raw, expected in [(True, 'off'), (False, 'high')]:
            handler = types.SimpleNamespace(
                path='/esp32_preview?ip=back&ip2=front&shelf_id=shelf&boost=high&flash=1&duty=135' + ('&tm_raw=1' if raw else ''),
                _send_json=Mock())
            ns['do_GET'](handler)
            kwargs = manager.fetch_dual_esp32_frame.call_args.kwargs
            self.assertEqual(kwargs['boost_mode'], expected)
            self.assertEqual((kwargs['flash'], kwargs['duty']), (1, 135))
            code, data = handler._send_json.call_args.args
            self.assertEqual(code, 200)
            self.assertEqual(data['tm_raw'], raw)
            self.assertEqual((data['flash'], data['duty']), (1, 135))
            self.assertTrue(captures[data['capture']['id']]['complete'])
            self.assertEqual(captures[data['capture']['id']]['validation_token'], 'active-event')
        shadow.submit.assert_called_once()
        self.assertEqual(shadow.submit.call_args.args, ('pair',))
        self.assertEqual(shadow.submit.call_args.kwargs['boost'], 'high')


if __name__ == '__main__':
    unittest.main()
