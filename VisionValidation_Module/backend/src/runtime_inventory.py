"""Camera-first inventory authority. RAM only: no file, cloud or restore path."""
from collections import OrderedDict
import copy
import math
import os
import threading
import time
import uuid
from .dual_cam_inventory import decode_observation, prove_unit_action, validate_confidences, validate_capture, REQUIRED_MATCHING_PAIRS
from .shelves import SHELF_IDS, SHELF_SET


def runtime_enabled():
    mode=os.getenv('SMART_STORAGE_INVENTORY_MODE','runtime').strip().lower()
    if mode!='runtime':raise ValueError('Only runtime inventory is supported; remove the legacy SMART_STORAGE_INVENTORY_MODE setting')
    return True


class RuntimeInventory:
    def __init__(self,run_id=None,quiet_seconds=2,clock=time.time,monotonic=time.monotonic):
        if not math.isfinite(quiet_seconds) or not 0<quiet_seconds<=60:raise ValueError('Quiet window must be positive and at most 60 seconds')
        self.run_id=run_id or str(uuid.uuid4());self.quiet_seconds=quiet_seconds
        self.clock=clock;self.monotonic=monotonic;self.guard=threading.RLock()
        self.shelves={};self.cameras={};self.models_ready=False;self.last_interaction=monotonic()
        self.cctv_enabled=True

    def _state(self,shelf):
        if shelf not in SHELF_SET:raise ValueError('Unknown shelf')
        if shelf not in self.shelves:
            self.shelves[shelf]=dict(lock=threading.RLock(),version=0,state='Starting',baseline=None,observation=None,
                born_at=self.clock(),ready_at=None,events=OrderedDict(),count_check=None)
        return self.shelves[shelf]

    def snapshot(self,shelf):
        with self.guard:
            row=self._state(shelf)
            with row['lock']:
                baseline=copy.deepcopy(row['baseline'])
                # A baseline that is no longer authoritative stays visible only as a clearly historical count.
                historical=dict(total=baseline['total'],count_range=baseline['count_range'],precision=baseline['precision'],
                    observed_at=baseline.get('last_capture_at')) if baseline and row['state']!='Ready' else None
                return dict(shelf_id=shelf,historical=historical,cctv_enabled=self.cctv_enabled,
                    count_check=copy.deepcopy(row['count_check']),count_check_unverified=bool(baseline and baseline.get('count_check_unverified')),
                    action_capable=bool(baseline and baseline.get('action_capable')) and row['state']=='Ready',
                    observed_at=baseline.get('last_capture_at') if baseline else None,run_id=self.run_id,version=row['version'],state=row['state'],
                    ready_at=row['ready_at'],born_at=row['born_at'],baseline=baseline,latest_observation=copy.deepcopy(row['observation']),
                    authoritative=row['state']=='Ready',
                    display_state='Ready Limited' if row['state']=='Ready' and baseline and baseline['precision']=='RANGE' else row['state'],
                    slots=baseline['slots'] if baseline and row['state']=='Ready' else [None]*5,precision=baseline['precision'] if baseline and row['state']=='Ready' else 'UNKNOWN',
                    current_quantity=baseline['total'] if baseline and row['state']=='Ready' else None,
                    count_range=baseline['count_range'] if baseline and row['state']=='Ready' else [0,5],inventory_mode='runtime')

    def invalidate(self,reason='Offline'):
        with self.guard:
            self.last_interaction=self.monotonic()
            for row in self.shelves.values():
                with row['lock']:
                    row.update(state=reason,ready_at=None,born_at=self.clock(),version=row['version']+1)
                    # Retain last accepted baseline in RAM as diagnostic history only;
                    # state/ready_at prevent it from authorizing further actions.

    def reset_shelf(self,shelf):
        """Operator/scheduler rescan request: this shelf alone loses authority until a fresh count is accepted."""
        with self.guard:
            row=self._state(shelf)
            with row['lock']:
                row.update(state='Starting',ready_at=None,born_at=self.clock(),version=row['version']+1)

    def interrupt_actions(self,disable_actions=False):
        """Discard in-flight action authority while retaining the last counted quantities/timestamps."""
        with self.guard:
            self.last_interaction=self.monotonic()
            for row in self.shelves.values():
                with row['lock']:
                    row['version']+=1
                    if row['state']=='Ready':row['ready_at']=self.clock()
                    if disable_actions and row['baseline']:row['baseline']['action_capable']=False

    def set_cctv_enabled(self,enabled):
        with self.guard:
            self.cctv_enabled=enabled
            self.cameras.clear()

    def clear_baselines(self):
        """Drop RAM baselines/observations after calibration change; each shelf must rebaseline fresh."""
        with self.guard:
            for row in self.shelves.values():
                with row['lock']:row.update(baseline=None,observation=None)

    def hold(self,shelf,event_id=None):
        if event_id is not None and (not isinstance(event_id,str) or not event_id):event_id=None
        with self.guard:
            row=self._state(shelf)
            with row['lock']:
                if event_id is not None and event_id in row['events']:return False
                if event_id is not None and len(row['events'])<200:row['events'][event_id]='HELD'
                row.update(state='Held',ready_at=None,born_at=self.clock(),version=row['version']+1)
                return True

    def observe(self,shelf,candidate,capture):
        with self.guard:
            self._state(shelf)['observation']=dict(candidate=copy.deepcopy(candidate),capture=copy.deepcopy(capture),accepted=False)

    def count_check_failed(self,shelf,code,message,expected_version):
        """A failed background check never discards a last known count or alters a newer transaction."""
        with self.guard:
            row=self._state(shelf)
            with row['lock']:
                if row['state']!='Ready' or row['version']!=expected_version:return False
                row['count_check']=dict(status='Check deferred' if code=='COUNT_CHECK_SKIPPED' else 'Check failed',code=code,message=message,checked_at=self.clock())
                if row['baseline'] and code!='COUNT_CHECK_SKIPPED':
                    row['baseline']['action_capable']=False;row['baseline']['count_check_unverified']=True
                return True

    def camera(self,cid,observed_at,healthy,interacting=False):
        if cid not in ('CCTV_01','CCTV_02','CCTV_03'):raise ValueError('Unknown CCTV')
        with self.guard:
            previous=self.cameras.get(cid)
            valid=healthy and 0<=self.clock()-observed_at<1.5
            lost_since=None if valid else (previous or {}).get('lost_since')
            if not valid and lost_since is None:lost_since=self.monotonic()
            self.cameras[cid]=dict(at=observed_at,healthy=valid,lost_since=lost_since,
                interacting=bool(valid and interacting),
                was_live=valid or bool(previous and previous.get('was_live')),
                invalidated=False if valid else bool(previous and previous.get('invalidated')))
            if interacting:self.last_interaction=self.monotonic()
            # A missing startup camera never blocks counting. Brief outages preserve counts;
            # action freshness still fails immediately, independently of this grace period.
            camera=self.cameras[cid]
            if not valid and camera['was_live'] and not camera['invalidated'] and self.monotonic()-lost_since>5:
                camera['invalidated']=True;self.invalidate();return True
            return False

    def cameras_fresh(self):
        with self.guard:
            return self.cctv_enabled and all(self.cameras.get(c,{}).get('healthy') and 0<=self.clock()-self.cameras[c]['at']<1.5
                for c in ('CCTV_01','CCTV_02','CCTV_03'))

    def _environment_issue(self,require_cameras):
        """Caller holds guard; one source for the readiness gate and its user-facing explanation."""
        if not self.models_ready:return 'Native models unavailable','Waiting for the box-reading models to load'
        if require_cameras and not self.cameras_fresh():
            down=[cid for cid in ('CCTV_01','CCTV_02','CCTV_03') if not (
                self.cameras.get(cid,{}).get('healthy') and 0<=self.clock()-self.cameras[cid]['at']<1.5)]
            message='Waiting for live frames from '+', '.join(down) if self.cctv_enabled else 'Waiting for CCTV to start'
            return 'Fresh CCTV feeds required',message
        remaining=self.quiet_seconds-(self.monotonic()-self.last_interaction)
        if remaining>0:
            active=[cid for cid,camera in self.cameras.items() if self.cctv_enabled and camera.get('interacting') and
                camera['healthy'] and 0<=self.clock()-camera['at']<1.5]
            if active:
                detected='; '.join(cid+(' detects someone in the entry zone' if cid=='CCTV_03' else ' detects someone in a shelf region') for cid in active)
                message='Waiting for the rack to clear: '+detected
            else:message=f'Waiting for the rack to stay clear for another {remaining:.1f} s'
            return 'Quiet interaction window not satisfied',message
        return None

    def environment_wait_reason(self,require_cameras=True):
        with self.guard:
            issue=self._environment_issue(require_cameras)
            return issue[1] if issue else None

    def environment_ready(self,require_cameras=True):
        """Count scans need models and a quiet rack; actions additionally need all three fresh CCTV feeds."""
        with self.guard:
            issue=self._environment_issue(require_cameras)
            if issue:raise ValueError(issue[0])
            return True

    def accept(self,shelf,*,run_id,event_id,expected_version,samples,captures,lease,source,event=None,transport_ok=True):
        with self.guard:
            row=self._state(shelf)
            with row['lock']:
                if run_id!=self.run_id or type(expected_version) is not int or expected_version!=row['version']:raise ValueError('Stale run or inventory version')
                if not isinstance(lease,str) or not lease:raise ValueError('Lease identity required')
                if not isinstance(event_id,str) or not event_id or event_id in row['events']:raise ValueError('Duplicate/invalid event')
                count_check=source=='COUNT_CHECK'
                if len(row['events'])>=200 and not count_check:raise ValueError('In-run event limit reached; restart and reobserve')
                self.environment_ready(require_cameras=source not in ('STARTUP','COUNT_CHECK'))
                if not isinstance(samples,list) or len(samples)!=REQUIRED_MATCHING_PAIRS:
                    raise ValueError(f'{REQUIRED_MATCHING_PAIRS} consecutive matching paired captures required')
                seen=set();slots=None;previous=None;latest=None;first_start=None
                for sample in samples:
                    validate_confidences(sample)
                    observation=decode_observation(sample.get('front_class'),sample.get('back_class'))
                    if slots is not None and slots!=observation['slots']:raise ValueError('Occupancy changed between captures')
                    slots=observation['slots']
                    timing=validate_capture(sample,self.clock(),captures,shelf,lease,seen,previous,row['born_at'],self.run_id)
                    identity,completed,started=timing
                    first_start=started if first_start is None else first_start
                    if self.clock()-started > self.monotonic()-self.last_interaction:
                        raise ValueError('Interaction overlaps capture window')
                    seen.add(identity);previous=completed;latest=dict(observation)
                latest.update(slots=list(slots),count_range=list(latest['count_range']),precision='RANGE' if latest['total'] is None else 'EXACT',
                    observation_valid=True,action_authorized=False,
                    first_capture_at=first_start,last_capture_at=previous,
                    # Counted while CCTV or the action transport was down: usable as a count, never as an action baseline.
                    action_capable=bool(transport_ok) and self.cameras_fresh())
                row['observation']=latest
                baseline=row['baseline']
                retained_hidden=False
                if count_check:
                    if row['state']!='Ready' or baseline is None:raise ValueError('Shelf not Ready for count check')
                    visible_match=all(value is None or value==baseline['slots'][i] for i,value in enumerate(slots))
                    retained_hidden=any(value is None for value in slots) and visible_match
                    changed=not retained_hidden and (latest['total']!=baseline['total'] or latest['count_range']!=baseline['count_range'])
                    row['count_check']=dict(status='Hidden box not checked' if retained_hidden else 'Count changed' if changed else 'Count unchanged',
                        message='Visible positions match; previous count retained. Hidden B1 was not checked.' if retained_hidden else
                            'Hidden B1 was not checked; showing the observed count range.' if latest['precision']=='RANGE' else 'Two matching Back/Front reads confirmed.',
                        checked_at=self.clock(),code=None)
                    if retained_hidden:
                        # Keep the prior count and its timestamp as last observed, never fill an unknown slot.
                        baseline['action_capable']=False;baseline['count_check_unverified']=True
                    else:
                        latest['action_capable']=latest['action_capable'] and bool(baseline.get('action_capable'))
                elif source=='STARTUP':
                    # A new camera baseline is an observation, never a historical transaction.
                    if row['state']=='Ready' and baseline and baseline.get('action_capable'):raise ValueError('Ready shelf cannot be silently rebaselined')
                else:
                    if row['state']!='Ready' or row['ready_at'] is None or baseline is None:raise ValueError('Shelf not Ready')
                    if not baseline.get('action_capable'):raise ValueError('Count was taken while CCTV or MQTT was unavailable; rescan required before actions')
                    if source!='CCTV' or not event or event.get('trigger_id')!=event_id or event.get('shelf_id')!=shelf or not event.get('claimed') or not event.get('consensus_met') or event.get('validation_token')!=lease:
                        raise ValueError('Bound CCTV event required')
                    if event.get('expected_action') not in ('BORROW','RETURN'):raise ValueError('Server action required')
                    if not row['ready_at']<=event.get('timestamp',0)<=event.get('claimed_at',0)<=first_start or not 0<=self.clock()-event['timestamp']<60:
                        raise ValueError('Event predates runtime readiness or capture')
                    proof=prove_unit_action(baseline['slots'],slots,event['expected_action'])
                    latest['possible_deltas']=list(proof.possible_deltas)
                    if not proof.accepted:
                        latest['hold_code']=proof.code;self.hold(shelf,event_id)
                        raise ValueError(f'{proof.code}: single net action not proven')
                    latest['action_authorized']=True
                before=baseline['total'] if baseline else None
                accepted=baseline if retained_hidden else latest
                row.update(baseline=accepted,version=row['version']+1,state='Ready',ready_at=self.clock())
                if not count_check:row['events'][event_id]='BASELINE' if source=='STARTUP' else 'ACCEPTED'
                for identity in seen:captures.pop(identity,None)
                return dict(status='success',run_id=self.run_id,version=row['version'],precision=accepted['precision'],
                    occupancy=list(accepted['slots']),count_range=accepted['count_range'],readiness='Ready',snapshot_initialized=source=='STARTUP',
                    count_check=copy.deepcopy(row['count_check']) if count_check else None,
                    comparison=dict(quantity_before=before,quantity_after=accepted['total'],delta=None if source=='STARTUP' or before is None or accepted['total'] is None else accepted['total']-before,
                        state_delta='COUNT_CHECK' if count_check else 'BASELINE' if source=='STARTUP' else 'VERIFIED_RUNTIME_ACTION'))
