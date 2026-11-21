"""Isolated real route/comparator tests: no camera, network, or MQTT imports."""
import ast
from collections import deque
from datetime import datetime, timezone
import importlib.util
import math
from pathlib import Path
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'VisionValidation_Module/backend/src'
spec = importlib.util.spec_from_file_location('periodic_recheck', SRC/'periodic_recheck.py')
periodic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(periodic)


def method(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def isolated_function(node, namespace):
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), '<real source>', 'exec'), namespace)
    return namespace[node.name]


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(frame_bgr='old')
        self.manager = types.SimpleNamespace(
            baseline_cache={'s':self.previous}, baseline_freeze_until={},
            fetch_dual_esp32_frame=Mock(return_value=('new','combined','back','front')),
            _compute_absdiff_score=Mock(return_value=4), freeze_baseline=Mock())

    def test_changed_candidate_preserves_baseline(self):
        self.assertEqual(periodic.inspect_candidate(self.manager,'s','ip1','ip2',3.5),4)
        self.assertIs(self.manager.baseline_cache['s'], self.previous)
        self.manager.freeze_baseline.assert_called_once_with('s',20)

    def test_below_threshold_accepts_candidate(self):
        self.manager._compute_absdiff_score.return_value=3.49
        self.assertIsNone(periodic.inspect_candidate(self.manager,'s','ip1','ip2',3.5))
        self.assertEqual(self.manager.baseline_cache['s']['frame_bgr'],'new')

    def test_threshold_boundary_triggers(self):
        self.manager._compute_absdiff_score.return_value=3.5
        self.assertEqual(periodic.inspect_candidate(self.manager,'s','ip1','ip2',3.5),3.5)

    def test_initial_baseline(self):
        self.manager.baseline_cache.clear()
        self.assertIsNone(periodic.inspect_candidate(self.manager,'s','ip1','ip2',3.5))
        self.assertEqual(self.manager.baseline_cache['s']['url_front'],'front')
        self.manager._compute_absdiff_score.assert_not_called()

    def test_partial_and_synthetic_frames_rejected(self):
        for result in [('synthetic','url',None,None),('single','url','back',None)]:
            self.manager.fetch_dual_esp32_frame.return_value=result
            self.assertIsNone(periodic.inspect_candidate(self.manager,'s','ip1','ip2',3.5))
            self.assertIs(self.manager.baseline_cache['s'],self.previous)

    def test_frozen_capture_skipped(self):
        self.manager.baseline_freeze_until['s']=time.time()+20
        self.assertIsNone(periodic.inspect_candidate(self.manager,'s','ip1','ip2',3.5))
        self.manager.fetch_dual_esp32_frame.assert_not_called()

    def test_lease_exclusion_expiry_and_token_ownership(self):
        coordinator=periodic.RecheckCoordinator()
        with patch.object(periodic.time,'time',return_value=100):
            token=coordinator.begin('s')
            self.assertIsNone(coordinator.begin('s'))
            self.assertFalse(coordinator.end('s','wrong'))
            self.assertTrue(coordinator.owns('s',token))
        with patch.object(periodic.time,'time',return_value=221):
            self.assertFalse(coordinator.owns('s',token))
            fresh=coordinator.begin('s')
            self.assertNotEqual(fresh,token)
            self.assertTrue(coordinator.end('s',fresh))


spec_inventory = importlib.util.spec_from_file_location('dual_cam_inventory', SRC/'dual_cam_inventory.py')
inventory = importlib.util.module_from_spec(spec_inventory)
spec_inventory.loader.exec_module(inventory)


