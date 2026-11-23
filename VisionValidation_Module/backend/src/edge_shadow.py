"""Managed, passive native inference. No lease, consensus, readiness or database access.

The browser remains the sole authoritative processor. Service cutover is deliberately
unavailable until calibration, action-tracker parity and hardware acceptance exist.
"""
from collections import OrderedDict, deque
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid


def processor_mode(value):
    mode = str(value or 'browser').strip().lower()
    if mode not in {'browser', 'shadow', 'service'}:
        raise ValueError('SMART_STORAGE_PROCESSOR_MODE must be browser, shadow or service')
    if mode == 'service':
        raise RuntimeError('SERVICE_CUTOVER_BLOCKED: native action tracking, calibration and hardware parity acceptance required; use browser or shadow')
    return mode


class EdgeShadow:
    def __init__(self, root, startup_id, mode='browser', manifest=None, node='node', log=None):
        self.mode = processor_mode(mode)
        self.root = Path(root).resolve()
        self.startup_id = startup_id
        self.manifest = str(Path(manifest).resolve() if manifest else self.root/'.runtime/edge-models/manifest.json')
        self.node = node
        self.log = log or (lambda *args, **kwargs: None)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.pending = None
        self.proc = None
        self.thread = None
        self.seen = OrderedDict()
        self.last_observed = OrderedDict()
        self.latest = deque(maxlen=10)
        self.state = 'Disabled' if self.mode == 'browser' else 'Starting'
        self.failures = self.dropped = self.processed = 0
        self.versions = {}
        self.in_flight = False

    def start(self):
        if self.mode != 'shadow':
            return
        if not Path(self.manifest).is_file():
            self.state = 'Held: model manifest missing'
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name='EdgeShadow')
        self.thread.start()

    def submit(self, kind, *, identity, observed_at, shelf_id=None, camera_id=None, **images):
        """Copy a server-acquired frame; never cause additional camera/flash traffic."""
        if self.mode != 'shadow' or self.stop_event.is_set() or self.state.startswith('Held'):
            return False
        ttl = 1.5 if kind == 'pose' else 15.0
        now = time.time()
        if kind not in {'pose', 'pair'} or not isinstance(identity, str) or not 0 <= now-observed_at < ttl:
            return False
        allowed = ('image',) if kind == 'pose' else ('image_back', 'image_front')
        if any(not isinstance(images.get(key), str) or len(images[key]) > 8_000_000 for key in allowed):
            return False
        with self.condition:
            if identity in self.seen:
                return False
            stream = (kind, camera_id or shelf_id or identity)
            if observed_at <= self.last_observed.get(stream, float('-inf')):
                return False
            # Latest-only queue, with occupancy pairs taking priority over pose frames.
            if self.pending and self.pending['kind'] == 'pair' and kind == 'pose':
                self.dropped += 1
                return False
            if self.pending:
                self.dropped += 1
            self.seen[identity] = True
            self.last_observed[stream] = observed_at
            while len(self.seen) > 200:
                self.seen.popitem(last=False)
            while len(self.last_observed) > 200:
                self.last_observed.popitem(last=False)
            self.pending = dict(kind=kind, id=str(uuid.uuid4()), startup_id=self.startup_id,
                expires_at=observed_at+ttl, shelf_id=shelf_id, camera_id=camera_id,
                **{key:images[key] for key in allowed})
            if kind == 'pair':
                self.pending['boost'] = images.get('boost', 'off')
            self.condition.notify()
        return True

    def snapshot(self):
        with self.lock:
            return dict(mode=self.mode, authoritative_processor='none', inventory_mode='runtime', service_cutover_available=False,
                startup_id=self.startup_id, state=self.state, queue_depth=int(self.pending is not None),
                in_flight=self.in_flight, processed=self.processed, dropped=self.dropped,
                restart_count=self.failures, model_versions=self.versions, latest=list(self.latest),
                authorizes_write=False)

    def _spawn(self):
        # Do not pass Supabase, MQTT, camera credentials or other application secrets.
        allowed = {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','HOME','USERPROFILE','LOCALAPPDATA'}
        env = {k:v for k,v in os.environ.items() if k.upper() in allowed}
        proc = subprocess.Popen([self.node, str(self.root/'VisionValidation_Module/edge_service/worker.cjs'), self.manifest],
            cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.stdout is None or proc.stdin is None:
            raise RuntimeError('WORKER_PIPES_UNAVAILABLE')
        self.proc = proc
        stdout = proc.stdout
        replies = queue.Queue(maxsize=2)
        def read():
            try:
                while True:
                    line = stdout.readline(4_000_001)
                    if not line or len(line) > 4_000_000:
                        break
                    replies.put_nowait(json.loads(line))
            except (ValueError, queue.Full, OSError):
                pass
            finally:
                try: replies.put_nowait({'error':'WORKER_EXIT'})
                except queue.Full: pass
        threading.Thread(target=read, daemon=True, name='EdgeShadowReplies').start()
        return replies

    def _reply(self, replies, timeout):
        deadline = time.monotonic()+timeout
        while not self.stop_event.is_set() and time.monotonic()<deadline:
            try: return replies.get(timeout=min(.2, max(.001, deadline-time.monotonic())))
            except queue.Empty: pass
        raise TimeoutError('WORKER_TIMEOUT')

    def _run(self):
        while not self.stop_event.is_set() and self.failures < 3:
            try:
                self.state = 'Loading models'
                replies = self._spawn()
                ready = self._reply(replies, 60)
                if not ready.get('ready'):
                    raise RuntimeError('MODEL_INITIALIZATION_FAILED')
                self.versions = ready['versions']
                self.state = 'Shadow only'
                while not self.stop_event.is_set():
                    proc = self.proc
                    if proc is None or proc.stdin is None or proc.poll() is not None:
                        raise RuntimeError('WORKER_EXIT')
                    with self.condition:
                        if self.pending is None:
                            self.condition.wait(timeout=.5)
                            continue
                        job, self.pending = self.pending, None
                    expires_at = job.get('expires_at')
                    if not isinstance(expires_at, (int, float)) or time.time() > expires_at:
                        self.dropped += 1
                        continue
                    self.in_flight = True
                    proc.stdin.write(json.dumps(job)+'\n')
                    proc.stdin.flush()
                    reply = self._reply(replies, 15)
                    self.in_flight = False
                    if reply.get('error'):
                        self.dropped += 1
                        continue
                    if reply.get('id') != job['id'] or reply.get('startup_id') != self.startup_id or time.time() > expires_at:
                        self.dropped += 1
                        continue
                    result = reply['result']
                    # Only compact candidate/metrics metadata leaves the worker controller.
                    record = dict(kind=job['kind'], shelf_id=job['shelf_id'], camera_id=job['camera_id'],
                        timestamp=time.time(), metrics=result['metrics'],
                        candidate=result.get('candidate'), persons=len(result.get('persons', [])))
                    with self.lock:
                        self.latest.append(record)
                        self.processed += 1
                    self.log(self.startup_id,'edge_shadow_prediction',status=job['kind'],
                        shelf_id=job['shelf_id'],
                        code=(result.get('candidate') or {}).get('hold') or 'SHADOW_ONLY')
            except Exception:
                if not self.stop_event.is_set():
                    self.failures += 1
                    self.state = 'Restarting' if self.failures<3 else 'Held: worker unavailable'
                    self.log(self.startup_id,'edge_shadow_failure',code='WORKER_UNAVAILABLE',attempt=self.failures)
            finally:
                self.in_flight = False
                self._stop_child()
            if not self.stop_event.is_set() and self.failures<3:
                self.stop_event.wait(2**self.failures)

    def _stop_child(self):
        proc = self.proc
        if proc:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    try: stream.close()
                    except (OSError, ValueError): pass
            self.proc = None

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.pending = None
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=5)
        self.state = 'Stopped' if self.mode=='shadow' else 'Disabled'
