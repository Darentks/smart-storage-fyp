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
from src.autonomous_edge import ActionTracker,polygon,AutonomousProcessor,NativeClient,load_service_config,load_cameras,CHECKS,digest,ScanFailure,MAX_SCAN_ATTEMPTS,CAPTURE_BOOST,CAPTURE_DUTY

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
        self.assertEqual(len(votes),1);self.assertEqual(votes[0]['expected_action'],'RECHECK')
        self.assertEqual(tracker.update([],102),[])
        self.assertTrue(all(vote['expected_action']=='RECHECK' for vote in votes))

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

    def test_cctv_defaults_off_without_preventing_count_scan(self):
        processor=self.make()
        self.assertFalse(processor.cctv_enabled)
        processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'))
        self.assertTrue(any(path=='/esp32_validate' for path,_ in self.calls))

    def test_start_can_switch_cctv_on_through_the_normal_control_boundary(self):
        with patch('src.autonomous_edge.threading.Thread'):
            processor=self.make();processor.runtime=Mock()
            processor.start()
            self.assertFalse(processor.cctv_enabled,'default start keeps CCTV paused')
            processor.invalidate.assert_not_called()
            auto=self.make();auto.runtime=Mock();auto.pose_native=Mock()
            auto.start(cctv=True)
        self.assertTrue(auto.cctv_enabled)
        auto.invalidate.assert_called_once_with('CCTV started',preserve_counts=True,disable_actions=True)
        self.assertEqual(auto.runtime.set_cctv_enabled.call_args_list[-1].args,(True,))

    def test_bridge_starts_cctv_automatically_unless_opted_out(self):
        source=(Path(__file__).parents[2]/'camera_service.py').read_text(encoding='utf8')
        self.assertIn("os.getenv('SMART_STORAGE_CCTV_AUTOSTART','1')",source)
        self.assertIn('AUTONOMOUS.start(cctv=cctv_autostart)',source)

    def test_stop_preserves_counts_and_does_not_close_box_worker(self):
        processor=self.make();processor.pose_native=Mock();processor.cctv_enabled=True
        processor.runtime=Mock()
        processor.set_cctv_enabled(False)
        self.assertFalse(processor.cctv_enabled)
        processor.native.close.assert_not_called();processor.pose_native.close.assert_called_once()
        processor.invalidate.assert_called_once_with('CCTV stopped',preserve_counts=True,disable_actions=True)
        processor.set_cctv_enabled(False)
        self.assertEqual(processor.invalidate.call_count,1,'stop is idempotent')
        processor.set_cctv_enabled(True)
        processor.invalidate.assert_called_with('CCTV started',preserve_counts=True,disable_actions=True)

    def test_two_matching_pairs_use_guarded_route_then_release(self):
        processor=self.make();shelf=dict(shelf_id='s',ip_address='back',secondary_ip_address='front')
        result=processor.validate(shelf,dict(trigger_id='event',expected_action='BORROW',source='CCTV'))
        payload=next(b for p,b in self.calls if p=='/esp32_validate')
        self.assertEqual(result['status'],'accepted');self.assertEqual(len(payload['dual_cam_evidence']['samples']),2)
        self.assertEqual(sum(p.startswith('/esp32_preview') for p,_ in self.calls),2,'stops as soon as two consecutive pairs agree')
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
        with self.assertRaises(ValueError):processor.validate(dict(shelf_id='s',ip_address='b',secondary_ip_address='f'),dict(trigger_id='e',expected_action='BORROW',source='CCTV'))
        self.assertFalse(any(p=='/esp32_validate' for p,b in self.calls),'actions need all three live CCTV feeds')
        processor=self.make();processor.stop.is_set.side_effect=[False,False,True]
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

    def previews(self):
        import urllib.parse
        return [dict(urllib.parse.parse_qsl(p.split('?',1)[1])) for p,_ in self.calls if p.startswith('/esp32_preview')]

    BASE=dict(front_class='Front_011',back_class='Back_11X',front_confidence=98,back_confidence=97,slots=[0,1,1,1,1],total=4,hold=None)

    def test_mismatch_then_two_matches_uses_a_third_pair_and_submits_the_last_two(self):
        other=dict(self.BASE,slots=[0,1,0,1,1],total=3)
        processor=self.make(iter([other,self.BASE,self.BASE]));shelf=dict(shelf_id='s',ip_address='back',secondary_ip_address='front')
        processor.validate(shelf)
        payload=next(b for p,b in self.calls if p=='/esp32_validate')
        self.assertEqual(len(self.previews()),3);self.assertEqual([s['capture']['id'] for s in payload['dual_cam_evidence']['samples']],['2','3'])
        self.assertEqual(payload['source'],'STARTUP');self.assertEqual(payload['boost'],CAPTURE_BOOST)

    def test_no_pair_run_of_two_fails_after_three_pairs_without_lighting_sweep(self):
        a,b,c=(dict(self.BASE,slots=s) for s in ([0,1,1,1,1],[0,1,0,1,1],[1,1,0,1,1]))
        processor=self.make(iter([a,b,c]))
        with self.assertRaises(ScanFailure) as raised:processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'))
        self.assertEqual(raised.exception.code,'OCCUPANCY_CHANGED');self.assertFalse(any(p=='/esp32_validate' for p,_ in self.calls))
        queries=self.previews();self.assertEqual(len(queries),3)
        self.assertTrue(all(q['boost']==CAPTURE_BOOST and q['duty']==str(CAPTURE_DUTY) for q in queries),'one fixed capture setting, no automatic sweep')
        self.assertEqual(self.calls[-1][0],'/validation_end')

    def test_a_bad_pair_breaks_the_consecutive_run(self):
        low=dict(hold='LOW_CONFIDENCE')
        processor=self.make(iter([self.BASE,low,self.BASE]))
        with self.assertRaises(ScanFailure) as raised:processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'))
        self.assertEqual(raised.exception.code,'LOW_CONFIDENCE');self.assertFalse(any(p=='/esp32_validate' for p,_ in self.calls))
        processor=self.make(iter([low,self.BASE,self.BASE]))
        processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'))
        self.assertTrue(any(p=='/esp32_validate' for p,_ in self.calls),'a transient bad pair is absorbed by the three-pair budget')

    def test_offline_camera_is_named_and_never_counted_as_empty(self):
        for missing,code in (('image_front','FRONT_CAMERA_OFFLINE'),('image_back','BACK_CAMERA_OFFLINE')):
            processor=self.make();base=processor.request
            def request(path,body,base=base,missing=missing):
                reply=base(path,body)
                return dict(reply,**{missing:None}) if path.startswith('/esp32_preview') else reply
            processor.request=request
            with self.assertRaises(ScanFailure) as raised:processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'))
            self.assertEqual(raised.exception.code,code);processor.native.infer.assert_not_called()
            self.assertFalse(any(p=='/esp32_validate' for p,_ in self.calls))

    def test_unassigned_camera_fails_before_any_lease_or_capture(self):
        processor=self.make()
        with self.assertRaises(ScanFailure) as raised:processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address=None))
        self.assertEqual(raised.exception.code,'FRONT_CAMERA_NOT_CONFIGURED');self.assertEqual(self.calls,[])

    def test_initial_count_needs_no_cctv_but_actions_do(self):
        processor=self.make();processor.cameras_ready=lambda:False;processor.runtime=Mock();processor.runtime.snapshot.return_value={'version':1}
        processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'))
        processor.runtime.environment_ready.assert_called_with(require_cameras=False)
        self.assertTrue(any(p=='/esp32_validate' for p,_ in self.calls))
        processor.runtime.environment_ready.reset_mock()
        with self.assertRaises(ValueError):processor.validate(dict(shelf_id='s',ip_address='back',secondary_ip_address='front'),dict(trigger_id='e',expected_action='BORROW',source='CCTV'))
        processor.runtime.environment_ready.assert_called_with(require_cameras=True)

    def test_initial_scan_publishes_the_waiting_camera_reason_before_capturing(self):
        processor=self.make();processor.runtime=Mock()
        processor.runtime.snapshot.return_value={'version':1}
        processor.runtime.environment_ready.side_effect=[ValueError('Quiet interaction window not satisfied'),True]
        message='Waiting for the rack to clear: CCTV_02 detects someone in a shelf region'
        processor.runtime.environment_wait_reason.return_value=message
        observed=[]
        def wait(delay):
            if delay==.2:
                observed.append(processor.shelf_status('SHELF_R1_C1')['scan'])
                self.assertFalse(any(path.startswith('/esp32_preview') for path,_ in self.calls))
            return False
        processor.stop.wait.side_effect=wait
        processor.validate(dict(shelf_id='SHELF_R1_C1',ip_address='back',secondary_ip_address='front'))
        self.assertEqual(observed[0]['state'],'Waiting');self.assertEqual(observed[0]['message'],message)
        self.assertTrue(any(path=='/esp32_validate' for path,_ in self.calls))

