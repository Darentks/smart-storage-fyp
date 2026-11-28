"""Scaling checks against production scheduling helpers and extracted poller functions."""
import os
import types
import unittest
from unittest.mock import Mock, MagicMock, patch
import urllib.request
import json
import time
import test_periodic_recheck as existing

periodic = existing.periodic


class GlobalQueueTests(unittest.TestCase):
    def test_one_lease_across_different_shelves(self):
        queue=periodic.RecheckCoordinator()
        token=queue.begin('s1')
        self.assertIsNone(queue.begin('s2'))
        self.assertEqual(len(queue.leases),1)
        queue.end('s1',token)
        self.assertIsNotNone(queue.begin('s2'))

    def test_live_and_manual_precede_older_periodic_waiters(self):
        queue=periodic.RecheckCoordinator()
        token=queue.begin('active')
        self.assertIsNone(queue.begin('backup','PERIODIC_RECHECK','backup',1))
        self.assertIsNone(queue.begin('live','CCTV','live',3))
        self.assertIsNone(queue.begin('manual','MANUAL','manual',2))
        queue.end('active',token)
        self.assertIsNone(queue.begin('backup','PERIODIC_RECHECK','backup',1))
        self.assertIsNone(queue.begin('live','CCTV','live',3))
        manual=queue.begin('manual','MANUAL','manual',2)
        self.assertIsNotNone(manual)
        queue.end('manual',manual)
        live=queue.begin('live','CCTV','live',3)
        self.assertIsNotNone(live)
        queue.end('live',live)
        self.assertIsNotNone(queue.begin('backup','PERIODIC_RECHECK','backup',1))

    def test_waiters_bounded_cancelled_and_expire(self):
        queue=periodic.RecheckCoordinator()
        with patch.object(periodic.time,'time',return_value=100):
            queue.begin('active')
            for index in range(100):queue.begin(str(index),request_id=str(index))
            self.assertEqual(len(queue.waiting),20)
            queue.cancel('0')
            self.assertEqual(len(queue.waiting),19)
        with patch.object(periodic.time,'time',return_value=221):
            self.assertFalse(queue.busy())
            self.assertEqual(len(queue.waiting),0)

    def test_renew_requires_owner_and_extends_global_lease(self):
        queue=periodic.RecheckCoordinator()
        with patch.object(periodic.time,'time',return_value=100):token=queue.begin('s')
        with patch.object(periodic.time,'time',return_value=200):
            self.assertFalse(queue.renew('s','wrong'))
            self.assertTrue(queue.renew('s',token))
        with patch.object(periodic.time,'time',return_value=230):
            self.assertTrue(queue.owns('s',token))
            self.assertIsNone(queue.begin('other'))


    def test_legacy_other_shelf_write_blocked_by_global_lease(self):
        route=existing.ValidationRouteTests()
        route.setUp()
        route.body=dict(shelf_id='other',predicted_box_count=3)
        route.route(route.handler)
        self.assertEqual(route.response[0],409)
        route.rest.select.assert_not_called()

    def test_bridge_routes_register_live_priority_and_release_global_slot(self):
        route=existing.ValidationRouteTests()
        route.setUp()
        route.coordinator.end('s',route.token)
        def call(path,body):
            route.handler.path=path
            route.body=body
            route.route(route.handler)
            self.assertEqual(route.response[0],200)
            return route.response[1]
        self.assertTrue(call('/validation_enqueue',dict(shelf_id='live',source='CCTV',request_id='l',created_at=2))['queued'])
        backup=dict(shelf_id='backup',source='PERIODIC_RECHECK',request_id='b',created_at=1)
        self.assertIsNone(call('/validation_begin',backup)['token'])
        live=call('/validation_begin',dict(shelf_id='live',source='CCTV',request_id='l',created_at=2))['token']
        self.assertIsNotNone(live)
        self.assertIsNone(call('/validation_begin',backup)['token'])
        self.assertTrue(call('/validation_end',dict(shelf_id='live',token=live))['released'])
        self.assertIsNotNone(call('/validation_begin',backup)['token'])

    def test_unclaimed_live_camera_event_blocks_periodic_admission(self):
        route=existing.ValidationRouteTests()
        route.setUp()
        route.coordinator.end('s',route.token)
        route.ns['PENDING_CCTV_TRIGGERS'].append(dict(source='CCTV',timestamp=time.time(),claimed=False))
        route.handler.path='/validation_begin'
        route.body=dict(shelf_id='backup',source='PERIODIC_RECHECK',request_id='b')
        route.route(route.handler)
        self.assertIsNone(route.response[1]['token'])
        self.assertFalse(route.coordinator.busy())




if __name__=='__main__':unittest.main()
