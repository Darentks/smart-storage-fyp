"""Authoritative confirmed state and idempotent server-only inventory RPC."""
import os
import urllib.parse
from .config_loader import _supabase_rest, find_and_load_env
from .dual_cam_inventory import VERSION

LAYOUT_VERSION = 'five-slot-stacked-v1'
STATE_COLUMNS = ('shelf_id,current_quantity,confirmed_occupancy,last_verified_at,'
                 'layout_version,evidence_version,revision')


class InventoryPersistenceError(ValueError):
    pass


def checked_state(row):
    if not isinstance(row, dict) or not set(STATE_COLUMNS.split(',')).issubset(row):
        raise InventoryPersistenceError('Persistent inventory schema unavailable; apply the Phase 1 migration')
    quantity, revision = row['current_quantity'], row['revision']
    if type(quantity) is not int or quantity < 0 or type(revision) is not int or revision < 0:
        raise InventoryPersistenceError('Invalid authoritative inventory state')
    slots = row['confirmed_occupancy']
    if slots is None:
        if any(row[key] is not None for key in ('last_verified_at','layout_version','evidence_version')):
            raise InventoryPersistenceError('Incomplete confirmed state requires reconciliation')
    else:
        if (not isinstance(slots, list) or len(slots) != 5
                or any(type(bit) is not int or bit not in (0,1) for bit in slots)
                or slots[2] > slots[1] or slots[4] > slots[3] or sum(slots) != quantity
                or not row['last_verified_at'] or row['layout_version'] != LAYOUT_VERSION
                or row['evidence_version'] != VERSION):
            raise InventoryPersistenceError('Confirmed slots/version disagree with stored quantity; reconcile inventory')
    return dict(row)


def load_inventory_state(shelf_id):
    rows = _supabase_rest(table='compartments', query=f'?select={STATE_COLUMNS}&shelf_id=eq.{urllib.parse.quote(shelf_id)}&limit=1')
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('shelf_id') != shelf_id:
        raise InventoryPersistenceError('Fresh persistent inventory unavailable; no update applied')
    return checked_state(rows[0])


def restore_inventory_cache(cache):
    """No fallback from default quantities or stale RAM if startup reads fail."""
    cache.clear()
    rows = _supabase_rest(table='compartments', query=f'?select={STATE_COLUMNS}')
    if not isinstance(rows, list):
        raise InventoryPersistenceError('Persistent inventory unavailable at startup')
    restored = {}
    for row in rows:
        state = checked_state(row)
        if state['confirmed_occupancy'] is not None:
            restored[state['shelf_id']] = tuple(state['confirmed_occupancy'])
    cache.update(restored)
    return len(cache)


def commit_inventory_state(before, slots, action, event_id):
    before = checked_state(before)
    # Do not serialize an unknown bit, bool, or guessed total into a commit.
    proposed = dict(before, confirmed_occupancy=list(slots), current_quantity=sum(slots) if None not in slots else -1,
                    last_verified_at='pending-server-verification',layout_version=LAYOUT_VERSION,evidence_version=VERSION)
    checked_state(proposed)
    find_and_load_env()
    key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '').strip()
    if not key:
        raise InventoryPersistenceError('Server-only SUPABASE_SERVICE_ROLE_KEY is required for inventory commits')
    payload = dict(p_shelf_id=before['shelf_id'], p_event_id=event_id,
                   p_expected_revision=before['revision'], p_expected_quantity=before['current_quantity'],
                   p_expected_occupancy=before['confirmed_occupancy'], p_occupancy=list(slots),
                   p_action=action, p_layout_version=LAYOUT_VERSION,p_evidence_version=VERSION)
    # An uncertain HTTP acknowledgement retries the IDENTICAL RPC/event request.
    # The database receipt makes this safe even if the first request committed.
    for _ in range(2):
        result = _supabase_rest(method='POST',table='rpc/commit_inventory_observation',payload=payload,api_key=key)
        if isinstance(result,dict) and result.get('status') in ('accepted','duplicate'):
            state, receipt = checked_state(result.get('state')), checked_state(result.get('receipt'))
            if (receipt['shelf_id'] != before['shelf_id'] or receipt['revision'] != before['revision']+1
                    or receipt['confirmed_occupancy'] != list(slots)
                    or state != receipt):
                raise InventoryPersistenceError('Inventory changed after this event; reload and reconcile')
            return state
    raise InventoryPersistenceError('Atomic inventory commit not confirmed; migration, credentials, revision or database availability must be checked')


def operator_rpc(authorization, operation, payload):
    if operation not in ('operator_reconciliation_context','reconcile_inventory'):
        raise PermissionError('Invalid operator operation')
    if not authorization or not authorization.startswith('Bearer '):
        raise PermissionError('Operator sign-in required')
    token=authorization[7:].strip()
    if not token: raise PermissionError('Operator sign-in required')
    for _ in range(2 if operation=='reconcile_inventory' else 1):
        result=_supabase_rest(method='POST',table='rpc/'+operation,payload=payload,bearer_token=token)
        if isinstance(result,dict) and result:return result
    raise PermissionError('Operator access denied, stale request, or reconciliation RPC unavailable; refresh authoritative state before retrying')
