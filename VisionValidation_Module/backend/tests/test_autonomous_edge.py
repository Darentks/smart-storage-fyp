import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from src.autonomous_edge import ActionTracker,trajectory_action,polygon,AutonomousProcessor,load_service_config,CHECKS,digest

ROI={'x_min':.1,'x_max':.9,'y_min':.1,'y_max':.9}
def person(y=.3,visible=True):
    points=[dict(x=0,y=0,visibility=0) for _ in range(33)]
    points[15]=dict(x=.5,y=y,visibility=.95 if visible else 0)
    return {'landmarks':points}

class TrackingTests(unittest.TestCase):
    def test_actual_browser_action_function_matches_native_sequences(self):
        import subprocess
        scenarios=[]
        for role,rois,direction in [('GRID_6_ROI',{'s':ROI},1),('GRID_6_ROI',{'s':ROI},-1),('SIDE_PRESENCE',{'RACK_ENTRY_ZONE':ROI},0),('GRID_6_ROI',{'s':ROI,'overlap':ROI},1)]:
            frames=[dict(at=100+i*.2,persons=[person(.5+direction*(i-10)*.01)]) for i in range(24)]
            frames += [dict(at=105,persons=[]),dict(at=105.2,persons=[])]
            scenarios.append(dict(role=role,rois=rois,frames=frames))
        root=Path(__file__).parents[3]
        result=subprocess.run(['node',str(Path(__file__).with_name('browser_action_fixture.js'))],cwd=root,input=json.dumps(scenarios),text=True,capture_output=True,timeout=15,check=True)
        browser=json.loads(result.stdout)
        for scenario,expected in zip(scenarios,browser):
            tracker=ActionTracker(scenario['role'],scenario['rois']);actual=[]
            for frame in scenario['frames']:
                actual += [dict(v,at=frame['at']) for v in tracker.update(frame['persons'],frame['at'])]
            self.assertEqual(len(actual),len(expected))
            for a,b in zip(actual,expected):
                for key in ('shelf_id','expected_action','oks_confidence'):self.assertEqual(a[key],b[key])
                for key in ('at','dwell_duration_seconds'):self.assertAlmostEqual(a[key],b[key])

    def test_withdrawal_only_and_direction(self):
        tracker=ActionTracker('GRID_6_ROI',{'s':ROI})
        for i in range(14):self.assertEqual(tracker.update([person(.2+i*.02)],100+i*.1),[])
        votes=tracker.update([],101.7)
        self.assertEqual(len(votes),1);self.assertEqual(votes[0]['expected_action'],'RETURN')
        self.assertEqual(tracker.update([],102),[])
        self.assertEqual(trajectory_action([{'x':.5,'y':.8-i*.03} for i in range(8)]),'BORROW')

    def test_overlap_and_camera_gap_cannot_synthesize_withdrawal(self):
        for rois,gap in [({'a':ROI,'b':ROI},.1),({'a':ROI},2)]:
            tracker=ActionTracker('GRID_6_ROI',rois)
            for i in range(14):self.assertEqual(tracker.update([person()],100+i*.1),[])
            self.assertEqual(tracker.update([],101.3+gap),[])

    def test_side_presence_refresh_and_low_visibility(self):
        tracker=ActionTracker('SIDE_PRESENCE',{'RACK_ENTRY_ZONE':ROI})
        votes=[]
        for i in range(42):votes+=tracker.update([person()],100+i*.1)
        self.assertEqual(len(votes),2);self.assertTrue(all(v['shelf_id']=='ANY_SHELF' for v in votes))
        self.assertEqual(tracker.update([],104.5),[])
        tracker=ActionTracker('GRID_6_ROI',{'s':ROI})
        for i in range(20):self.assertEqual(tracker.update([person(visible=False)],100+i*.1),[])

    def test_bad_geometry_rejected(self):
        with self.assertRaises(ValueError):polygon({'vertices':[[0,0],[1,1],[1,0],[0,1]]})
        with self.assertRaises(ValueError):polygon(dict(ROI,coordinate_space='pixels'))

