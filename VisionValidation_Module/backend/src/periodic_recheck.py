"""Periodic evidence gating and expiring cross-browser validation leases."""
import threading
import time
import uuid

class RecheckCoordinator:
    """One expiring lease across this bridge; bounded priority/FIFO waiters."""
    def __init__(self):
        self.global_lock = threading.RLock()
        self.leases = {}
        self.waiting = {}
        self.sequence = 0

    def _prune(self):
        now = time.time()
        self.leases = {s: lease for s, lease in self.leases.items() if lease[1] > now}
        self.waiting = {key: job for key, job in self.waiting.items() if job["expires"] > now}

    def busy(self, shelf=None):
        with self.global_lock:
            self._prune()
            return bool(self.leases) if shelf is None else shelf in self.leases

    def enqueue(self, shelf, source, request_id, created_at=None):
        with self.global_lock:
            self._prune()
            if request_id not in self.waiting:
                if len(self.waiting) >= 20:
                    return False
                self.sequence += 1
                self.waiting[request_id] = dict(sequence=self.sequence)
            self.waiting[request_id].update(shelf=shelf, source=source,
                order=created_at if isinstance(created_at, (int, float)) else self.waiting[request_id].get("order", time.time()),
                expires=time.time()+60)
            return True

    def begin(self, shelf, source="MANUAL", request_id=None, created_at=None):
        with self.global_lock:
            request_id = request_id or shelf
            if not self.enqueue(shelf, source, request_id, created_at):
                return None
            if self.leases:
                return None
            first = min(self.waiting, key=lambda key: (
                self.waiting[key]["source"] == "PERIODIC_RECHECK",
                self.waiting[key]["order"], self.waiting[key]["sequence"]))
            if first != request_id:
                return None
            self.waiting.pop(request_id)
            token = str(uuid.uuid4())
            self.leases[shelf] = (token, time.time()+120)
            return token

    def cancel(self, request_id):
        with self.global_lock:
            self.waiting.pop(request_id, None)

    def owns(self, shelf, token):
        with self.global_lock:
            self._prune()
            return shelf in self.leases and self.leases[shelf][0] == token

    def renew(self, shelf, token):
        with self.global_lock:
            if not self.owns(shelf, token):
                return False
            self.leases[shelf] = (token, time.time()+120)
            return True

    def end(self, shelf, token):
        with self.global_lock:
            if self.owns(shelf, token):
                self.leases.pop(shelf, None)
                return True
            return False


class StaggeredPollSchedule:
    """Start one shelf per slot, without catch-up bursts after slow requests."""
    def __init__(self):
        self.shelves = ()
        self.index = 0
        self.next_slot = 0.0

    def take(self, shelves, now):
        shelves = tuple(sorted(set(shelves)))
        if shelves != self.shelves:
            self.shelves, self.index, self.next_slot = shelves, 0, now
        if not shelves or now < self.next_slot:
            return None
        shelf = shelves[self.index % len(shelves)]
        self.index = (self.index + 1) % len(shelves)
        self.next_slot = now + max(2.0, 30.0 / len(shelves))
        return shelf


def has_pending_shelf(pending, shelf, now):
    return any(t.get("shelf_id") == shelf and now-t.get("timestamp", 0) < 60 for t in pending)


def queue_periodic_trigger(pending, shelf, score, now):
    """Caller holds the shared consensus lock; only metadata enters the queue."""
    pending[:] = [t for t in pending if now-t.get("timestamp", 0) < 60]
    if has_pending_shelf(pending, shelf, now):
        return False
    pending.append(dict(trigger_id=str(uuid.uuid4()), shelf_id=shelf,
        source="PERIODIC_RECHECK", expected_action="RECHECK", consensus_met=False,
        diff_score=score, timestamp=now,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))))
    del pending[:-20]
    return True


def inspect_candidate(manager, shelf, ip1, ip2, threshold):
    """Preserve changed evidence until a dual-angle recount accepts it."""
    if time.time() < manager.baseline_freeze_until.get(shelf, 0):
        return None
    frame, combined, back, front = manager.fetch_dual_esp32_frame(
        esp32_ip1=ip1, esp32_ip2=ip2, mode="live", boost_mode="off", duty=90)
    if frame is None or not combined or not back or not front:
        return None  # Includes synthetic and single-angle fallback captures.
    previous = manager.baseline_cache.get(shelf)
    candidate = dict(frame_bgr=frame, data_url=combined, data_url_2=front,
                     url_back=back, url_front=front,
                     updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    score = manager._compute_absdiff_score(previous["frame_bgr"], frame) if previous and previous.get("frame_bgr") is not None else 0
    if score < threshold:
        manager.baseline_cache[shelf] = candidate
        return None
    manager.freeze_baseline(shelf, 20)
    return score