def calibration_fixture(folder):
    root=Path(folder);code=root/'code';code.write_text('code');manifest=root/'models';manifest.write_text('models')
    cameras=[];env={}
    for i in range(1,4):
        cid=f'CCTV_0{i}';role='SIDE_PRESENCE' if i==3 else 'GRID_6_ROI';source=f'rtsp://192.168.0.{i}/stream'
        env[cid]=source;cameras.append(dict(cctv_id=cid,camera_role=role,binding={'source':source.replace('/stream',':554/stream'),'camera_role':role},
            rois={'RACK_ENTRY_ZONE':ROI} if i==3 else {f'SHELF_R1_C{j}':dict(x_min=(j-1)/3,x_max=j/3,y_min=0,y_max=1) for j in range(1,4)}))
    calibration=root/'calibration';calibration.write_text(json.dumps({'version':1,'cameras':cameras}))
    config=root/'config';config.write_text(json.dumps(dict(version=1,calibration_file='calibration',source_environment={k:k for k in env})))
    return root,code,manifest,env,calibration,config


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

    def test_normal_startup_needs_no_acceptance_but_keeps_calibration_and_source_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root,code,manifest,env,calibration,config=calibration_fixture(folder)
            bad=root/'acceptance';bad.write_text(json.dumps(dict(version=1,inventory_contract='runtime-v1',operator_accepted=False)))
            with patch.dict(os.environ,env):
                loaded,reports=load_service_config(config,manifest,None,[code],calibration)
                self.assertEqual([c['id'] for c in loaded],['CCTV_01','CCTV_02','CCTV_03']);self.assertEqual(reports,[])
                # A supplied record is still fully verified; it only ever upgrades the label.
                with self.assertRaisesRegex(ValueError,'SERVICE_CUTOVER_BLOCKED'):load_service_config(config,manifest,bad,[code],calibration)
                with self.assertRaisesRegex(ValueError,'local ROI store'):load_service_config(config,manifest,None,[code],root/'different-rois')
                os.environ['CCTV_02']='rtsp://192.168.0.99/stream'
                with self.assertRaisesRegex(ValueError,'CCTV_02: Camera source/role binding changed'):load_service_config(config,manifest,None,[code],calibration)

    def test_load_cameras_names_the_missing_calibration_or_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root,code,manifest,env,calibration,config=calibration_fixture(folder)
            with self.assertRaisesRegex(ValueError,'No camera calibration is installed'):load_cameras(root/'absent.json',env.get)
            self.assertEqual(len(load_cameras(calibration,env.get,calibration)),3)
            with self.assertRaisesRegex(ValueError,'CCTV_03: no saved camera source'):load_cameras(calibration,lambda cid:None if cid=='CCTV_03' else env[cid])


