"""
Local Camera Control, MQTT Edge Hardware Bridge, & ESP32-CAM Reference Sample API Service
Smart Storage FYP
Author: Daren Tan Khai Shuen

Runs a lightweight HTTP server on port 8008 providing:
1. Macro Hikvision CCTV Control:
   - GET    /status                  : Service & MQTT health status
   - GET    /test_feed               : Capture single CCTV frame snapshot & latency
   - POST   /run_calibration         : Open desktop OpenCV ROI calibrator

2. Micro ESP32-CAM Node & Shelf Count Validation:
   - GET    /esp32_nodes             : Read Shelf Compartments, paired observations, live MQTT health & recent comparison logs
   - GET    /esp32_preview           : Live test preview from http://<esp32_ip>/capture
   - POST   /esp32_trigger           : Dispatch CAPTURE_STATE or CAPTURE_REFERENCE over MQTT
   - GET    /esp32_reference_samples : List stored ESP32-CAM reference samples
   - POST   /esp32_reference_samples : Capture & store an ESP32-CAM scenario reference (5, 4, 3, 2, 1, 0 boxes)
   - DELETE /esp32_reference_samples : Delete an ESP32-CAM reference sample
   - POST   /esp32_validate          : Run hybrid paired observation + reference validation and apply Supabase delta
"""

import base64
from collections import deque
import concurrent.futures
import glob
import hashlib
import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add backend directory to sys.path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "backend"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

cv2 : Any
np : Any
try:
    import cv2 as _cv2_module
    import numpy as _np_module
    cv2 = _cv2_module
    np = _np_module
    
    _OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _OPENCV_AVAILABLE = False

from src.config_loader import (  # type: ignore
    get_active_rtsp_url,
    mask_rtsp_url,
    list_esp32_cameras,
    update_esp32_health_in_supabase,
    update_compartment_baseline_in_supabase,
    list_comparison_logs,
    SupabaseRestFallback,
)
from src.periodic_recheck import RecheckCoordinator
from src.bridge_runtime import InstanceLock, configure_logs, event
from src.edge_shadow import EdgeShadow
from src.runtime_inventory import RuntimeInventory, runtime_enabled
from src.preview_panel import diagnostic_preview
from src.model_assets import ModelAssets
from src.runtime_validation import validate_runtime_request, discard_runtime_attempt, reset_runtime_evidence, prepare_runtime_retry
RUNTIME_INVENTORY = None
from src.local_operations import LatestIntents, RoiStore, LocalFailures, CLOUD_METRICS
from src.bridge_access import BridgeAccessError, require_application_user
LOCAL_INTENTS = None
LOCAL_ROIS = None
EDGE_SHADOW = None
AUTONOMOUS = None
NATIVE_CALIBRATION_RELOAD = None  # Set in service mode: re-validates installed ROIs and schedules fresh baselines.
PROCESSOR_AUTHORITY = 'browser'
_PROCESSOR_CAPABILITY = object()
from src.validation_diagnostics import failure_code
from src.shelf_readiness import ShelfReadiness
READINESS = ShelfReadiness()

from src.dual_cam_inventory import validate_evidence, validate_transition, decode_observation
from src.shelves import SHELF_SET  # type: ignore
from src.inventory_persistence import (load_inventory_state, commit_inventory_state,
                                       restore_inventory_cache, operator_rpc)

# Short-lived local capture receipts, never sent to Supabase.
DUAL_CAM_CAPTURES = {}
# Cache only: every validation reads authoritative confirmed slots/revision from Supabase.
CONFIRMED_OCCUPANCY = {}
DUAL_CAM_CAPTURE_LOCK = threading.Lock()

from src.replay_manager import CctvReplayManager
from src.spatial_mapper import SpatialMapper  # type: ignore
from src.mqtt_client import MQTTClientNode  # type: ignore
from src.reference_validator import (  # type: ignore
    ESP32ReferenceManager,
    encode_bgr_to_data_url,
    decode_data_url_to_bgr,
)

# Global Managers & State
ROI_CONFIG_PATH = os.path.join(BACKEND_DIR, "config", "roi_config.json")
spatial_mapper = SpatialMapper(ROI_CONFIG_PATH)

esp32_ref_manager = ESP32ReferenceManager()
RECHECKS = RecheckCoordinator()

ACTIVE_ESP32_IPS: Dict[str, Any] = {
    "SHELF_R1_C1": {
        "ip1": os.getenv("ESP32_CAM_IP", ""),
        "ip2": os.getenv("ESP32_CAM_IP2", ""),
    },
}

# Buffer of pending CCTV intent confirmation triggers to be consumed by the UI Teachable Machine engine
PENDING_CCTV_TRIGGERS: list = []
LAST_TM_BASE_URL: str = "https://teachablemachine.withgoogle.com/models/0cQO4TjOm/"
TM_MODEL_ASSETS=ModelAssets()

# Running YOLO11n-Pose CCTV pipeline process manager
RUNNING_PIPELINES: Dict[str, Dict[str, Any]] = {}

# Every enabled camera must align within four seconds; absent votes never disable a camera.
CCTV_ALIGN_STATE: Dict[str, Dict[str, Any]] = {}
CONSENSUS_WINDOW_SEC = 4.0
CONSENSUS_COOLDOWN_SEC = 4.0
CCTV_CAMERA_IDS = {"CCTV_01", "CCTV_02", "CCTV_03"}
CCTV_CAMERA_CONFIG = {
    "CCTV_01": {"camera_role": "GRID_6_ROI", "is_active": True},
    "CCTV_02": {"camera_role": "GRID_6_ROI", "is_active": True},
    "CCTV_03": {"camera_role": "SIDE_PRESENCE", "is_active": True},
}
CCTV_LAST_SEEN_TS = {}
CCTV_CONSENSUS_LOCK = threading.RLock()
LAST_SHELF_TRIGGER_TS: Dict[str, float] = {}
CCTV_SEEN_TRIGGER_IDS: Dict[str, float] = {}

# In-memory cache for Supabase compartments and local ring buffer for comparison logs (cap: 5)
SUPABASE_COMPARTMENTS_CACHE: Dict[str, Any] = {"data": [], "fetched_at": 0.0}
LATEST_COMPARISONS_DEQUE: deque = deque(maxlen=0)  # LatestIntents is the only intent-history buffer.
SUPABASE_CACHE_TTL_SEC: float = 300.0
HEARTBEAT_TTL = 60.0
LAST_CLOUD_BASELINE_SYNC_TS: Dict[str, float] = {}
BRIDGE_PUBLIC_URL: str = (os.getenv("NEXT_PUBLIC_BRIDGE_URL") or os.getenv("VITE_CAMERA_BRIDGE_URL") or "").strip().rstrip("/")
BRIDGE_SSL_ENABLED: bool = False


def configure_cctv_alignment(cameras):
    """Synchronize CRUD roles and enabled flags without inferring availability from votes."""
    if not isinstance(cameras, list):
        raise ValueError("cameras must be a list")
    config = {}
    for camera in cameras:
        if not isinstance(camera, dict):
            raise ValueError("Each camera must be an object")
        cid, role = camera.get("cctv_id"), camera.get("camera_role")
        if cid not in CCTV_CAMERA_IDS or cid in config:
            raise ValueError("Camera IDs must be unique CCTV_01/CCTV_02/CCTV_03")
        if role not in {"GRID_6_ROI", "SIDE_PRESENCE"}:
            raise ValueError("Invalid camera_role")
        if not isinstance(camera.get("is_active"), bool):
            raise ValueError("is_active must be boolean")
        config[cid] = {"camera_role": role, "is_active": camera["is_active"]}
    with CCTV_CONSENSUS_LOCK:
        if config != CCTV_CAMERA_CONFIG:
            CCTV_CAMERA_CONFIG.clear()
            CCTV_CAMERA_CONFIG.update(config)
            CCTV_ALIGN_STATE.clear()
            # Discard unclaimed events from the superseded camera configuration.
            PENDING_CCTV_TRIGGERS[:] = [t for t in PENDING_CCTV_TRIGGERS if t.get("claimed") or t.get("source") == "PERIODIC_RECHECK"]
    return {"status": "success", "required_cameras": sum(c["is_active"] for c in config.values())}


def consensus_view():
    """Read-only live agreement state for the dashboard: current votes and any consensus awaiting box-count confirmation."""
    now = time.time()
    with CCTV_CONSENSUS_LOCK:
        votes = {cid: dict(shelf_id=v["shelf_id"], action=v["action"], role=v["camera_role"], age=round(now - v["ts"], 1))
                 for cid, v in CCTV_ALIGN_STATE.items() if 0 <= now - v["ts"] <= CONSENSUS_WINDOW_SEC}
        pending = [dict(shelf_id=t["shelf_id"], action=t.get("expected_action"), claimed=bool(t.get("claimed")), age=round(now - t["timestamp"], 1))
                   for t in PENDING_CCTV_TRIGGERS if 0 <= now - t.get("timestamp", 0) < 60][-3:]
    return dict(window_seconds=CONSENSUS_WINDOW_SEC, votes=votes, pending=pending)


def record_cctv_consensus(shelf_id, camera_id, action, confidence, camera_role=None,
                          trigger_id=None, dwell_duration_seconds=1.2, consensus_context=None, detected_at=None, queue_ready=None):
    """Atomically align distinct enabled cameras and queue one claimable validation."""
    # Replays use isolated buffers/configuration and never enter the live trigger queue.
    camera_config = CCTV_CAMERA_CONFIG if consensus_context is None else consensus_context["cameras"]
    align_state = CCTV_ALIGN_STATE if consensus_context is None else consensus_context["votes"]
    seen_ids = CCTV_SEEN_TRIGGER_IDS if consensus_context is None else consensus_context["seen_ids"]
    last_triggers = LAST_SHELF_TRIGGER_TS if consensus_context is None else consensus_context["last_triggers"]
    pending = PENDING_CCTV_TRIGGERS if consensus_context is None else consensus_context["events"]
    shelves = set(SHELF_SET)
    if action not in {"BORROW", "RETURN", "RECHECK", ""} and camera_config.get(camera_id, {}).get("camera_role") != "SIDE_PRESENCE":
        raise ValueError("Invalid interaction vote")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("oks_confidence must be finite and between 0 and 1")
    if not math.isfinite(dwell_duration_seconds) or dwell_duration_seconds < 1.2:
        raise ValueError("dwell_duration_seconds must be at least 1.2")
    with CCTV_CONSENSUS_LOCK:
        camera = camera_config.get(camera_id)
        if not camera or not camera["is_active"]:
            raise ValueError("Camera is not enabled")
        role = camera_role or camera["camera_role"]
        if role != camera["camera_role"]:
            raise ValueError("camera_role does not match camera configuration")
        if (role == "SIDE_PRESENCE" and shelf_id != "ANY_SHELF") or (
                role == "GRID_6_ROI" and shelf_id not in shelves):
            raise ValueError("Grid cameras require an exact shelf; side cameras require ANY_SHELF")
        now = time.time()
        evidence_ts = now if detected_at is None else float(detected_at)
        if not math.isfinite(evidence_ts) or now - evidence_ts > CONSENSUS_WINDOW_SEC or evidence_ts - now > 1.0:
            raise ValueError("CCTV detection timestamp is stale or invalid")
        evidence_ts = min(now, evidence_ts)
        active = {cid: cam for cid, cam in camera_config.items() if cam["is_active"]}
        for cid, vote in list(align_state.items()):
            if now - vote["ts"] > CONSENSUS_WINDOW_SEC:
                del align_state[cid]
        for tid, timestamp in list(seen_ids.items()):
            if now - timestamp > 60.0:
                del seen_ids[tid]
        if trigger_id and trigger_id in seen_ids:
            return {"status": "debounced", "consensus_met": False, "shelf_id": shelf_id,
                    "required_cameras": len(active)}
        if trigger_id:
            seen_ids[trigger_id] = now
        align_state[camera_id] = {
            "ts": evidence_ts, "action": action, "conf": confidence, "shelf_id": shelf_id, "camera_role": role,
        }
        # Grid views agree on shelf/time; side presence supports agreement, never direction.
        grid_votes = [align_state[cid] for cid, cam in active.items()
                      if cam["camera_role"] == "GRID_6_ROI" and cid in align_state]
        target = grid_votes[0]["shelf_id"] if grid_votes else None
        target_action = 'RECHECK'  # CCTV identifies shelf/time; ESP32 observations determine direction.
        supporters = {cid: vote for cid, vote in align_state.items()
                      if cid in active
                      and (vote["camera_role"] == "SIDE_PRESENCE"
                           or vote["shelf_id"] == target)}
        all_grids_present = all(cid in align_state for cid, cam in active.items()
                                if cam["camera_role"] == "GRID_6_ROI")
        aligned = (target is not None and target in shelves and all_grids_present and len(supporters) == len(active))
        if not aligned or target is None:
            if any(cid in align_state for cid in ('CCTV_01','CCTV_02')) and consensus_context is None and (intent_store := globals().get('LOCAL_INTENTS')) is not None:
                intent_store.add(dict(id=trigger_id or str(uuid.uuid4()),timestamp=now,camera_id=camera_id,shelf_id=shelf_id,
                    action='RECHECK',status='PENDING',reason='MISSING_CCTV_CONSENSUS'))
            return {"status": "pending_consensus", "consensus_met": False, "shelf_id": shelf_id,
                    "confirmed_count": len(supporters), "required_cameras": len(active),
                    "window_seconds": CONSENSUS_WINDOW_SEC}
        last_ts = last_triggers.get(target)
        if last_ts is not None and now - last_ts < CONSENSUS_COOLDOWN_SEC and not any(t.get('shelf_id')==target and 0<=now-t.get('timestamp',0)<60 for t in pending):
            if consensus_context is None and (intent_store := globals().get('LOCAL_INTENTS')) is not None:
                intent_store.add(dict(id=trigger_id or str(uuid.uuid4()),timestamp=now,camera_id=camera_id,shelf_id=target,
                    action=target_action,status='HELD',reason='VALIDATION_REJECTED'))
            return {"status": "debounced", "consensus_met": False, "shelf_id": target,
                    "retry_after": CONSENSUS_COOLDOWN_SEC - (now - last_ts)}
        confirmed_id = f"trig_{target}_{uuid.uuid4().hex}"
        if consensus_context is None and (intent_store := globals().get('LOCAL_INTENTS')) is not None:
            intent_store.add(dict(id=confirmed_id,timestamp=now,camera_id=camera_id,shelf_id=target,action=target_action,status='CONSENSUS',reason=None))
        result = {"status": "consensus_confirmed", "consensus_met": True,
                  "shelf_id": target, "confirmed_shelf_id": target, "trigger_id": confirmed_id,
                  "expected_action": target_action, "required_cameras": len(active),
                  "confirmed_count": len(supporters), "confirmed_cameras": sorted(supporters),
                  "evidence_started_at": min(v['ts'] for v in supporters.values()),
                  "evidence_completed_at": max(v['ts'] for v in supporters.values()),
                  "confidence": min(v["conf"] for v in supporters.values())}
        if consensus_context is not None:
            result["test_only"] = True
        overlapping=[t for t in pending if t.get('shelf_id')==target and 0<=now-t.get('timestamp',0)<60]
        for previous in overlapping:previous['overlap']=True
        result['overlap']=bool(overlapping)
        eligible=queue_ready is None or queue_ready(target) or any(t.get('claimed') for t in overlapping)
        result['inventory_eligible']=eligible
        if consensus_context is None and (notices:=globals().get('CCTV_LAST_CONSENSUS')) is not None:
            notices[target]={**result,'timestamp':now}
        if eligible:pending.append({**result, "source": "CCTV", "timestamp": now})
        del pending[:-20]
        last_triggers[target] = now
        align_state.clear()  # Consume all evidence so presence cannot validate another shelf.
        return result


# At most one recent notification per active shelf. Independent of the latest-five
# intent list so frequent side-presence observations cannot hide a real agreement.
CCTV_LAST_CONSENSUS = {}

def recent_cctv_consensus():
    with CCTV_CONSENSUS_LOCK:
        now=time.time()
        for sid,row in list(CCTV_LAST_CONSENSUS.items()):
            if not 0<=now-row['timestamp']<60:CCTV_LAST_CONSENSUS.pop(sid,None)
        return [dict(row) for row in CCTV_LAST_CONSENSUS.values()]


CCTV_REPLAY_MANAGER = CctvReplayManager(record_cctv_consensus, window_seconds=CONSENSUS_WINDOW_SEC)

