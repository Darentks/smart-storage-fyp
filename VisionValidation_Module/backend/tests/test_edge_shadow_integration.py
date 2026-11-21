"""Opt-in real Node worker IPC/lifecycle test, without cameras or Supabase."""
import base64
import os
from pathlib import Path
import struct
import sys
import time
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).parents[1]))
from src.edge_shadow import EdgeShadow


@unittest.skipUnless(os.getenv('EDGE_TEST_MANIFEST'), 'Set EDGE_TEST_MANIFEST to staged native models')
class NativeWorkerIntegrationTests(unittest.TestCase):
    def test_real_worker_pair_and_shutdown_without_any_database_client(self):
        def chunk(kind, payload):
            return struct.pack('!I',len(payload))+kind+payload+struct.pack('!I',zlib.crc32(kind+payload))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',32,24,8,2,0,0,0))
        png+=chunk(b'IDAT',zlib.compress((b'\x00'+b'\x64\x54\x44'*32)*24))+chunk(b'IEND',b'')
        image='data:image/png;base64,'+base64.b64encode(png).decode()
        root=Path(__file__).resolve().parents[3]
        worker=EdgeShadow(root,'integration-run','shadow',manifest=os.environ['EDGE_TEST_MANIFEST'])
        worker.start()
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline and worker.state!='Shadow only':time.sleep(.05)
            self.assertEqual(worker.state,'Shadow only')
            self.assertTrue(worker.submit('pair',identity='capture',observed_at=time.time(),shelf_id='shelf',image_back=image,image_front=image,boost='off'))
            deadline=time.monotonic()+10
            while time.monotonic()<deadline and worker.processed<1:time.sleep(.05)
            status=worker.snapshot()
            self.assertEqual(status['processed'],1)
            self.assertEqual(status['latest'][0]['shelf_id'],'shelf')
            self.assertFalse(status['latest'][0]['candidate']['authorizes_write'])
            self.assertFalse(status['authorizes_write'])
            self.assertNotIn('data:image',str(status))
        finally:worker.close()
        self.assertFalse(worker.thread.is_alive())
        self.assertIsNone(worker.proc)


if __name__=='__main__':unittest.main()