class SchedulerTests(unittest.TestCase):
    """Count readiness is independent of CCTV/MQTT; actions need all of them. Retries are bounded."""
    C1=dict(shelf_id='SHELF_R1_C1',ip_address='back',secondary_ip_address='front')

    def make(self,statuses,mqtt=True,cameras_live=True,shelves=None,pending=None):
        p=AutonomousProcessor('run',Mock(),[],[],Mock(),Mock(),Mock(),Mock(),Mock(),Mock())
        p.models_loaded=True;p.cctv_enabled=True;p.pose_loaded=True
        p.cameras=[dict(id='CCTV_01',role='GRID_6_ROI',source='s',tracker=Mock())]
        p.health={'CCTV_01':{'state':'Online' if cameras_live else 'Offline','at':time.time()}}
        p.shelves=Mock(return_value=shelves or [self.C1]);p.pending=Mock(return_value=pending or [])
        p.request=Mock(return_value={'mqtt_connected':mqtt,'shelves':statuses})
        p.validate=Mock()
        p.stop=Mock();p.stop.is_set.side_effect=[False,True];p.stop.wait.return_value=False
        return p

    def test_first_missing_cctv_does_not_cancel_count_lease(self):
        for previous,expected in ((None,0),('Offline',0),('Online',0)):
            with self.subTest(previous=previous):
                p=self.make({})
                p.health={} if previous is None else {'CCTV_01':{'state':previous,'at':time.time()}}
                p.runtime=Mock();p.runtime.camera.return_value=False;p.capture=Mock(return_value={'status':'error'})
                p.stop.is_set.side_effect=[False,False,True]
                p._poses()
                self.assertEqual(p.invalidate.call_count,expected)
                p.runtime.camera.assert_called_once()
                self.assertEqual(p.health['CCTV_01']['state'],'Offline')

    def test_late_cctv_frame_after_stop_cannot_restart_pose_or_vote(self):
        p=self.make({});p.pose_native=Mock()
        def late_frame(_):
            p.cctv_enabled=False;p.cctv_generation+=1
            return {'status':'success','seq':1,'image':'late'}
        p.capture=late_frame;p.stop.is_set.side_effect=[False,False,True]
        p._poses();p.pose_native.infer.assert_not_called();p.request.assert_not_called()

    def test_every_failed_capture_is_logged_with_safe_camera_reason_and_duration(self):
        p=self.make({});p.runtime=Mock();p.runtime.camera.return_value=False
        cam=p.cameras[0]
        p._frame_failed(cam,'FRAME_UNAVAILABLE',100)
        p._frame_failed(cam,'FRAME_UNAVAILABLE',102)
        self.assertEqual(p.log.call_count,2)
        self.assertEqual(p.log.call_args.kwargs,dict(camera_id='CCTV_01',reason='FRAME_UNAVAILABLE',duration_ms=2000))

    def test_counts_proceed_while_mqtt_and_cctv_are_down(self):
        p=self.make({'SHELF_R1_C1':{'state':'Starting','runtime':{}}},mqtt=False,cameras_live=False)
        p._validations();p.validate.assert_called_once_with(self.C1,None)
        self.assertEqual(p.shelf_status('SHELF_R1_C1')['actions_paused_reason'],'MQTT unavailable')

    def test_queued_scan_explains_model_loading_and_the_current_shelf_check(self):
        p=self.make({});p.models_loaded=False
        self.assertIn('models',p.shelf_status('SHELF_R1_C2')['scan']['message'])
        p.models_loaded=True;p.active_shelf='SHELF_R1_C1'
        self.assertIn('SHELF_R1_C1',p.shelf_status('SHELF_R1_C2')['scan']['message'])
        p.active_shelf=None
        self.assertNotIn('SHELF_R1_C1',p.shelf_status('SHELF_R1_C2')['scan']['message'])

    def test_disabled_cctv_never_captures_or_loads_pose(self):
        p=self.make({});p.cctv_enabled=False;p.pose_loaded=False;p.pose_native=Mock()
        p._poses();p.capture.assert_not_called();p.pose_native.warm.assert_not_called()

    def test_worker_recovery_is_independent_of_exhausted_shelf_scans(self):
        p=self.make({});p.native.replies=None;p.exhausted.add('SHELF_R1_C1')
        p.stop.wait.side_effect=[False,True]
        p._recover_workers();p.native.warm.assert_called_once()
        self.assertIn('SHELF_R1_C1',p.exhausted,'worker recovery does not override the shelf retry budget')

    def test_count_taken_while_degraded_is_redone_only_once_everything_is_live(self):
        status={'SHELF_R1_C1':{'state':'Ready','runtime':{'action_capable':False,'state':'Ready'}}}
        p=self.make(status,mqtt=False);p._validations();p.validate.assert_not_called()
        p=self.make(status);p._validations();p.validate.assert_called_once_with(self.C1,None)

    def test_start_cctv_recounts_with_live_frames_even_without_mqtt(self):
        status={'SHELF_R1_C1':{'state':'Ready','runtime':{'action_capable':False,'state':'Ready'}}}
        p=self.make(status,mqtt=False);p.cctv_recounts.add('SHELF_R1_C1')
        p._validations();p.validate.assert_called_once_with(self.C1,None)
        self.assertNotIn('SHELF_R1_C1',p.cctv_recounts)
        self.assertEqual(p.actions_blocker(),'MQTT unavailable','recount never enables actions without MQTT')

    def test_actions_need_live_transports_and_an_action_capable_baseline(self):
        trigger=dict(trigger_id='t',shelf_id='SHELF_R1_C1',expected_action='BORROW',source='CCTV')
        ready={'SHELF_R1_C1':{'state':'Ready','runtime':{'action_capable':True,'state':'Ready'}}}
        p=self.make(ready,pending=[trigger]);p._validations();p.validate.assert_called_once_with(self.C1,trigger)
        for kwargs in (dict(mqtt=False),dict(cameras_live=False)):
            p=self.make(ready,pending=[trigger],**kwargs);p._validations();p.validate.assert_not_called()
        count_only={'SHELF_R1_C1':{'state':'Ready','runtime':{'action_capable':False,'state':'Ready'}}}
        p=self.make(count_only,pending=[trigger]);p._validations()
        p.validate.assert_called_once_with(self.C1,None)  # recount first; the old trigger is never applied to a count-only baseline

    def test_retries_are_bounded_then_wait_for_retry_scan(self):
        p=self.make({'SHELF_R1_C1':{'state':'Held','runtime':{}}})
        p.validate.side_effect=ScanFailure('LOW_CONFIDENCE')
        p._attempt(self.C1,None)
        self.assertEqual(p.scan['SHELF_R1_C1']['state'],'Retrying');self.assertGreater(p.retry_at['SHELF_R1_C1'],time.monotonic())
        for _ in range(MAX_SCAN_ATTEMPTS-1):p._attempt(self.C1,None)
        self.assertIn('SHELF_R1_C1',p.exhausted);self.assertEqual(p.scan['SHELF_R1_C1']['state'],'Needs retry')
        self.assertIn('Retry scan',p.scan['SHELF_R1_C1']['message']);self.assertIn('Low confidence',p.scan['SHELF_R1_C1']['message'])
        calls=p.validate.call_count;p.stop.is_set.side_effect=[False,True];p._validations()
        self.assertEqual(p.validate.call_count,calls,'no endless automatic retries after the budget is spent')
        p.reset_scan_budget('SHELF_R1_C1');p.stop.is_set.side_effect=[False,True];p.retry_at.clear();p._validations()
        self.assertEqual(p.validate.call_count,calls+1)

    def test_success_clears_budget_and_reports_count_ready(self):
        p=self.make({});p.attempts['SHELF_R1_C1']=2;p._attempt(self.C1,None)
        self.assertEqual(p.scan['SHELF_R1_C1']['state'],'Count ready');self.assertNotIn('SHELF_R1_C1',p.attempts)

    def test_held_action_is_never_retried_as_that_action(self):
        trigger=dict(trigger_id='t',shelf_id='SHELF_R1_C1',expected_action='BORROW',source='CCTV')
        p=self.make({});p.validate.side_effect=ValueError('VISIBLE_CHANGE_NET_UNPROVEN: single net action not proven')
        p._attempt(self.C1,trigger)
        scan=p.scan['SHELF_R1_C1'];self.assertEqual(scan['state'],'Waiting');self.assertTrue(scan['message'].startswith('Action held'))
        self.assertNotIn('SHELF_R1_C1',p.exhausted);self.assertEqual(p.retry_at.get('SHELF_R1_C1',0),0)

    def test_missing_esp32_angle_blocks_that_count_and_is_never_zero(self):
        for field,text in (('secondary_ip_address','front camera'),('ip_address','back camera')):
            shelf=dict(self.C1,**{field:None})
            p=self.make({'SHELF_R1_C1':{'state':'Starting','runtime':{}}},shelves=[shelf]);p._validations()
            p.validate.assert_not_called();self.assertEqual(p.scan['SHELF_R1_C1']['state'],'Unavailable')
            self.assertIn(text,p.scan['SHELF_R1_C1']['message']);self.assertTrue(p.scan['SHELF_R1_C1']['message'].startswith('Count unavailable'))

    def test_in_flight_and_unknown_shelves_are_left_alone(self):
        p=self.make({'SHELF_R1_C1':{'state':'Validating Action','runtime':{}},'SHELF_R1_C9':{'state':'Starting','runtime':{}}})
        p._validations();p.validate.assert_not_called()

    def test_actions_blocker_reports_the_first_real_reason(self):
        p=self.make({});self.assertIsNone(p.actions_blocker())
        p.health['CCTV_01']['state']='Offline';self.assertIn('CCTV_01',p.actions_blocker());p.health['CCTV_01']['state']='Online'
        p.mqtt_ok=False;self.assertEqual(p.actions_blocker(),'MQTT unavailable');p.mqtt_ok=True
        p.calibration_issue='CCTV_02: no region saved for SHELF_R1_C2';self.assertEqual(p.actions_blocker(),p.calibration_issue)
        p.set_calibration_issue('No camera calibration is installed yet');self.assertEqual(p.cameras,[])
        p.models_loaded=False;self.assertIn('models',p.actions_blocker())
        p.model_issue='Box-reading models unavailable: x';self.assertEqual(p.actions_blocker(),p.model_issue)

    def test_status_is_honest_about_field_acceptance_and_counts_while_paused(self):
        p=self.make({});snapshot=p.snapshot()
        self.assertEqual(snapshot['acceptance'],'NOT_FIELD_ACCEPTED');self.assertEqual(snapshot['release_blockers'],['REAL_CAMERA_ACCEPTANCE'])
        self.assertFalse(snapshot['authorizes_write']);self.assertEqual(snapshot['state'],'Running')
        p.mqtt_ok=False;self.assertEqual(p.snapshot()['state'],'Counting; actions paused')
        p.acceptance='ACCEPTED';self.assertEqual(p.snapshot()['release_blockers'],[])

    def test_model_boot_reports_setup_issue_then_recovers(self):
        p=AutonomousProcessor('run',Mock(),[],[],Mock(),Mock(),Mock(),Mock(),Mock(),Mock())
        p.runtime=Mock();p.native.warm.side_effect=[RuntimeError('Native models unavailable'),None];p.native.versions={'Front':{},'Back':{},'pose':{}}
        p.stop=Mock();p.stop.is_set.return_value=False;p.stop.wait.return_value=False
        p._boot()
        self.assertTrue(p.models_loaded);self.assertIsNone(p.model_issue);self.assertTrue(p.runtime.models_ready)
        self.assertEqual(p.native.warm.call_count,2);p.native.owner._stop_child.assert_called()


