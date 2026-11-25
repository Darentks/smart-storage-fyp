"""Runtime request orchestration, independent of HTTP and bridge globals.

The inventory module owns RAM atomicity; this adapter owns lease, camera mapping,
consensus selection and readiness responses. It never performs inventory I/O.
"""
from .runtime_inventory import RuntimeInventory
from .validation_diagnostics import failure_code
from .dual_cam_inventory import VERSION
from .shelves import SHELF_IDS, SHELF_SET
from contextlib import nullcontext
import time
from typing import Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from .local_operations import LatestIntents


def discard_runtime_attempt(shelf,token,*,coordinator,consensus_lock,pending,capture_lock,captures,votes=None):
    """Caller owns the coordinator lock; discard evidence before recovery can run."""
    coordinator.end(shelf,token)
    with consensus_lock:
        if votes is not None:votes.clear()
        pending[:]=[event for event in pending if event.get('shelf_id')!=shelf]
    with capture_lock:
        for identity in list(captures):
            if captures[identity].get('shelf_id')==shelf:captures.pop(identity,None)

def reset_runtime_evidence(*,authority,coordinator,consensus_lock,pending,capture_lock,captures,votes,readiness,intents=None,reason='Camera unavailable',preserve_counts=False,disable_actions=False):
    """Invalidate authority and all in-flight evidence as one recovery boundary."""
    with coordinator.global_lock:
        if preserve_counts:authority.interrupt_actions(disable_actions=disable_actions)
        else:authority.invalidate()
        coordinator.leases.clear();coordinator.waiting.clear()
        with consensus_lock:
            abandoned=list(pending)
            pending.clear();votes.clear()
        with capture_lock:captures.clear()
        metadata_error=None
        for sid in set(readiness.states) | set(authority.shelves):
            try:
                if preserve_counts and authority.snapshot(sid)['state']=='Ready':
                    readiness.set(sid,'Ready',reason+'; last count retained')
                else:readiness.set(sid,'Offline',reason+'; fresh quiet baseline required')
            except OSError as exc:metadata_error=exc  # RAM state is already set; continue invalidating other shelves.
        # Disk failure must never prevent invalidating stale evidence/readiness.
        if intents is not None:
            for event in abandoned:
                if event.get('trigger_id'):
                    try:intents.update(event['trigger_id'],'HELD',failure_code(reason))
                    except OSError as exc:metadata_error=exc
        if metadata_error is not None:raise metadata_error

def validate_runtime_request(
    body: dict, *, authority: RuntimeInventory | None, coordinator, mqtt,
    load_mappings: Callable[[], list[dict]], consensus_lock, pending,
    capture_lock, captures, readiness, intents: 'LatestIntents | None'=None,
    votes: dict | None=None,release_freeze: Callable[[str],object] | None=None,consensus_window=4.0,
) -> tuple[int, dict]:
    if authority is None:
        raise RuntimeError('Runtime authority unavailable')
    sid = str(body.get('shelf_id', ''))
    token = body.get('validation_token')
    with coordinator.global_lock:
        if not coordinator.owns(sid, token):
            return (409, {'status': 'VALIDATION_PENDING', 'error': 'Validation lease expired'})
        try:
            source = body.get('source')
            # Initial counts use the ESP32 HTTP pair only; actions also need the MQTT action transport.
            if source not in ('STARTUP','COUNT_CHECK') and not mqtt.is_connected:
                raise ValueError('MQTT unavailable')
            if not body.get('image_back') or not body.get('image_front'):
                raise ValueError('Both camera images required')
            mappings = [row for row in load_mappings() if row.get('shelf_id') == sid]
            if len(mappings) != 1 or not body.get('ip') or (not body.get('ip2')) or (body['ip'] == body['ip2']) or ((body['ip'], body['ip2']) != (mappings[0].get('ip_address'), mappings[0].get('secondary_ip_address'))):
                raise ValueError('Configured paired cameras required')
            event = None
            if source == 'CCTV':
                with consensus_lock:
                    events = [dict(t) for t in pending if t.get('trigger_id') == body.get('trigger_id') and t.get('shelf_id') == sid and (t.get('validation_token') == token) and t.get('claimed') and t.get('consensus_met')]
                if len(events) != 1:
                    raise ValueError('Bound CCTV event required')
                event = events[0]
            evidence = body.get('dual_cam_evidence', {})
            if evidence.get('version') != VERSION:
                raise ValueError('Wrong evidence version')
            samples = evidence.get('samples')
            with (consensus_lock if source=='COUNT_CHECK' else nullcontext()):
                if source=='COUNT_CHECK' and (any(t.get('source')=='CCTV' and 0<=time.time()-t.get('timestamp',0)<60 for t in pending) or
                        any(0<=time.time()-v.get('ts',0)<=consensus_window for v in (votes or {}).values())):
                    return 409,{'status':'deferred','code':'COUNT_CHECK_SKIPPED','error':'CCTV check pending'}
                with capture_lock:
                    for sample in samples if isinstance(samples, list) else []:
                        receipt = captures.get(sample.get('capture', {}).get('id'), {})
                        if (receipt.get('ip1'), receipt.get('ip2')) != (body.get('ip'), body.get('ip2')):
                            raise ValueError('Camera mapping mismatch')
                    result = authority.accept(sid, run_id=body.get('run_id'), expected_version=body.get('expected_version'), event_id=body.get('trigger_id') if event else body.get('runtime_event_id'), source=source, samples=samples, captures=captures, lease=token, event=event, transport_ok=bool(mqtt.is_connected))
            if source!='COUNT_CHECK':readiness.set(sid, 'Ready', 'Fresh runtime ' + result['precision'] + ' observation; no persisted inventory', result['version'])
            if source=='CCTV' and intents is not None:intents.update(body['trigger_id'],'ACCEPTED')
            return (200, result)
        except (ValueError, TypeError, KeyError) as exc:
            reason = str(exc)
            if body.get('source')=='COUNT_CHECK':
                if sid in SHELF_SET:authority.count_check_failed(sid,failure_code(reason),'Count check failed; last count retained.',body.get('expected_version'))
                return 409,{'status':'count_retained','code':failure_code(reason),'error':'Count check failed; last count retained'}
            if sid in SHELF_SET and (not any((k in reason for k in ('Stale run', 'Duplicate/invalid', 'inventory version')))):
                identity=body.get('trigger_id') if body.get('source')=='CCTV' else body.get('runtime_event_id')
                authority.hold(sid,identity)
                readiness.set(sid, 'Offline' if 'unavailable' in reason else 'Held', reason)
                try:
                    if body.get('source')=='CCTV' and intents is not None:intents.update(identity,'HELD',failure_code(reason))
                finally:
                    discard_runtime_attempt(sid,token,coordinator=coordinator,consensus_lock=consensus_lock,
                        pending=pending,capture_lock=capture_lock,captures=captures,votes=votes)
                    if release_freeze is not None:release_freeze(sid)
            return (409, {'status': 'VALIDATION_PENDING', 'error': reason, 'code': failure_code(reason)})
