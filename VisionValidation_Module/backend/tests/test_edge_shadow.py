import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from src.edge_shadow import EdgeShadow, processor_mode


class EdgeShadowTests(unittest.TestCase):
    def test_single_authority_and_cutover_fail_closed(self):
        self.assertEqual(processor_mode(None), 'browser')
        self.assertEqual(processor_mode('shadow'), 'shadow')
        with self.assertRaisesRegex(RuntimeError, 'SERVICE_CUTOVER_BLOCKED'):
            processor_mode('service')
        with self.assertRaises(ValueError): processor_mode('auto')
        worker=EdgeShadow('.', 'run')
        worker.start()
        self.assertIsNone(worker.thread)
        self.assertFalse(worker.snapshot()['authorizes_write'])
        self.assertEqual(worker.snapshot()['authoritative_processor'],'browser')

    def test_missing_models_are_held_without_affecting_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=EdgeShadow(directory,'run','shadow')
            worker.start()
            self.assertTrue(worker.state.startswith('Held'))
            self.assertIsNone(worker.proc)

    def test_stale_duplicate_and_out_of_order_frames_rejected(self):
        worker=EdgeShadow('.','run','shadow')
        now=time.time()
        self.assertFalse(worker.submit('pose',identity='stale',observed_at=now-2,image='image'))
        self.assertFalse(worker.submit('pose',identity='future',observed_at=now+3,image='image'))
        self.assertTrue(worker.submit('pose',identity='first',observed_at=now,camera_id='CCTV_01',image='image'))
        self.assertFalse(worker.submit('pose',identity='first',observed_at=now,camera_id='CCTV_01',image='image'))
        self.assertFalse(worker.submit('pose',identity='older',observed_at=now-.1,camera_id='CCTV_01',image='image'))
        self.assertEqual(worker.snapshot()['queue_depth'],1)

    def test_queue_bounded_and_pair_priority(self):
        worker=EdgeShadow('.','run','shadow')
        for i in range(250):
            self.assertTrue(worker.submit('pose',identity=str(i),observed_at=time.time(),image='image'))
        self.assertLessEqual(len(worker.seen),200)
        self.assertEqual(worker.snapshot()['queue_depth'],1)
        self.assertTrue(worker.submit('pair',identity='pair',observed_at=time.time(),image_back='back',image_front='front'))
        self.assertFalse(worker.submit('pose',identity='last',observed_at=time.time(),image='image'))
        self.assertEqual(worker.pending['kind'],'pair')
        worker.close()
        self.assertIsNone(worker.pending)
        self.assertFalse(worker.submit('pose',identity='after-stop',observed_at=time.time(),image='image'))

    def test_secret_environment_not_inherited(self):
        worker=EdgeShadow('.','run','shadow')
        proc=Mock();proc.stdout.readline.return_value='';proc.poll.return_value=0
        with patch.dict(os.environ, {'SUPABASE_SERVICE_ROLE_KEY':'secret','MQTT_PASSWORD':'secret'}), patch('src.edge_shadow.subprocess.Popen',return_value=proc) as spawn:
            worker._spawn()
        env=spawn.call_args.kwargs['env']
        self.assertNotIn('SUPABASE_SERVICE_ROLE_KEY',env)
        self.assertNotIn('MQTT_PASSWORD',env)
        self.assertEqual(spawn.call_args.kwargs['stderr'],-3)

    def test_wrong_startup_or_late_reply_never_retained(self):
        for reply_run,expired in [('old',False),('run',True)]:
            worker=EdgeShadow('.','run','shadow')
            worker.submit('pair',identity='sample',observed_at=time.time(),image_back='back',image_front='front')
            job=worker.pending
            clock=[time.time()]
            proc=Mock();proc.poll.return_value=None;replies=queue.Queue()
            replies.put({'ready':True,'versions':{}})
            def write(_):
                if expired: clock[0]+=20
                replies.put({'id':job['id'],'startup_id':reply_run,'result':{'metrics':{}}})
            proc.stdin.write.side_effect=write
            def spawn():worker.proc=proc;return replies
            worker._spawn=spawn
            def stop():worker.stop_event.set()
            # The loop gets one reply, then waits. Stop on its next condition wait.
            worker.condition.wait=Mock(side_effect=lambda **_:stop())
            worker._stop_child=Mock()
            with patch('src.edge_shadow.time.time',side_effect=lambda:clock[0]):worker._run()
            self.assertEqual(worker.processed,0)
            self.assertEqual(len(worker.latest),0)
            self.assertEqual(worker.dropped,1)

    def test_worker_failure_has_finite_restart_budget(self):
        worker=EdgeShadow('.','run','shadow')
        worker._spawn=Mock(side_effect=OSError('secret error should not leak'))
        worker.stop_event.wait=Mock(return_value=False)
        worker._run()
        self.assertEqual(worker._spawn.call_count,3)
        self.assertTrue(worker.state.startswith('Held'))
        self.assertNotIn('secret',str(worker.snapshot()))


if __name__=='__main__':unittest.main()