def claim_cctv_trigger(trigger_id, shelf_id=None, validation_token=None):
    """Bind one queued event to its owning shelf lease before taking evidence."""
    with RECHECKS.global_lock:
        if not RECHECKS.owns(shelf_id, validation_token) or not READINESS.ready(shelf_id):
            return False
        authority=globals().get('RUNTIME_INVENTORY')
        if authority is not None and authority.event_terminal(shelf_id,trigger_id):return False
        with CCTV_CONSENSUS_LOCK:
            if any(t.get("validation_token") == validation_token for t in PENDING_CCTV_TRIGGERS):
                return False
            for trigger in PENDING_CCTV_TRIGGERS:
                if (trigger.get("trigger_id") == trigger_id and trigger.get("shelf_id") == shelf_id
                        and not trigger.get("claimed")
                        and (trigger['timestamp'] >= READINESS.get(shelf_id).get('ready_at',0) or trigger.get('overlap') or trigger.get('retry_count')==1)
                        and time.time()>=trigger.get('retry_after',0)
                        and 0 <= time.time() - trigger["timestamp"] < 60.0):
                    trigger.update(claimed=True, validation_token=validation_token, claimed_at=time.time(),retry_pending=False)
                    READINESS.set(shelf_id,"Validating Action","CCTV consensus received")
                    return True
    return False


def get_cached_compartments(force_refresh: bool = False) -> list:
    """Returns cached compartments from Supabase, refreshing only after TTL or when forced."""
    if runtime_enabled():
        from src.config_loader import _supabase_rest, ESP32_CAMERAS_COLS
        now=time.time()
        if force_refresh or now-SUPABASE_COMPARTMENTS_CACHE['fetched_at']>=SUPABASE_CACHE_TTL_SEC:
            devices=_supabase_rest(table='esp32_cameras',query=f'?select={ESP32_CAMERAS_COLS}&order=shelf_id.asc,angle_index.asc',distinguish_failure=True)
            from src.config_loader import device_shelves
            if isinstance(devices, list):
                SUPABASE_COMPARTMENTS_CACHE.update(data=device_shelves(devices),fetched_at=now)
            else:
                SUPABASE_COMPARTMENTS_CACHE['fetched_at']=now
        authority=globals().get('RUNTIME_INVENTORY')
        return [dict(row,**{k:v for k,v in authority.snapshot(row['shelf_id']).items() if k!='shelf_id'}) if authority else dict(row,current_quantity=None) for row in SUPABASE_COMPARTMENTS_CACHE['data']]
    now = time.time()
    if force_refresh or (now - SUPABASE_COMPARTMENTS_CACHE["fetched_at"] >= SUPABASE_CACHE_TTL_SEC) or not SUPABASE_COMPARTMENTS_CACHE["data"]:
        try:
            data = list_esp32_cameras()
            if data:
                SUPABASE_COMPARTMENTS_CACHE["data"] = data
                SUPABASE_COMPARTMENTS_CACHE["fetched_at"] = now
        except Exception as e:
            print(f"[CameraService Cache] Compartments refresh warning: {e}")
    else:
        CLOUD_METRICS.hit()
    return SUPABASE_COMPARTMENTS_CACHE["data"]


def get_cached_comparison_logs(force_refresh: bool = False, limit: int = 5) -> list:
    """Returns the latest comparison logs from the in-memory ring buffer (max 5 items, zero DB egress)."""
    return list(LATEST_COMPARISONS_DEQUE)[: max(1, min(limit, 5))]

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
mqtt_bridge = MQTTClientNode(
    broker_address=MQTT_BROKER,
    port=MQTT_PORT,
    rack_id=spatial_mapper.rack_id,
    client_id_prefix="CameraServiceBridge",
    sync_supabase=True,
    esp32_validator=esp32_ref_manager,
)


def node_is_online(node):
    if not node:
        return False
    if time.time() - node.get("last_seen_ts", 0) > HEARTBEAT_TTL:
        node["status"] = "OFFLINE"
    return node.get("status", "ONLINE") != "OFFLINE"


def record_probe_result(ip, data=None):
    """Update individual camera liveness without treating old heartbeats as current."""
    now = time.time()
    matching_ids = [key for key, node in list(mqtt_bridge.edge_nodes.items()) if node.get("ip_address") == ip]
    if data:
        device_id = data.get("device_id") or data.get("mqtt_client_id") or ip
        if device_id not in matching_ids:
            matching_ids.append(device_id)
    for device_id in matching_ids:
        node = dict(mqtt_bridge.edge_nodes.get(device_id, {}))
        if data:
            node.update(data)
            node.update(ip_address=ip, status="ONLINE", last_seen_ts=now,
                        last_seen_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
        else:
            node["status"] = "OFFLINE"
        mqtt_bridge.edge_nodes[device_id] = node


def get_merged_compartments_and_nodes():
    """
    Combines local roi_config.json compartments, Supabase 'public.compartments' rows,
    esp32_ref_manager baseline cache, and live MQTT health telemetry into a unified list.
    """
    spatial_mapper.load_roi_config()
    local_comps = spatial_mapper.compartments
    db_rows = get_cached_compartments(force_refresh=False)
    db_nodes = {row.get("mqtt_client_id"): row for row in db_rows if row.get("mqtt_client_id")}
    db_by_shelf = {row.get("shelf_id"): row for row in db_rows if row.get("shelf_id")}
    live_mqtt = mqtt_bridge.edge_nodes

    merged = []
    seen_mqtt_ids = set()
    seen_shelf_ids = set()

    for comp in local_comps:
        shelf_id = comp["shelf_id"]
        if shelf_id not in SHELF_SET:
            continue  # Retired columns stay hidden even if legacy local config lists them.
        seen_shelf_ids.add(shelf_id)
        mqtt_id = comp.get("mqtt_client_id") or shelf_id.replace("SHELF_", "ESP32_")
        seen_mqtt_ids.add(mqtt_id)
        seen_mqtt_ids.add(f"{mqtt_id}_CAM1")

        db_row = db_nodes.get(mqtt_id) or db_by_shelf.get(shelf_id, {})
        cam2_mqtt_id = db_row.get("secondary_mqtt_client_id") or f"{mqtt_id}_CAM2"
        seen_mqtt_ids.add(cam2_mqtt_id)

        live_row = live_mqtt.get(mqtt_id) or live_mqtt.get(f"{mqtt_id}_CAM1") or {}
        live_cam2_row = live_mqtt.get(cam2_mqtt_id) or {}
        cached_baseline = esp32_ref_manager.baseline_cache.get(shelf_id, {})

        active_override = ACTIVE_ESP32_IPS.get(shelf_id)
        if isinstance(active_override, dict):
            override_ip1 = active_override.get("ip1")
            override_ip2 = active_override.get("ip2")
        elif isinstance(active_override, str) and active_override.strip():
            override_ip1 = active_override.strip()
            override_ip2 = None
        else:
            override_ip1 = None
            override_ip2 = None

        status = "ONLINE" if (node_is_online(live_row) or node_is_online(live_cam2_row)) else "OFFLINE"
        baseline_url = cached_baseline.get("data_url") or db_row.get("baseline_image_url")
        baseline_2_url = cached_baseline.get("data_url_2") or db_row.get("baseline_image_2_url")
        baseline_updated = cached_baseline.get("updated_at") or db_row.get("baseline_updated_at")
        ip_addr = override_ip1 or live_row.get("ip_address") or db_row.get("ip_address") or ""
        sec_ip_addr = override_ip2 or live_cam2_row.get("ip_address") or db_row.get("secondary_ip_address") or ""

        merged.append({
            "camera_id": db_row.get("camera_id"),
            "shelf_id": shelf_id,
            "rack_id": db_row.get("rack_id") or spatial_mapper.rack_id,
            "mqtt_client_id": mqtt_id,
            "secondary_mqtt_client_id": cam2_mqtt_id,
            "camera_name": comp.get("camera_name") or db_row.get("camera_name") or f"ESP32 Node ({shelf_id})",
            "container_id": comp.get("container_id") or db_row.get("container_id"),
            "ip_address": ip_addr,
            "secondary_ip_address": sec_ip_addr,
            **({"vertices": comp["vertices"], "coordinate_space": "pixels"} if "vertices" in comp else {}),
            "x_min": comp["x_min"],
            "y_min": comp["y_min"],
            "x_max": comp["x_max"],
            "y_max": comp["y_max"],
            "dwell_threshold_seconds": comp.get("dwell_threshold_seconds", 1.2),
            "status": status,
            "current_quantity": db_row.get("current_quantity"),
            "max_capacity": int(db_row.get("max_capacity") or 5),
            "baseline_image_url": baseline_url,
            "baseline_image_2_url": baseline_2_url,
            "baseline_updated_at": baseline_updated,
            "rssi_dbm": live_row.get("rssi_dbm", live_cam2_row.get("rssi_dbm", db_row.get("rssi_dbm"))),
            "free_heap_bytes": live_row.get("free_heap_bytes", live_cam2_row.get("free_heap_bytes", db_row.get("free_heap_bytes"))),
            "mac_address": live_row.get("mac_address", db_row.get("mac_address")),
            "firmware_version": live_row.get("firmware_version", db_row.get("firmware_version")),
            "bus_vcc": live_row.get("bus_vcc", db_row.get("bus_vcc")),
            "last_seen_at": live_row.get("last_seen_at", live_cam2_row.get("last_seen_at", db_row.get("last_seen_at"))),
        })

    # Include any compartments from Supabase public.compartments not yet present in local roi_config.json
    for db_row in db_rows:
        shelf_id = db_row.get("shelf_id")
        if not shelf_id or shelf_id in seen_shelf_ids:
            continue
        seen_shelf_ids.add(shelf_id)
        mqtt_id = db_row.get("mqtt_client_id") or shelf_id.replace("SHELF_", "ESP32_")
        cam2_mqtt_id = db_row.get("secondary_mqtt_client_id") or f"{mqtt_id}_CAM2"
        seen_mqtt_ids.add(mqtt_id)
        seen_mqtt_ids.add(f"{mqtt_id}_CAM1")
        seen_mqtt_ids.add(cam2_mqtt_id)

        live_row = live_mqtt.get(mqtt_id) or live_mqtt.get(f"{mqtt_id}_CAM1") or {}
        live_cam2_row = live_mqtt.get(cam2_mqtt_id) or {}
        cached_baseline = esp32_ref_manager.baseline_cache.get(shelf_id, {})

        active_override = ACTIVE_ESP32_IPS.get(shelf_id)
        override_ip1 = active_override.get("ip1") if isinstance(active_override, dict) else (active_override if isinstance(active_override, str) else None)
        override_ip2 = active_override.get("ip2") if isinstance(active_override, dict) else None

        status = "ONLINE" if (node_is_online(live_row) or node_is_online(live_cam2_row)) else "OFFLINE"
        baseline_url = cached_baseline.get("data_url") or db_row.get("baseline_image_url")
        baseline_2_url = cached_baseline.get("data_url_2") or db_row.get("baseline_image_2_url")
        baseline_updated = cached_baseline.get("updated_at") or db_row.get("baseline_updated_at")
        ip_addr = override_ip1 or live_row.get("ip_address") or db_row.get("ip_address") or ""
        sec_ip_addr = override_ip2 or live_cam2_row.get("ip_address") or db_row.get("secondary_ip_address") or ""

        merged.append({
            "camera_id": db_row.get("camera_id"),
            "shelf_id": shelf_id,
            "rack_id": db_row.get("rack_id") or spatial_mapper.rack_id,
            "mqtt_client_id": mqtt_id,
            "secondary_mqtt_client_id": cam2_mqtt_id,
            "camera_name": db_row.get("camera_name") or f"ESP32 Node ({shelf_id})",
            "container_id": db_row.get("container_id"),
            "ip_address": ip_addr,
            "secondary_ip_address": sec_ip_addr,
            "x_min": db_row.get("roi_x_min", 100),
            "y_min": db_row.get("roi_y_min", 150),
            "x_max": db_row.get("roi_x_max", 300),
            "y_max": db_row.get("roi_y_max", 350),
            "dwell_threshold_seconds": float(db_row.get("dwell_threshold_seconds", 1.2)),
            "status": status,
            "current_quantity": db_row.get("current_quantity"),
            "max_capacity": int(db_row.get("max_capacity") or 5),
            "baseline_image_url": baseline_url,
            "baseline_image_2_url": baseline_2_url,
            "baseline_updated_at": baseline_updated,
            "rssi_dbm": live_row.get("rssi_dbm", live_cam2_row.get("rssi_dbm", db_row.get("rssi_dbm"))),
            "free_heap_bytes": live_row.get("free_heap_bytes", live_cam2_row.get("free_heap_bytes", db_row.get("free_heap_bytes"))),
            "mac_address": live_row.get("mac_address", db_row.get("mac_address")),
            "firmware_version": live_row.get("firmware_version", db_row.get("firmware_version")),
            "bus_vcc": live_row.get("bus_vcc", db_row.get("bus_vcc")),
            "last_seen_at": live_row.get("last_seen_at", live_cam2_row.get("last_seen_at", db_row.get("last_seen_at"))),
        })

    for mqtt_id, live_row in live_mqtt.items():
        if mqtt_id not in seen_mqtt_ids and not str(mqtt_id).upper().endswith("_CAM2"):
            db_row = db_nodes.get(mqtt_id, {})
            shelf_id = live_row.get("shelf_id", db_row.get("shelf_id", "UNASSIGNED"))
            cached_baseline = esp32_ref_manager.baseline_cache.get(shelf_id, {})
            baseline_url = cached_baseline.get("data_url") or db_row.get("baseline_image_url")
            baseline_2_url = cached_baseline.get("data_url_2") or db_row.get("baseline_image_2_url")
            baseline_updated = cached_baseline.get("updated_at") or db_row.get("baseline_updated_at")
            ip_addr = live_row.get("ip_address") or db_row.get("ip_address") or ""
            sec_ip_addr = db_row.get("secondary_ip_address") or ""

            merged.append({
                "camera_id": db_row.get("camera_id"),
                "shelf_id": shelf_id,
                "rack_id": live_row.get("rack_id", db_row.get("rack_id", spatial_mapper.rack_id)),
                "mqtt_client_id": mqtt_id,
                "camera_name": db_row.get("camera_name") or f"ESP32 Node ({mqtt_id})",
                "container_id": db_row.get("container_id"),
                "ip_address": ip_addr,
                "secondary_ip_address": sec_ip_addr,
                "x_min": db_row.get("roi_x_min", 100),
                "y_min": db_row.get("roi_y_min", 150),
                "x_max": db_row.get("roi_x_max", 300),
                "y_max": db_row.get("roi_y_max", 350),
                "dwell_threshold_seconds": float(db_row.get("dwell_threshold_seconds", 1.2)),
                "status": "ONLINE",
                "current_quantity": db_row.get("current_quantity"),
                "max_capacity": int(db_row.get("max_capacity") or 5),
                "baseline_image_url": baseline_url,
                "baseline_image_2_url": baseline_2_url,
                "baseline_updated_at": baseline_updated,
                "rssi_dbm": live_row.get("rssi_dbm"),
                "free_heap_bytes": live_row.get("free_heap_bytes"),
                "mac_address": live_row.get("mac_address"),
                "firmware_version": live_row.get("firmware_version"),
                "bus_vcc": live_row.get("bus_vcc"),
                "last_seen_at": live_row.get("last_seen_at"),
            })

    if runtime_enabled():
        for row in merged:
            row.update(RUNTIME_INVENTORY.snapshot(row['shelf_id']) if RUNTIME_INVENTORY else {'current_quantity':None,'precision':'UNKNOWN','count_range':[0,5]})
    for row in merged:
        row['camera_configuration'] = 'Configured' if row.get('ip_address') and row.get('secondary_ip_address') else 'Not configured'
    return merged


def get_discovered_cameras_list(compartments_list=None) -> list:
    """
    Returns a unified list of all discovered / known ESP32-CAM nodes from live MQTT edge_nodes
    and configured compartments so the UI dropdown can list every individual camera IP.
    """
    discovered_by_ip = {}

    for dev_id, node in list(mqtt_bridge.edge_nodes.items()):
        ip = str(node.get("ip_address") or node.get("ip") or "").strip()
        if not ip:
            continue
        cid = str(node.get("device_id") or node.get("mqtt_client_id") or dev_id).strip()
        angle = "FRONT" if cid.upper().endswith("_CAM2") else "BACK"
        discovered_by_ip[ip] = {
            "device_id": cid,
            "shelf_id": node.get("shelf_id") or "SHELF_R1_C1",
            "ip_address": ip,
            "rssi_dbm": node.get("rssi_dbm"),
            "status": "ONLINE" if node_is_online(node) else "OFFLINE",
            "angle": angle,
            "last_seen_at": node.get("last_seen_at"),
        }

    comps = compartments_list if compartments_list is not None else get_merged_compartments_and_nodes()
    for comp in comps:
        shelf_id = comp.get("shelf_id", "SHELF_R1_C1")
        ip1 = str(comp.get("ip_address") or "").strip()
        ip2 = str(comp.get("secondary_ip_address") or "").strip()
        mqtt1 = comp.get("mqtt_client_id") or f"ESP32_{shelf_id.replace('SHELF_', '')}_CAM1"
        mqtt2 = comp.get("secondary_mqtt_client_id") or f"ESP32_{shelf_id.replace('SHELF_', '')}_CAM2"

        if ip1 and ip1 not in discovered_by_ip:
            discovered_by_ip[ip1] = {
                "device_id": mqtt1,
                "shelf_id": shelf_id,
                "ip_address": ip1,
                "rssi_dbm": comp.get("rssi_dbm"),
                "status": "ONLINE" if any(node_is_online(node) and node.get("ip_address") == ip1 for node in list(mqtt_bridge.edge_nodes.values())) else "OFFLINE",
                "angle": "BACK",
                "last_seen_at": comp.get("last_seen_at"),
            }
        if ip2 and ip2 not in discovered_by_ip:
            discovered_by_ip[ip2] = {
                "device_id": mqtt2,
                "shelf_id": shelf_id,
                "ip_address": ip2,
                "rssi_dbm": None,
                "status": "ONLINE" if any(node_is_online(node) and node.get("ip_address") == ip2 for node in list(mqtt_bridge.edge_nodes.values())) else "OFFLINE",
                "angle": "FRONT",
                "last_seen_at": comp.get("last_seen_at"),
            }

    return list(discovered_by_ip.values())


def discover_active_esp32_cameras(subnet_prefix=None) -> list:
    """
    Actively probes candidate ESP32-CAM IPs in parallel via http://<ip>/status (0.6s timeout),
    registers online nodes into mqtt_bridge.edge_nodes, and returns discovered_cameras.
    """
    candidates = set()

    # a) Known MQTT edge_nodes
    for node in list(mqtt_bridge.edge_nodes.values()):
        ip = str(node.get("ip_address") or node.get("ip") or "").strip()
        if ip:
            candidates.add(ip)

    # b) Standard PlatformIO static IP pool (.201 to .212)
    prefixes = {"192.168.0"}
    if subnet_prefix and str(subnet_prefix).strip():
        clean_p = str(subnet_prefix).strip().rstrip(".")
        if clean_p.count(".") == 2:
            prefixes.add(clean_p)

    

    # Auto-detect local LAN subnet prefix if possible
    try:
        hostname = socket.gethostname()
        for addr in socket.gethostbyname_ex(hostname)[2]:
            if addr.startswith(("192.168.", "10.", "172.")):
                parts = addr.split(".")
                if len(parts) == 4:
                    prefixes.add(".".join(parts[:3]))
    except Exception:
        pass

    for pfx in prefixes:
        for host_id in list(range(101, 113)) + list(range(201, 213)):
            candidates.add(f"{pfx}.{host_id}")

    # c) Configured compartments and ACTIVE_ESP32_IPS (using 300s RAM cache)
    try:
        for comp in get_cached_compartments(force_refresh=False):
            if comp.get("ip_address"):
                candidates.add(str(comp["ip_address"]).strip())
            if comp.get("secondary_ip_address"):
                candidates.add(str(comp["secondary_ip_address"]).strip())
    except Exception:
        pass

    for val in list(ACTIVE_ESP32_IPS.values()):
        if isinstance(val, dict):
            if val.get("ip1"):
                candidates.add(str(val["ip1"]).strip())
            if val.get("ip2"):
                candidates.add(str(val["ip2"]).strip())
        elif val:
            candidates.add(str(val).strip())

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    online_found = []

    def _probe_ip(ip_str: str):
        clean_ip = ip_str.split("//")[-1].split("/")[0].strip()
        if not clean_ip:
            return None
        url = f"http://{clean_ip}/status"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SmartStorageScanner/1.0"})
            with urllib.request.urlopen(req, timeout=0.6) as resp:
                if resp.status == 200:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    dev_id = str(data.get("device_id") or data.get("mqtt_client_id") or f"ESP32_{clean_ip.split('.')[-1]}").strip()
                    shelf = str(data.get("shelf_id") or "SHELF_R1_C1").strip()
                    rssi = data.get("rssi_dbm") if data.get("rssi_dbm") is not None else data.get("rssi")
                    angle = "FRONT" if dev_id.upper().endswith("_CAM2") else "BACK"
                    node_info = {
                        "device_id": dev_id,
                        "mqtt_client_id": dev_id,
                        "shelf_id": shelf,
                        "ip_address": str(data.get("ip_address") or clean_ip).strip(),
                        "rssi_dbm": rssi,
                        "free_heap_bytes": data.get("free_heap_bytes"),
                        "status": "ONLINE",
                        "angle": angle,
                        "last_seen_at": now_iso,
                        "last_seen_ts": time.time(),
                    }
                    mqtt_bridge.edge_nodes[dev_id] = node_info
                    return node_info
        except Exception:
            pass
        record_probe_result(clean_ip)
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        futures = [executor.submit(_probe_ip, ip) for ip in candidates if ip]
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            if res:
                online_found.append(res)

    return get_discovered_cameras_list()


class _PersistentStreamReader:
    """
    Keeps an RTSP / Webcam cv2.VideoCapture open in a background daemon thread
    and continuously drains frames so /test_feed returns the latest real-time frame
    in <2ms instead of reconnecting (800-2000ms) on every HTTP request.
    Automatically closes after 15s of inactivity.
    """

    def __init__(self, source_str: Any):
        try:
            self.source: Any = int(source_str)
            self.is_webcam = True
        except (ValueError, TypeError):
            self.source = str(source_str)
            self.is_webcam = False

        self.masked = mask_rtsp_url(self.source)
        self.lock = threading.Lock()
        self.latest_frame: Any = None
        self.latest_seq: int = 0
        self.encoded_seq: int = -1
        self.cached_payload: Optional[Dict[str, Any]] = None
        self.last_access_ts: float = time.time()
        self.running: bool = True
        self.error_msg: Optional[str] = None
        self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self._reader_loop, daemon=True, name=f"RTSPReader-{self.masked}")
        self.thread.start()

    def _log_frame_failure(self,reason,duration_ms):
        processor=globals().get('AUTONOMOUS')
        for camera in list(processor.cameras if processor is not None else []):
            if str(camera['source'])==str(self.source):
                event(READINESS.run_id,'cctv_reader_failure',camera_id=camera['id'],reason=reason,duration_ms=duration_ms)

    def _reader_loop(self) -> None:
        cap = None
        failure_started=None
        opened_at=time.monotonic()
        try:
            print(f"[CameraService] Opening persistent low-latency stream: {self.masked}")
            if self.is_webcam:
                cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(self.source)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
                cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG) if hasattr(cv2, "CAP_FFMPEG") else cv2.VideoCapture(self.source)

            if not cap or not cap.isOpened():
                self._log_frame_failure('SOURCE_OPEN_FAILED',int((time.monotonic()-opened_at)*1000))
                self.error_msg = f"Failed to open video source ({self.masked}). Check camera IP, power, and credentials."
                self.running = False
                self.ready_event.set()
                return

            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            fail_count = 0

            while self.running:
                if (time.time() - self.last_access_ts) > 15.0:
                    print(f"[CameraService] Closing idle stream after 15s inactivity: {self.masked}")
                    break

                ret, frame = cap.read()
                if not ret or frame is None:
                    if failure_started is None:failure_started=time.monotonic()
                    self._log_frame_failure('FRAME_READ_FAILED',int((time.monotonic()-failure_started)*1000))
                    fail_count += 1
                    if fail_count > 30:
                        self.error_msg = f"Stream disconnected for {self.masked}"
                        break
                    time.sleep(0.02)
                    continue

                fail_count = 0;failure_started=None
                with self.lock:
                    self.latest_frame = frame
                    self.latest_seq += 1
                if not self.ready_event.is_set():
                    self.ready_event.set()

                if self.is_webcam:
                    time.sleep(0.01)
        except Exception as exc:
            self._log_frame_failure('FRAME_READER_EXCEPTION',int((time.monotonic()-(failure_started or opened_at))*1000))
            self.error_msg = f"Exception in stream reader: {str(exc)}"
        finally:
            self.running = False
            self.ready_event.set()
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    def get_frame_payload(self) -> Dict[str, Any]:
        t0 = time.time()
        self.last_access_ts = t0

        if not self.ready_event.is_set():
            self.ready_event.wait(timeout=4.0)

        with self.lock:
            frame = self.latest_frame
            seq = self.latest_seq
            if frame is not None and seq == self.encoded_seq and self.cached_payload is not None:
                return self.cached_payload

        if frame is None:
            return {
                "status": "failed",
                "error": self.error_msg or f"Failed to read frame buffer from {self.masked}.",
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "source": self.masked,
            }

        h, w = frame.shape[:2]
        if w > 640:
            frame_resized = cv2.resize(frame, (640, 480), interpolation=cv2.INTER_LINEAR)
            h, w = 480, 640
        else:
            frame_resized = frame

        channels = frame_resized.shape[2] if len(frame_resized.shape) > 2 else 1
        success, buffer = cv2.imencode(".jpg", frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
        latency = round((time.time() - t0) * 1000, 1)

        if not success:
            return {
                "status": "failed",
                "error": "Failed to encode captured frame to JPEG format.",
                "latency_ms": latency,
                "source": self.masked,
            }

        b64_str = base64.b64encode(buffer).decode("utf-8")
        payload = {
            "status": "success",
            "latency_ms": latency,
            "width": w,
            "height": h,
            "channels": channels,
            "seq": seq,
            "image": f"data:image/jpeg;base64,{b64_str}",
            "source": self.masked,
        }
        with self.lock:
            self.encoded_seq = seq
            self.cached_payload = payload
        return payload


_PERSISTENT_STREAMS: Dict[str, _PersistentStreamReader] = {}
_PERSISTENT_STREAMS_LOCK = threading.Lock()


def capture_single_frame(source_str) -> Dict[str, Any]:
    """
    Returns the latest real-time frame from a persistent background RTSP/Webcam stream reader.
    """
    if not _OPENCV_AVAILABLE:
        return {
            "status": "failed",
            "error": "OpenCV (cv2) is not installed in the current Python environment.",
            "latency_ms": 0,
        }

    key = str(source_str).strip()
    with _PERSISTENT_STREAMS_LOCK:
        reader = _PERSISTENT_STREAMS.get(key)
        if reader is None or not reader.running:
            reader = _PersistentStreamReader(source_str)
            _PERSISTENT_STREAMS[key] = reader

    return reader.get_frame_payload()


def resolve_edge_relay_repo_path(custom_path: Optional[str] = None) -> Optional[str]:
    """
    Resolves the directory containing the EdgeHardwareRelay PlatformIO project (platformio.ini).
    Checks custom_path, os.getenv("EDGE_RELAY_REPO_PATH"), sibling folders, and ~/Desktop or ~/Documents.
    """
    candidates = []
    if custom_path and str(custom_path).strip():
        candidates.append(os.path.abspath(str(custom_path).strip()))
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), str(custom_path).strip())))
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", str(custom_path).strip())))

    env_path = os.getenv("EDGE_RELAY_REPO_PATH", "").strip()
    if env_path:
        candidates.append(os.path.abspath(env_path))
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), env_path)))
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", env_path)))

    base_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = os.path.abspath(os.path.join(base_dir, ".."))

    # Sibling folders
    for name in ["EdgeHardwareRelay-main", "EdgeHardwareRelay", "EdgeNodeRelay"]:
        candidates.append(os.path.abspath(os.path.join(workspace_dir, "..", name)))
        candidates.append(os.path.abspath(os.path.join(workspace_dir, name)))
        candidates.append(os.path.abspath(os.path.join(base_dir, "..", name)))

    # ~/Desktop and ~/Documents
    home = os.path.expanduser("~")
    for base in [
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "OneDrive", "Documents"),
    ]:
        for name in ["EdgeHardwareRelay-main", "EdgeHardwareRelay", "EdgeNodeRelay"]:
            candidates.append(os.path.join(base, name))

    seen = set()
    for cand in candidates:
        norm = os.path.normpath(cand)
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isdir(norm) and os.path.isfile(os.path.join(norm, "platformio.ini")):
            return norm

    # Fallback to existing directory among candidates
    for cand in candidates:
        if os.path.isdir(cand):
            return os.path.normpath(cand)

    return os.path.normpath(candidates[0]) if candidates else None


