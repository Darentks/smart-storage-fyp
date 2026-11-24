"""Read-only panel data. No leases, inventory receipts, MQTT or cloud writes."""
import copy
import ipaddress
import threading
import time
from .shelves import SHELF_IDS, SHELF_SET

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
    if native is not None:return 409,{'error':'Native service owns cameras; consume native observations'}
    try:
        shelf=query.get('shelf_id',[''])[0]
        if shelf not in SHELF_SET:raise ValueError('Invalid shelf')
        ips=[str(ipaddress.IPv4Address(query.get(key,[''])[0])) for key in ('ip','ip2')]
        if ips[0]==ips[1]:raise ValueError('Two different camera IPs required')
        if not all(ipaddress.ip_address(ip).is_private for ip in ips):raise ValueError('Private camera addresses required')
        with coordinator.global_lock:
            if coordinator.busy():return 409,{'error':'Validation owns cameras; retry after validation'}
            _,combined,back,front=manager.fetch_dual_esp32_frame(esp32_ip1=ips[0],esp32_ip2=ips[1],mode='live',flash=1,duty=140,boost_mode='off')
        if not back or not front:return 502,{'error':'Both camera frames required'}
        return 200,dict(status='success',run_mode='diagnostic',authorizes_write=False,shelf_id=shelf,image=combined,image_back=back,image_front=front,timestamp=time.time())
    except (ValueError,OSError):return 502,{'error':'Diagnostic capture failed; verify camera addresses/connectivity'}