class ValidationRouteTests(unittest.TestCase):
    def setUp(self):
        self.coordinator=periodic.RecheckCoordinator()
        self.token=self.coordinator.begin('s')
        self.quantity=4
        self.baseline={'frame_bgr':'before','data_url':'before-url'}
        self.manager=types.SimpleNamespace(baseline_cache={'s':self.baseline}, baseline_freeze_until={},
            freeze_baseline=Mock(), _compute_absdiff_score=lambda a,b:5)
        ns=dict(datetime=datetime,timezone=timezone,time=time,
                decode_data_url_to_bgr=lambda image: image, SCENARIO_MAP={})
        comparator=isolated_function(method(SRC/'reference_validator.py','compare_with_30s_baseline_and_references'),ns)
        self.manager.compare_with_30s_baseline_and_references=types.MethodType(comparator,self.manager)
        self.rest=Mock()
        self.rest.select.side_effect=lambda *a,**kw: [{'shelf_id':'s','current_quantity':self.quantity}]
        self.apply=Mock(return_value={'log_id':'saved'})
        self.logs=deque(maxlen=5)
        self.captures = {}
        self.occupancy = {'s': (1,1,1,1,0)}
        self.persisted_slots = dict(self.occupancy)
        self.revision = 0
        def load_state(shelf):
            rows=self.rest.select('compartments')
            if not rows: raise ValueError('Fresh inventory unavailable')
            slots=self.persisted_slots.get(shelf)
            return dict(shelf_id=shelf,current_quantity=rows[0]['current_quantity'],revision=self.revision,
                confirmed_occupancy=list(slots) if slots is not None else None,
                last_verified_at='verified' if slots is not None else None,
                layout_version='five-slot-stacked-v1' if slots is not None else None,
                evidence_version=inventory.VERSION if slots is not None else None)
        self.load_state=Mock(side_effect=load_state)
        def commit_state(before,slots,action,event_id):
            if sum(slots)!=before['current_quantity']:
                result=self.apply(shelf_id='s',trigger_action=action)
                if result is False: raise ValueError('Database failed')
            self.revision+=1
            self.persisted_slots['s']=tuple(slots)
            return dict(before,revision=self.revision,confirmed_occupancy=list(slots),current_quantity=sum(slots))
        self.commit_state=Mock(side_effect=commit_state)
        self.ns=dict(CCTV_ALIGN_STATE={},mqtt_bridge=types.SimpleNamespace(is_connected=True),decode_observation=inventory.decode_observation,failure_code=lambda reason:'VALIDATION_REJECTED',READINESS=types.SimpleNamespace(ready=lambda shelf:True,active=lambda shelf:True,record=Mock(),set=Mock(),get=lambda shelf:{'state':'Ready'}),load_inventory_state=self.load_state,commit_inventory_state=self.commit_state,CONFIRMED_OCCUPANCY=self.occupancy, validate_transition=inventory.validate_transition, DUAL_CAM_CAPTURES=self.captures, DUAL_CAM_CAPTURE_LOCK=threading.Lock(), validate_evidence=inventory.validate_evidence, RECHECKS=self.coordinator, esp32_ref_manager=self.manager,
            SupabaseRestFallback=lambda:self.rest, urllib=urllib,math=math,uuid=uuid,time=time,
            apply_inventory_delta_in_supabase=self.apply,get_cached_compartments=Mock(),
            spatial_mapper=types.SimpleNamespace(rack_id='rack'),LATEST_COMPARISONS_DEQUE=self.logs,
            CCTV_CONSENSUS_LOCK=threading.RLock(),PENDING_CCTV_TRIGGERS=[],
            decode_data_url_to_bgr=lambda image:image)
        self.ns['PROCESSOR_AUTHORITY']='browser'
        self.route=isolated_function(method(ROOT/'VisionValidation_Module/camera_service.py','do_POST'),self.ns)
        self.body=dict(shelf_id='s',source='MANUAL',expected_action='BORROW',validation_token=self.token,
            predicted_box_count=3,tm_confidence=90,image='after',image_back='back',image_front='front',
            periodic_evidence=dict(first_count=3,first_confidence=90,delay_ms=2000))
        self.fresh_evidence()
        self.handler=types.SimpleNamespace(path='/esp32_validate',_read_json_body=lambda:self.body,
            _send_json=lambda status,payload:setattr(self,'response',(status,payload)))

    def fresh_evidence(self):
        now = time.time()
        self.captures.clear()
        for i in range(3):
            self.captures[str(i)] = dict(shelf_id='s', complete=True, completed_at=now-6+i*2, duration_ms=500, validation_token=self.token)
        self.body['dual_cam_evidence'] = dict(version=inventory.VERSION, samples=[dict(
            front_class='Front_110', back_class='Back_100', front_confidence=95, back_confidence=94,
            capture=dict(id=str(i))) for i in range(3)])

    def test_confirmed_removal_then_idempotent_repeat(self):
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertEqual(self.response[1]['comparison']['delta'],-1)
        self.assertEqual(self.logs[0]['source'],'MANUAL')
        self.assertEqual(self.occupancy['s'], (1,1,0,1,0))
        self.assertEqual(self.manager.baseline_cache['s']['url_front'],'front')
        self.quantity=3
        self.body['source']='PERIODIC_RECHECK'
        self.fresh_evidence()
        self.route(self.handler)
        self.assertEqual(self.response[1]['comparison']['delta'],0)
        self.assertIsNone(self.response[1]['log'])
        self.assertEqual(self.apply.call_count,1)
        self.assertEqual(len(self.logs),1)

    def test_confirmation_mismatch_aborts(self):
        self.body['dual_cam_evidence']['samples'][0]['front_class']='Front_111'
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()
        self.assertIs(self.manager.baseline_cache['s'],self.baseline)

    def test_unavailable_fresh_inventory_aborts(self):
        self.rest.select.return_value=[]
        self.rest.select.side_effect=None
        self.route(self.handler)
        self.assertEqual(self.response[0],503)
        self.apply.assert_not_called()

    def test_failed_database_write_restores_baseline(self):
        self.apply.return_value=False
        self.route(self.handler)
        self.assertEqual(self.response[0],502)
        self.assertIs(self.manager.baseline_cache['s'],self.baseline)
        self.assertEqual(len(self.logs),0)

    def test_second_browser_cannot_validate_active_shelf(self):
        self.body['validation_token']='other'
        self.route(self.handler)
        self.assertEqual(self.response[0],409)
        self.rest.select.assert_not_called()

    def test_low_confidence_aborts(self):
        self.body['dual_cam_evidence']['samples'][0]['front_confidence']=64
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()

    def test_six_box_prediction_rejected(self):
        self.body['predicted_box_count'] = 6
        self.route(self.handler)
        self.assertEqual(self.response[0], 400)
        self.apply.assert_not_called()

    def test_reused_receipts_rejected(self):
        self.route(self.handler)
        self.route(self.handler)
        self.assertEqual(self.response[0], 400)
        self.assertEqual(self.apply.call_count, 1)

    def test_missing_evidence_rejects_manual_and_cctv(self):
        self.body.pop('dual_cam_evidence')
        for source in ('MANUAL', 'PERIODIC_RECHECK'):
            self.body['source'] = source
            self.route(self.handler)
            self.assertEqual(self.response[0], 400)
        self.apply.assert_not_called()
        self.assertEqual(len(self.logs), 0)

    def test_seven_boxes_rejected_by_periodic_route(self):
        self.body['predicted_box_count'] = 7
        self.body['periodic_evidence']['first_count'] = 7
        self.route(self.handler)
        self.assertEqual(self.response[0], 400)
        self.apply.assert_not_called()

    def test_periodic_change_is_held(self):
        self.body['source']='PERIODIC_RECHECK'
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()
        self.assertEqual(self.occupancy['s'], (1,1,1,1,0))

    def test_uninitialized_change_is_held(self):
        self.occupancy.clear()
        self.persisted_slots.clear()
        self.route(self.handler)
        self.assertEqual(self.response[0],403)
        self.apply.assert_not_called()

    def test_five_box_initialization_then_cctv_f1_removal(self):
        self.occupancy.clear()
        self.persisted_slots['s']=(1,1,1,1,1)  # Established by authorized recount.
        self.quantity=5
        self.body['predicted_box_count']=5
        for sample in self.body['dual_cam_evidence']['samples']:
            sample.update(front_class='Front_111',back_class='Back_11X')
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertFalse(self.response[1]['snapshot_initialized'])
        self.assertEqual(self.occupancy['s'],(1,1,1,1,1))
        self.apply.assert_not_called()
        self.fresh_evidence()
        self.body.update(source='CCTV',trigger_id='remove-f1',predicted_box_count=4)
        for sample in self.body['dual_cam_evidence']['samples']:
            sample.update(front_class='Front_011',back_class='Back_11X')
        self.ns['PENDING_CCTV_TRIGGERS'].append(self.event('remove-f1'))
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertFalse(self.response[1]['snapshot_initialized'])
        self.assertEqual(self.response[1]['comparison']['delta'],-1)
        self.assertEqual(self.occupancy['s'],(0,1,1,1,1))
        self.assertEqual(self.apply.call_count,1)

    def test_missing_snapshot_reports_actual_counts(self):
        self.occupancy.clear()
        self.persisted_slots.clear()
        self.quantity=5
        self.body['predicted_box_count']=4
        for sample in self.body['dual_cam_evidence']['samples']:
            sample.update(front_class='Front_011',back_class='Back_11X')
        self.route(self.handler)
        self.assertEqual(self.response[0],403)
        self.assertIn('Authorized physical recount required',self.response[1]['error'])
        self.assertEqual(self.occupancy,{})
        self.apply.assert_not_called()

    def test_restart_uses_persistent_slots_without_manual_initialization(self):
        self.occupancy.clear()  # The database still has the prior confirmed slots.
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertFalse(self.response[1]['snapshot_initialized'])
        self.assertEqual(self.occupancy['s'],(1,1,0,1,0))
        self.commit_state.assert_called_once()

    def test_legacy_initialization_commits_slots_even_without_quantity_delta(self):
        self.persisted_slots.clear()
        self.quantity=3
        self.route(self.handler)
        self.assertEqual(self.response[0],403)
        self.commit_state.assert_not_called()
        self.apply.assert_not_called()

    def test_failed_legacy_commission_stays_unverified(self):
        self.persisted_slots.clear()
        self.quantity=3
        self.commit_state.side_effect=ValueError('RPC unavailable')
        self.route(self.handler)
        self.assertEqual(self.response[0],403)
        self.assertEqual(self.persisted_slots,{})
        self.assertNotIn('s',self.occupancy)
        self.assertIs(self.manager.baseline_cache['s'],self.baseline)
        self.assertEqual(len(self.logs),0)

    def test_ram_snapshot_cannot_override_unverified_database(self):
        self.persisted_slots.clear()
        self.occupancy['s']=(1,1,1,1,0)
        self.route(self.handler)
        self.assertEqual(self.response[0],403)
        self.assertNotIn('s',self.occupancy)
        self.commit_state.assert_not_called()

    def readiness(self):
        spec=importlib.util.spec_from_file_location('src.shelf_readiness',SRC/'shelf_readiness.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        obj=module.ShelfReadiness();self.ns['READINESS']=obj
        return obj

    def test_startup_matching_snapshot_ready_without_commit(self):
        ready=self.readiness()
        self.body.update(source='STARTUP',predicted_box_count=4)
        for sample in self.body['dual_cam_evidence']['samples']:
            sample.update(front_class='Front_111',back_class='Back_100')
        # Front M2 must match Back M2: use exact supported old snapshot instead.
        self.persisted_slots['s']=(0,1,1,1,1)
        for sample in self.body['dual_cam_evidence']['samples']:
            sample.update(front_class='Front_011',back_class='Back_11X')
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertTrue(ready.ready('s'))
        self.commit_state.assert_not_called()
        self.assertEqual(self.captures,{})

    def test_startup_mismatch_never_writes(self):
        ready=self.readiness();self.body['source']='STARTUP'
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.assertEqual(ready.get('s')['state'],'Needs Reconciliation')
        self.commit_state.assert_not_called()

    def test_untrusted_legacy_startup_requires_commissioning(self):
        ready=self.readiness();self.persisted_slots.clear();self.quantity=3
        self.body['source']='STARTUP';self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.assertEqual(ready.get('s')['state'],'Needs Reconciliation')
        self.commit_state.assert_not_called()

    def test_startup_interaction_not_admitted_to_queue(self):
        self.readiness();self.coordinator.end('s',self.token)
        self.handler.path='/validation_begin';self.body.update(source='CCTV',request_id='interaction')
        self.route(self.handler)
        self.assertEqual(self.response[0],409)
        self.assertFalse(self.coordinator.busy())

    def test_ready_shelf_normal_action_passes_persistence_and_returns_ready(self):
        ready=self.readiness()
        ready.set('s','Ready','Freshly verified',self.revision)
        ready.set('s','Validating Action','CCTV consensus received')
        self.assertFalse(ready.ready('s'))
        self.assertTrue(ready.active('s'))
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertTrue(ready.ready('s'))
        self.commit_state.assert_called_once()

    def test_recount_requires_identity_confirmation_and_reverification(self):
        ready=self.readiness();self.coordinator.end('s',self.token)
        self.handler.path='/reconcile_inventory';self.handler.headers={'Authorization':'Bearer operator-jwt'}
        self.body=dict(shelf_id='s',run_id=ready.run_id,confirmed=False)
        self.ns['operator_rpc']=Mock(return_value=dict(status='accepted',state=dict(shelf_id='s',confirmed_occupancy=[0,1,1,1,1],revision=7)))
        self.ns['event']=Mock()
        self.route(self.handler)
        self.assertEqual(self.response[0],409);self.ns['operator_rpc'].assert_not_called()
        self.body['confirmed']=True;self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertEqual(ready.get('s')['state'],'Starting')
        self.assertFalse(ready.ready('s'))
        self.assertEqual(self.occupancy['s'],(0,1,1,1,1))

    def test_denied_recount_does_not_change_cache(self):
        ready=self.readiness();self.coordinator.end('s',self.token)
        self.handler.path='/reconcile_inventory';self.handler.headers={}
        self.body=dict(shelf_id='s',run_id=ready.run_id,confirmed=True)
        self.ns['operator_rpc']=Mock(side_effect=PermissionError('Operator access denied'))
        before=dict(self.occupancy)
        self.route(self.handler)
        self.assertEqual(self.response[0],403);self.assertEqual(self.occupancy,before)

    def test_initialization_without_write(self):
        self.occupancy.clear()
        self.persisted_slots.clear()
        self.quantity=3
        self.route(self.handler)
        self.assertEqual(self.response[0],403)
        self.commit_state.assert_not_called()
        self.apply.assert_not_called()

    def test_failed_write_preserves_occupancy(self):
        self.apply.return_value=False
        self.route(self.handler)
        self.assertEqual(self.response[0],502)
        self.assertEqual(self.occupancy['s'], (1,1,1,1,0))

    def test_wrong_event_receipts(self):
        self.captures['0']['validation_token']='other-event'
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()

    def test_wrong_action_and_same_total_move_are_held(self):
        self.body['expected_action']='RETURN'
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.quantity=3
        self.occupancy['s']=(0,1,1,1,0)
        self.persisted_slots['s']=(0,1,1,1,0)
        for sample in self.body['dual_cam_evidence']['samples']:
            sample['front_class']='Front_110'
            sample['back_class']='Back_100'
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()
        self.assertEqual(self.occupancy['s'],(0,1,1,1,0))

    def test_cctv_uses_established_action_and_preserves_newer_event(self):
        self.body.update(source='CCTV',expected_action='RETURN',trigger_id='established')
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        pending.extend([dict(trigger_id='established',shelf_id='s',claimed=True,
                            consensus_met=True,timestamp=time.time()-10,expected_action='BORROW',
                            validation_token=self.token,claimed_at=time.time()-9),
                        dict(trigger_id='newer',shelf_id='s',timestamp=time.time())])
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertEqual(self.apply.call_args.kwargs['trigger_action'],'BORROW')
        self.assertEqual([t['trigger_id'] for t in pending],['newer'])

    def test_cctv_missing_stale_or_future_event_is_held(self):
        self.body.update(source='CCTV',trigger_id='event')
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        for age in (None,70,-1):
            pending.clear()
            if age is not None:
                pending.append(dict(trigger_id='event',shelf_id='s',claimed=True,
                    consensus_met=True,timestamp=time.time()-age,expected_action='BORROW',
                    validation_token=self.token,claimed_at=time.time()-9))
            self.route(self.handler)
            self.assertEqual(self.response[0],409)
        self.apply.assert_not_called()

    def test_capture_before_cctv_event_is_held(self):
        self.body.update(source='CCTV',trigger_id='event')
        self.ns['PENDING_CCTV_TRIGGERS'].append(dict(trigger_id='event',shelf_id='s',claimed=True,
            consensus_met=True,timestamp=time.time()-1,expected_action='BORROW',
            validation_token=self.token,claimed_at=time.time()-1))
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()

    def event(self, trigger_id, action='BORROW', token=None, **overrides):
        return dict(trigger_id=trigger_id, shelf_id='s', claimed=True, consensus_met=True,
            timestamp=time.time()-10, claimed_at=time.time()-9,
            expected_action=action, validation_token=token or self.token, **overrides)

    def test_exact_event_wins_over_newer_and_orphan_claims(self):
        self.body.update(source='CCTV',trigger_id='selected',expected_action='RETURN')
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        old=self.event('orphan',token='expired-lease')
        selected=self.event('selected')
        newer=self.event('newer','RETURN',token='other-lease')
        newer['timestamp']=time.time()-8
        pending.extend([old,selected,newer])
        self.route(self.handler)
        self.assertEqual(self.response[0],200)
        self.assertEqual(self.apply.call_args.kwargs['trigger_action'],'BORROW')
        self.assertEqual([t['trigger_id'] for t in pending],['orphan','newer'])

    def test_other_event_cannot_authorize_opposite_action(self):
        self.body.update(source='CCTV',trigger_id='borrow',expected_action='BORROW',predicted_box_count=5)
        for sample in self.body['dual_cam_evidence']['samples']:
            sample.update(front_class='Front_111',back_class='Back_11X')
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        pending.extend([self.event('borrow'),self.event('different-return','RETURN',token='other-lease')])
        pending[1]['timestamp']=time.time()-8
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()
        self.assertEqual(len(pending),2)

    def test_missing_wrong_id_wrong_shelf_and_wrong_lease_fail_closed(self):
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        pending.append(self.event('selected'))
        self.body['source']='CCTV'
        for trigger_id in (None,'unrelated'):
            self.body['trigger_id']=trigger_id
            self.route(self.handler)
            self.assertEqual(self.response[0],409)
        self.body['trigger_id']='selected'
        for key,value in [('validation_token','old-lease'),('shelf_id','other')]:
            original=pending[0][key]
            pending[0][key]=value
            self.route(self.handler)
            self.assertEqual(self.response[0],409)
            pending[0][key]=original
        self.apply.assert_not_called()

    def test_preclaim_capture_rejected(self):
        self.body.update(source='CCTV',trigger_id='selected')
        event=self.event('selected')
        event['claimed_at']=time.time()-1
        self.ns['PENDING_CCTV_TRIGGERS'].append(event)
        self.route(self.handler)
        self.assertEqual(self.response[0],400)
        self.apply.assert_not_called()

    def test_claim_requires_owning_lease_and_one_event_per_operation(self):
        claim=isolated_function(method(ROOT/'VisionValidation_Module/camera_service.py','claim_cctv_trigger'),self.ns)
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        pending.extend([self.event('orphan',token='abandoned'),self.event('one'),self.event('two')])
        for event in pending[1:]:
            event.update(claimed=False,validation_token=None)
        self.assertFalse(claim('one','other',self.token))
        self.assertFalse(claim('one','s','wrong-token'))
        self.assertTrue(claim('one','s',self.token))
        self.assertEqual(pending[1]['validation_token'],self.token)
        self.assertFalse(claim('two','s',self.token))
        self.assertFalse(claim('one','s',self.token))

    def test_release_preserves_other_operations_event(self):
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        pending.extend([self.event('mine'),self.event('other',token='abandoned')])
        self.handler.path='/validation_end'
        self.body.update(token=self.token,trigger_id='other')
        self.route(self.handler)
        self.assertTrue(self.response[1]['released'])
        self.assertEqual([t['trigger_id'] for t in pending],['mine','other'])

    def test_release_clears_only_bound_event(self):
        pending=self.ns['PENDING_CCTV_TRIGGERS']
        pending.extend([self.event('mine'),self.event('other',token='abandoned')])
        self.handler.path='/validation_end'
        self.body.update(token=self.token,trigger_id='mine')
        self.route(self.handler)
        self.assertTrue(self.response[1]['released'])
        self.assertEqual([t['trigger_id'] for t in pending],['other'])

    def test_unknown_b1_never_writes_or_advances_prior_snapshot(self):
        for source in ('MANUAL','PERIODIC_RECHECK'):
            for guessed_count in (2,3):
                self.body.update(source=source,predicted_box_count=guessed_count)
                for sample in self.body['dual_cam_evidence']['samples']:
                    sample.update(front_class='Front_011',back_class='Back_X01')
                self.route(self.handler)
                self.assertEqual(self.response[0],400)
                self.assertIn('B1 unknown',self.response[1]['error'])
                self.assertEqual(self.occupancy['s'],(1,1,1,1,0))
        self.apply.assert_not_called()
        self.assertEqual(len(self.logs),0)
        self.assertIs(self.manager.baseline_cache['s'],self.baseline)

    def test_manual_requires_active_lease(self):
        self.body.pop('validation_token')
        self.route(self.handler)
        self.assertEqual(self.response[0],409)
        self.apply.assert_not_called()

    def test_existing_six_requires_explicit_recount(self):
        self.quantity = 6
        self.route(self.handler)
        self.assertEqual(self.response[0], 409)
        self.apply.assert_not_called()
        self.assertIs(self.manager.baseline_cache['s'], self.baseline)


class InventoryWriteTests(unittest.TestCase):
    def test_six_box_write_and_clamping(self):
        for predicted, expected in [(5, 5), (6, 6), (9, 6), (-2, 0)]:
            with self.subTest(predicted=predicted):
                rest = Mock(return_value=[{'shelf_id': 's', 'current_quantity': expected}])
                ns = dict(SupabaseRestFallback=Mock(), _supabase_rest=rest, urllib=urllib,
                          datetime=datetime, timezone=timezone, uuid=uuid)
                apply = isolated_function(method(SRC/'config_loader.py','apply_inventory_delta_in_supabase'), ns)
                result = apply('s','device','RETURN',dict(delta=predicted-4,
                    state_delta='ITEM_RETURNED',matched_box_count=predicted),4)
                self.assertEqual(rest.call_args.kwargs['payload'], {'current_quantity': expected})
                self.assertEqual(result['quantity_after'], expected)
                self.assertEqual(result['matched_box_count'], expected)

    def test_compare_and_set_rejects_concurrent_database_change(self):
        rest = Mock(return_value=[])
        ns = dict(SupabaseRestFallback=Mock(), _supabase_rest=rest, urllib=urllib,
                  datetime=datetime, timezone=timezone, uuid=uuid)
        apply = isolated_function(method(SRC/'config_loader.py','apply_inventory_delta_in_supabase'), ns)
        result = apply('s','device','BORROW',dict(delta=-1,state_delta='ITEM_REMOVED',matched_box_count=3),4)
        self.assertIs(result,False)
        self.assertIn('current_quantity=eq.4',rest.call_args.kwargs['query'])
        self.assertEqual(rest.call_args.kwargs['payload'],{'current_quantity':3})


if __name__ == '__main__':
    unittest.main()
