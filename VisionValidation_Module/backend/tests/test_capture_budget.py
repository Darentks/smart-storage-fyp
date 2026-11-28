"""Exercise the actual paired capture method with a deterministic HTTP clock."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
import types
import unittest
from unittest.mock import Mock
import urllib.request

from sys import path
path.insert(0, str(Path(__file__).parents[1]))
from src.dual_cam_inventory import (HTTP_CAPTURE_BUDGET_SECONDS, HTTP_FIRST_ATTEMPT_SECONDS,
    HTTP_RETRY_DELAY_SECONDS, MAX_CAPTURE_DURATION_MS, DIAGNOSTIC_CAPTURE_BUDGET_SECONDS, DIAGNOSTIC_FIRST_ATTEMPT_SECONDS)


class CaptureBudgetTests(unittest.TestCase):
    def capture(self, elapsed, fail_first, **options):
        tree=ast.parse((Path(__file__).parents[1]/'src/reference_validator.py').read_text(encoding='utf-8'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='fetch_dual_esp32_frame')
        clock=[0.0];timeouts=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            consumed=False
            def read(self, size):
                if self.consumed:return b''
                self.consumed=True;clock[0]+=elapsed;return b'image'
        def open_request(request,timeout):
            timeouts.append(timeout)
            if fail_first and len(timeouts)==1:
                clock[0]+=timeout
                raise TimeoutError()
            return Response()
        namespace=dict(ThreadPoolExecutor=ThreadPoolExecutor,HTTP_CAPTURE_BUDGET_SECONDS=HTTP_CAPTURE_BUDGET_SECONDS,
            HTTP_FIRST_ATTEMPT_SECONDS=HTTP_FIRST_ATTEMPT_SECONDS,HTTP_RETRY_DELAY_SECONDS=HTTP_RETRY_DELAY_SECONDS,
            time=types.SimpleNamespace(monotonic=lambda:clock[0],time=time.time,sleep=lambda n:clock.__setitem__(0,clock[0]+n)),
            urllib=types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request,urlopen=open_request)),
            np=types.SimpleNamespace(asarray=lambda *a,**kw:None,uint8=None),cv2=types.SimpleNamespace(imdecode=lambda *a:object(),IMREAD_COLOR=1),
            _OPENCV_AVAILABLE=True,enhance_low_light_frame=lambda frame,**kw:frame,encode_bgr_to_data_url=lambda frame:'image',
            synthesize_compartment_frame=lambda *a,**kw:None)
        module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),method],type_ignores=[])
        exec(compile(ast.fix_missing_locations(module),'capture','exec'),namespace)
        result=namespace['fetch_dual_esp32_frame'](types.SimpleNamespace(vision_boost_mode='off'),'camera',flash=0,**options)
        return result,timeouts

    def test_retry_uses_remaining_budget(self):
        result,timeouts=self.capture(.2,True)
        self.assertEqual(result[2],'image')
        self.assertAlmostEqual(timeouts[0],.8)
        self.assertAlmostEqual(timeouts[1],.9)
        self.assertLess(HTTP_CAPTURE_BUDGET_SECONDS*1000,MAX_CAPTURE_DURATION_MS)

    def test_late_response_cannot_become_capture_evidence(self):
        result,timeouts=self.capture(2.1,False)
        self.assertIsNone(result[2]);self.assertIsNone(result[3])
        self.assertEqual(len(timeouts),1)

    def test_fast_first_capture_needs_no_retry(self):
        result,timeouts=self.capture(.1,False)
        self.assertEqual(result[2],'image');self.assertEqual(len(timeouts),1)

    def test_inventory_budget_is_unchanged(self):
        self.assertEqual((HTTP_CAPTURE_BUDGET_SECONDS,HTTP_FIRST_ATTEMPT_SECONDS,HTTP_RETRY_DELAY_SECONDS),(1.8,0.8,0.1))
        result,timeouts=self.capture(2.1,False,timeout=9.0)
        self.assertIsNone(result[2],'a longer timeout argument alone never extends the inventory budget')

    def test_explicit_diagnostic_budget_lets_slow_cameras_finish(self):
        options=dict(budget=DIAGNOSTIC_CAPTURE_BUDGET_SECONDS,first_attempt=DIAGNOSTIC_FIRST_ATTEMPT_SECONDS)
        result,timeouts=self.capture(2.1,False,**options)
        self.assertEqual(result[2],'image');self.assertAlmostEqual(timeouts[0],2.5)
        result,timeouts=self.capture(1.0,True,**options)
        self.assertEqual(result[2],'image');self.assertAlmostEqual(timeouts[1],5.0-2.5-HTTP_RETRY_DELAY_SECONDS)
        result,timeouts=self.capture(.1,False,budget=60)
        self.assertAlmostEqual(timeouts[0],5.0,msg='budget is capped at 10 s, first attempt defaults to half')

    def test_training_and_preview_captures_request_the_longer_budget(self):
        from src.preview_panel import diagnostic_preview
        import threading
        manager=Mock();manager.fetch_dual_esp32_frame.return_value=(None,'combined','back','front')
        coordinator=types.SimpleNamespace(global_lock=threading.RLock(),busy=Mock(return_value=False),waiting={})
        query={k:[v] for k,v in dict(run_mode='diagnostic',shelf_id='SHELF_R1_C1',ip='192.168.0.201',ip2='192.168.0.202').items()}
        diagnostic_preview(dict(query,purpose=['training']),manager,coordinator,types.SimpleNamespace(cctv_busy=lambda:False))
        self.assertEqual(manager.fetch_dual_esp32_frame.call_args.kwargs['budget'],DIAGNOSTIC_CAPTURE_BUDGET_SECONDS)
        diagnostic_preview(query,manager,coordinator,None)
        self.assertEqual(manager.fetch_dual_esp32_frame.call_args.kwargs['budget'],DIAGNOSTIC_CAPTURE_BUDGET_SECONDS)
        # Inventory captures (the scanner's /esp32_preview route) never pass a budget.
        service=(Path(__file__).parents[2]/'camera_service.py').read_text(encoding='utf-8')
        self.assertNotIn('budget=',service[service.index("if path == \"/esp32_preview\""):][:3000])
