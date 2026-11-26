"""Read-only panel data. No leases, inventory receipts, MQTT or cloud writes."""
from collections import OrderedDict
import copy
import ipaddress
import os
import threading
import time
import uuid
from .shelves import SHELF_IDS, SHELF_SET
from .dual_cam_inventory import DIAGNOSTIC_CAPTURE_BUDGET_SECONDS, DIAGNOSTIC_FIRST_ATTEMPT_SECONDS

# One bright capture setting shared by the native scanner and the quick diagnostic preview: ESP32 flash on,
# LED duty 140, raw image (no brightness boost). This is the dashboard's original first capture round and the
# first setting of the Teachable Machine training capture. SMART_STORAGE_CAPTURE_DUTY (0-255) overrides the duty.
def _capture_duty():
    try:return max(0,min(255,int(os.getenv('SMART_STORAGE_CAPTURE_DUTY','140'))))
    except ValueError:return 140

LIVE_CAPTURE_BOOST='off'
LIVE_CAPTURE_DUTY=_capture_duty()
OBSERVATION_KEEP_SECONDS=600   # latest picture per shelf shown in the panel (display only)
PREVIEW_KEEP_SECONDS=120       # diagnostic pictures kept so classification can run later without a new capture
PREVIEW_STORE_SIZE=4


class InferenceBusy(RuntimeError):
    """A diagnostic preview found the box-reading model in use; it never waits for it."""


class NativeObservations:
    """Latest pictures per shelf for the dashboard panel: scans, count checks and training captures.
    Display only; never inventory evidence."""
    def __init__(self):self.lock=threading.Lock();self.rows={}
    def put(self,shelf,preview,candidate,source='scan'):
        if shelf not in SHELF_SET:return
        row=dict(shelf_id=shelf,timestamp=time.time(),source=source,image=preview.get('image'),image_back=preview.get('image_back'),image_front=preview.get('image_front'),
            candidate=copy.deepcopy(candidate) if candidate is not None else None,authorizes_write=False)
        with self.lock:self.rows[shelf]=row
    def get(self,shelf,max_age=OBSERVATION_KEEP_SECONDS):
        with self.lock:
            row=self.rows.get(shelf)
            if row and 0<=time.time()-row['timestamp']<=max_age:
                row=copy.deepcopy(row);row['age_seconds']=int(time.time()-row['timestamp']);return row
            self.rows.pop(shelf,None)
    def clear(self):
        with self.lock:self.rows.clear()


class PreviewPictures:
    """A few recent diagnostic preview pairs, so a deferred or failed classification can be retried."""
    def __init__(self):self.lock=threading.Lock();self.rows=OrderedDict()
    def put(self,shelf,back,front):
        key=str(uuid.uuid4())
        with self.lock:
            self.rows[key]=dict(shelf_id=shelf,image_back=back,image_front=front,expires=time.monotonic()+PREVIEW_KEEP_SECONDS)
            while len(self.rows)>PREVIEW_STORE_SIZE:self.rows.popitem(last=False)
        return key
    def get(self,key):
        with self.lock:
            row=self.rows.get(key)
            if row and time.monotonic()<row['expires']:return dict(row)
            self.rows.pop(key,None)


PREVIEWS=PreviewPictures()


def _missing_camera(back,front,ips):
    """Name the camera that did not answer: (message, which)."""
    if not back and not front:
        return f'Neither camera returned a picture (Back {ips[0]}, Front {ips[1]}). Check that both are powered and on the network.','both'
    if not back:return f'The Back camera ({ips[0]}) did not return a picture; the Front camera worked.','back'
    return f'The Front camera ({ips[1]}) did not return a picture; the Back camera worked.','front'


def _scans_idle(coordinator,native):
    idle=getattr(coordinator,'scans_idle',None)
    busy=not idle() if callable(idle) else bool(coordinator.busy() or getattr(coordinator,'waiting',{}))
    return not busy and getattr(native,'active_shelf',None) is None


def classify(native,coordinator,back,front):
    """Native classification for a diagnostic preview, only when no scan is running or queued; it never waits for the
    model, so a scan cannot time out behind it. Returns the fields to add to the preview reply."""
    if not getattr(native,'models_loaded',False):
        return dict(classification='unavailable',classification_message='The box-reading models are still loading; pictures shown without a prediction.')
    deferred=dict(classification='deferred',classification_message='An automatic check is using the box-reading model; classification will run when it finishes.')
    if not _scans_idle(coordinator,native):return deferred
    # Exactly the scanner's engine, staged models, boost and pair decoder. No lease, receipt or inventory acceptance.
    try:result=native.native.infer(dict(kind='pair',image_back=back,image_front=front,boost=LIVE_CAPTURE_BOOST),preview=True)
    except InferenceBusy:return deferred
    except Exception:
        return dict(classification='failed',classification_message='The box-reading model could not classify these pictures. Automatic checks are not affected; retry the classification or the preview.')
    return dict(classification='done',candidate=result['candidate'],inference_source='native',model_versions=native.native.versions)


