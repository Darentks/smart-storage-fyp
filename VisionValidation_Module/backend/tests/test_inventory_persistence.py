"""Persistence transport/state tests. No real database or camera calls."""
import importlib
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]))
persistence=importlib.import_module('src.inventory_persistence')


def state(slots=(1,1,1,1,1),revision=2):
    return dict(shelf_id='s',current_quantity=sum(slots) if slots is not None else 5,
        confirmed_occupancy=list(slots) if slots is not None else None,revision=revision,
        last_verified_at='2026-11-21T00:00:00Z' if slots is not None else None,
        layout_version=persistence.LAYOUT_VERSION if slots is not None else None,
        evidence_version=persistence.VERSION if slots is not None else None)


class PersistenceTests(unittest.TestCase):
    def test_restore_and_legacy_do_not_infer_slots(self):
        legacy=dict(state(None),shelf_id='legacy')
        cache={'stale':(0,0,0,0,0)}
        with patch.object(persistence,'_supabase_rest',return_value=[state(),legacy]):
            self.assertEqual(persistence.restore_inventory_cache(cache),1)
        self.assertEqual(cache,{'s':(1,1,1,1,1)})

    def test_invalid_state_and_missing_migration_rejected(self):
        cases=[{'shelf_id':'s','current_quantity':5},dict(state(),current_quantity=4),
               dict(state(),evidence_version='old'),dict(state(),revision=True),
               dict(state(),confirmed_occupancy=[1,1,1,None,0]),
               dict(state(),confirmed_occupancy=[1,True,1,1,1])]
        for row in cases:
            with self.subTest(row=row),self.assertRaises(persistence.InventoryPersistenceError):
                persistence.checked_state(row)

    def test_fresh_read_never_uses_ram_or_default_quantity(self):
        with patch.object(persistence,'_supabase_rest',return_value=[]):
            with self.assertRaises(persistence.InventoryPersistenceError):persistence.load_inventory_state('s')
        with patch.object(persistence,'_supabase_rest',return_value=[state()]):
            self.assertEqual(persistence.load_inventory_state('s')['revision'],2)

    def test_atomic_rpc_and_uncertain_ack_retry_identical_event(self):
        before=state();after=state((0,1,1,1,1),3)
        response=dict(status='duplicate',state=after,receipt=after)
        with patch.dict(persistence.os.environ,{'SUPABASE_SERVICE_ROLE_KEY':'test-server-only'}),\
             patch.object(persistence,'find_and_load_env'),\
             patch.object(persistence,'_supabase_rest',side_effect=[[],response]) as rest:
            self.assertEqual(persistence.commit_inventory_state(before,(0,1,1,1,1),'BORROW','event-a'),after)
        first,second=rest.call_args_list
        self.assertEqual(first,second)
        self.assertEqual(first.kwargs['table'],'rpc/commit_inventory_observation')
        self.assertEqual(first.kwargs['payload']['p_expected_revision'],2)
        self.assertEqual(first.kwargs['payload']['p_expected_occupancy'],[1,1,1,1,1])
        self.assertEqual(first.kwargs['payload']['p_event_id'],'event-a')
        self.assertEqual(first.kwargs['api_key'],'test-server-only')

    def test_service_secret_required_without_publishable_fallback(self):
        with patch.dict(persistence.os.environ,{'SUPABASE_SERVICE_ROLE_KEY':''}),\
             patch.object(persistence,'find_and_load_env'),patch.object(persistence,'_supabase_rest') as rest:
            with self.assertRaises(persistence.InventoryPersistenceError):
                persistence.commit_inventory_state(state(),(0,1,1,1,1),'BORROW','event-a')
            rest.assert_not_called()

    def test_database_failure_never_confirms_success(self):
        with patch.dict(persistence.os.environ,{'SUPABASE_SERVICE_ROLE_KEY':'test-only'}),\
             patch.object(persistence,'find_and_load_env'),patch.object(persistence,'_supabase_rest',return_value=[]):
            with self.assertRaises(persistence.InventoryPersistenceError):
                persistence.commit_inventory_state(state(),(0,1,1,1,1),'BORROW','event-a')

    def test_unknown_b1_is_not_submitted(self):
        with patch.object(persistence,'_supabase_rest') as rest:
            with self.assertRaises(persistence.InventoryPersistenceError):
                persistence.commit_inventory_state(state(),(0,1,1,None,0),'BORROW','event-a')
            rest.assert_not_called()

    def test_duplicate_after_later_revision_requires_reload(self):
        receipt=state((0,1,1,1,1),3);current=state((0,1,0,1,1),4)
        with patch.dict(persistence.os.environ,{'SUPABASE_SERVICE_ROLE_KEY':'test-only'}),\
             patch.object(persistence,'find_and_load_env'),patch.object(persistence,'_supabase_rest',return_value=dict(status='duplicate',state=current,receipt=receipt)):
            with self.assertRaises(persistence.InventoryPersistenceError):
                persistence.commit_inventory_state(state(),(0,1,1,1,1),'BORROW','event-a')

if __name__=='__main__':unittest.main()
