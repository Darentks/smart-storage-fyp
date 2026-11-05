"""
Local Camera Control, MQTT Edge Hardware Bridge, & ESP32-CAM Reference Sample API Service
Smart Storage FYP
Author: Daren Tan Khai Shuen

Runs a lightweight HTTP server on port 8008 providing:
1. Macro Hikvision CCTV Control:
   - GET    /status                  : Service & MQTT health status
   - GET    /test_feed               : Capture single CCTV frame snapshot & latency
   - POST   /run_calibration         : Open desktop OpenCV ROI calibrator

2. Micro ESP32-CAM Node & 30s Baseline Comparison Pipeline:
   - GET    /esp32_nodes             : Read Shelf Compartments, 30s baselines, live MQTT health & recent comparison logs
   - GET    /esp32_preview           : Live test preview from http://<esp32_ip>/capture
   - POST   /esp32_trigger           : Dispatch CAPTURE_STATE or CAPTURE_REFERENCE over MQTT
   - GET    /esp32_reference_samples : List stored ESP32-CAM reference samples
   - POST   /esp32_reference_samples : Capture & store an ESP32-CAM scenario reference (4, 3, 2, 1, 0 boxes)
   - DELETE /esp32_reference_samples : Delete an ESP32-CAM reference sample
   - POST   /esp32_validate          : Run hybrid 30s baseline + reference validation and apply Supabase delta
"""

import base64
from collections import deque
import concurrent.futures
import glob
import json
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
    apply_inventory_delta_in_supabase,
    list_comparison_logs,
)
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

ACTIVE_ESP32_IPS: Dict[str, Any] = {
    "SHELF_R1_C1": {
        "ip1": os.getenv("ESP32_CAM_IP", "192.168.0.201"),
        "ip2": os.getenv("ESP32_CAM_IP2", "192.168.0.202"),
    },
}

# Buffer of pending CCTV intent confirmation triggers to be consumed by the UI Teachable Machine engine
PENDING_CCTV_TRIGGERS: list = []
LAST_TM_BASE_URL: str = "https://teachablemachine.withgoogle.com/models/0cQO4TjOm/"

# Running YOLO11n-Pose CCTV pipeline process manager
RUNNING_PIPELINES: Dict[str, Dict[str, Any]] = {}

# Multi-CCTV Trigger deduplication & debounce tracking (6s window)
LAST_SHELF_TRIGGER_TS: Dict[str, float] = {}
MULTI_CCTV_DEBOUNCE_SEC = 6.0

# In-memory cache for Supabase compartments and local ring buffer for comparison logs (cap: 5)
SUPABASE_COMPARTMENTS_CACHE: Dict[str, Any] = {"data": [], "fetched_at": 0.0}
LATEST_COMPARISONS_DEQUE: deque = deque(maxlen=5)
SUPABASE_CACHE_TTL_SEC: float = 300.0
HEARTBEAT_TTL = 45.0
LAST_CLOUD_BASELINE_SYNC_TS: Dict[str, float] = {}
BRIDGE_PUBLIC_URL: str = (os.getenv("NEXT_PUBLIC_BRIDGE_URL") or os.getenv("VITE_CAMERA_BRIDGE_URL") or "").strip().rstrip("/")
BRIDGE_SSL_ENABLED: bool = False


