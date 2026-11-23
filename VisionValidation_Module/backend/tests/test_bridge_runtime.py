import importlib.util
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).parents[1]))
from src.bridge_runtime import InstanceLock,configure_logs,event,LOG
from src.inventory_persistence import operator_rpc

class RuntimeTests(unittest.TestCase):
    def test_second_process_denied_and_crash_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock=str(Path(directory)/'bridge.lock')
            code="from src.bridge_runtime import InstanceLock; import sys,time; lock=InstanceLock(sys.argv[1]);lock.__enter__();print('locked',flush=True);time.sleep(60)"
            import os
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]))
            child=subprocess.Popen([sys.executable,'-B','-c',code,lock],stdout=subprocess.PIPE,env=env,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'locked')
                with self.assertRaises(RuntimeError):
                    with InstanceLock(lock):pass
            finally:child.kill();child.wait();child.stdout.close()
            with InstanceLock(lock):pass

    def test_rotating_logs_do_not_serialize_secrets(self):
        handlers=LOG.handlers[:]
        with tempfile.TemporaryDirectory() as directory:
            try:
                configure_logs(directory)
                LOG.handlers[0].maxBytes=20000
                for _ in range(1000):event('run','failure',shelf_id='s',code='DATABASE_FAILURE',token='SECRET',image='SECRET')
                event('run','failure',state='rtsp://user:SECRET@host/stream',status='data:image/jpeg;base64,SECRET',code='SECRET')
                event('run','data:image/jpeg;base64,SECRET',code='DATABASE_FAILURE')
                files=list(Path(directory).glob('bridge.jsonl*'))
                self.assertGreater(len(files),1)
                self.assertLessEqual(len(files),6)
                self.assertNotIn('SECRET',''.join(p.read_text() for p in files))
                self.assertFalse(LOG.propagate)
            finally:
                for handler in LOG.handlers:handler.close()
                LOG.handlers=handlers

    def test_supervisor_clean_exit_and_finite_crash_backoff(self):
        script=Path(__file__).parents[3]/'deploy/run_bridge.py'
        spec=importlib.util.spec_from_file_location('bridge_supervisor',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.subprocess,'Popen',return_value=Mock(wait=Mock(return_value=0))) as launch:
            self.assertEqual(module.main(),0);self.assertEqual(launch.call_count,1)
        with patch.object(module.subprocess,'Popen',return_value=Mock(wait=Mock(return_value=1))) as launch,patch.object(module.time,'sleep') as sleep:
            self.assertEqual(module.main(),1);self.assertEqual(launch.call_count,5)
            self.assertEqual([x.args[0] for x in sleep.call_args_list],[2,4,8,16])

    def test_operator_calls_forward_user_jwt_not_identity_claims(self):
        with patch('src.inventory_persistence._supabase_rest',return_value={'shelf_id':'s'}) as rest:
            operator_rpc('Bearer user-session-jwt','operator_reconciliation_context',{'p_shelf_id':'s'})
            self.assertEqual(rest.call_args.kwargs['bearer_token'],'user-session-jwt')
        with self.assertRaises(PermissionError):operator_rpc(None,'reconcile_inventory',{})

if __name__=='__main__':unittest.main()