def find_platformio_executable() -> str:
    """Finds the PlatformIO CLI executable (pio or pio.exe)."""
    env_pio = shutil.which("pio")
    if env_pio:
        return env_pio

    home = os.path.expanduser("~")
    penv_candidates = [
        os.path.join(home, ".platformio", "penv", "Scripts", "pio.exe"),
        os.path.join(home, ".platformio", "penv", "Scripts", "platformio.exe"),
        os.path.join(home, ".platformio", "penv", "bin", "pio"),
        os.path.join(home, ".platformio", "penv", "bin", "platformio"),
        shutil.which("platformio"),
    ]
    for p in penv_candidates:
        if p and os.path.isfile(p):
            return p
    return "pio"


def compile_and_locate_firmware(force_build: bool = True, custom_repo_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Runs [pio_exe, "run"] (timeout 120s) when force_build=True, then finds the newest
    .pio/build/*/firmware.bin (>10KB) and returns:
    {"status": "success", "repo_path", "env_name", "bin_path", "size_bytes", "modified_at", "build_log"}
    """
    repo_dir = resolve_edge_relay_repo_path(custom_repo_path)
    if not repo_dir or not os.path.isdir(repo_dir):
        return {
            "status": "failed",
            "error": f"Edge hardware relay repository not found at '{repo_dir}'. Checked EDGE_RELAY_REPO_PATH and candidate folders.",
            "repo_path": repo_dir,
            "build_log": None,
        }

    build_log = ""
    if force_build:
        pio_exe = find_platformio_executable()
        print(f"[CameraService OTA] Compiling PlatformIO project in '{repo_dir}' using '{pio_exe}'...")
        try:
            proc = subprocess.run(
                [pio_exe, "run"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            build_log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            if proc.returncode != 0:
                print(f"[CameraService OTA] PlatformIO build failed (exit code {proc.returncode})")
                return {
                    "status": "failed",
                    "error": f"PlatformIO compilation failed with exit code {proc.returncode}",
                    "repo_path": repo_dir,
                    "build_log": build_log,
                }
            print("[CameraService OTA] PlatformIO compilation succeeded.")
        except subprocess.TimeoutExpired as te:
            out = (te.stdout or "") if hasattr(te, "stdout") else ""
            err = (te.stderr or "") if hasattr(te, "stderr") else ""
            return {
                "status": "failed",
                "error": "PlatformIO build timed out (120s limit exceeded).",
                "repo_path": repo_dir,
                "build_log": f"{out}\n{err}".strip(),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"Failed to execute PlatformIO build: {str(exc)}",
                "repo_path": repo_dir,
                "build_log": str(exc),
            }

    # Locate newest .pio/build/*/firmware.bin (>10KB)
    pattern = os.path.join(repo_dir, ".pio", "build", "*", "firmware.bin")
    candidate_bins = glob.glob(pattern)
    valid_bins = [b for b in candidate_bins if os.path.isfile(b) and os.path.getsize(b) > 10240]

    if not valid_bins:
        return {
            "status": "failed",
            "error": "No compiled firmware.bin (>10KB) found in .pio/build/*/. Please enable compilation or run 'pio run'.",
            "repo_path": repo_dir,
            "build_log": build_log or None,
        }

    valid_bins.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    newest_bin = valid_bins[0]
    env_name = os.path.basename(os.path.dirname(newest_bin))
    size_bytes = os.path.getsize(newest_bin)
    modified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(newest_bin)))

    return {
        "status": "success",
        "repo_path": repo_dir,
        "env_name": env_name,
        "bin_path": newest_bin,
        "size_bytes": size_bytes,
        "modified_at": modified_at,
        "build_log": build_log or None,
    }