class NativeRecoveryTests(unittest.TestCase):
    def make(self):
        client=NativeClient(Path(__file__).parents[3],'run','manifest')
        client.owner=Mock(startup_id='run');client.replies=object()
        return client

    def successful_reply(self,client):
        request=json.loads(client.owner.proc.stdin.write.call_args.args[0])
        return dict(id=request['id'],startup_id='run',result={'candidate':'fixture'})

    def test_inference_success_resets_consecutive_failures(self):
        client=self.make();client.failures=2
        client.owner._reply.side_effect=lambda *_:self.successful_reply(client)
        self.assertEqual(client.infer({'kind':'pair'})['candidate'],'fixture')
        self.assertEqual(client.failures,0);self.assertEqual(client.retry_at,0)

    def test_repeated_failures_back_off_then_restart_without_permanent_exhaustion(self):
        client=self.make();client.owner._reply.side_effect=RuntimeError('worker failed')
        with patch('src.autonomous_edge.time.monotonic',return_value=100):
            for _ in range(3):
                client.replies=object()
                with self.assertRaises(RuntimeError):client.infer({'kind':'pair'})
            self.assertEqual(client.failures,3);self.assertEqual(client.restart_count,3)
            with self.assertRaisesRegex(RuntimeError,'cooling down'):client.warm()
            client.owner._spawn.assert_not_called()
        with patch('src.autonomous_edge.time.monotonic',return_value=103):
            client.owner._reply.side_effect=None;client.owner._reply.return_value={'ready':True,'versions':{}}
            client.warm();client.owner._spawn.assert_called_once()
            client.owner._reply.side_effect=lambda *_:self.successful_reply(client)
            client.infer({'kind':'pair'});self.assertEqual(client.failures,0)


if __name__=='__main__':unittest.main()