def get_cached_compartments(force_refresh: bool = False) -> list:
    """Returns cached compartments from Supabase, refreshing only after TTL or when forced."""
    now = time.time()
    if force_refresh or (now - SUPABASE_COMPARTMENTS_CACHE["fetched_at"] >= SUPABASE_CACHE_TTL_SEC) or not SUPABASE_COMPARTMENTS_CACHE["data"]:
        try:
            data = list_esp32_cameras()
            if data:
                SUPABASE_COMPARTMENTS_CACHE["data"] = data
                SUPABASE_COMPARTMENTS_CACHE["fetched_at"] = now
        except Exception as e:
            print(f"[CameraService Cache] Compartments refresh warning: {e}")
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
    return bool(node) and node.get("status", "ONLINE") != "OFFLINE" and time.time() - node.get("last_seen_ts", 0) <= HEARTBEAT_TTL


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
        ip_addr = override_ip1 or live_row.get("ip_address") or db_row.get("ip_address") or "192.168.0.201"
        sec_ip_addr = override_ip2 or live_cam2_row.get("ip_address") or db_row.get("secondary_ip_address") or "192.168.0.202"

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
            "x_min": comp["x_min"],
            "y_min": comp["y_min"],
            "x_max": comp["x_max"],
            "y_max": comp["y_max"],
            "dwell_threshold_seconds": comp.get("dwell_threshold_seconds", 1.2),
            "status": status,
            "current_quantity": db_row.get("current_quantity", 4),
            "max_capacity": db_row.get("max_capacity", 4),
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
        ip_addr = override_ip1 or live_row.get("ip_address") or db_row.get("ip_address") or "192.168.0.201"
        sec_ip_addr = override_ip2 or live_cam2_row.get("ip_address") or db_row.get("secondary_ip_address") or "192.168.0.202"

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
            "current_quantity": db_row.get("current_quantity", 4),
            "max_capacity": db_row.get("max_capacity", 4),
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
            ip_addr = live_row.get("ip_address") or db_row.get("ip_address") or "192.168.0.201"
            sec_ip_addr = db_row.get("secondary_ip_address") or "192.168.0.202"

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
                "current_quantity": db_row.get("current_quantity", 4),
                "max_capacity": db_row.get("max_capacity", 4),
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

    def _reader_loop(self) -> None:
        cap = None
        try:
            print(f"[CameraService] Opening persistent low-latency stream: {self.masked}")
            if self.is_webcam:
                cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(self.source)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
                cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG) if hasattr(cv2, "CAP_FFMPEG") else cv2.VideoCapture(self.source)

            if not cap or not cap.isOpened():
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
                    fail_count += 1
                    if fail_count > 30:
                        self.error_msg = f"Stream disconnected for {self.masked}"
                        break
                    time.sleep(0.02)
                    continue

                fail_count = 0
                with self.lock:
                    self.latest_frame = frame
                    self.latest_seq += 1
                if not self.ready_event.is_set():
                    self.ready_event.set()

                if self.is_webcam:
                    time.sleep(0.01)
        except Exception as exc:
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
        for base in model_bases:
            try:
                base_resolved = base.resolve()
                target = (base_resolved / filename).resolve()
                if not target.is_relative_to(base_resolved) or not target.is_file():
                    continue
                data_bytes = None if head_only else target.read_bytes()
                content_length = target.stat().st_size if head_only else len(data_bytes)
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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
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
            base_url = query.get("base", [LAST_TM_BASE_URL])[0]
            if not base_url.endswith("/"):
                base_url += "/"
            if "base" in query:
                LAST_TM_BASE_URL = base_url

            clean_filename = filename.split("?")[0]
            target_url = f"{base_url}{clean_filename}?t={int(time.time() * 1000)}"
            data_bytes = None

            try:
                req = urllib.request.Request(
                    target_url,
                    headers={
                        "User-Agent": "SmartStorageVision/1.0",
                        "Cache-Control": "no-cache, no-store",
                        "Pragma": "no-cache",
                    },
                )
                with urllib.request.urlopen(req, timeout=7.0) as resp:
                    data_bytes = resp.read()
            except Exception as net_err:
                print(f"[TM Proxy] Remote fetch failed for {target_url}: {net_err}")

            target_dirs = [
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "tm_0cQO4TjOm")),
                os.path.abspath(os.path.join(os.path.dirname(__file__), "models", "tm_0cQO4TjOm")),
            ]
            if data_bytes:
                for d in target_dirs:
                    try:
                        os.makedirs(d, exist_ok=True)
                        with open(os.path.join(d, clean_filename), "wb") as out_f:
                            out_f.write(data_bytes)
                    except Exception:
                        pass
            else:
                for d in target_dirs:
                    local_f = os.path.join(d, clean_filename)
                    if os.path.isfile(local_f):
                        try:
                            with open(local_f, "rb") as lf:
                                data_bytes = lf.read()
                            break
                        except Exception:
                            pass

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
                "mqtt_connected": mqtt_bridge.is_connected,
                "mqtt_broker": f"{mqtt_bridge.broker_address}:{mqtt_bridge.port}",
                "active_esp32_nodes": len(mqtt_bridge.edge_nodes),
                "running_pipelines": active_pipes,
                "latest_comparisons_count": len(LATEST_COMPARISONS_DEQUE),
                "port": 8008,
            })
            return

        if path in ["/api/comparisons/latest", "/comparisons/latest"]:
            self._send_json(200, list(LATEST_COMPARISONS_DEQUE))
            return

        if path == "/test_feed":
            source_param = query.get("source", [None])[0]
            if not source_param:
                source_param = get_active_rtsp_url(default_source=0)
            result = capture_single_frame(source_param)
            self._send_json(200 if result.get("status") == "success" else 502, result)
            return

        if path == "/esp32_nodes":
            nodes = get_merged_compartments_and_nodes()
            logs = list(LATEST_COMPARISONS_DEQUE)
            discovered = get_discovered_cameras_list(nodes)

            # Expose pending CCTV triggers (retain recent items within last 60s, max 20)
            # Dashboard deduplicates by trigger_id via processedCctvTriggers set
            now = time.time()
            pending = [t for t in PENDING_CCTV_TRIGGERS if (now - t.get("timestamp", 0)) < 60.0][-20:]

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

        if path == "/esp32_snapshot_raw":
            host = str(query.get("ip", [""])[0]).split("//")[-1].split("/")[0].strip()
            mode = query.get("mode", ["live"])[0]
            try:
                duty = max(0, min(255, int(query.get("flash", ["0"])[0])))
                if not host or not re.fullmatch(r"[A-Za-z0-9.:-]+", host) or mode not in ("live", "baseline"):
                    raise ValueError("Invalid snapshot IP or mode")
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                req = urllib.request.Request(f"http://{host}/capture?mode={mode}&duty={duty}",
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
                prev_ip1 = prev_entry.get("ip1") if isinstance(prev_entry, dict) else (str(prev_entry) if prev_entry else "192.168.0.201")
                prev_ip2 = prev_entry.get("ip2") if isinstance(prev_entry, dict) else "192.168.0.202"
                ACTIVE_ESP32_IPS[shelf_id] = {
                    "ip1": clean_ip1 or prev_ip1,
                    "ip2": clean_ip2 or prev_ip2,
                }


            if esp32_ip and esp32_ip2:
                # Dual-camera capture: Back Cam (ip) + Front Cam (ip2)
                frame_bgr, combined_url, url1, url2 = esp32_ref_manager.fetch_dual_esp32_frame(
                    esp32_ip1=esp32_ip,
                    esp32_ip2=esp32_ip2,
                    mode="live",
                    flash=flash,
                    duty=duty,
                    boost_mode=boost,
                )
                latency_ms = int((time.time() - t0) * 1000)

                cached = esp32_ref_manager.baseline_cache.get(shelf_id)
                if not cached or not cached.get("data_url"):
                    esp32_ref_manager.update_30s_baseline(
                        shelf_id=shelf_id,
                        esp32_ip=esp32_ip,
                        esp32_ip2=esp32_ip2,
                        image_data_url=combined_url,
                        image_2_data_url=url2,
                        sync_supabase=False,
                    )

                self._send_json(200, {
                    "status": "success",
                    "shelf_id": shelf_id,
                    "ip": esp32_ip,
                    "ip2": esp32_ip2,
                    "image": combined_url,
                    "image_back": url1,
                    "image_front": url2,
                    "latency_ms": latency_ms,
                })
                return

            frame_bgr, data_url = esp32_ref_manager.fetch_esp32_frame(esp32_ip=esp32_ip, mode="live", flash=flash, duty=duty, boost_mode=boost)
            latency_ms = int((time.time() - t0) * 1000)

            # If no baseline image exists yet for this shelf, populate it with this live frame
            cached = esp32_ref_manager.baseline_cache.get(shelf_id)
            if not cached or not cached.get("data_url"):
                esp32_ref_manager.update_30s_baseline(
                    shelf_id=shelf_id,
                    esp32_ip=esp32_ip,
                    image_data_url=data_url,
                    sync_supabase=False,
                )

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
        body = self._read_json_body()

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

        # ASSIGN DUAL CAMERA IPs TO SHELF: Immediately update ACTIVE_ESP32_IPS and Supabase public.compartments
        if path == "/esp32_assign_ips":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            raw_ip1 = str(body.get("ip_address") or body.get("ip") or "192.168.0.201").split("//")[-1].split("/")[0].strip()
            raw_ip2 = str(body.get("secondary_ip_address") or body.get("ip2") or "192.168.0.202").split("//")[-1].split("/")[0].strip()

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

        # MANUAL TRIGGER: Dispatch CAPTURE_STATE or CAPTURE_REFERENCE over MQTT
        if path == "/esp32_trigger":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            command = str(body.get("command", "CAPTURE_STATE")).upper()
            expected_action = str(body.get("expected_action", "BORROW")).upper()
            rack_id = str(body.get("rack_id", spatial_mapper.rack_id)).strip()
            source = str(body.get("source", "MANUAL")).upper()

            # Multi-CCTV Debounce: When 2 or 3 CCTVs see the same hand interaction simultaneously
            if command == "CAPTURE_STATE" and source == "CCTV":
                now_ts = time.time()
                last_ts = LAST_SHELF_TRIGGER_TS.get(shelf_id, 0.0)
                if (now_ts - last_ts) < MULTI_CCTV_DEBOUNCE_SEC:
                    print(f"[CameraService] Debounced duplicate multi-CCTV trigger for {shelf_id} ({now_ts - last_ts:.2f}s < {MULTI_CCTV_DEBOUNCE_SEC}s)")
                    self._send_json(200, {
                        "status": "debounced",
                        "shelf_id": shelf_id,
                        "message": "Duplicate multi-CCTV trigger ignored within 6s window",
                    })
                    return
                LAST_SHELF_TRIGGER_TS[shelf_id] = now_ts

            # Guardrail 1: Freeze 30s baseline for 10s during interaction
            esp32_ref_manager.freeze_baseline(shelf_id, 10.0)

            if command == "CAPTURE_REFERENCE":
                box_count = int(body.get("box_count", 4))
                sample_label = body.get("sample_label")
                dispatched = mqtt_bridge.publish_reference_capture(
                    shelf_id=shelf_id,
                    box_count=box_count,
                    sample_label=sample_label,
                    rack_id=rack_id,
                )
            else:
                # Record CCTV / State trigger for UI browser Teachable Machine processing (keep up to 20)
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
                    confidence=1.0,
                    rack_id=rack_id,
                )

            self._send_json(200, {
                "status": "success",
                "mqtt_connected": mqtt_bridge.is_connected,
                "dispatched": dispatched,
            })
            return

        # VALIDATE: Run 30s baseline + Teachable Machine validation and apply Supabase delta
        if path == "/esp32_validate":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            boost = body.get("boost") or "auto"
            if boost in ("auto", "high", "off"):
                esp32_ref_manager.vision_boost_mode = boost
            # Guardrail 1: Freeze 30s baseline for 10s during interaction
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

            # Determine current quantity from local cache (zero Supabase egress)
            curr_qty = 4
            try:
                for comp in get_cached_compartments(force_refresh=False):
                    if comp.get("shelf_id") == shelf_id:
                        curr_qty = int(comp.get("current_quantity", 4))
                        if not esp32_ip2 and comp.get("secondary_ip_address"):
                            esp32_ip2 = comp.get("secondary_ip_address")
                        break
            except Exception:
                pass

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
            if predicted_box_count is not None:
                delta_val = int(comp_res.get("delta", 0))
                derived_action = "BORROW" if delta_val < 0 else ("RETURN" if delta_val > 0 else expected_action)
                before_img = comp_res.get("before_image") or comp_res.get("before_image_url")
                after_img = after_image or comp_res.get("after_image") or comp_res.get("after_image_url")

                # ONLY update current_quantity in Supabase if box count actually changed (delta != 0)
                if delta_val != 0:
                    delta_res = apply_inventory_delta_in_supabase(
                        shelf_id=shelf_id,
                        mqtt_client_id=mqtt_client_id,
                        trigger_action=derived_action,
                        comparison_result=comp_res,
                        current_quantity_hint=curr_qty,
                    )
                    if delta_res is False:
                        if previous_baseline is None:
                            esp32_ref_manager.baseline_cache.pop(shelf_id, None)
                        else:
                            esp32_ref_manager.baseline_cache[shelf_id] = previous_baseline
                        self._send_json(502, {"status": "failed", "error": "Inventory update was not confirmed by the database"})
                        return
                    if isinstance(delta_res, dict):
                        log_entry = delta_res
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
                LATEST_COMPARISONS_DEQUE.appendleft(log_entry)
                PENDING_CCTV_TRIGGERS[:] = [t for t in PENDING_CCTV_TRIGGERS if t.get("shelf_id") != shelf_id]

            self._send_json(200, {
                "status": "success",
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


def esp32_http_baseline_poller():
    """
    Background daemon thread running every 30 seconds:
    - Polls ACTIVE_ESP32_IPS (defaulting SHELF_R1_C1 to ESP32_CAM_IP or 192.168.0.201).
    - Queries http://<ip>/status (2s timeout) to verify node connectivity & extract rssi.
    - Calls esp32_ref_manager.update_30s_baseline(shelf_id, esp32_ip=ip) to fetch
      the ESP32-CAM's latest 30s resting baseline JPEG and sync it into public.compartments,
      ensuring fresh baseline snapshots even if Mosquitto MQTT is offline.
    """
    time.sleep(2.0)  # Brief warm-up before first cycle
    while True:
        try:
            # Ensure default for SHELF_R1_C1 and populate configured shelves from Supabase
            default_ip = os.getenv("ESP32_CAM_IP", "192.168.0.201")
            default_ip2 = os.getenv("ESP32_CAM_IP2", "192.168.0.202")
            ACTIVE_ESP32_IPS.setdefault("SHELF_R1_C1", {"ip1": default_ip, "ip2": default_ip2})

            db_compartments = []
            try:
                db_compartments = get_cached_compartments(force_refresh=False)
                for comp_row in db_compartments:
                    s_id = comp_row.get("shelf_id")
                    if s_id and s_id not in ACTIVE_ESP32_IPS:
                        ACTIVE_ESP32_IPS[s_id] = {
                            "ip1": comp_row.get("ip_address") or "192.168.0.201",
                            "ip2": comp_row.get("secondary_ip_address") or "192.168.0.202",
                        }
            except Exception:
                pass

            for shelf_id, ip_entry in list(ACTIVE_ESP32_IPS.items()):
                if isinstance(ip_entry, dict):
                    raw_ip1 = ip_entry.get("ip1")
                    raw_ip2 = ip_entry.get("ip2")
                else:
                    raw_ip1 = ip_entry
                    raw_ip2 = None

                if not raw_ip1 or not str(raw_ip1).strip():
                    continue

                clean_ip = str(raw_ip1).split("//")[-1].split("/")[0].strip()
                status_url = f"http://{clean_ip}/status"
                rssi_dbm = None
                online = False

                # 1. Quick probe to http://<ip>/status (1.5s timeout)
                try:
                    req = urllib.request.Request(status_url, headers={"User-Agent": "SmartStoragePoller/1.0"})
                    with urllib.request.urlopen(req, timeout=1.5) as resp:
                        if resp.status == 200:
                            online = True
                            raw = resp.read().decode("utf-8")
                            try:
                                status_data = json.loads(raw)
                                rssi_dbm = status_data.get("rssi_dbm") or status_data.get("rssi")
                            except Exception:
                                pass
                except Exception:
                    online = False

                # 2. If reachable (or for baseline refresh), fetch baseline & sync Supabase
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                sec_ip = str(raw_ip2).split("//")[-1].split("/")[0].strip() if raw_ip2 else None
                if not sec_ip:
                    for comp_row in db_compartments:
                        if comp_row.get("shelf_id") == shelf_id:
                            sec_ip = comp_row.get("secondary_ip_address") or "192.168.0.202"
                            break
                    if not sec_ip:
                        sec_ip = "192.168.0.202"

                if online:
                    dev_id1 = f"ESP32_{shelf_id.replace('SHELF_', '')}"
                    node_health = {
                        "device_id": dev_id1,
                        "mqtt_client_id": dev_id1,
                        "shelf_id": shelf_id,
                        "ip_address": clean_ip,
                        "rssi_dbm": rssi_dbm,
                        "status": "ONLINE",
                        "angle": "BACK",
                        "last_seen_at": now_iso,
                        "last_seen_ts": time.time(),
                    }
                    mqtt_bridge.edge_nodes[dev_id1] = node_health
                else:
                    record_probe_result(clean_ip)

                # Also probe secondary camera (CAM 2 Front Angle)
                online_cam2 = False
                rssi_cam2 = None
                if sec_ip:
                    try:
                        req2 = urllib.request.Request(f"http://{sec_ip}/status", headers={"User-Agent": "SmartStoragePoller/1.0"})
                        with urllib.request.urlopen(req2, timeout=1.5) as resp2:
                            if resp2.status == 200:
                                online_cam2 = True
                                raw2 = resp2.read().decode("utf-8")
                                try:
                                    s_data2 = json.loads(raw2)
                                    rssi_cam2 = s_data2.get("rssi_dbm") or s_data2.get("rssi")
                                except Exception:
                                    pass
                    except Exception:
                        online_cam2 = False

                if online_cam2:
                    dev_id2 = f"ESP32_{shelf_id.replace('SHELF_', '')}_CAM2"
                    node_health2 = {
                        "device_id": dev_id2,
                        "mqtt_client_id": dev_id2,
                        "shelf_id": shelf_id,
                        "ip_address": sec_ip,
                        "rssi_dbm": rssi_cam2,
                        "status": "ONLINE",
                        "angle": "FRONT",
                        "last_seen_at": now_iso,
                        "last_seen_ts": time.time(),
                    }
                    mqtt_bridge.edge_nodes[dev_id2] = node_health2
                elif sec_ip:
                    record_probe_result(sec_ip)

                # Keep 30s baseline strictly in local RAM (sync_supabase=False) to prevent Base64 egress
                if online or online_cam2:
                    esp32_ref_manager.update_30s_baseline(
                        shelf_id=shelf_id,
                        esp32_ip=clean_ip,
                        esp32_ip2=sec_ip,
                        sync_supabase=False,
                    )
                else:
                    cached = esp32_ref_manager.baseline_cache.get(shelf_id)
                    if not cached or not cached.get("data_url"):
                        esp32_ref_manager.update_30s_baseline(
                            shelf_id=shelf_id,
                            esp32_ip=clean_ip,
                            esp32_ip2=sec_ip,
                            sync_supabase=False,
                        )

        except Exception as exc:
            print(f"[CameraService Poller] Exception in baseline poller cycle: {exc}")

        time.sleep(30.0)


def ensure_local_tm_model_cached(model_id: str = "0cQO4TjOm") -> None:
    """
    Downloads and caches Teachable Machine model files (model.json, metadata.json, weights.bin)
    into models/tm_<model_id>/ so Guardrail 3's offline local fallback works when lab Wi-Fi has no internet.
    """
    base_url = f"https://teachablemachine.withgoogle.com/models/{model_id}/"
    target_dirs = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", f"tm_{model_id}")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "models", f"tm_{model_id}")),
    ]
    for d in target_dirs:
        os.makedirs(d, exist_ok=True)

    for fname in ("model.json", "metadata.json", "weights.bin"):
        if all(os.path.isfile(os.path.join(d, fname)) and os.path.getsize(os.path.join(d, fname)) > 0 for d in target_dirs):
            continue
        try:
            req = urllib.request.Request(
                base_url + fname,
                headers={"User-Agent": "SmartStorageVision/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                raw_bytes = resp.read()
                if raw_bytes:
                    for d in target_dirs:
                        with open(os.path.join(d, fname), "wb") as out_f:
                            out_f.write(raw_bytes)
        except Exception:
            # Offline or unreachable; skip silently
            pass


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


def run_service(host="0.0.0.0", port=8008, ssl_cert: Optional[str] = None, ssl_key: Optional[str] = None):
    global BRIDGE_SSL_ENABLED
    threading.Thread(target=ensure_local_tm_model_cached, daemon=True, name="TMModelCacheWorker").start()
    mqtt_bridge.connect_broker(timeout=1.5)

    # Start background 30s HTTP baseline poller daemon thread
    poller_thread = threading.Thread(target=esp32_http_baseline_poller, daemon=True, name="ESP32BaselinePoller")
    poller_thread.start()
    print("[CameraService] Started background 30s ESP32 HTTP baseline poller daemon.")

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, CameraServiceRequestHandler)

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
    print("    - GET    /esp32_nodes             : Shelf ROI, 30s baselines, & comparison logs")
    print("    - GET    /esp32_preview           : Live test snapshot from ESP32 camera")
    print("    - GET    /esp32_probe             : Proxy status probe to ESP32-CAM over LAN")
    print("    - POST   /esp32_nvs_config        : Proxy NVS config update to ESP32-CAM")
    print("    - POST   /esp32_ota_upload        : Proxy manual .bin OTA upload to ESP32-CAM")
    print("    - POST   /esp32_ota_flash         : 1-Click PlatformIO build & wireless OTA flash")
    print("    - POST   /esp32_validate          : Run hybrid comparison against baseline")
    print("=================================================================")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[CameraService] Shutting down server...")
    finally:
        mqtt_bridge.disconnect()
        httpd.server_close()
        print("[CameraService] Server stopped.")


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