def push_ota_firmware_to_esp32(esp32_ip: str, bin_path: str, timeout: float = 45.0) -> dict:
    """
    Streams firmware.bin as multipart/form-data (name="file"; filename="firmware.bin")
    via POST http://{clean_ip}/update and returns:
    {"status", "ip", "http_status", "elapsed_ms", "device_response"}
    """
    clean_ip = str(esp32_ip).split("//")[-1].split("/")[0].strip()
    if not clean_ip:
        return {
            "status": "failed",
            "ip": esp32_ip,
            "http_status": None,
            "elapsed_ms": 0,
            "device_response": None,
            "error": "Empty or invalid IP address",
        }

    if not os.path.isfile(bin_path):
        return {
            "status": "failed",
            "ip": clean_ip,
            "http_status": None,
            "elapsed_ms": 0,
            "device_response": None,
            "error": f"Firmware binary not found at '{bin_path}'",
        }

    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    part_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="firmware.bin"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    part_footer = f"\r\n--{boundary}--\r\n".encode("utf-8")

    try:
        with open(bin_path, "rb") as bf:
            file_bytes = bf.read()
    except Exception as exc:
        return {
            "status": "failed",
            "ip": clean_ip,
            "http_status": None,
            "elapsed_ms": 0,
            "device_response": None,
            "error": f"Failed to read firmware binary: {str(exc)}",
        }

    body_bytes = part_header + file_bytes + part_footer
    headers = {
        "User-Agent": "SmartStorageOTA/1.0",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body_bytes)),
    }
    url = f"http://{clean_ip}/update"
    t0 = time.time()

    try:
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = int((time.time() - t0) * 1000)
            status_code = resp.status
            dev_resp = resp.read().decode("utf-8", errors="replace")
            print(f"[CameraService OTA] Successfully flashed {clean_ip} in {elapsed_ms}ms: {dev_resp[:100]}")
            return {
                "status": "success" if (200 <= status_code < 300) else "failed",
                "ip": clean_ip,
                "http_status": status_code,
                "elapsed_ms": elapsed_ms,
                "device_response": dev_resp,
            }
    except urllib.error.HTTPError as he:
        elapsed_ms = int((time.time() - t0) * 1000)
        dev_resp = he.read().decode("utf-8", errors="replace") if he.fp else str(he)
        print(f"[CameraService OTA] HTTP error flashing {clean_ip}: {he.code} {he.reason} - {dev_resp[:100]}")
        return {
            "status": "failed",
            "ip": clean_ip,
            "http_status": he.code,
            "elapsed_ms": elapsed_ms,
            "device_response": dev_resp,
            "error": f"HTTP {he.code}: {he.reason}",
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - t0) * 1000)
        print(f"[CameraService OTA] Exception flashing {clean_ip}: {exc}")
        return {
            "status": "failed",
            "ip": clean_ip,
            "http_status": None,
            "elapsed_ms": elapsed_ms,
            "device_response": None,
            "error": str(exc),
        }


