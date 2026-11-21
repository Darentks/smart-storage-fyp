import ast
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock,patch
import urllib.parse
sys.path.insert(0,str(Path(__file__).parents[1]))
from src.local_operations import LatestIntents,RoiStore,LocalFailures,CloudMetrics,atomic_json,validate_rois

def rois():
    cameras=[]
    for i in range(1,4):
        cid=f'CCTV_0{i}';role='SIDE_PRESENCE' if i==3 else 'GRID_6_ROI'
        grid={f'SHELF_R1_C{j+1}':dict(x_min=j/6,x_max=(j+1)/6,y_min=0,y_max=1) for j in range(6)}
        cameras.append(dict(cctv_id=cid,camera_role=role,binding=dict(source=f'rtsp://192.168.0.{i}:554/stream',camera_role=role),
            rois={'RACK_ENTRY_ZONE':dict(x_min=0,x_max=1,y_min=0,y_max=1)} if i==3 else grid))
    return dict(version=1,cameras=cameras)

class LocalStoreTests(unittest.TestCase):
    def test_display_cache_changes_only_after_confirmed_commit(self):
        from test_periodic_recheck import ValidationRouteTests
        for failure in (False,True):
            fixture=ValidationRouteTests();fixture.setUp()
            row={'shelf_id':'s','current_quantity':fixture.quantity}
            fixture.ns['SUPABASE_COMPARTMENTS_CACHE']={'data':[row]}
            before=row['current_quantity']
            if failure:fixture.ns['commit_inventory_state']=Mock(side_effect=ValueError('Unknown database acknowledgment'))
            fixture.route(fixture.handler)
            if failure:self.assertEqual(row['current_quantity'],before)
            else:self.assertEqual(row['current_quantity'],fixture.response[1]['comparison']['quantity_after'])
    def test_phase6_release_cannot_be_approved_around_storage_blocker(self):
        from src.autonomous_edge import load_service_config
        with patch.dict('os.environ',{'SMART_STORAGE_RELEASE_PHASE':'6'}):
            with self.assertRaisesRegex(ValueError,'PHASE6_RELEASE_BLOCKED'):load_service_config('unused','unused','unused',[])
    def test_unresolved_failure_survives_restart_and_only_ready_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'failures';store=LocalFailures(path)
            store.set('SHELF_R1_C1','Held','UNKNOWN_B1');store.set('SHELF_R1_C1','Starting',None)
            self.assertEqual(LocalFailures(path).snapshot()['SHELF_R1_C1']['code'],'UNKNOWN_B1')
            store.set('SHELF_R1_C1','Ready',None);self.assertEqual(LocalFailures(path).snapshot(),{})

    def test_real_rest_helper_instruments_mocked_transport_bytes(self):
        from src import config_loader
        from src.local_operations import CLOUD_METRICS
        response=io.BytesIO(b'[]');response.status=200
        before=CLOUD_METRICS.snapshot()
        with patch.object(config_loader,'get_resolved_supabase_url',return_value='https://fixture.invalid'),patch.object(config_loader,'get_resolved_supabase_key',return_value='fixture'),patch.object(config_loader.urllib.request,'urlopen',return_value=response):
            self.assertEqual(config_loader._supabase_rest(table='compartments',query='?select=current_quantity'),[])
        after=CLOUD_METRICS.snapshot()
        self.assertEqual(after['calls']-before['calls'],1);self.assertEqual(after['response_bytes']-before['response_bytes'],2)
        self.assertGreater(after['request_bytes_estimate']-before['request_bytes_estimate'],0)
    def test_global_five_survive_restart_and_update_without_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'intents.json';store=LatestIntents(path)
            for i in range(12):store.add(dict(id=str(i),timestamp=i,camera_id=f'CCTV_0{i%3+1}',shelf_id='SHELF_R1_C1',action='BORROW',status='PENDING',image='secret'))
            self.assertEqual([r['id'] for r in LatestIntents(path).snapshot()],list(map(str,range(7,12))))
            store.update('11','HELD','UNKNOWN_B1');self.assertEqual(len(store.snapshot()),5)
            self.assertEqual(LatestIntents(path).snapshot()[-1]['reason'],'UNKNOWN_B1')
            self.assertNotIn('secret',path.read_text());self.assertEqual(len(list(Path(directory).iterdir())),1)

    def test_atomic_failure_preserves_previous_and_corruption_holds(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state';atomic_json(path,{'version':1,'records':[]})
            before=path.read_bytes()
            with patch('src.local_operations.os.replace',side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):atomic_json(path,{'new':True})
            self.assertEqual(path.read_bytes(),before);self.assertEqual(len(list(Path(directory).iterdir())),1)
            path.write_text('{corrupt')
            with self.assertRaises(ValueError):LatestIntents(path)

    def test_startup_trims_and_concurrent_insert_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state';atomic_json(path,{'version':1,'records':[dict(id=str(i),timestamp=i,status='PENDING') for i in range(8)]})
            store=LatestIntents(path);self.assertEqual(len(store.snapshot()),5)
            threads=[threading.Thread(target=store.add,args=(dict(id=str(i),timestamp=i,status='PENDING'),)) for i in range(30)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
            self.assertEqual(len(LatestIntents(path).snapshot()),5)

    def test_roi_migration_isolation_reboot_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RoiStore(Path(directory)/'rois');value=rois();original=store.import_config(value)
            value['cameras'][2]['rois']['RACK_ENTRY_ZONE']['x_min']=.1
            edited=store.import_config(value)
            self.assertEqual(original['cameras'][:2],edited['cameras'][:2])
            self.assertEqual(RoiStore(store.path).load(),edited)
            self.assertEqual(json.loads(Path(str(store.path)+'.previous').read_text()),original)
            # Normalized coordinates remain invariant across input resolutions.
            self.assertEqual(edited['cameras'][0]['rois'],original['cameras'][0]['rois'])

    def test_invalid_swapped_duplicate_missing_overlapping_and_pixel_rois(self):
        value=rois();value['cameras'].reverse();self.assertEqual(validate_rois(value)['cameras'][0]['cctv_id'],'CCTV_01')
        for kind in ('duplicate','missing','pixel','overlap','secret'):
            value=rois()
            if kind=='duplicate':value['cameras'][1]=value['cameras'][0]
            if kind=='missing':value['cameras'].pop()
            if kind=='pixel':value['cameras'][0]['rois']['SHELF_R1_C1']['coordinate_space']='pixels'
            if kind=='overlap':value['cameras'][0]['rois']['SHELF_R1_C2']=value['cameras'][0]['rois']['SHELF_R1_C1']
            if kind=='secret':value['cameras'][0]['binding']['source']='rtsp://user:password@192.168.0.1:554/stream'
            with self.assertRaises((ValueError,KeyError)):validate_rois(value)

    def test_request_metrics_count_bytes_and_failures_without_payloads(self):
        metrics=CloudMetrics();metrics.record('GET',100,250,True);metrics.record('PATCH',200,0,False)
        self.assertEqual(metrics.snapshot(),dict(calls=2,reads=1,writes=1,request_bytes_estimate=300,response_bytes=250,failures=1,cache_hits=0))

    def test_status_polling_has_zero_inventory_reads(self):
        source=Path(__file__).parents[2]/'camera_service.py';tree=ast.parse(source.read_text(encoding='utf8'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='do_GET')
        load=Mock(side_effect=AssertionError('Monitoring must not read cloud state'))
        ns=dict(urllib=types.SimpleNamespace(parse=urllib.parse),READINESS=types.SimpleNamespace(run_id='test',get=lambda s:dict(state='Ready',revision=1),set=Mock()),
            PROCESSOR_AUTHORITY='browser',get_cached_compartments=lambda:[dict(shelf_id=f's{i}') for i in range(6)],
            mqtt_bridge=types.SimpleNamespace(is_connected=True),RECHECKS=types.SimpleNamespace(busy=lambda s:False),load_inventory_state=load)
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),ns)
        handler=types.SimpleNamespace(path='/shelf_readiness',_send_json=Mock())
        for _ in range(10):ns['do_GET'](handler)
        load.assert_not_called();self.assertEqual(handler._send_json.call_count,10)

if __name__=='__main__':unittest.main()