class CoordinatorTests(unittest.TestCase):
    def make(self,candidates=None):
        self.calls=[];self.capture_number=0
        def request(path,body):
            self.calls.append((path,body))
            if path=='/validation_begin':return {'token':'lease'}
            if path=='/validation_renew':return {'renewed':True}
            if path=='/cctv_trigger_claim':return {'claimed':True}
            if path.startswith('/esp32_preview'):
                self.capture_number+=1
                return dict(image='combined',image_back='back',image_front='front',capture=dict(id=str(self.capture_number),duration_ms=100,completed_at=time.time()))
            if path=='/esp32_validate':return {'status':'accepted'}
            return {}
        candidate=dict(front_class='Front_011',back_class='Back_11X',front_confidence=98,back_confidence=97,slots=[0,1,1,1,1],total=4,hold=None)
        native=Mock();native.infer.side_effect=lambda job:dict(candidate=copy.deepcopy(next(candidates) if candidates else candidate),metrics={})
        processor=AutonomousProcessor('run',native,[],[],request,Mock(),Mock(),Mock(),Mock(),Mock())
        processor.stop=Mock();processor.stop.is_set.return_value=False;processor.stop.wait.return_value=False
        processor.cameras_ready=lambda:True
        return processor

    def test_three_fresh_pairs_use_guarded_route_then_release(self):
        processor=self.make();shelf=dict(shelf_id='s',ip_address='back',secondary_ip_address='front')
        result=processor.validate(shelf,dict(trigger_id='event',expected_action='BORROW',source='CCTV'))
        payload=next(b for p,b in self.calls if p=='/esp32_validate')
        self.assertEqual(result['status'],'accepted');self.assertEqual(len(payload['dual_cam_evidence']['samples']),3)
        self.assertEqual(payload['validation_token'],'lease');self.assertEqual(payload['trigger_id'],'event')
        self.assertEqual(self.calls[-1][0],'/validation_end')

    def test_low_confidence_unknown_and_stop_never_submit(self):
        for reason in ('LOW_CONFIDENCE','UNKNOWN_B1','M2_CONFLICT'):
            processor=self.make(iter([dict(hold=reason)]*3))
            with self.assertRaises(ValueError):processor.validate(dict(shelf_id='s',ip_address='b',secondary_ip_address='f'))
            self.assertFalse(any(p=='/esp32_validate' for p,b in self.calls))
            self.assertEqual(self.calls[-1][0],'/validation_end')

    def test_shutdown_and_lost_health_prevent_commit(self):
        processor=self.make();processor.cameras_ready=lambda:False
        with self.assertRaises(ValueError):processor.validate(dict(shelf_id='s',ip_address='b',secondary_ip_address='f'))
        self.assertFalse(any(p=='/esp32_validate' for p,b in self.calls))
        processor=self.make();processor.stop.is_set.side_effect=[False,False,False,True]
        with self.assertRaises(ValueError):processor.validate(dict(shelf_id='s',ip_address='b',secondary_ip_address='f'))
        self.assertFalse(any(p=='/esp32_validate' for p,b in self.calls))

    def test_browser_and_forged_header_cannot_write_in_service_mode(self):
        source=Path(__file__).parents[2]/'camera_service.py';tree=ast.parse(source.read_text(encoding='utf8'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='do_POST')
        import urllib.parse
        cap=object();ns=dict(PROCESSOR_AUTHORITY='service',_PROCESSOR_CAPABILITY=cap,urllib=types.SimpleNamespace(parse=urllib.parse))
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),ns)
        for path in ('/esp32_validate','/esp32_trigger','/validation_begin','/readiness_recovery','/cctv_alignment_config','/run_pipeline'):
            handler=types.SimpleNamespace(path=path,headers={'X-Processor':'service'},_send_json=Mock(),_read_json_body=Mock())
            ns['do_POST'](handler);self.assertEqual(handler._send_json.call_args.args[0],403)
            handler._read_json_body.assert_not_called()

class GateTests(unittest.TestCase):
    def test_gate_requires_reports_and_rejects_changed_code_or_camera(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);code=root/'code';code.write_text('code');manifest=root/'models';manifest.write_text('models')
            cameras=[];env={}
            for i in range(1,4):
                cid=f'CCTV_0{i}';role='SIDE_PRESENCE' if i==3 else 'GRID_6_ROI';source=f'rtsp://192.168.0.{i}/stream'
                env[cid]=source;cameras.append(dict(cctv_id=cid,camera_role=role,binding={'source':source.replace('/stream',':554/stream'),'camera_role':role},
                    rois={'RACK_ENTRY_ZONE':ROI} if i==3 else {f'SHELF_R1_C{j}':dict(x_min=(j-1)/6,x_max=j/6,y_min=0,y_max=1) for j in range(1,7)}))
            calibration=root/'calibration';calibration.write_text(json.dumps({'version':1,'cameras':cameras}))
            config=root/'config';config.write_text(json.dumps(dict(version=1,calibration_file='calibration',source_environment={k:k for k in env})))
            report=root/'parity';report.write_text(json.dumps(dict(pass_=True)))
            # Synthetic gate fixtures exercise binding logic only; never use as field acceptance.
            report.write_text(json.dumps({'pass':True,'models':{'Front':{},'Back':{},'pose':{}},'cases':[{'kind':'pair','pass':True},{'kind':'pose','pass':True}]}))
            record=dict(version=1,inventory_contract='runtime-v1',operator_accepted=True,operator='test fixture',accepted_at=time.time(),code_sha256=digest(code),manifest_sha256=digest(manifest),
                calibration_sha256=digest(calibration),checks={k:True for k in CHECKS},reports=[dict(path='parity',sha256=digest(report))])
            acceptance=root/'acceptance';acceptance.write_text(json.dumps(record))
            with patch.dict(os.environ,dict(env,SMART_STORAGE_RELEASE_PHASE='5')):
                self.assertEqual(len(load_service_config(config,manifest,acceptance,[code])[0]),3)
                self.assertEqual(len(load_service_config(config,manifest,acceptance,[code],calibration)[0]),3)
                with self.assertRaisesRegex(ValueError,'local ROI store'):load_service_config(config,manifest,acceptance,[code],root/'different-rois')
                record['inventory_contract']='persistent-v1';acceptance.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError,'Runtime contract'):load_service_config(config,manifest,acceptance,[code])
                record['inventory_contract']='runtime-v1'
                for check in CHECKS:
                    record['checks'][check]=False;acceptance.write_text(json.dumps(record))
                    with self.assertRaisesRegex(ValueError,'SERVICE_CUTOVER_BLOCKED'):load_service_config(config,manifest,acceptance,[code])
                    record['checks'][check]=True
                acceptance.write_text(json.dumps(record))
                code.write_text('changed')
                with self.assertRaises(ValueError):load_service_config(config,manifest,acceptance,[code])
                code.write_text('code');record['checks']['camera_outage']=False;acceptance.write_text(json.dumps(record))
                with self.assertRaises(ValueError):load_service_config(config,manifest,acceptance,[code])

if __name__=='__main__':unittest.main()
