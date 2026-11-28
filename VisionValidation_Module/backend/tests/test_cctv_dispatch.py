"""Exercise the actual consensus and trigger handler without camera/network imports."""
import ast
import math
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock
import uuid
import runpy
SHELF_SET=runpy.run_path(str(Path(__file__).resolve().parents[1]/'src'/'shelves.py'))['SHELF_SET']  # for AST-isolated bridge functions

SOURCE = Path(__file__).resolve().parents[2] / 'camera_service.py'

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.ns = dict(SHELF_SET=SHELF_SET,READINESS=types.SimpleNamespace(ready=lambda shelf:True),math=math, time=types.SimpleNamespace(time=lambda:self.now), uuid=uuid,
            CCTV_CAMERA_CONFIG={f'CCTV_0{i}':dict(is_active=True,camera_role='SIDE_PRESENCE' if i==3 else 'GRID_6_ROI') for i in (1,2,3)},
            CCTV_CONSENSUS_LOCK=threading.RLock(), CCTV_ALIGN_STATE={}, CCTV_SEEN_TRIGGER_IDS={},
            LAST_SHELF_TRIGGER_TS={}, PENDING_CCTV_TRIGGERS=[], CCTV_LAST_CONSENSUS={}, CONSENSUS_WINDOW_SEC=4.,CONSENSUS_COOLDOWN_SEC=4.,
            spatial_mapper=types.SimpleNamespace(rack_id='RACK_01'),esp32_ref_manager=Mock(),mqtt_bridge=Mock(is_connected=True))
        tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
        record=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='record_cctv_consensus')
        route=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='path' and any(isinstance(x,ast.Constant) and x.value=='/esp32_trigger' for x in n.test.comparators))
        handler=ast.FunctionDef(name='handler',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=x) for x in ('self','path','body')],kwonlyargs=[],kw_defaults=[],defaults=[]),body=[route],decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[record,handler],type_ignores=[])),str(SOURCE),'exec'),self.ns)
        self.response=Mock()

    def vote(self, camera, detected_at=None):
        return self.ns['record_cctv_consensus'](shelf_id='ANY_SHELF' if camera==3 else 'SHELF_R1_C2',camera_id=f'CCTV_0{camera}',action='' if camera==3 else 'RETURN',confidence=.9,detected_at=detected_at)

    def test_side_vote_last_dispatches_agreed_action_exactly_once(self):
        self.vote(1);self.vote(2)
        body=dict(camera_id='CCTV_03',camera_role='SIDE_PRESENCE',shelf_id='ANY_SHELF',source='CCTV_BROWSER_DWELL',detected_at=self.now)
        self.ns['handler'](self.response,'/esp32_trigger',body)
        mqtt=self.ns['mqtt_bridge']
        self.assertEqual(mqtt.publish_trigger.call_args.kwargs['expected_action'],'RECHECK')
        self.assertEqual(mqtt.publish_trigger.call_args.kwargs['shelf_id'],'SHELF_R1_C2')
        self.ns['handler'](self.response,'/esp32_trigger',body)
        self.assertEqual(mqtt.publish_trigger.call_count,1)

    def test_invalid_consensus_never_dispatches(self):
        valid=dict(consensus_met=True,shelf_id='SHELF_R1_C2',expected_action='RETURN',confidence=.9,trigger_id='t')
        for key,value in [('shelf_id','ANY_SHELF'),('expected_action',''),('confidence',float('nan')),('trigger_id',None)]:
            self.ns['record_cctv_consensus']=lambda **kwargs:dict(valid,**{key:value})
            self.ns['handler'](self.response,'/esp32_trigger',dict(source='CCTV_BROWSER_DWELL'))
            self.assertEqual(self.response._send_json.call_args.args[0],500)
        self.ns['mqtt_bridge'].publish_trigger.assert_not_called()
        self.ns['esp32_ref_manager'].freeze_baseline.assert_not_called()

    def test_stale_nonfinite_future_detections_rejected(self):
        for timestamp in [95.9,float('nan'),float('inf'),102.]:
            with self.assertRaises(ValueError):self.vote(1,timestamp)
        self.assertEqual(self.ns['CCTV_ALIGN_STATE'],{})

    def test_detection_time_not_arrival_time_controls_window(self):
        self.vote(1,96.1)
        self.now=100.2
        self.vote(2,100.2)
        self.assertFalse(self.vote(3,100.2)['consensus_met'])
        self.assertTrue(self.vote(1,100.2)['consensus_met'])

    def test_live_consensus_popup_evidence_without_ready_baseline_never_dispatches(self):
        self.ns['READINESS'].ready=lambda shelf:False
        for camera in (1,2,3):
            body=dict(camera_id=f'CCTV_0{camera}',camera_role='SIDE_PRESENCE' if camera==3 else 'GRID_6_ROI',
                shelf_id='ANY_SHELF' if camera==3 else 'SHELF_R1_C2',source='CCTV',expected_action='BORROW',
                dwell_duration_seconds=1.3,oks_confidence=.9,detected_at=self.now)
            self.ns['handler'](self.response,'/esp32_trigger',body)
        status,payload=self.response._send_json.call_args.args
        self.assertEqual(status,200);self.assertTrue(payload['consensus_met']);self.assertFalse(payload['inventory_eligible'])
        self.assertEqual(payload['confirmed_cameras'],['CCTV_01','CCTV_02','CCTV_03'])
        self.assertEqual(self.ns['PENDING_CCTV_TRIGGERS'],[])
        self.ns['mqtt_bridge'].publish_trigger.assert_not_called()
        self.ns['esp32_ref_manager'].freeze_baseline.assert_not_called()
        self.assertEqual(self.ns['CCTV_LAST_CONSENSUS']['SHELF_R1_C2']['trigger_id'],payload['trigger_id'])
        self.assertEqual(self.ns['CCTV_ALIGN_STATE'],{})

    def test_legacy_votes_still_work_and_four_second_cooldown_remains(self):
        self.vote(1);self.vote(2);self.assertTrue(self.vote(3)['consensus_met'])
        self.ns['PENDING_CCTV_TRIGGERS'].clear()  # No outstanding event: normal cooldown still applies.
        self.now+=3.9
        self.vote(1);self.vote(2);self.assertEqual(self.vote(3)['status'],'debounced')
        self.now+=.2
        self.assertTrue(self.vote(1)['consensus_met'])