def diagnostic_preview(query,manager,coordinator,native,training_token=None,store=PREVIEWS):
    if query.get('run_mode',[''])[0]!='diagnostic':return 400,{'error':'Explicit diagnostic mode required'}
    purpose=query.get('purpose',['preview'])[0]
    if purpose not in ('preview','training'):return 400,{'error':'Invalid capture purpose'}
    # Read-only beside native processing. Training yields to every queued/active native check unless its shelf is
    # reserved; an ordinary preview briefly reserves its shelf and never holds the shared lock while capturing.
    try:
        shelf=query.get('shelf_id',[''])[0]
        if shelf not in SHELF_SET:raise ValueError('Invalid shelf')
        ips=[str(ipaddress.IPv4Address(query.get(key,[''])[0])) for key in ('ip','ip2')]
        if ips[0]==ips[1]:raise ValueError('Two different camera IPs required')
        if not all(ipaddress.ip_address(ip).is_private for ip in ips):raise ValueError('Private camera addresses required')
        if not coordinator.global_lock.acquire(blocking=False):
            return 409,{'code':'BUSY','error':'Another capture is in progress; retrying'}
        locked=True;preview_token=None
        try:
            admission=getattr(coordinator,'training_capture_allowed',None)
            reserved=admission(training_token,shelf,ips) if callable(admission) else None
            if reserved is False or (reserved is True and purpose!='training'):
                return 409,{'code':'TRAINING_SESSION_UNAVAILABLE','error':'This shelf is reserved for training, or the training session is waiting, expired or owned by another batch'}
            if purpose=='training':
                cctv_busy=getattr(native,'cctv_busy',None)
                if reserved is not True and (coordinator.busy() or getattr(coordinator,'waiting',{}) or (callable(cctv_busy) and cctv_busy())):
                    return 409,{'code':'BUSY','error':'Automatic camera checks have priority; training capture will retry'}
            else:
                if coordinator.busy(shelf) or getattr(native,'active_shelf',None)==shelf:
                    return 409,{'code':'BUSY','error':'This shelf is being scanned right now; retrying'}
                begin=getattr(coordinator,'begin_preview',None)
                if callable(begin):
                    preview_token=begin(shelf,DIAGNOSTIC_CAPTURE_BUDGET_SECONDS+2)
                    if not preview_token:return 409,{'code':'BUSY','error':'An automatic check is queued for this shelf; retrying'}
            # Reserved training and previews hold their own shelf reservation; other shelves use other cameras,
            # so their scans must not wait behind this capture.
            if reserved is True or preview_token:coordinator.global_lock.release();locked=False
            _,combined,back,front=manager.fetch_dual_esp32_frame(esp32_ip1=ips[0],esp32_ip2=ips[1],mode='live',flash=1,duty=LIVE_CAPTURE_DUTY,
                boost_mode=LIVE_CAPTURE_BOOST,budget=DIAGNOSTIC_CAPTURE_BUDGET_SECONDS,first_attempt=DIAGNOSTIC_FIRST_ATTEMPT_SECONDS)
        finally:
            if locked:coordinator.global_lock.release()
            if preview_token:coordinator.end_preview(preview_token)
        if not back or not front:
            message,which=_missing_camera(back,front,ips)
            return 502,{'code':'CAMERA_NO_PICTURE','camera':which,'error':message}
        row=dict(status='success',run_mode='diagnostic',purpose=purpose,authorizes_write=False,shelf_id=shelf,image=combined,image_back=back,image_front=front,timestamp=time.time())
        observations=getattr(native,'panel_observations',None)
        if purpose=='training':
            if observations is not None:observations.put(shelf,row,None,source='training')
        elif native is not None:
            row.update(classify(native,coordinator,back,front))
            if row['classification']!='done':row['preview_id']=store.put(shelf,back,front)
        return 200,row
    except (ValueError,OSError):return 502,{'code':'CAPTURE_FAILED','error':'Diagnostic capture failed; verify the camera addresses and that both cameras are on the network'}


def diagnostic_classify(query,coordinator,native,store=PREVIEWS):
    """Classify stored preview pictures later (after an automatic check finished) without a new capture."""
    row=store.get(query.get('preview_id',[''])[0])
    if row is None:return 404,{'code':'PREVIEW_EXPIRED','error':'These preview pictures expired; run Diagnostic Preview again'}
    if native is None:return 409,{'code':'NO_NATIVE','error':'Native processor is not running'}
    result=classify(native,coordinator,row['image_back'],row['image_front'])
    return 200,dict(result,shelf_id=row['shelf_id'],authorizes_write=False)