class CameraServiceRequestHandler(BaseHTTPRequestHandler):
    _processor_capability: Optional[object] = None
    """HTTP Request Handler for camera control, MQTT triggers, and ESP32 reference validation."""

    def log_message(self, format: str, *args: Any) -> None:
        if "/test_feed" in self.path or "/esp32_nodes" in self.path:
            return
        if len(args) >= 2:
            sys.stderr.write(f"[CameraService HTTP] {args[0]} - {args[1]}\n")
        else:
            super().log_message(format, *args)

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")

    def _send_json(self, status_code: int, payload: Any):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        body_bytes = self.rfile.read(content_length)
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return {}

    def _install_local_rois(self, body):
        """Install dashboard calibrations through the local ROI CLI, then re-verify the native processor."""
        if self.client_address[0] not in ('127.0.0.1','::1','::ffff:127.0.0.1'):
            try:
                require_application_user(getattr(self,'headers',{}).get('Authorization'))
            except BridgeAccessError as exc:
                self._send_json(exc.status,{'error':str(exc)});return
        roi_store=LOCAL_ROIS
        if roi_store is None:
            self._send_json(409,{'error':'Local ROI store not initialized'});return
        if not isinstance(body,dict) or not body.get('cameras'):
            self._send_json(400,{'error':'Expected calibration JSON with three cameras'});return
        script=Path(__file__).resolve().parent.parent/'deploy'/'local_roi.py'
        descriptor,temp=tempfile.mkstemp(prefix='roi-upload-',suffix='.json')
        try:
            with os.fdopen(descriptor,'w',encoding='utf8') as stream:json.dump(body,stream)
            # Always the bridge's own store directory, so the CLI and service read the same file.
            result=subprocess.run([sys.executable,'-B',str(script),'import','--file',temp,'--directory',str(roi_store.path.parent)],
                capture_output=True,text=True,timeout=30)
        finally:
            if os.path.exists(temp):os.unlink(temp)
        output=(result.stdout+result.stderr).strip()[-2000:]
        if result.returncode!=0:
            self._send_json(422,{'status':'failed','error':'ROI import rejected','output':output});return
        reload=NATIVE_CALIBRATION_RELOAD
        if reload is None:
            verification={'status':'not_running','message':'The native processor is not running in this mode; the installed file is used after a normal restart.'}
        else:
            try:
                reload()
                verification={'status':'verifying','message':'Calibration reloaded; every shelf is recounted after the quiet window from two matching paired ESP32 pictures.'}
            except Exception as exc:
                verification={'status':'blocked','message':str(exc)[:300]+' — counts continue, but actions stay paused until this is fixed and installed again.'}
        self._send_json(200,{'status':'installed','output':output,'native':verification,'run_id':READINESS.run_id})

    def _control_cctv(self,body):
        """Same local/application-user access rule as scan requests; no browser evidence writer."""
        if self.client_address[0] not in ('127.0.0.1','::1','::ffff:127.0.0.1'):
            try:require_application_user(getattr(self,'headers',{}).get('Authorization'))
            except BridgeAccessError as exc:
                self._send_json(exc.status,{'error':str(exc)});return
        if not isinstance(body,dict) or type(body.get('enabled')) is not bool:
            self._send_json(400,{'error':'enabled must be a boolean'});return
        if AUTONOMOUS is None:
            self._send_json(409,{'error':'The native processor is not running'});return
        AUTONOMOUS.set_cctv_enabled(body['enabled'])
        if not body['enabled']:
            with _PERSISTENT_STREAMS_LOCK:
                for camera in AUTONOMOUS.cameras:
                    reader=_PERSISTENT_STREAMS.pop(str(camera['source']).strip(),None)
                    if reader is not None:reader.running=False
        self._send_json(200,{'cctv_enabled':AUTONOMOUS.cctv_enabled,'status':'CCTV started; recount when cameras are live' if body['enabled'] else 'CCTV stopped; last counts retained'})

    def _request_scan(self, body):
        """Retry scan: ask the shared native scanner to recount. The browser never captures or writes inventory."""
        if self.client_address[0] not in ('127.0.0.1','::1','::ffff:127.0.0.1'):
            try:
                require_application_user(getattr(self,'headers',{}).get('Authorization'))
            except BridgeAccessError as exc:
                self._send_json(exc.status,{'error':str(exc)});return
        processor, authority = AUTONOMOUS, globals().get('RUNTIME_INVENTORY')
        if processor is None or authority is None:
            self._send_json(409,{'error':'The native processor is not running on this bridge'});return
        shelf = body.get('shelf_id') if isinstance(body, dict) else None
        if shelf not in (None, '') and shelf not in SHELF_SET:
            self._send_json(400,{'error':'Unknown shelf'});return
        scheduled, busy = [], []
        with RECHECKS.global_lock:
            for sid in ([shelf] if shelf else sorted(SHELF_SET)):
                snapshot = authority.snapshot(sid)
                if RECHECKS.busy(sid):
                    busy.append(sid);continue  # a scan or action is already in flight for this shelf
                if not shelf and snapshot['state'] == 'Ready' and snapshot['action_capable']:
                    continue  # "scan all" never discards a healthy action-ready count
                authority.reset_shelf(sid)
                READINESS.set(sid,'Starting','Fresh count requested')
                with CCTV_CONSENSUS_LOCK:
                    PENDING_CCTV_TRIGGERS[:] = [t for t in PENDING_CCTV_TRIGGERS if t.get('shelf_id') != sid]
                with DUAL_CAM_CAPTURE_LOCK:
                    for key in list(DUAL_CAM_CAPTURES):
                        if DUAL_CAM_CAPTURES[key].get('shelf_id') == sid:DUAL_CAM_CAPTURES.pop(key,None)
                processor.reset_scan_budget(sid)
                scheduled.append(sid)
        self._send_json(200,{'status':'scheduled','shelves':scheduled,'busy':busy,'run_id':READINESS.run_id})

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _serve_model_asset(self, filename: str, head_only: bool = False):
        service_dir = Path(__file__).resolve().parent
        model_bases = [
            service_dir.parent / "models",
            service_dir / "models",
            service_dir / "backend" / "models",
        ]
        for base_dir in model_bases:
            try:
                base = base_dir.resolve()
                target = (base / filename).resolve()
                if not target.is_relative_to(base):
                    self._send_json(403, {"error": "Model path is outside the model directory"})
                    return
                if not target.is_file():
                    continue
                if head_only:
                    data_bytes = None
                    content_length = target.stat().st_size
                else:
                    data_bytes = target.read_bytes()
                    content_length = len(data_bytes)
            except (OSError, ValueError):
                continue
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(content_length))
            self._send_cors_headers()
            self.end_headers()
            if data_bytes is not None:
                self.wfile.write(data_bytes)
            return
        self._send_json(404, {"error": "Model asset not found"})

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/models/"):
            filename = urllib.parse.unquote(path[len("/models/"):])
            self._serve_model_asset(filename, head_only=True)
            return
        self._send_json(404, {"error": "Endpoint not found"})

    def _runtime_validate(self, body):
        status_code, payload = validate_runtime_request(
            body, authority=RUNTIME_INVENTORY, coordinator=RECHECKS, mqtt=mqtt_bridge,
            load_mappings=get_cached_compartments, consensus_lock=CCTV_CONSENSUS_LOCK,
            pending=PENDING_CCTV_TRIGGERS, capture_lock=DUAL_CAM_CAPTURE_LOCK,
            captures=DUAL_CAM_CAPTURES, readiness=READINESS,intents=LOCAL_INTENTS,votes=CCTV_ALIGN_STATE,
            release_freeze=lambda shelf:esp32_ref_manager.baseline_freeze_until.pop(shelf,None),consensus_window=CONSENSUS_WINDOW_SEC)
        self._send_json(status_code, payload)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == '/diagnostic_preview':
            code,payload=diagnostic_preview(urllib.parse.parse_qs(parsed.query),esp32_ref_manager,RECHECKS,AUTONOMOUS)
            self._send_json(code,payload)
            return
        if path == '/native_observation':
            shelf=urllib.parse.parse_qs(parsed.query).get('shelf_id',[''])[0]
            row=AUTONOMOUS.panel_observations.get(shelf) if AUTONOMOUS else None
            self._send_json(200,{'observation':row,'authorizes_write':False})
            return
        if path == '/local_operations':
            failures=READINESS.failures
            self._send_json(200,dict(run_id=READINESS.run_id,intents=LOCAL_INTENTS.snapshot() if LOCAL_INTENTS else [],
                changes=RUNTIME_INVENTORY.latest_changes() if globals().get('RUNTIME_INVENTORY') else [],
                cctv_warnings=RUNTIME_INVENTORY.movement_warnings() if globals().get('RUNTIME_INVENTORY') else [],
                cloud_metrics=CLOUD_METRICS.snapshot(),metrics_scope='Python PostgREST helper only; SDK/browser calls excluded',
                unresolved_failures=failures.snapshot() if failures is not None else {},
                release_blockers=['REAL_CAMERA_ACCEPTANCE'] if globals().get('RUNTIME_INVENTORY') else ['STRICT_CLOUD_SCHEMA_MIGRATION','REAL_CAMERA_ACCEPTANCE'],
                inventory_contract='runtime-v1' if globals().get('RUNTIME_INVENTORY') else 'persistent-v1',
                strict_cloud_scope=globals().get('RUNTIME_INVENTORY') is not None,
                consensus_notifications=recent_cctv_consensus()))
            return
        if path == '/local_rois':
            try:
                roi_store=LOCAL_ROIS
                if roi_store is None:raise ValueError('Local ROI store not initialized')
                self._send_json(200,roi_store.load())
            except (OSError,ValueError,TypeError,KeyError):self._send_json(409,{'error':'Missing or invalid local ROI configuration; import using local CLI'})
            return
        if globals().get('RUNTIME_INVENTORY') is not None and path == '/esp32_preview' and getattr(self,'_processor_capability',None) is not _PROCESSOR_CAPABILITY:
            self._send_json(403,{'error':'Service owns live captures; dashboard is monitoring-only'})
            return
        if path == '/edge_status':
            status = AUTONOMOUS.snapshot() if AUTONOMOUS else EDGE_SHADOW.snapshot() if EDGE_SHADOW else {
                'mode':'browser','authoritative_processor':'none','service_cutover_available':False,
                'state':'Disabled','startup_id':READINESS.run_id,'authorizes_write':False}
            self._send_json(200, dict(status, consensus=consensus_view()) if AUTONOMOUS else status)
            return
        if path == "/validation_diagnostics":
            shelf = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('shelf_id',[None])[0]
            with READINESS.lock:
                while READINESS.diagnostics and time.time()-READINESS.diagnostics[0]['timestamp']>300:READINESS.diagnostics.popleft()
                entries=[r for r in READINESS.diagnostics if shelf is None or r['shelf_id']==shelf]
            self._send_json(200, {'run_id':READINESS.run_id,'entries':entries})
            return
        if path == "/shelf_readiness":
            shelves = [r['shelf_id'] for r in get_cached_compartments() if r.get('shelf_id')]
            for shelf in shelves:
                if not mqtt_bridge.is_connected and PROCESSOR_AUTHORITY != 'service':
                    # Service mode counts over ESP32 HTTP; MQTT loss only pauses actions (see native.actions_paused_reason).
                    READINESS.set(shelf,'Offline','MQTT unavailable')
                    continue
                old=READINESS.get(shelf)
                if old['state']=='Offline':
                    retries=getattr(READINESS,'recovery_retries',{})
                    if time.monotonic()>=retries.get(shelf,0):
                        retries[shelf]=time.monotonic()+30;READINESS.recovery_retries=retries
                        READINESS.set(shelf,'Starting','Recovery requires fresh validation, not cached inventory')
                if old['state'] in {'Checking Shelf','Validating Action'} and not RECHECKS.busy(shelf):
                    if (runtime:=globals().get('RUNTIME_INVENTORY')) is not None:
                        runtime.hold(shelf)
                        with CCTV_CONSENSUS_LOCK:
                            CCTV_ALIGN_STATE.clear();PENDING_CCTV_TRIGGERS[:]=[t for t in PENDING_CCTV_TRIGGERS if t.get('shelf_id')!=shelf]
                        with DUAL_CAM_CAPTURE_LOCK:
                            for key in list(DUAL_CAM_CAPTURES):
                                if DUAL_CAM_CAPTURES[key].get('shelf_id')==shelf:DUAL_CAM_CAPTURES.pop(key,None)
                    READINESS.set(shelf,'Held','Verification interrupted or lease expired; retry verification')
                # Monitoring is local; validation/recovery always reads fresh state.
            authority, processor = globals().get('RUNTIME_INVENTORY'), globals().get('AUTONOMOUS')
            shelf_views = {}
            for key in shelves:
                row = READINESS.get(key)
                if authority is not None:
                    snapshot = authority.snapshot(key)
                    row = dict(row, runtime=snapshot)
                    if processor is not None:
                        row['native'] = processor.shelf_status(key, snapshot)
                shelf_views[key] = row
            self._send_json(200, {"run_id": READINESS.run_id, "processor_mode": PROCESSOR_AUTHORITY,
                'mqtt_connected':mqtt_bridge.is_connected,
                'inventory_mode':'runtime' if globals().get('RUNTIME_INVENTORY') else 'persistent',
                "shelves": shelf_views})
            return

        query = urllib.parse.parse_qs(parsed.query)

        if path in ["/dashboard", "/ui"]:
            index_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "index.html"))
            html_content = None
            if os.path.isfile(index_path):
                try:
                    with open(index_path, "r", encoding="utf-8") as f:
                        html_content = f.read()
                except Exception:
                    pass

            if html_content is not None:
                content_bytes = html_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content_bytes)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(content_bytes)
                return
            else:
                self._send_json(404, {"error": "Dashboard index.html file not found"})
                return

        # Teachable Machine Cache-Busting Proxy (/tm_proxy/<timestamp>/<filename>)
        if path.startswith("/tm_proxy/"):
            filename = path.split("/")[-1]
            global LAST_TM_BASE_URL
            # Source travels in the directory, so TF.js relative weight requests keep
            # the same model binding without depending on a global or query string.
            source_parts = path.split("/")
            base_url = (urllib.parse.unquote(source_parts[3]) if path.startswith("/tm_proxy/source/")
                        and len(source_parts) == 6 else query.get("base", [LAST_TM_BASE_URL])[0])
            if not base_url.endswith("/"):
                base_url += "/"
            if "base" in query:
                LAST_TM_BASE_URL = base_url

            clean_filename = filename.split("?")[0]
            def fetch_model_asset(source,name):
                req = urllib.request.Request(
                    f"{source}{name}?t={int(time.time() * 1000)}",
                    headers={
                        "User-Agent": "SmartStorageVision/1.0",
                        "Cache-Control": "no-cache, no-store",
                        "Pragma": "no-cache",
                    },
                )
                with urllib.request.urlopen(req, timeout=7.0) as resp:
                    return resp.read(50_000_001)
            try:
                data_bytes=TM_MODEL_ASSETS.read(base_url,clean_filename,fetch_model_asset)
            except (OSError,ValueError,KeyError,TypeError):
                self._send_json(502, {"error":"Model asset unavailable; check the staged models or diagnostic source"})
                return

            if data_bytes is not None:
                ctype = "application/json" if clean_filename.endswith(".json") else "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data_bytes)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(data_bytes)
                return
            else:
                self._send_json(502, {"error": f"Failed to fetch Teachable Machine asset {clean_filename}"})
                return

        # Serve local ONNX and Teachable Machine assets from all model directories.
        if path.startswith("/models/"):
            filename = urllib.parse.unquote(path[len("/models/"):])
            self._serve_model_asset(filename)
            return

        if path in ["/", "/status", "/health"]:
            active_pipes = [
                {"cctv_id": k, "pid": v["proc"].pid, "source": v["source"], "running": (v["proc"].poll() is None)}
                for k, v in list(RUNNING_PIPELINES.items()) if v.get("proc") and v["proc"].poll() is None
            ]
            proto = "https" if BRIDGE_SSL_ENABLED else "http"
            self._send_json(200, {
                "status": "ok",
                "service": "Smart Storage Local Camera Control & MQTT Telemetry Service",
                "protocol": proto,
                "bridge_url": BRIDGE_PUBLIC_URL or f"{proto}://localhost:8008",
                "opencv_available": _OPENCV_AVAILABLE,
                "startup_id": READINESS.run_id, "heartbeat_at": time.time(),
                "edge_processor": AUTONOMOUS.snapshot() if AUTONOMOUS else EDGE_SHADOW.snapshot() if EDGE_SHADOW else {'mode':'browser','authoritative_processor':'browser'},
                "health": "degraded" if not mqtt_bridge.is_connected else "checking" if not READINESS.states else "ready" if all(r["state"]=="Ready" for r in READINESS.states.values()) else "degraded",
                "mqtt_connected": mqtt_bridge.is_connected,
                "mqtt_broker": f"{mqtt_bridge.broker_address}:{mqtt_bridge.port}",
                "active_esp32_nodes": len(mqtt_bridge.edge_nodes),
                "running_pipelines": active_pipes,
                "latest_comparisons_count": len(LATEST_COMPARISONS_DEQUE),
                "port": 8008,
            })
            return

        if path in ["/api/comparisons/latest", "/comparisons/latest"]:
            self._send_json(200, RUNTIME_INVENTORY.latest_changes(identity=query.get('id',[None])[0],include_images=True) if globals().get('RUNTIME_INVENTORY') else list(LATEST_COMPARISONS_DEQUE))
            return

        if path == "/test_feed":
            source_param = query.get("source", [None])[0]
            if not source_param:
                source_param = get_active_rtsp_url(default_source=0)
            result = capture_single_frame(source_param)
            camera_id = query.get("cctv_id", [None])[0]
            if camera_id in CCTV_CAMERA_IDS and result.get("status") == "success":
                with CCTV_CONSENSUS_LOCK:
                    CCTV_LAST_SEEN_TS[camera_id] = time.time()
                if EDGE_SHADOW:
                    EDGE_SHADOW.submit('pose', identity=f"{camera_id}:{hashlib.sha256(str(source_param).encode()).hexdigest()}:{result.get('seq')}",
                        observed_at=time.time(), camera_id=camera_id, image=result.get('image'))
            self._send_json(200 if result.get("status") == "success" else 502, result)
            return

        if path == "/esp32_nodes":
            nodes = get_merged_compartments_and_nodes()
            logs = list(LATEST_COMPARISONS_DEQUE)
            discovered = get_discovered_cameras_list(nodes)

            # Expose pending CCTV triggers (retain recent items within last 60s, max 20)
            # Dashboard deduplicates by trigger_id via processedCctvTriggers set
            now = time.time()
            with CCTV_CONSENSUS_LOCK:
                pending = [dict(t) for t in PENDING_CCTV_TRIGGERS
                           if not t.get("claimed") and (now - t.get("timestamp", 0)) < 60.0][-20:]

            active_pipes = [
                {"cctv_id": k, "pid": v["proc"].pid, "source": v["source"], "running": (v["proc"].poll() is None)}
                for k, v in list(RUNNING_PIPELINES.items()) if v.get("proc") and v["proc"].poll() is None
            ]
            self._send_json(200, {
                "status": "success",
                "rack_id": spatial_mapper.rack_id,
                "mqtt_connected": mqtt_bridge.is_connected,
                "mqtt_broker": f"{mqtt_bridge.broker_address}:{mqtt_bridge.port}",
                "compartments": nodes,
                "discovered_cameras": discovered,
                "recent_responses": list(reversed(mqtt_bridge.recent_responses[-15:])),
                "comparison_logs": logs,
                "latest_5_actions": logs,
                "pending_cctv_triggers": pending,
                "running_pipelines": active_pipes,
            })
            return

        if path == "/esp32_discover":
            subnet_prefix = query.get("subnet_prefix", [None])[0]
            discovered = discover_active_esp32_cameras(subnet_prefix=subnet_prefix)
            self._send_json(200, {
                "status": "success",
                "discovered_cameras": discovered,
            })
            return

        if path in ("/esp32_raw_snapshot", "/esp32_snapshot_raw"):
            host = str(query.get("ip", [""])[0]).split("//")[-1].split("/")[0].strip()
            mode = query.get("mode", ["live"])[0]
            try:
                flash = max(0, min(255, int(query.get("flash", ["0"])[0])))
                if not host or not re.fullmatch(r"[A-Za-z0-9.:-]+", host) or mode not in ("live", "baseline"):
                    raise ValueError("Invalid snapshot IP or mode")
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                req = urllib.request.Request(f"http://{host}/capture?mode={mode}&flash={flash}",
                                             headers={"User-Agent": "SmartStorageSnapshot/1.0"})
                with urllib.request.urlopen(req, timeout=5.0) as response:
                    image_bytes = response.read()
                if not image_bytes.startswith(b"\xff\xd8"):
                    raise ValueError("Camera did not return a JPEG image")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(image_bytes)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(image_bytes)
            except Exception as exc:
                record_probe_result(host)
                self._send_json(502, {"status": "failed", "error": str(exc)})
            return

        if path == "/esp32_preview":
            t0 = time.time()
            esp32_ip = query.get("ip", [None])[0]
            esp32_ip2 = query.get("ip2", [None])[0]
            shelf_id = query.get("shelf_id", ["SHELF_R1_C1"])[0]
            boost = query.get("boost", ["auto"])[0]
            # TM owns crop and enhancement in the browser, exactly once.
            tm_raw = query.get("tm_raw", ["0"])[0] == "1"
            try:
                duty = int(query["duty"][0]) if "duty" in query else None
                flash = int(query["flash"][0]) if "flash" in query else None
                if duty is not None and not 0 <= duty <= 255:
                    raise ValueError("duty must be between 0 and 255")
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if boost in ("auto", "high", "off"):
                esp32_ref_manager.vision_boost_mode = boost
            else:
                esp32_ref_manager.vision_boost_mode = "auto"

            clean_ip1 = str(esp32_ip).split("//")[-1].split("/")[0].strip() if (esp32_ip and str(esp32_ip).strip()) else None
            clean_ip2 = str(esp32_ip2).split("//")[-1].split("/")[0].strip() if (esp32_ip2 and str(esp32_ip2).strip()) else None

            if clean_ip1 or clean_ip2:
                prev_entry = ACTIVE_ESP32_IPS.get(shelf_id)
                prev_ip1 = prev_entry.get("ip1") if isinstance(prev_entry, dict) else (str(prev_entry) if prev_entry else "")
                prev_ip2 = prev_entry.get("ip2") if isinstance(prev_entry, dict) else ""
                ACTIVE_ESP32_IPS[shelf_id] = {
                    "ip1": clean_ip1 or prev_ip1,
                    "ip2": clean_ip2 or prev_ip2,
                }


            if esp32_ip and esp32_ip2:
                # Bind receipts to the lease that existed BEFORE camera HTTP requests.
                with RECHECKS.global_lock:
                    capture_token = RECHECKS.leases.get(shelf_id, (None,))[0] if RECHECKS.busy(shelf_id) else None
                # Dual-camera capture: Back Cam (ip) + Front Cam (ip2)
                frame_bgr, combined_url, url1, url2 = esp32_ref_manager.fetch_dual_esp32_frame(
                    esp32_ip1=esp32_ip,
                    esp32_ip2=esp32_ip2,
                    mode="live",
                    flash=flash,
                    duty=duty,
                    boost_mode="off" if tm_raw else boost,
                )
                latency_ms = int((time.time() - t0) * 1000)
                capture = {"id": str(uuid.uuid4()), "completed_at": time.time(), "duration_ms": latency_ms}
                with DUAL_CAM_CAPTURE_LOCK:
                    for key in list(DUAL_CAM_CAPTURES):
                        if time.time() - DUAL_CAM_CAPTURES[key]["completed_at"] > 60:
                            del DUAL_CAM_CAPTURES[key]
                    if len(DUAL_CAM_CAPTURES) >= 200:
                        DUAL_CAM_CAPTURES.pop(next(iter(DUAL_CAM_CAPTURES)))
                    DUAL_CAM_CAPTURES[capture["id"]] = {**capture, "shelf_id": shelf_id, "validation_token": capture_token, "complete": bool(url1 and url2),
                        'run_id':getattr(globals().get('READINESS'),'run_id',None),'ip1':esp32_ip,'ip2':esp32_ip2}

                if tm_raw and url1 and url2 and EDGE_SHADOW:
                    EDGE_SHADOW.submit('pair', identity=capture['id'], observed_at=capture['completed_at'],
                        shelf_id=shelf_id, image_back=url1, image_front=url2, boost=boost)

                self._send_json(200, {
                    "capture": capture,
                    "status": "success",
                    "shelf_id": shelf_id,
                    "ip": esp32_ip,
                    "ip2": esp32_ip2,
                    "image": combined_url,
                    "image_back": url1,
                    "image_front": url2,
                    "latency_ms": latency_ms,
                    "tm_raw": tm_raw,
                    "flash": 1 if flash is None else flash,
                    "duty": duty,
                })
                return

            frame_bgr, data_url = esp32_ref_manager.fetch_esp32_frame(esp32_ip=esp32_ip, mode="live", flash=flash, duty=duty, boost_mode="off" if tm_raw else boost)
            latency_ms = int((time.time() - t0) * 1000)

            self._send_json(200, {
                "status": "success",
                "shelf_id": shelf_id,
                "ip": esp32_ip,
                "image": data_url,
                "image_back": data_url,
                "image_front": None,
                "latency_ms": latency_ms,
            })
            return

        if path == "/esp32_ota_info":
            repo_param = query.get("repo_path")
            custom_repo: Optional[str] = repo_param[0] if (repo_param and len(repo_param) > 0) else None
            info = compile_and_locate_firmware(force_build=False, custom_repo_path=custom_repo)
            self._send_json(200, info)
            return

        if path == "/esp32_probe":
            raw_ip = query.get("ip", [""])[0]
            clean_ip = str(raw_ip).replace("http://", "").replace("https://", "").split("/")[0].strip()
            if not clean_ip:
                self._send_json(400, {"status": "failed", "error": "Missing ip parameter"})
                return
            t0 = time.time()
            try:
                req = urllib.request.Request(f"http://{clean_ip}/status", headers={"User-Agent": "SmartStorageBridgeProbe/1.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
                    data = json.loads(raw)
                    data["latency_ms"] = int((time.time() - t0) * 1000)
                    data["status"] = "ONLINE"
                    record_probe_result(clean_ip, data)
                    self._send_json(200, data)
                    return
            except Exception as exc:
                record_probe_result(clean_ip)
                self._send_json(502, {"status": "OFFLINE", "ip_address": clean_ip, "error": str(exc)})
                return

        self._send_json(404, {"error": "Endpoint not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if (globals().get('RUNTIME_INVENTORY') is not None or PROCESSOR_AUTHORITY == 'service') and path not in {'/esp32_assign_ips','/esp32_nvs_config','/esp32_ota_upload','/esp32_ota_flash','/reconciliation_context','/reconcile_inventory','/local_rois','/scan_request','/cctv_control'} and getattr(self,'_processor_capability',None) is not _PROCESSOR_CAPABILITY:
            self._send_json(403,{'error':'Service owns processing; normal dashboard operations are monitoring-only'})
            return
        body = self._read_json_body()

        if path == '/local_rois':
            self._install_local_rois(body)
            return

        if path == '/scan_request':
            self._request_scan(body)
            return

        if path == '/cctv_control':
            self._control_cctv(body)
            return

        if path == "/cctv_replay":
            try:
                self._send_json(200, CCTV_REPLAY_MANAGER.request(body))
            except (ValueError, TypeError, KeyError) as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
            return

        if path == "/cctv_alignment_config":
            try:
                self._send_json(200, configure_cctv_alignment(body.get("cameras")))
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
            return

        if path in ("/validation_begin", "/validation_end", "/validation_cancel", "/validation_renew", "/validation_enqueue"):
            shelf = str(body.get("shelf_id", ""))
            with RECHECKS.global_lock:
                if path == "/validation_cancel":
                    RECHECKS.cancel(body.get("request_id"))
                    self._send_json(200, {"cancelled": True})
                elif path == "/validation_enqueue":
                    queued = RECHECKS.enqueue(shelf, body.get("source") or "MANUAL",
                        body.get("request_id") or shelf, body.get("created_at"))
                    self._send_json(200, {"queued": queued})
                elif path == "/validation_begin":
                    source = body.get("source") or "MANUAL"
                    if source in {'CCTV','PERIODIC_RECHECK','COUNT_CHECK'} and not READINESS.ready(shelf):
                        RECHECKS.cancel(body.get('request_id'))
                        self._send_json(409, {'error':'Shelf not Ready; interaction not validated, retry only after readiness'})
                        return
                    # An unclaimed live CCTV event in another tab also has priority.
                    with CCTV_CONSENSUS_LOCK:
                        live_waiting = any(t.get("source") == "CCTV" and not t.get("claimed")
                            and time.time()-t.get("timestamp", 0) < 60 for t in PENDING_CCTV_TRIGGERS)
                    if source in {"PERIODIC_RECHECK","COUNT_CHECK"} and live_waiting:
                        self._send_json(200, {"token": None})
                        return
                    token = RECHECKS.begin(shelf, source, body.get("request_id"), body.get("created_at"))
                    if token:
                        esp32_ref_manager.freeze_baseline(shelf, 120)
                    self._send_json(200, {"token": token})
                elif path == "/validation_renew":
                    renewed = RECHECKS.renew(shelf, body.get("token"))
                    if renewed:
                        esp32_ref_manager.freeze_baseline(shelf, 120)
                    self._send_json(200, {"renewed": renewed})
                else:
                    ended = RECHECKS.end(shelf, body.get("token"))
                    if ended:
                        with DUAL_CAM_CAPTURE_LOCK:
                            for identity in list(DUAL_CAM_CAPTURES):
                                if DUAL_CAM_CAPTURES[identity].get('validation_token')==body.get('token'):DUAL_CAM_CAPTURES.pop(identity,None)
                        esp32_ref_manager.baseline_freeze_until.pop(shelf, None)
                        trigger_id = body.get("trigger_id")
                        with CCTV_CONSENSUS_LOCK:
                            for queued_trigger in PENDING_CCTV_TRIGGERS:
                                if queued_trigger.get('trigger_id')==trigger_id and queued_trigger.get('validation_token')==body.get('token') and queued_trigger.get('retry_pending'):
                                    queued_trigger.update(claimed=False)
                                    queued_trigger.pop('validation_token',None);queued_trigger.pop('claimed_at',None)
                            PENDING_CCTV_TRIGGERS[:] = [t for t in PENDING_CCTV_TRIGGERS if not (t.get("trigger_id") == trigger_id and t.get("validation_token") == body.get("token"))]
                    self._send_json(200, {"released": ended})
            return

        if path == '/readiness_recovery':
            with RECHECKS.global_lock:
                if body.get('run_id') != READINESS.run_id:
                    self._send_json(409,{'error':'Bridge run changed; reload status'})
                    return
                if (authority:=globals().get('RUNTIME_INVENTORY')) is not None:
                    reset_runtime_evidence(authority=authority,coordinator=RECHECKS,consensus_lock=CCTV_CONSENSUS_LOCK,
                        pending=PENDING_CCTV_TRIGGERS,capture_lock=DUAL_CAM_CAPTURE_LOCK,captures=DUAL_CAM_CAPTURES,
                        votes=CCTV_ALIGN_STATE,readiness=READINESS,intents=LOCAL_INTENTS,reason='Connection recovered')
                    self._send_json(200,{'status':'checking','run_id':READINESS.run_id})
                    return
                RECHECKS.leases.clear();RECHECKS.waiting.clear()
                with CCTV_CONSENSUS_LOCK:
                    PENDING_CCTV_TRIGGERS.clear();CCTV_ALIGN_STATE.clear()
                with DUAL_CAM_CAPTURE_LOCK: DUAL_CAM_CAPTURES.clear()
                for shelf in list(READINESS.states):
                    READINESS.set(shelf,'Starting','Connection recovered; fresh verification required')
                self._send_json(200,{'status':'checking','run_id':READINESS.run_id})
            return

        if path in {'/reconciliation_context','/reconcile_inventory'}:
            if globals().get('RUNTIME_INVENTORY') is not None:
                self._send_json(409,{'error':'Runtime mode requires fresh camera recovery; persistent recount RPC is disabled'})
                return
            try:
                shelf=str(body.get('shelf_id',''))
                with RECHECKS.global_lock:
                    if path=='/reconciliation_context':
                        saved=operator_rpc(self.headers.get('Authorization'),'operator_reconciliation_context',{'p_shelf_id':shelf})
                        self._send_json(200,{'saved':saved,'operating':READINESS.get(shelf),
                            'observed':getattr(READINESS,'observations',{}).get(shelf), 'run_id':READINESS.run_id})
                    else:
                        if body.get('run_id') != READINESS.run_id or RECHECKS.busy():
                            raise ValueError('Bridge changed or validation busy; reload recount preview')
                        if body.get('confirmed') is not True: raise ValueError('Explicit physical recount confirmation required')
                        result=operator_rpc(self.headers.get('Authorization'),'reconcile_inventory',{
                            'p_shelf_id':shelf,'p_reconciliation_id':body.get('reconciliation_id'),
                            'p_expected_revision':body.get('revision'),'p_quantity':body.get('quantity'),
                            'p_slots':body.get('slots'),'p_reason':body.get('reason'),
                            'p_context':{'run_id':READINESS.run_id,'observed_slots':getattr(READINESS,'observations',{}).get(shelf,{}).get('slots')}})
                        state=result.get('state',{})
                        if result.get('status') not in {'accepted','duplicate'} or state.get('shelf_id')!=shelf:
                            raise ValueError('Reconciliation not confirmed')
                        CONFIRMED_OCCUPANCY[shelf]=tuple(state['confirmed_occupancy'])
                        for row in globals().get('SUPABASE_COMPARTMENTS_CACHE',{}).get('data',[]):
                            if row.get('shelf_id')==shelf:row['current_quantity']=state['current_quantity']
                        with CCTV_CONSENSUS_LOCK:
                            PENDING_CCTV_TRIGGERS[:]=[t for t in PENDING_CCTV_TRIGGERS if t.get('shelf_id')!=shelf]
                            CCTV_ALIGN_STATE.clear()
                        with DUAL_CAM_CAPTURE_LOCK:
                            for key in list(DUAL_CAM_CAPTURES):
                                if DUAL_CAM_CAPTURES[key].get('shelf_id')==shelf: DUAL_CAM_CAPTURES.pop(key,None)
                        READINESS.set(shelf,'Starting','Recount saved; fresh camera verification required',state['revision'])
                        event(READINESS.run_id,'reconciliation',shelf_id=shelf,revision=state['revision'],status=result['status'])
                        self._send_json(200,result)
            except PermissionError as exc: self._send_json(403,{'error':str(exc)})
            except (ValueError,KeyError,TypeError) as exc: self._send_json(409,{'error':str(exc)})
            return

        if path == "/validation_progress":
            shelf = str(body.get('shelf_id',''))
            with RECHECKS.global_lock:
                if not RECHECKS.owns(shelf,body.get('validation_token')):
                    self._send_json(409, {'error':'Validation lease expired'})
                    return
                if body.get('run_id') and body['run_id'] != READINESS.run_id:
                    self._send_json(409,{'error':'Bridge run changed; stale progress rejected'})
                    return
                old=READINESS.get(shelf)
                stage=str(body.get('stage',''))[:300]
                if body.get('failed'):
                    retry=globals().get('prepare_runtime_retry')
                    if retry and body.get('trigger_id') and retry(shelf,body.get('validation_token'),body.get('failure_code') or failure_code(stage),authority=RUNTIME_INVENTORY,coordinator=RECHECKS,consensus_lock=CCTV_CONSENSUS_LOCK,pending=PENDING_CCTV_TRIGGERS):
                        READINESS.set(shelf,'Ready','Nonterminal validation; one fresh retry pending')
                        self._send_json(200,{'status':'retry_pending'})
                        return
                    if (authority:=globals().get('RUNTIME_INVENTORY')) is not None:
                        changed=authority.hold(shelf,body.get('trigger_id'))
                        discard_runtime_attempt(shelf,body.get('validation_token'),coordinator=RECHECKS,
                            consensus_lock=CCTV_CONSENSUS_LOCK,pending=PENDING_CCTV_TRIGGERS,
                            capture_lock=DUAL_CAM_CAPTURE_LOCK,captures=DUAL_CAM_CAPTURES,votes=CCTV_ALIGN_STATE)
                        esp32_ref_manager.baseline_freeze_until.pop(shelf,None)
                        if not changed:
                            self._send_json(409,{'error':'Runtime event already terminal; current baseline preserved'})
                            return
                    if (intent_store := globals().get('LOCAL_INTENTS')) is not None and body.get('trigger_id'):
                        intent_store.update(body['trigger_id'],'HELD',failure_code(stage))
                    if old['state'] != 'Needs Reconciliation':
                        READINESS.set(shelf,'Offline' if any(word in stage.lower() for word in ('camera unavailable','preview failed','connection refused')) else 'Held',stage)
                elif old['state'] in {'Ready','Validating Action'}:
                    READINESS.set(shelf,'Validating Action',stage)
                elif old['state'] != 'Needs Reconciliation':
                    READINESS.set(shelf,'Checking Shelf',stage)
                READINESS.record(shelf,body.get('validation_token'),body.get('trigger_id'),
                    stage=stage,code=None if failure_code(stage)=='VALIDATION_REJECTED' and not body.get('failed') else failure_code(stage),
                    retryable=not body.get('failed') and failure_code(stage)!='UNKNOWN_B1',
                    **{k:body[k] for k in ('attempt','capture_number','max_attempts','boost','duty','front_confidence','back_confidence','capture_completed_at') if k in body})
                self._send_json(200, {'status':'recorded'})
            return

        if path == "/cctv_trigger_claim":
            self._send_json(200, {"claimed": claim_cctv_trigger(body.get("trigger_id"), body.get("shelf_id"), body.get("validation_token"))})
            return

        if path == "/run_calibration":
            source = body.get("source") or get_active_rtsp_url(default_source=0)
            cctv_id = body.get("cctv_id") or "CCTV_01"
            script_path = os.path.join(BACKEND_DIR, "calibrate_roi.py")
            if not os.path.isfile(script_path):
                self._send_json(500, {"status": "failed", "error": f"Script not found at {script_path}"})
                return

            masked = mask_rtsp_url(source)
            print(f"[CameraService] Spawning ROI Calibration GUI for [{cctv_id}]: {masked}")
            try:
                cmd = [sys.executable, script_path, "--source", str(source), "--cctv-id", str(cctv_id)]
                proc = subprocess.Popen(
                    cmd,
                    cwd=BACKEND_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._send_json(200, {
                    "status": "launched",
                    "cctv_id": cctv_id,
                    "pid": proc.pid,
                    "source": masked,
                    "message": f"Shelf ROI Calibration window for {cctv_id} opened on local desktop.",
                })
            except Exception as exc:
                self._send_json(500, {"status": "failed", "error": f"Failed to spawn calibration process: {str(exc)}"})
            return

        # RUN YOLO11n-Pose PIPELINE for specific CCTV
        if path == "/run_pipeline":
            cctv_id = str(body.get("cctv_id") or "CCTV_01").strip()
            source = body.get("source") or get_active_rtsp_url(default_source=0)
            privacy = bool(body.get("privacy", False))
            script_path = os.path.join(BACKEND_DIR, "run_pipeline.py")

            if not os.path.isfile(script_path):
                self._send_json(500, {"status": "failed", "error": f"Pipeline script not found at {script_path}"})
                return

            # Terminate existing running process for this cctv_id if active
            existing = RUNNING_PIPELINES.get(cctv_id)
            if existing and existing.get("proc") and existing["proc"].poll() is None:
                try:
                    existing["proc"].terminate()
                    time.sleep(0.3)
                except Exception as e:
                    print(f"[CameraService] Error stopping previous pipeline for {cctv_id}: {e}")

            masked = mask_rtsp_url(source)
            print(f"[CameraService] Launching YOLO11n-Pose pipeline for [{cctv_id}]: {masked}")
            try:
                cmd = [sys.executable, script_path, "--source", str(source), "--cctv-id", str(cctv_id)]
                if privacy:
                    cmd.append("--privacy")
                proc = subprocess.Popen(
                    cmd,
                    cwd=BACKEND_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                RUNNING_PIPELINES[cctv_id] = {
                    "proc": proc,
                    "cctv_id": cctv_id,
                    "source": masked,
                    "started_at": now_iso,
                    "pid": proc.pid,
                }
                self._send_json(200, {
                    "status": "launched",
                    "cctv_id": cctv_id,
                    "pid": proc.pid,
                    "source": masked,
                })
            except Exception as exc:
                self._send_json(500, {"status": "failed", "error": f"Failed to spawn pipeline: {str(exc)}"})
            return

        # STOP YOLO11n-Pose PIPELINE
        if path == "/stop_pipeline":
            cctv_id = body.get("cctv_id")
            stop_all = bool(body.get("all", False))
            stopped = []

            for cid, item in list(RUNNING_PIPELINES.items()):
                if stop_all or cid == cctv_id:
                    proc = item.get("proc")
                    if proc and proc.poll() is None:
                        try:
                            proc.terminate()
                            stopped.append(cid)
                        except Exception as e:
                            print(f"[CameraService] Error terminating pipeline {cid}: {e}")
                    del RUNNING_PIPELINES[cid]

            self._send_json(200, {
                "status": "stopped",
                "cctv_id": cctv_id,
                "stopped_pipelines": stopped,
            })
            return

        # ASSIGN DUAL CAMERA IPs TO SHELF: Update runtime mappings; never PATCH compartment IP/telemetry columns
        if path == "/esp32_assign_ips":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            raw_ip1 = str(body.get("ip_address") or body.get("ip") or "").split("//")[-1].split("/")[0].strip()
            raw_ip2 = str(body.get("secondary_ip_address") or body.get("ip2") or "").split("//")[-1].split("/")[0].strip()

            if shelf_id not in SHELF_SET or not raw_ip1 or not raw_ip2:
                self._send_json(400, {"error": "Shelf and both camera addresses must be configured"})
                return

            ACTIVE_ESP32_IPS[shelf_id] = {
                "ip1": raw_ip1,
                "ip2": raw_ip2,
            }

            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if raw_ip1:
                update_esp32_health_in_supabase({
                    "device_id": f"ESP32_{shelf_id.replace('SHELF_', '')}",
                    "shelf_id": shelf_id,
                    "ip_address": raw_ip1,
                    "status": "ONLINE",
                    "last_seen_at": now_iso,
                })
            if raw_ip2:
                update_esp32_health_in_supabase({
                    "device_id": f"ESP32_{shelf_id.replace('SHELF_', '')}_CAM2",
                    "shelf_id": shelf_id,
                    "ip_address": raw_ip2,
                    "status": "ONLINE",
                    "last_seen_at": now_iso,
                })

            get_cached_compartments(force_refresh=True)
            self._send_json(200, {
                "status": "success",
                "shelf_id": shelf_id,
                "ip_address": raw_ip1,
                "secondary_ip_address": raw_ip2,
            })
            return

        if path == "/cctv_invalidate_votes":
            with CCTV_CONSENSUS_LOCK:
                CCTV_ALIGN_STATE.clear()
            self._send_json(200, {"status": "success"})
            return

        # MANUAL TRIGGER: Dispatch CAPTURE_STATE or CAPTURE_REFERENCE over MQTT
        if path == "/esp32_trigger":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            command = str(body.get("command", "CAPTURE_STATE")).upper()
            expected_action = str(body.get("expected_action", "")).upper()
            rack_id = str(body.get("rack_id", spatial_mapper.rack_id)).strip()
            source = str(body.get("source", "MANUAL")).upper()

            if source not in {"CCTV", "CCTV_BROWSER_DWELL"} and not expected_action:
                expected_action = "BORROW"
            consensus = None
            if source in {"CCTV", "CCTV_BROWSER_DWELL"}:
                try:
                    consensus = record_cctv_consensus(
                        shelf_id=shelf_id,
                        camera_id=str(body.get("camera_id") or body.get("cctv_id") or "CCTV_01").strip(),
                        action=expected_action,
                        confidence=float(body.get("oks_confidence", body.get("confidence", body.get("conf", 1.0)))),
                        camera_role=body.get("camera_role"),
                        trigger_id=body.get("trigger_id"),
                        dwell_duration_seconds=float(body.get("dwell_duration_seconds", 1.2)),
                        detected_at=body.get("detected_at"),
                        queue_ready=READINESS.ready,
                    )
                except (ValueError, TypeError) as exc:
                    self._send_json(400, {"status": "error", "error": str(exc)})
                    return
                if not consensus["consensus_met"]:
                    self._send_json(200, consensus)
                    return
                if (consensus.get("shelf_id") not in SHELF_SET
                        or consensus.get("expected_action") not in {"BORROW", "RETURN", "RECHECK"}
                        or not isinstance(consensus.get("confidence"), (int, float))
                        or not math.isfinite(consensus["confidence"]) or not 0 <= consensus["confidence"] <= 1
                        or not isinstance(consensus.get("trigger_id"), str) or not consensus["trigger_id"]):
                    self._send_json(500, {"status": "error", "error": "Invalid consensus; no capture dispatched"})
                    return
                shelf_id = consensus["shelf_id"]
                expected_action = consensus["expected_action"]
                command = "CAPTURE_STATE"
                if not consensus.get('inventory_eligible',True):
                    self._send_json(200,{**consensus,'status':'consensus_only','mqtt_connected':mqtt_bridge.is_connected,
                        'dispatched':None,'message':'All cameras agreed; shelf baseline is not Ready. No action queued or capture dispatched.'})
                    return

            # Guardrail 1: Freeze paired observation for 10s during interaction
            esp32_ref_manager.freeze_baseline(shelf_id, 10.0)

            if command == "CAPTURE_REFERENCE":
                box_count = int(body.get("box_count", 5))
                sample_label = body.get("sample_label")
                dispatched = mqtt_bridge.publish_reference_capture(
                    shelf_id=shelf_id,
                    box_count=box_count,
                    sample_label=sample_label,
                    rack_id=rack_id,
                )
            else:
                # CCTV events were queued atomically at consensus; manual triggers remain direct.
                if consensus is None:
                    with CCTV_CONSENSUS_LOCK:
                        trigger_id = f"trig_{shelf_id}_{int(time.time() * 1000)}"
                        PENDING_CCTV_TRIGGERS.append({
                            "trigger_id": trigger_id,
                            "shelf_id": shelf_id,
                            "expected_action": expected_action,
                            "source": source,
                            "timestamp": time.time(),
                        })
                        if len(PENDING_CCTV_TRIGGERS) > 20:
                            del PENDING_CCTV_TRIGGERS[:-20]

                dispatched = mqtt_bridge.publish_trigger(
                    shelf_id=shelf_id,
                    expected_action=expected_action,
                    confidence=consensus["confidence"] if consensus else 1.0,
                    rack_id=rack_id,
                )

            self._send_json(200, {
                "status": "success",
                **(consensus or {}),
                "mqtt_connected": mqtt_bridge.is_connected,
                "dispatched": dispatched,
            })
            return

        # VALIDATE: Run paired observation + Teachable Machine validation and apply Supabase delta
        if path == "/esp32_validate":
            if globals().get('RUNTIME_INVENTORY') is not None:
                self._runtime_validate(body)
                return
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            with RECHECKS.global_lock:
                if RECHECKS.busy() and not RECHECKS.owns(shelf_id, body.get("validation_token")):
                    self._send_json(409, {"error": "Shelf validation already active"})
                    return
                if not RECHECKS.owns(shelf_id, body.get("validation_token")):
                    self._send_json(409, {"error": "Validation lease expired; start a fresh validation"})
                    return
                boost = body.get("boost") or "auto"
                if boost in ("auto", "high", "off"):
                    esp32_ref_manager.vision_boost_mode = boost
                # Guardrail 1: Freeze paired observation for 10s during interaction
                esp32_ref_manager.freeze_baseline(shelf_id, 10.0)

                mqtt_client_id = str(body.get("mqtt_client_id") or f"ESP32_{shelf_id.replace('SHELF_', '')}").strip()
                expected_action = str(body.get("expected_action", "BORROW")).upper()
                esp32_ip = body.get("ip_address") or body.get("ip")
                esp32_ip2 = body.get("ip2") or body.get("secondary_ip_address") or body.get("secondary_ip")
                after_image = body.get("image") or body.get("image_url")
                baseline_box_count = body.get("baseline_box_count")
                predicted_box_count = body.get("predicted_box_count")
                if predicted_box_count is None and "box_count" in body:
                    predicted_box_count = body.get("box_count")
                scenario_label = body.get("scenario_label")
                tm_confidence = body.get("tm_confidence") or body.get("confidence")

                source = body.get("source") or "MANUAL"
                periodic = source == "PERIODIC_RECHECK"
                if not mqtt_bridge.is_connected:
                    READINESS.set(shelf_id,'Offline','MQTT unavailable; fresh verification required on recovery')
                    self._send_json(503,{'error':'MQTT unavailable; inventory validation held'})
                    return
                # Read authoritative quantity; cache refresh may silently return stale rows.
                try:
                    authoritative_state = load_inventory_state(shelf_id)
                    curr_qty = authoritative_state['current_quantity']
                    CONFIRMED_OCCUPANCY.pop(shelf_id, None)
                    if authoritative_state['confirmed_occupancy'] is not None:
                        CONFIRMED_OCCUPANCY[shelf_id] = tuple(authoritative_state['confirmed_occupancy'])
                except Exception:
                    CONFIRMED_OCCUPANCY.pop(shelf_id, None)
                    READINESS.set(shelf_id,"Offline","Authoritative database unavailable")
                    self._send_json(503, {"error": "Fresh persistent inventory unavailable; check Phase 1 migration and database access; no update applied"})
                    return
                if curr_qty < 0 or curr_qty > 5:
                    self._send_json(409, {"error": "Five-slot layout requires an explicit physical recount and quantity migration; existing quantity was retained"})
                    return
                if authoritative_state['confirmed_occupancy'] is None and source != 'STARTUP':
                    READINESS.set(shelf_id,'Needs Reconciliation','Authorized physical recount required to commission an unverified shelf')
                    self._send_json(403,{'error':'Authorized physical recount required; a default quantity or anonymous Full Validation cannot commission inventory'})
                    return
                cctv_event: Optional[Dict[str, Any]] = None
                try:
                    if source == "CCTV":
                        with CCTV_CONSENSUS_LOCK:
                            events = [t for t in PENDING_CCTV_TRIGGERS if t.get("shelf_id") == shelf_id
                                      and t.get("trigger_id") == body.get("trigger_id")
                                      and t.get("validation_token") == body.get("validation_token")
                                      and t.get("timestamp",0) >= READINESS.get(shelf_id).get("ready_at",0)
                                      and t.get("claimed") and t.get("consensus_met")
                                      and t.get("expected_action") in {"BORROW", "RETURN"}
                                      and 0 <= time.time() - t.get("timestamp", 0) < 60]
                        if len(events) != 1:
                            raise ValueError("Fresh CCTV consensus bound to this trigger and validation lease is required")
                        selected_event = events[0]
                        if not isinstance(selected_event, dict):
                            raise ValueError("Fresh CCTV consensus bound to this trigger and validation lease is required")
                        cctv_event = selected_event
                        expected_action = selected_event["expected_action"]
                    elif periodic or source == "STARTUP":
                        expected_action = "RECHECK"
                    elif source != "MANUAL" or expected_action not in {"BORROW", "RETURN"}:
                        raise ValueError("Expected BORROW or RETURN action is required")
                except ValueError as exc:
                    READINESS.record(shelf_id,body.get('validation_token'),body.get('trigger_id'),stage=str(exc)[:300],code=failure_code(exc),retryable=False)
                    self._send_json(409, {"status": "VALIDATION_PENDING", "code": failure_code(exc), "error": str(exc)})
                    return
                try:
                    try:
                        sample=body.get('dual_cam_evidence',{}).get('samples',[{}])[-1]
                        observed=decode_observation(sample.get('front_class'),sample.get('back_class'))
                        observed.update(front_confidence=sample.get('front_confidence'),back_confidence=sample.get('back_confidence'),unverified=True)
                        if not hasattr(READINESS,'observations'): READINESS.observations={}
                        READINESS.observations[shelf_id]=observed
                    except (ValueError,TypeError,IndexError): pass
                    with DUAL_CAM_CAPTURE_LOCK:
                        slots, capture_ids = validate_evidence(body.get("dual_cam_evidence"), predicted_box_count,
                            time.time(), DUAL_CAM_CAPTURES, shelf_id, body.get("validation_token"),
                            max(cctv_event["timestamp"], cctv_event["claimed_at"]) if cctv_event is not None else None)
                    if not after_image or not body.get('image_back') or not body.get('image_front'):
                        raise ValueError('Both camera images are required')
                    if source in {'CCTV','PERIODIC_RECHECK'} and not READINESS.active(shelf_id):
                        raise ValueError('Shelf not Ready; interaction not validated')
                    if source == 'STARTUP' or (not READINESS.active(shelf_id) and authoritative_state['confirmed_occupancy'] is not None):
                        READINESS.verify(shelf_id,authoritative_state,slots)
                        with DUAL_CAM_CAPTURE_LOCK:
                            for capture_id in capture_ids: DUAL_CAM_CAPTURES.pop(capture_id,None)
                        # Startup verification does not create a BORROW/RETURN or rewrite inventory.
                        self._send_json(200, {'status':'success','readiness':'Ready','occupancy':list(slots),
                            'comparison':{'delta':0,'quantity_before':curr_qty,'quantity_after':curr_qty,'state_delta':'NO_CHANGE'}})
                        return
                    if source in {'CCTV','PERIODIC_RECHECK'} and not READINESS.active(shelf_id):
                        raise ValueError('Shelf not Ready; interaction not validated')
                    initializing_snapshot = shelf_id not in CONFIRMED_OCCUPANCY
                    if READINESS.active(shelf_id):
                        READINESS.set(shelf_id,"Validating Action","Expected-action check")
                    validate_transition(CONFIRMED_OCCUPANCY.get(shelf_id), slots, curr_qty, expected_action)
                    if not after_image or not body.get("image_back") or not body.get("image_front"):
                        raise ValueError("Both camera images are required")
                except (ValueError, TypeError, KeyError) as exc:
                    if READINESS.get(shelf_id)['state'] != 'Needs Reconciliation':
                        READINESS.set(shelf_id,'Needs Reconciliation' if 'Occupancy change conflicts' in str(exc) else 'Held',str(exc))
                    READINESS.record(shelf_id,body.get('validation_token'),body.get('trigger_id'),stage=str(exc)[:300],code=failure_code(exc),retryable=False)
                    self._send_json(400, {"status": "VALIDATION_PENDING", "code": failure_code(exc), "error": str(exc)})
                    return
                # Derive accepted count/confidence from the validated contract, never a legacy heuristic.
                predicted_box_count = sum(slots)
                tm_confidence = min(min(sample["front_confidence"], sample["back_confidence"])
                                    for sample in body["dual_cam_evidence"]["samples"])
                scenario_label = f"{predicted_box_count}/5 Boxes Present | M2 {'SKIPPED_OCCLUDED' if slots[4] else 'PASS'}"
                with DUAL_CAM_CAPTURE_LOCK:
                    for capture_id in capture_ids:
                        DUAL_CAM_CAPTURES.pop(capture_id, None)

                previous_baseline = esp32_ref_manager.baseline_cache.get(shelf_id)
                comp_res = esp32_ref_manager.compare_with_30s_baseline_and_references(
                    mqtt_client_id=mqtt_client_id,
                    shelf_id=shelf_id,
                    expected_action=expected_action,
                    current_quantity=curr_qty,
                    esp32_ip=esp32_ip,
                    esp32_ip2=esp32_ip2,
                    after_data_url=after_image,
                    baseline_box_count=int(baseline_box_count) if baseline_box_count is not None else None,
                    predicted_box_count=int(predicted_box_count) if predicted_box_count is not None else None,
                    scenario_label=scenario_label,
                    tm_confidence=float(tm_confidence) if tm_confidence is not None else None,
                )

                log_entry: Optional[Dict[str, Any]] = None
                committed_state: Optional[Dict[str, Any]] = None
                if predicted_box_count is not None:
                    delta_val = int(comp_res.get("delta", 0))
                    derived_action = "BORROW" if delta_val < 0 else ("RETURN" if delta_val > 0 else expected_action)
                    before_img = comp_res.get("before_image") or comp_res.get("before_image_url")
                    after_img = after_image or comp_res.get("after_image") or comp_res.get("after_image_url")

                    # Persist even NO_CHANGE commissioning/verification. One database
                    # transaction owns revision checks, slot/quantity writes and event receipt.
                    event_id = cctv_event['trigger_id'] if cctv_event is not None else 'capture:' + body['dual_cam_evidence']['samples'][0]['capture']['id']
                    try:
                        if READINESS.active(shelf_id): READINESS.set(shelf_id,"Validating Action","Saving inventory")
                        committed_state = commit_inventory_state(authoritative_state, slots, expected_action, event_id)
                    except Exception:
                        if previous_baseline is None:
                            esp32_ref_manager.baseline_cache.pop(shelf_id, None)
                        else:
                            esp32_ref_manager.baseline_cache[shelf_id] = previous_baseline
                        CONFIRMED_OCCUPANCY.pop(shelf_id, None)
                        try:
                            restored = load_inventory_state(shelf_id)
                            if restored['confirmed_occupancy'] is not None:
                                CONFIRMED_OCCUPANCY[shelf_id] = tuple(restored['confirmed_occupancy'])
                        except Exception:
                            pass
                        READINESS.set(shelf_id,"Held","Database commit not confirmed; verify again")
                        READINESS.record(shelf_id,body.get("validation_token"),event_id,stage="Atomic database commit not confirmed",code="DATABASE_FAILURE",retryable=False)
                        self._send_json(502, {"status": "failed", "code": "DATABASE_FAILURE", "error": "Atomic inventory commit not confirmed; state reloaded where available. Check migration, server credential, revision and database availability."})
                        return
                    get_cached_compartments(force_refresh=True)

                    if not isinstance(log_entry, dict):
                        log_entry = {
                            "log_id": str(uuid.uuid4()),
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "shelf_id": shelf_id,
                            "rack_id": spatial_mapper.rack_id,
                            "mqtt_client_id": mqtt_client_id,
                            "trigger_action": derived_action,
                            "diff_score": comp_res.get("diff_score", 0.0),
                            "similarity_score": comp_res.get("similarity_score", 0.0),
                            "matched_box_count": comp_res.get("matched_box_count", predicted_box_count),
                            "scenario_label": comp_res.get("scenario_label") or scenario_label or f"{predicted_box_count} Boxes",
                            "state_delta": "ITEM_REMOVED" if delta_val < 0 else ("ITEM_RETURNED" if delta_val > 0 else "NO_CHANGE"),
                            "quantity_before": comp_res.get("quantity_before", curr_qty),
                            "quantity_after": comp_res.get("quantity_after", predicted_box_count),
                        }
                    log_entry["before_image_url"] = before_img
                    log_entry["after_image_url"] = after_img
                    log_entry["log_id"] = str(uuid.uuid4())
                    log_entry["state_delta"] = "ITEM_REMOVED" if delta_val < 0 else ("ITEM_RETURNED" if delta_val > 0 else "NO_CHANGE")
                    log_entry["source"] = source
                    log_entry["trigger_source"] = source
                    if periodic:
                        log_entry["scenario_label"] = "Periodic Recheck (Backup) | " + str(log_entry.get("scenario_label") or "")
                    for quantity_key in ("quantity_before", "quantity_after", "matched_box_count"):
                        if log_entry.get(quantity_key) is not None:
                            log_entry[quantity_key] = max(0, min(5, int(log_entry[quantity_key])))
                    if not periodic or delta_val != 0:
                        LATEST_COMPARISONS_DEQUE.appendleft(log_entry)
                    else:
                        log_entry = None
                    if cctv_event is not None:
                        with CCTV_CONSENSUS_LOCK:
                            PENDING_CCTV_TRIGGERS[:] = [t for t in PENDING_CCTV_TRIGGERS
                                if not (t.get("trigger_id") == cctv_event["trigger_id"]
                                        and t.get("validation_token") == body.get("validation_token"))]

                if isinstance(after_image, str) and after_image and body.get("image_back") and body.get("image_front"):
                    decoded = decode_data_url_to_bgr(after_image) if isinstance(after_image, str) and after_image else None
                    if decoded is not None:
                        esp32_ref_manager.baseline_cache[shelf_id] = dict(
                            frame_bgr=decoded, data_url=after_image, data_url_2=body["image_front"],
                            url_back=body["image_back"], url_front=body["image_front"],
                            updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                CONFIRMED_OCCUPANCY[shelf_id] = slots
                for row in globals().get('SUPABASE_COMPARTMENTS_CACHE',{}).get('data',[]):
                    if row.get('shelf_id')==shelf_id:row['current_quantity']=predicted_box_count
                committed_revision = committed_state["revision"] if committed_state is not None else None
                READINESS.set(shelf_id,"Ready",f"Inventory updated ({curr_qty} -> {predicted_box_count}) after confirmed commit",committed_revision)
                if (intent_store := globals().get('LOCAL_INTENTS')) is not None and body.get('trigger_id'):
                    intent_store.update(body['trigger_id'],'ACCEPTED')
                self._send_json(200, {
                    "status": "success",
                    "occupancy": list(slots),
                    "snapshot_initialized": initializing_snapshot,
                    "revision": committed_revision,
                    "m2_check": "SKIPPED_OCCLUDED" if slots[4] else "PASS",
                    "comparison": comp_res,
                    "log": log_entry,
                    "latest_comparisons": list(LATEST_COMPARISONS_DEQUE),
                })
                return

        # PROXY NVS CONFIG PUSH TO ESP32-CAM (Avoids Vercel HTTPS -> LAN HTTP Mixed Content)
        if path == "/esp32_nvs_config":
            raw_ip = body.get("ip") or body.get("ip_address") or ""
            clean_ip = str(raw_ip).replace("http://", "").replace("https://", "").split("/")[0].strip()
            if not clean_ip:
                self._send_json(400, {"status": "failed", "error": "Missing target camera IP"})
                return
            nvs_payload = body.get("config") if isinstance(body.get("config"), dict) else {
                k: v for k, v in body.items() if k not in ("ip", "ip_address")
            }
            try:
                encoded = json.dumps(nvs_payload).encode("utf-8")
                req = urllib.request.Request(
                    f"http://{clean_ip}/config",
                    data=encoded,
                    method="POST",
                    headers={"Content-Type": "application/json", "User-Agent": "SmartStorageBridgeNVS/1.0"},
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
                    try:
                        res_json = json.loads(raw)
                    except Exception:
                        res_json = {"status": "ok", "raw": raw}
                    self._send_json(resp.status, res_json)
                    return
            except Exception as exc:
                self._send_json(502, {"status": "failed", "ip": clean_ip, "error": f"Failed to push NVS config to {clean_ip}: {exc}"})
                return

        # PROXY MANUAL .BIN OTA UPLOAD TO ESP32-CAM (Avoids Vercel HTTPS -> LAN HTTP Mixed Content)
        if path == "/esp32_ota_upload":
            raw_ip = body.get("ip") or body.get("ip_address") or ""
            clean_ip = str(raw_ip).replace("http://", "").replace("https://", "").split("/")[0].strip()
            file_b64 = body.get("file_base64") or body.get("firmware_base64") or ""
            if not clean_ip or not file_b64:
                self._send_json(400, {"status": "failed", "error": "Missing ip or file_base64 payload"})
                return
            if "," in file_b64:
                file_b64 = file_b64.split(",", 1)[1]
            tmp_path = None
            try:
                fw_bytes = base64.b64decode(file_b64)
                with tempfile.NamedTemporaryFile(prefix="esp32_ota_", suffix=".bin", delete=False) as tf:
                    tf.write(fw_bytes)
                    tmp_path = tf.name
                res = push_ota_firmware_to_esp32(clean_ip, tmp_path, timeout=45.0)
                self._send_json(200 if res.get("status") == "success" else 502, res)
                return
            except Exception as exc:
                self._send_json(500, {"status": "failed", "ip": clean_ip, "error": str(exc)})
                return
            finally:
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        # 1-CLICK PLATFORMIO BUILD & WIRELESS OTA FLASH (Batch or Single)
        if path == "/esp32_ota_flash":
            repo_path_val = body.get("repo_path")
            repo_path: Optional[str] = str(repo_path_val).strip() if repo_path_val else None
            build_first = bool(body.get("build", True))
            raw_ips = body.get("ips") or body.get("ip") or []
            if isinstance(raw_ips, str):
                target_ips = [raw_ips]
            elif isinstance(raw_ips, list):
                target_ips = [str(x) for x in raw_ips if x]
            else:
                target_ips = []

            if not target_ips:
                self._send_json(400, {"status": "failed", "error": "No target camera IP(s) provided"})
                return

            print(f"[CameraService OTA] Received flash request for {target_ips} (build={build_first}, repo={repo_path})")
            firmware_res = compile_and_locate_firmware(force_build=build_first, custom_repo_path=repo_path)
            if firmware_res.get("status") != "success":
                self._send_json(400, {
                    "status": "failed",
                    "error": firmware_res.get("error", "Firmware compilation or location failed"),
                    "firmware": firmware_res,
                })
                return

            bin_path = firmware_res["bin_path"]
            results = []
            for tip in target_ips:
                res = push_ota_firmware_to_esp32(tip, bin_path, timeout=45.0)
                results.append(res)

            all_ok = all(r.get("status") == "success" for r in results) if results else False
            status_code = 200 if all_ok else 207
            self._send_json(status_code, {
                "status": "success" if all_ok else ("partial_success" if any(r.get("status") == "success" for r in results) else "failed"),
                "firmware": firmware_res,
                "results": results,
            })
            return

        self._send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self):
        self._send_json(404, {"error": "Endpoint not found"})


_FRONT_OCCUPANCY_LABELS = {f"Front_{s}" for s in ("000", "010", "011", "100", "110", "111")}
_BACK_OCCUPANCY_LABELS = {f"Back_{s}" for s in ("000", "100", "X01", "11X")}


def _has_valid_occupancy_metadata(dir_path: str) -> bool:
    """True for an independent 6-class Front or 4-class Back export."""
    meta_path = os.path.join(dir_path, "metadata.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as mf:
            meta = json.load(mf)
        labels = [str(l).strip() for l in (meta.get("labels") or [])]
        if len(set(labels)) != len(labels):
            return False
        label_set = set(labels)
        return label_set in (
            _FRONT_OCCUPANCY_LABELS,
            _BACK_OCCUPANCY_LABELS,
        )
    except Exception:
        return False


def _resolve_ssl_cert_and_key(ssl_cert: Optional[str] = None, ssl_key: Optional[str] = None):
    """Locates local SSL cert and key from CLI flags, env vars, or standard mkcert filenames."""
    cert = ssl_cert or os.getenv("SSL_CERT_FILE") or ""
    key = ssl_key or os.getenv("SSL_KEY_FILE") or ""
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        return cert, key

    search_dirs = [
        os.path.abspath(os.path.dirname(__file__)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    ]
    candidate_pairs = [
        ("localhost.pem", "localhost-key.pem"),
        ("localhost+2.pem", "localhost+2-key.pem"),
        ("cert.pem", "key.pem"),
    ]
    for d in search_dirs:
        for c_name, k_name in candidate_pairs:
            c_path = os.path.join(d, c_name)
            k_path = os.path.join(d, k_name)
            if os.path.isfile(c_path) and os.path.isfile(k_path):
                return c_path, k_path
    return None, None


def _run_service(host="0.0.0.0", port=8008, ssl_cert: Optional[str] = None, ssl_key: Optional[str] = None):
    global BRIDGE_SSL_ENABLED
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, CameraServiceRequestHandler)
    if EDGE_SHADOW:
        EDGE_SHADOW.start()
    try:
        restored_count = 0 if runtime_enabled() else restore_inventory_cache(CONFIRMED_OCCUPANCY)
        print('[Inventory] Runtime authority starts empty; no inventory restored.' if runtime_enabled()
            else f"[Inventory] Restored {restored_count} confirmed shelf snapshots from Supabase.")
    except Exception:
        CONFIRMED_OCCUPANCY.clear()
        print("[Inventory] Persistent state unavailable. Validations fail closed until migration/database access is ready.")
    mqtt_bridge.connect_broker(timeout=1.5)

    cert_path, key_path = _resolve_ssl_cert_and_key(ssl_cert, ssl_key)
    if cert_path and key_path:
        try:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
            httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
            BRIDGE_SSL_ENABLED = True
            print(f"[CameraService] HTTPS/TLS enabled using cert={cert_path}")
        except Exception as ssl_err:
            print(f"[CameraService] Warning: Failed to load SSL cert/key ({ssl_err}); falling back to HTTP.")

    proto = "https" if BRIDGE_SSL_ENABLED else "http"
    print("=================================================================")
    print(f"  SMART STORAGE CAMERA CONTROL & TELEMETRY SERVICE (:{port})")
    print(f"  Available at: {proto}://localhost:{port}/")
    if BRIDGE_PUBLIC_URL:
        print(f"  Public / Cloudflare Tunnel URL: {BRIDGE_PUBLIC_URL}")
    print("  Endpoints:")
    print("    - GET    /status                  : Service & MQTT health status")
    print("    - GET    /api/comparisons/latest  : In-memory ring buffer (latest 5 actions)")
    print("    - GET    /test_feed               : Capture single CCTV test snapshot")
    print("    - GET    /esp32_nodes             : Shelf ROI, paired observations, & comparison logs")
    print("    - GET    /esp32_preview           : Live test snapshot from ESP32 camera")
    print("    - GET    /esp32_probe             : Proxy status probe to ESP32-CAM over LAN")
    print("    - POST   /esp32_nvs_config        : Proxy NVS config update to ESP32-CAM")
    print("    - POST   /esp32_ota_upload        : Proxy manual .bin OTA upload to ESP32-CAM")
    print("    - POST   /esp32_ota_flash         : 1-Click PlatformIO build & wireless OTA flash")
    print("    - POST   /esp32_validate          : Run hybrid comparison against baseline")
    print("=================================================================")

    try:
        if AUTONOMOUS:
            AUTONOMOUS.start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[CameraService] Shutting down server...")
    finally:
        if AUTONOMOUS:
            AUTONOMOUS.close()
        for item in list(RUNNING_PIPELINES.values()):
            proc=item.get('proc')
            if proc and proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
        RUNNING_PIPELINES.clear()
        mqtt_bridge.disconnect()
        httpd.server_close()
        print("[CameraService] Server stopped.")


def run_service(host="0.0.0.0",port=8008,ssl_cert=None,ssl_key=None):
    global EDGE_SHADOW, AUTONOMOUS, PROCESSOR_AUTHORITY, LOCAL_INTENTS, LOCAL_ROIS, RUNTIME_INVENTORY, READINESS, NATIVE_CALIBRATION_RELOAD
    NATIVE_CALIBRATION_RELOAD=None
    runtime=os.path.join(os.path.dirname(__file__),'..','.runtime')
    from src.config_loader import find_and_load_env
    find_and_load_env()  # Protected local .env supplies processor/source settings; shell values still win.
    with InstanceLock(os.path.join(runtime,'bridge.lock')):
        import signal
        def stop_signal(*_): raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,stop_signal)
        if hasattr(signal,'SIGBREAK'): signal.signal(signal.SIGBREAK,stop_signal)
        READINESS=ShelfReadiness();RUNTIME_INVENTORY=None;AUTONOMOUS=None;PROCESSOR_AUTHORITY='browser'
        mqtt_bridge.on_connection_lost=None
        CONFIRMED_OCCUPANCY.clear();DUAL_CAM_CAPTURES.clear();PENDING_CCTV_TRIGGERS.clear();CCTV_ALIGN_STATE.clear()
        CCTV_SEEN_TRIGGER_IDS.clear();LAST_SHELF_TRIGGER_TS.clear();CCTV_LAST_SEEN_TS.clear()
        RECHECKS.leases.clear();RECHECKS.waiting.clear()
        configure_logs(runtime)
        event(READINESS.run_id,'startup')
        storage_lock=None
        try:
            data_dir=Path(os.getenv('SMART_STORAGE_DATA_DIR') or runtime).resolve()
            candidate_lock=InstanceLock(data_dir/'operations.lock');candidate_lock.__enter__();storage_lock=candidate_lock
            LOCAL_INTENTS=LatestIntents(data_dir/'latest-intents.json')
            LOCAL_ROIS=RoiStore(data_dir/'camera-rois.json')
            READINESS.failures=LocalFailures(data_dir/'unresolved-failures.json')
            # One normal workflow: the shared native processor. browser/shadow remain explicit read-only diagnostics.
            mode=os.getenv('SMART_STORAGE_PROCESSOR_MODE','service').strip().lower()
            if runtime_enabled():
                if mode not in {'browser','shadow','service'}:raise ValueError('Invalid processor mode')
                RUNTIME_INVENTORY=RuntimeInventory(READINESS.run_id,int(os.getenv('SMART_STORAGE_QUIET_SECONDS','2')))
                CONFIRMED_OCCUPANCY.clear();SUPABASE_COMPARTMENTS_CACHE.update(data=[],fetched_at=0)
                mqtt_bridge.sync_supabase=False
            if mode=='service':
                from src.autonomous_edge import (load_cameras, load_service_config, _accepted_reports, NativeClient,
                    AutonomousProcessor, acceptance_code_paths)
                from src.config_loader import get_cctv_sources
                root=Path(__file__).resolve().parent.parent
                manifest=os.getenv('SMART_STORAGE_EDGE_MANIFEST') or str(root/'.runtime/edge-models/manifest.json')
                code_paths=acceptance_code_paths(root)
                acceptance_path=os.getenv('SMART_STORAGE_EDGE_ACCEPTANCE') or None
                config_path=os.getenv('SMART_STORAGE_EDGE_CONFIG') or None
                roi_path=LOCAL_ROIS.path
                def resolve_service_cameras():
                    """Installed ROIs + real sources. Sources: EDGE_CCTV_0x_SOURCE override, else the saved camera records."""
                    if config_path:  # explicit legacy config file
                        return load_service_config(config_path,manifest,None,code_paths,roi_path)[0]
                    try:saved=get_cctv_sources()
                    except Exception:saved={}
                    return load_cameras(roi_path,lambda cid:os.getenv('EDGE_'+cid+'_SOURCE') or saved.get(cid),roi_path)
                accepted=False;reports=[]
                if acceptance_path:
                    # Optional record: it can only upgrade the label; a missing/expired one never blocks normal startup.
                    try:reports=_accepted_reports(acceptance_path,manifest,roi_path,code_paths);accepted=True
                    except Exception as exc:print('[CameraService] Acceptance record not applied ('+str(exc)[:120]+'); running NOT_FIELD_ACCEPTED.')
                setup_issue=None
                try:cameras=resolve_service_cameras()
                except (ValueError,OSError,KeyError,TypeError) as exc:cameras=[];setup_issue=str(exc)[:300]
                print('[CameraService] Native processor starting'+('' if accepted else ' (NOT field-accepted: counts and actions are unverified on real hardware)')+'.')
                if setup_issue:print('[CameraService] Setup needed: '+setup_issue)
                def local_request(path,body):
                    handler=object.__new__(CameraServiceRequestHandler)
                    handler.path=path;handler.headers=HTTPMessage();handler.client_address=('127.0.0.1',0)
                    handler._processor_capability=_PROCESSOR_CAPABILITY
                    handler._read_json_body=lambda:body
                    replies: List[tuple[int, Any]]=[]
                    handler._send_json=lambda status_code,payload:replies.append((status_code,payload))
                    (CameraServiceRequestHandler.do_GET if body is None else CameraServiceRequestHandler.do_POST)(handler)
                    if len(replies)!=1:raise ValueError('Guarded bridge request rejected')
                    if not 200<=replies[0][0]<300:
                        if RUNTIME_INVENTORY is not None:raise ValueError(str(replies[0][1].get('code','VALIDATION_REJECTED'))+': '+str(replies[0][1].get('error','Validation held')))
                        raise ValueError('Guarded bridge request rejected')
                    return replies[0][1]
                def pending():
                    with CCTV_CONSENSUS_LOCK:
                        return [dict(t) for t in PENDING_CCTV_TRIGGERS if not t.get('claimed') and 0<=time.time()-t.get('timestamp',0)<60][-20:]
                def cctv_busy():
                    with CCTV_CONSENSUS_LOCK:
                        now=time.time()
                        return any(cid in ('CCTV_01','CCTV_02') and 0<=now-v.get('ts',0)<=CONSENSUS_WINDOW_SEC for cid,v in CCTV_ALIGN_STATE.items()) or any(
                            t.get('source')=='CCTV' and 0<=now-t.get('timestamp',0)<60 for t in PENDING_CCTV_TRIGGERS)
                def invalidate(reason='Camera unavailable',preserve_counts=False,disable_actions=False):
                    try:
                        reset_runtime_evidence(authority=RUNTIME_INVENTORY,coordinator=RECHECKS,
                            consensus_lock=CCTV_CONSENSUS_LOCK,pending=PENDING_CCTV_TRIGGERS,
                            capture_lock=DUAL_CAM_CAPTURE_LOCK,captures=DUAL_CAM_CAPTURES,votes=CCTV_ALIGN_STATE,
                            readiness=READINESS,intents=LOCAL_INTENTS,reason=reason,
                            preserve_counts=preserve_counts,disable_actions=disable_actions)
                    except OSError:event(READINESS.run_id,'local_metadata_failure',code='VALIDATION_REJECTED')
                    esp32_ref_manager.baseline_freeze_until.clear()
                    if AUTONOMOUS is not None:
                        AUTONOMOUS.panel_observations.clear()
                        if reason!='CCTV outage exceeded five seconds':
                            AUTONOMOUS.reset_scan_budget()  # Explicit lifecycle boundary, not a frame dropout.
                            for camera in AUTONOMOUS.cameras:camera['tracker'].reset()
                mqtt_bridge.on_connection_lost=lambda:invalidate('MQTT unavailable')
                AUTONOMOUS=AutonomousProcessor(READINESS.run_id,
                    NativeClient(root,READINESS.run_id,manifest,os.getenv('SMART_STORAGE_NODE','node')),
                    cameras,reports,local_request,capture_single_frame,get_cached_compartments,pending,invalidate,event,
                    pose_native=NativeClient(root,READINESS.run_id,manifest,os.getenv('SMART_STORAGE_NODE','node'),kind='pose'),cctv_busy=cctv_busy)
                AUTONOMOUS.runtime=RUNTIME_INVENTORY
                AUTONOMOUS.acceptance='ACCEPTED' if accepted else 'NOT_FIELD_ACCEPTED'
                if setup_issue:AUTONOMOUS.calibration_issue=setup_issue
                processor,authority=AUTONOMOUS,RUNTIME_INVENTORY
                def reload_native_calibration():
                    """Install to Bridge: re-validate the installed file, drop old evidence, recount every shelf."""
                    try:fresh=resolve_service_cameras()
                    except (ValueError,OSError,KeyError,TypeError) as exc:
                        invalidate('Calibration changed');processor.set_calibration_issue(str(exc)[:300]);raise
                    invalidate('Calibration changed')
                    if authority is not None:authority.clear_baselines()
                    processor.reload_calibration(fresh)
                    event(READINESS.run_id,'native_calibration_reloaded')
                NATIVE_CALIBRATION_RELOAD=reload_native_calibration
                configure_cctv_alignment([{'cctv_id':cid,'camera_role':'SIDE_PRESENCE' if cid=='CCTV_03' else 'GRID_6_ROI','is_active':True}
                    for cid in ('CCTV_01','CCTV_02','CCTV_03')])
                PROCESSOR_AUTHORITY='service'
            EDGE_SHADOW = EdgeShadow(Path(__file__).resolve().parent.parent, READINESS.run_id,
                mode='browser' if mode=='service' else mode,
                manifest=os.getenv('SMART_STORAGE_EDGE_MANIFEST') or None,
                node=os.getenv('SMART_STORAGE_NODE','node'), log=event)
            _run_service(host,port,ssl_cert,ssl_key)
        except BaseException as exc:
            event(READINESS.run_id,'crash',code='SERVICE_CUTOVER_BLOCKED' if isinstance(exc,RuntimeError)
                and str(exc).startswith('SERVICE_CUTOVER_BLOCKED') else 'BRIDGE_EXIT')
            raise
        finally:
            if AUTONOMOUS:
                AUTONOMOUS.close()
            if EDGE_SHADOW:
                EDGE_SHADOW.close()
            if storage_lock:
                storage_lock.__exit__(None,None,None)
            event(READINESS.run_id,'shutdown')


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Smart Storage Local Camera Service Bridge")
    parser.add_argument("port_pos", nargs="?", type=int, default=None, help="Optional positional port (default 8008)")
    parser.add_argument("--port", type=int, default=8008, help="Port to listen on (default 8008)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host address to bind")
    parser.add_argument("--ssl-cert", type=str, default=None, help="Path to SSL certificate PEM file (e.g. mkcert localhost.pem)")
    parser.add_argument("--ssl-key", type=str, default=None, help="Path to SSL private key PEM file (e.g. mkcert localhost-key.pem)")
    args, _ = parser.parse_known_args()
    listen_port = args.port_pos if args.port_pos is not None else args.port
    run_service(host=args.host, port=listen_port, ssl_cert=args.ssl_cert, ssl_key=args.ssl_key)
