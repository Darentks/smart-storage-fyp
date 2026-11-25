"""Read-only panel data. No leases, inventory receipts, MQTT or cloud writes."""
import copy
import ipaddress
import os
import threading
import time
from .shelves import SHELF_IDS, SHELF_SET

# One bright capture setting shared by the native scanner and the quick diagnostic preview: ESP32 flash on,
# LED duty 140, raw image (no brightness boost). This is the dashboard's original first capture round and the
# first setting of the Teachable Machine training capture. SMART_STORAGE_CAPTURE_DUTY (0-255) overrides the duty.
def _capture_duty():
    try:return max(0,min(255,int(os.getenv('SMART_STORAGE_CAPTURE_DUTY','140'))))
    except ValueError:return 140

LIVE_CAPTURE_BOOST='off'
LIVE_CAPTURE_DUTY=_capture_duty()


class NativeObservations:
    def __init__(self):self.lock=threading.Lock();self.rows={}
    def put(self,shelf,preview,candidate):
        if shelf not in SHELF_SET:return
        row=dict(shelf_id=shelf,timestamp=time.time(),image=preview.get('image'),image_back=preview.get('image_back'),image_front=preview.get('image_front'),candidate=copy.deepcopy(candidate),authorizes_write=False)
        with self.lock:self.rows[shelf]=row
    def get(self,shelf):
        with self.lock:
            row=self.rows.get(shelf)
            if row and 0<=time.time()-row['timestamp']<=30:return copy.deepcopy(row)
            self.rows.pop(shelf,None)
    def clear(self):
        with self.lock:self.rows.clear()

def diagnostic_preview(query,manager,coordinator,native):
    if query.get('run_mode',[''])[0]!='diagnostic':return 400,{'error':'Explicit diagnostic mode required'}
    purpose=query.get('purpose',['preview'])[0]
    if purpose not in ('preview','training'):return 400,{'error':'Invalid capture purpose'}
    # Read-only beside native processing. Training yields to every queued/active
    # native check; ordinary previews retain their per-shelf exclusion rule.
    try:
        shelf=query.get('shelf_id',[''])[0]
        if shelf not in SHELF_SET:raise ValueError('Invalid shelf')
        ips=[str(ipaddress.IPv4Address(query.get(key,[''])[0])) for key in ('ip','ip2')]
        if ips[0]==ips[1]:raise ValueError('Two different camera IPs required')
        if not all(ipaddress.ip_address(ip).is_private for ip in ips):raise ValueError('Private camera addresses required')
        if not coordinator.global_lock.acquire(blocking=False):
            return 409,{'error':'Another capture is in progress; retry in a few seconds'}
        try:
            if purpose=='training':
                cctv_busy=getattr(native,'cctv_busy',None)
                if coordinator.busy() or getattr(coordinator,'waiting',{}) or (callable(cctv_busy) and cctv_busy()):
                    return 409,{'error':'Automatic camera checks have priority; training capture will retry'}
            elif coordinator.busy(shelf):return 409,{'error':'This shelf is being scanned right now; try again in a few seconds'}
            _,combined,back,front=manager.fetch_dual_esp32_frame(esp32_ip1=ips[0],esp32_ip2=ips[1],mode='live',flash=1,duty=LIVE_CAPTURE_DUTY,boost_mode=LIVE_CAPTURE_BOOST)
        finally:
            coordinator.global_lock.release()
        if not back or not front:return 502,{'error':'Both camera frames required'}
        row=dict(status='success',run_mode='diagnostic',purpose=purpose,authorizes_write=False,shelf_id=shelf,image=combined,image_back=back,image_front=front,timestamp=time.time())
        if native is not None and purpose!='training':
            if not native.models_loaded:return 503,{'error':'Native box-reading models are not ready'}
            # Exactly the scanner's engine, staged models, boost and pair decoder.
            # No lease, receipt or inventory acceptance is created by this diagnostic.
            try:
                result=native.native.infer(dict(kind='pair',image_back=back,image_front=front,boost=LIVE_CAPTURE_BOOST))
            except Exception:return 503,{'error':'Native diagnostic inference failed; retry after the scan finishes'}
            row.update(candidate=result['candidate'],inference_source='native',model_versions=native.native.versions)
        return 200,row
    except (ValueError,OSError):return 502,{'error':'Diagnostic capture failed; verify camera addresses/connectivity'}
