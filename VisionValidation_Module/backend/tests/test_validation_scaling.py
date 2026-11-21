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

    def test_periodic_dedup_includes_claimed_work_and_live_triggers(self):
        pending=[]
        for index in range(10):periodic.queue_periodic_trigger(pending,'s',5,100+index)
        self.assertEqual(len(pending),1)
        pending[0]['claimed']=True
        self.assertFalse(periodic.queue_periodic_trigger(pending,'s',6,110))
        pending=[dict(shelf_id='s',source='CCTV',timestamp=100)]
        self.assertFalse(periodic.queue_periodic_trigger(pending,'s',6,110))
        self.assertTrue(periodic.queue_periodic_trigger(pending,'s',6,161))
        self.assertEqual(len(pending),1)
        self.assertFalse(pending[0]['consensus_met'])

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


class StaggerTests(unittest.TestCase):
    def test_one_three_and_six_shelf_spacing(self):
        for count in [1,3,6]:
            schedule=periodic.StaggeredPollSchedule()
            shelves=['s'+str(i) for i in range(count)]
            step=30/count
            for i in range(count*2):
                self.assertEqual(schedule.take(shelves,i*step),shelves[i%count])
                self.assertIsNone(schedule.take(shelves,i*step+.1))

    def test_slow_capture_never_produces_catchup_burst(self):
        schedule=periodic.StaggeredPollSchedule()
        self.assertEqual(schedule.take(['a','b','c'],0),'a')
        self.assertEqual(schedule.take(['a','b','c'],100),'b')
        self.assertIsNone(schedule.take(['a','b','c'],100.1))
        self.assertEqual(schedule.take(['a','b','c'],110),'c')

    def test_three_shelf_filter_omits_other_cameras_and_missing_pairs(self):
        rows=[dict(shelf_id='s'+str(i),ip_address='back'+str(i),secondary_ip_address='front'+str(i)) for i in range(6)]
        rows.append(dict(shelf_id='missing',ip_address='back'))
        ns=dict(os=os,get_cached_compartments=Mock(return_value=rows),ACTIVE_ESP32_IPS={})
        fn=existing.isolated_function(existing.method(existing.ROOT/'VisionValidation_Module/camera_service.py','periodic_shelf_mappings'),ns)
        with patch.dict(os.environ,{'PERIODIC_RECHECK_SHELVES':'s0,s1,s2'}):
            self.assertEqual(set(fn()),{'s0','s1','s2'})
        with patch.dict(os.environ,{'PERIODIC_RECHECK_SHELVES':''}):
            self.assertEqual(len(fn()),6)

    def test_offline_pair_never_captured_online_pair_deduplicates(self):
        response=MagicMock()
        response.__enter__.return_value.status=200
        response.__enter__.return_value.read.return_value=b'{"rssi":-60}'
        network=Mock()
        network.request.Request=urllib.request.Request
        network.request.urlopen=Mock(side_effect=[OSError('offline'),response])
        inspector=Mock(return_value=5)
        ns=dict(time=time,urllib=network,json=json,mqtt_bridge=types.SimpleNamespace(edge_nodes={}),
            record_probe_result=Mock(),RECHECKS=periodic.RecheckCoordinator(),
            CCTV_CONSENSUS_LOCK=existing.threading.RLock(),PENDING_CCTV_TRIGGERS=[],
            has_pending_shelf=periodic.has_pending_shelf,queue_periodic_trigger=periodic.queue_periodic_trigger,
            inspect_candidate=inspector,esp32_ref_manager=Mock(),PERIODIC_DIFF_THRESHOLD=3.5)
        fn=existing.isolated_function(existing.method(existing.ROOT/'VisionValidation_Module/camera_service.py','poll_periodic_shelf'),ns)
        fn('s',{'ip1':'192.168.0.201','ip2':'192.168.0.202'})
        inspector.assert_not_called()
        self.assertEqual(network.request.urlopen.call_count,2)
        self.assertTrue(all(call.kwargs['timeout']==1.5 for call in network.request.urlopen.call_args_list))
        network.request.urlopen.side_effect=None
        network.request.urlopen.return_value=response
        fn('s',{'ip1':'192.168.0.201','ip2':'192.168.0.202'})
        fn('s',{'ip1':'192.168.0.201','ip2':'192.168.0.202'})
        inspector.assert_called_once()
        self.assertEqual(len(ns['PENDING_CCTV_TRIGGERS']),1)


if __name__=='__main__':unittest.main()
