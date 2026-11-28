"""Local page requests must never fall through to camera/native processing."""
import ast
import io
from pathlib import Path
import types
import unittest
from unittest.mock import Mock
import urllib.parse


class LocalPageRoutes(unittest.TestCase):
    def test_removed_ota_replay_and_replacement_routes_stay_removed(self):
        source=(Path(__file__).parents[2]/'camera_service.py').read_text(encoding='utf8')
        for gone in ('esp32_ota','/cctv_replay','/replacement/','ReplacementService','CctvReplayManager',
                     'def push_ota_firmware_to_esp32(','def compile_and_locate_firmware('):
            self.assertNotIn(gone,source)
        src=Path(__file__).parents[1]/'src'
        self.assertFalse((src/'replacement.py').exists());self.assertFalse((src/'replay_manager.py').exists())

    def test_local_pages_and_assets_use_the_shared_source_and_reject_remote_peers(self):
        bridge=Path(__file__).parents[2]/'camera_service.py'
        node=next(n for n in ast.walk(ast.parse(bridge.read_text(encoding='utf8'))) if isinstance(n,ast.FunctionDef) and n.name=='do_GET')
        namespace=dict(urllib=types.SimpleNamespace(parse=urllib.parse),Path=Path,__file__=str(bridge))
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(bridge),'exec'),namespace)
        expected=(bridge.parent.parent/'index.html').read_bytes()
        for path in ('/local-dashboard','/local-dashboard/cctv-configuration','/local-dashboard/esp32-cams','/local-dashboard/esp32-cams/'):
            for peer in ('127.0.0.1','192.0.2.8'):
                handler=types.SimpleNamespace(path=path,client_address=(peer,42),send_response=Mock(),send_header=Mock(),end_headers=Mock(),wfile=io.BytesIO(),_send_json=Mock())
                namespace['do_GET'](handler)
                if peer=='127.0.0.1':
                    handler.send_response.assert_called_once_with(200);self.assertEqual(handler.wfile.getvalue(),expected)
                else:
                    self.assertEqual(handler._send_json.call_args.args[0],403);self.assertEqual(handler.wfile.getvalue(),b'')
        for path in ('/dashboard_pages.js','/esp32_camera_manager.js'):
            handler=types.SimpleNamespace(path=path,send_response=Mock(),send_header=Mock(),end_headers=Mock(),wfile=io.BytesIO())
            namespace['do_GET'](handler)
            self.assertEqual(handler.wfile.getvalue(),(bridge.parent.parent/path.lstrip('/')).read_bytes())


if __name__=='__main__':unittest.main()
