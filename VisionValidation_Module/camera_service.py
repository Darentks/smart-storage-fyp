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
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Add backend directory to sys.path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "backend"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

try:
    import cv2
    import numpy as np
    _OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _OPENCV_AVAILABLE = False

from src.config_loader import (
    get_active_rtsp_url,
    mask_rtsp_url,
    list_esp32_cameras,
    update_esp32_health_in_supabase,
    update_compartment_baseline_in_supabase,
    apply_inventory_delta_in_supabase,
    list_comparison_logs,
    insert_esp32_reference_sample,
    list_esp32_reference_samples,
    delete_esp32_reference_sample,
)
from src.spatial_mapper import SpatialMapper
from src.mqtt_client import MQTTClientNode
from src.reference_validator import (
    ESP32ReferenceManager,
    extract_2x2_feature_signature,
    encode_bgr_to_data_url,
    decode_data_url_to_bgr,
)

# Global Managers & State
ROI_CONFIG_PATH = os.path.join(BACKEND_DIR, "config", "roi_config.json")
spatial_mapper = SpatialMapper(ROI_CONFIG_PATH)

esp32_ref_manager = ESP32ReferenceManager()

ACTIVE_ESP32_IPS = {
    "SHELF_R1_C1": os.getenv("ESP32_CAM_IP", "192.168.0.101"),
}

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


def get_merged_compartments_and_nodes():
    """
    Combines local roi_config.json compartments, Supabase 'public.compartments' rows,
    esp32_ref_manager baseline cache, and live MQTT health telemetry into a unified list.
    """
    spatial_mapper.load_roi_config()
    local_comps = spatial_mapper.compartments
    db_rows = list_esp32_cameras()
    db_nodes = {row.get("mqtt_client_id"): row for row in db_rows if row.get("mqtt_client_id")}
    db_by_shelf = {row.get("shelf_id"): row for row in db_rows if row.get("shelf_id")}
    live_mqtt = mqtt_bridge.edge_nodes

    merged = []
    seen_mqtt_ids = set()

    for comp in local_comps:
        shelf_id = comp["shelf_id"]
        mqtt_id = comp.get("mqtt_client_id") or shelf_id.replace("SHELF_", "ESP32_")
        seen_mqtt_ids.add(mqtt_id)

        db_row = db_nodes.get(mqtt_id) or db_by_shelf.get(shelf_id, {})
        live_row = live_mqtt.get(mqtt_id, {})
        cached_baseline = esp32_ref_manager.baseline_cache.get(shelf_id, {})

        status = "ONLINE" if live_row else db_row.get("status", "OFFLINE")
        baseline_url = cached_baseline.get("data_url") or db_row.get("baseline_image_url")
        baseline_updated = cached_baseline.get("updated_at") or db_row.get("baseline_updated_at")

        merged.append({
            "camera_id": db_row.get("camera_id"),
            "shelf_id": shelf_id,
            "rack_id": db_row.get("rack_id") or spatial_mapper.rack_id,
            "mqtt_client_id": mqtt_id,
            "camera_name": comp.get("camera_name") or db_row.get("camera_name") or f"ESP32 Node ({shelf_id})",
            "container_id": comp.get("container_id") or db_row.get("container_id"),
            "ip_address": live_row.get("ip_address") or db_row.get("ip_address") or "",
            "x_min": comp["x_min"],
            "y_min": comp["y_min"],
            "x_max": comp["x_max"],
            "y_max": comp["y_max"],
            "dwell_threshold_seconds": comp.get("dwell_threshold_seconds", 1.2),
            "status": status,
            "current_quantity": db_row.get("current_quantity", 4),
            "max_capacity": db_row.get("max_capacity", 4),
            "baseline_image_url": baseline_url,
            "baseline_updated_at": baseline_updated,
            "rssi_dbm": live_row.get("rssi_dbm", db_row.get("rssi_dbm")),
            "free_heap_bytes": live_row.get("free_heap_bytes", db_row.get("free_heap_bytes")),
            "mac_address": live_row.get("mac_address", db_row.get("mac_address")),
            "firmware_version": live_row.get("firmware_version", db_row.get("firmware_version")),
            "bus_vcc": live_row.get("bus_vcc", db_row.get("bus_vcc")),
            "last_seen_at": live_row.get("last_seen_at", db_row.get("last_seen_at")),
        })

    for mqtt_id, live_row in live_mqtt.items():
        if mqtt_id not in seen_mqtt_ids:
            db_row = db_nodes.get(mqtt_id, {})
            shelf_id = live_row.get("shelf_id", db_row.get("shelf_id", "UNASSIGNED"))
            cached_baseline = esp32_ref_manager.baseline_cache.get(shelf_id, {})
            baseline_url = cached_baseline.get("data_url") or db_row.get("baseline_image_url")
            baseline_updated = cached_baseline.get("updated_at") or db_row.get("baseline_updated_at")

            merged.append({
                "camera_id": db_row.get("camera_id"),
                "shelf_id": shelf_id,
                "rack_id": live_row.get("rack_id", db_row.get("rack_id", spatial_mapper.rack_id)),
                "mqtt_client_id": mqtt_id,
                "camera_name": db_row.get("camera_name") or f"ESP32 Node ({mqtt_id})",
                "container_id": db_row.get("container_id"),
                "ip_address": live_row.get("ip_address") or db_row.get("ip_address") or "",
                "x_min": db_row.get("roi_x_min", 100),
                "y_min": db_row.get("roi_y_min", 150),
                "x_max": db_row.get("roi_x_max", 300),
                "y_max": db_row.get("roi_y_max", 350),
                "dwell_threshold_seconds": float(db_row.get("dwell_threshold_seconds", 1.2)),
                "status": "ONLINE",
                "current_quantity": db_row.get("current_quantity", 4),
                "max_capacity": db_row.get("max_capacity", 4),
                "baseline_image_url": baseline_url,
                "baseline_updated_at": baseline_updated,
                "rssi_dbm": live_row.get("rssi_dbm"),
                "free_heap_bytes": live_row.get("free_heap_bytes"),
                "mac_address": live_row.get("mac_address"),
                "firmware_version": live_row.get("firmware_version"),
                "bus_vcc": live_row.get("bus_vcc"),
                "last_seen_at": live_row.get("last_seen_at"),
            })

    return merged


def capture_single_frame(source_str):
    """
    Connects to an RTSP or webcam source, captures a single frame, and measures latency.
    """
    if not _OPENCV_AVAILABLE:
        return {
            "status": "failed",
            "error": "OpenCV (cv2) is not installed in the current Python environment.",
            "latency_ms": 0,
        }

    try:
        source = int(source_str)
        is_webcam = True
    except (ValueError, TypeError):
        source = str(source_str)
        is_webcam = False

    masked = mask_rtsp_url(source)
    print(f"[CameraService] Capturing on-demand test frame from: {masked}")

    t_start = time.time()
    cap = None

    try:
        if is_webcam:
            cap = cv2.VideoCapture(source, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(source)
        else:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
            cap = cv2.VideoCapture(source)

        if not cap.isOpened():
            latency = round((time.time() - t_start) * 1000, 1)
            return {
                "status": "failed",
                "error": f"Failed to open video source ({masked}). Check camera IP, power, and credentials.",
                "latency_ms": latency,
                "source": masked,
            }

        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ret, frame = cap.read()
        latency = round((time.time() - t_start) * 1000, 1)

        if not ret or frame is None:
            return {
                "status": "failed",
                "error": f"Connection established but failed to read frame buffer from {masked}.",
                "latency_ms": latency,
                "source": masked,
            }

        h, w = frame.shape[:2]
        channels = frame.shape[2] if len(frame.shape) > 2 else 1

        success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not success:
            return {
                "status": "failed",
                "error": "Failed to encode captured frame to JPEG format.",
                "latency_ms": latency,
                "source": masked,
            }

        b64_str = base64.b64encode(buffer).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64_str}"

        return {
            "status": "success",
            "latency_ms": latency,
            "width": w,
            "height": h,
            "channels": channels,
            "image": data_url,
            "source": masked,
        }

    except Exception as exc:
        latency = round((time.time() - t_start) * 1000, 1)
        return {
            "status": "failed",
            "error": f"Exception during frame capture: {str(exc)}",
            "latency_ms": latency,
            "source": masked,
        }
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


class CameraServiceRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for camera control, MQTT triggers, and ESP32 reference validation."""

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")

    def _send_json(self, status_code: int, payload: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ["/dashboard", "/ui"]:
            html_candidates = [
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "index.html")),
                os.path.abspath(os.path.join(os.path.dirname(__file__), "camera_manager.html")),
            ]
            html_content = None
            for p in html_candidates:
                if os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            html_content = f.read()
                            break
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
                self._send_json(404, {"error": "Dashboard HTML file not found"})
                return

        if path in ["/", "/status", "/health"]:
            self._send_json(200, {
                "status": "ok",
                "service": "Smart Storage Local Camera Control & MQTT Telemetry Service",
                "opencv_available": _OPENCV_AVAILABLE,
                "mqtt_connected": mqtt_bridge.is_connected,
                "mqtt_broker": f"{mqtt_bridge.broker_address}:{mqtt_bridge.port}",
                "active_esp32_nodes": len(mqtt_bridge.edge_nodes),
                "port": 8008,
            })
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
            logs = list_comparison_logs(15)
            self._send_json(200, {
                "status": "success",
                "rack_id": spatial_mapper.rack_id,
                "mqtt_connected": mqtt_bridge.is_connected,
                "mqtt_broker": f"{mqtt_bridge.broker_address}:{mqtt_bridge.port}",
                "compartments": nodes,
                "recent_responses": list(reversed(mqtt_bridge.recent_responses[-15:])),
                "comparison_logs": logs,
            })
            return

        if path == "/esp32_preview":
            t0 = time.time()
            esp32_ip = query.get("ip", [None])[0]
            shelf_id = query.get("shelf_id", ["SHELF_R1_C1"])[0]

            if esp32_ip and str(esp32_ip).strip():
                clean_ip = str(esp32_ip).replace("http://", "").replace("/", "").strip()
                ACTIVE_ESP32_IPS[shelf_id] = clean_ip

                # Immediate update in Supabase public.compartments
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                node_health = {
                    "device_id": f"ESP32_{shelf_id.replace('SHELF_', '')}",
                    "shelf_id": shelf_id,
                    "ip_address": clean_ip,
                    "status": "ONLINE",
                    "last_seen_at": now_iso,
                }
                update_esp32_health_in_supabase(node_health)

            frame_bgr, data_url = esp32_ref_manager.fetch_esp32_http_frame(esp32_ip=esp32_ip, mode="live")
            latency_ms = int((time.time() - t0) * 1000)

            # If no baseline image exists yet for this shelf, populate it with this live frame
            cached = esp32_ref_manager.baseline_cache.get(shelf_id)
            if not cached or not cached.get("data_url"):
                esp32_ref_manager.update_30s_baseline(
                    shelf_id=shelf_id,
                    esp32_ip=esp32_ip,
                    image_data_url=data_url,
                )

            self._send_json(200, {
                "status": "success",
                "shelf_id": shelf_id,
                "ip": esp32_ip,
                "image": data_url,
                "latency_ms": latency_ms,
            })
            return

        if path == "/esp32_reference_samples":
            shelf_id = query.get("shelf_id", [None])[0]
            samples = list_esp32_reference_samples(shelf_id=shelf_id)
            self._send_json(200, {
                "status": "success",
                "shelf_id": shelf_id,
                "samples": samples,
            })
            return

        self._send_json(404, {"error": "Endpoint not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/run_calibration":
            source = body.get("source") or get_active_rtsp_url(default_source=0)
            script_path = os.path.join(BACKEND_DIR, "calibrate_roi.py")
            if not os.path.isfile(script_path):
                self._send_json(500, {"status": "failed", "error": f"Script not found at {script_path}"})
                return

            masked = mask_rtsp_url(source)
            print(f"[CameraService] Spawning ROI Calibration GUI for: {masked}")
            try:
                cmd = [sys.executable, script_path, "--source", str(source)]
                proc = subprocess.Popen(
                    cmd,
                    cwd=BACKEND_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._send_json(200, {
                    "status": "launched",
                    "pid": proc.pid,
                    "source": masked,
                    "message": "Shelf ROI Calibration window opened on local desktop.",
                })
            except Exception as exc:
                self._send_json(500, {"status": "failed", "error": f"Failed to spawn calibration process: {str(exc)}"})
            return

        # MANUAL TRIGGER: Dispatch CAPTURE_STATE or CAPTURE_REFERENCE over MQTT
        if path == "/esp32_trigger":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            command = str(body.get("command", "CAPTURE_STATE")).upper()
            expected_action = str(body.get("expected_action", "BORROW")).upper()
            rack_id = str(body.get("rack_id", spatial_mapper.rack_id)).strip()

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

        # STORE REFERENCE SAMPLE: Capture & store an ESP32 reference photo (4, 3, 2, 1, 0 boxes)
        if path == "/esp32_reference_samples":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            esp32_ip = body.get("ip_address") or body.get("ip")
            if esp32_ip and str(esp32_ip).strip():
                clean_ip = str(esp32_ip).replace("http://", "").replace("/", "").strip()
                ACTIVE_ESP32_IPS[shelf_id] = clean_ip

            box_count = int(body.get("box_count", 4))
            sample_label = str(body.get("sample_label") or f"{shelf_id} - {box_count} Boxes Ref").strip()
            provided_image = body.get("image") or body.get("image_url")

            t0 = time.time()
            frame_bgr = None
            data_url = ""
            if provided_image:
                frame_bgr = decode_data_url_to_bgr(provided_image)
                data_url = provided_image
            else:
                frame_bgr, data_url = esp32_ref_manager.fetch_esp32_http_frame(
                    esp32_ip=esp32_ip,
                    mode="live",
                )

            latency_ms = int((time.time() - t0) * 1000)
            feature_sig = extract_2x2_feature_signature(frame_bgr) if frame_bgr is not None else {}

            slot_states = body.get("slot_states") or {
                "front_top": 1,
                "back_top": 1,
                "front_bottom": 1,
                "back_bottom": 1,
            }
            scenario_code = str(body.get("scenario_code") or "").strip().lower()

            sample_record = {
                "shelf_id": shelf_id,
                "mqtt_client_id": f"ESP32_{shelf_id.replace('SHELF_', '')}",
                "rack_id": spatial_mapper.rack_id,
                "sample_label": sample_label,
                "box_count": box_count,
                "slot_states": slot_states,
                "image_url": data_url,
                "flash_enabled": bool(body.get("flash_enabled", True)),
                "feature_signature": feature_sig,
                "capture_latency_ms": latency_ms,
            }

            inserted = insert_esp32_reference_sample(sample_record)

            # Also save copy to local disk reference_esp32/{shelf_id}/ref_<scenario_or_label>.jpg if cv2 available
            if _OPENCV_AVAILABLE and frame_bgr is not None:
                try:
                    ref_dir = os.path.join(BACKEND_DIR, "reference_esp32", shelf_id)
                    os.makedirs(ref_dir, exist_ok=True)
                    clean_name = scenario_code or sample_label.lower()
                    safe_slug = re.sub(r"[^a-z0-9]+", "_", clean_name).strip("_")
                    if not safe_slug.startswith("ref_"):
                        safe_slug = f"ref_{safe_slug}"
                    f_name = f"{safe_slug}.jpg"
                    cv2.imwrite(os.path.join(ref_dir, f_name), frame_bgr)
                except Exception as e:
                    print(f"[CameraService] Local disk write skipped: {e}")

            self._send_json(200, {
                "status": "success",
                "sample": inserted or sample_record,
            })
            return

        # VALIDATE: Run hybrid 30s baseline + reference validation and apply Supabase delta
        if path == "/esp32_validate":
            shelf_id = str(body.get("shelf_id", "SHELF_R1_C1")).strip()
            mqtt_client_id = str(body.get("mqtt_client_id") or f"ESP32_{shelf_id.replace('SHELF_', '')}").strip()
            expected_action = str(body.get("expected_action", "BORROW")).upper()
            esp32_ip = body.get("ip_address") or body.get("ip")
            after_image = body.get("image") or body.get("image_url")

            # Determine current quantity
            curr_qty = 4
            try:
                for comp in list_esp32_cameras():
                    if comp.get("shelf_id") == shelf_id:
                        curr_qty = int(comp.get("current_quantity", 4))
                        break
            except Exception:
                pass

            comp_res = esp32_ref_manager.compare_with_30s_baseline_and_references(
                mqtt_client_id=mqtt_client_id,
                shelf_id=shelf_id,
                expected_action=expected_action,
                current_quantity=curr_qty,
                esp32_ip=esp32_ip,
                after_data_url=after_image,
            )

            # Apply delta into public.compartments & comparison_logs
            log_entry = apply_inventory_delta_in_supabase(
                shelf_id=shelf_id,
                mqtt_client_id=mqtt_client_id,
                trigger_action=expected_action,
                comparison_result=comp_res,
            )

            self._send_json(200, {
                "status": "success",
                "comparison": comp_res,
                "log": log_entry,
            })
            return

        self._send_json(404, {"error": "Endpoint not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/esp32_reference_samples":
            sample_id = query.get("sample_id", [None])[0]
            shelf_id = query.get("shelf_id", [None])[0]
            all_param = query.get("all", ["0"])[0]

            if not sample_id and not shelf_id:
                body = self._read_json_body()
                sample_id = body.get("sample_id")
                shelf_id = body.get("shelf_id")
                if "all" in body:
                    all_param = str(body.get("all"))

            # Bulk delete all samples for a shelf
            if shelf_id and (str(all_param) in ["1", "true", "True"] or not sample_id):
                deleted = delete_esp32_reference_sample(shelf_id=str(shelf_id).strip())
                # Also optionally remove local disk sample pictures
                try:
                    ref_dir = os.path.join(BACKEND_DIR, "reference_esp32", str(shelf_id).strip())
                    if os.path.isdir(ref_dir):
                        for f in os.listdir(ref_dir):
                            if f.endswith(".jpg"):
                                try:
                                    os.remove(os.path.join(ref_dir, f))
                                except Exception:
                                    pass
                except Exception:
                    pass

                self._send_json(200 if deleted else 500, {
                    "status": "success" if deleted else "failed",
                    "shelf_id": shelf_id,
                    "deleted_all": True,
                })
                return

            if not sample_id:
                self._send_json(400, {"status": "failed", "error": "Missing sample_id or shelf_id with all=1"})
                return

            deleted = delete_esp32_reference_sample(sample_id=str(sample_id))
            self._send_json(200 if deleted else 500, {
                "status": "success" if deleted else "failed",
                "sample_id": sample_id,
            })
            return

        self._send_json(404, {"error": "Endpoint not found"})

    def log_message(self, format, *args):
        sys.stderr.write(f"[CameraService HTTP] {args[0]} - {args[1]}\n")


def esp32_http_baseline_poller():
    """
    Background daemon thread running every 30 seconds:
    - Polls ACTIVE_ESP32_IPS (defaulting SHELF_R1_C1 to ESP32_CAM_IP or 192.168.0.101).
    - Queries http://<ip>/status (2s timeout) to verify node connectivity & extract rssi.
    - Calls esp32_ref_manager.update_30s_baseline(shelf_id, esp32_ip=ip) to fetch
      the ESP32-CAM's latest 30s resting baseline JPEG and sync it into public.compartments,
      ensuring fresh baseline snapshots even if Mosquitto MQTT is offline.
    """
    time.sleep(2.0)  # Brief warm-up before first cycle
    while True:
        try:
            # Ensure default for SHELF_R1_C1
            default_ip = os.getenv("ESP32_CAM_IP", "192.168.0.101")
            ACTIVE_ESP32_IPS.setdefault("SHELF_R1_C1", default_ip)

            for shelf_id, ip in list(ACTIVE_ESP32_IPS.items()):
                if not ip or not str(ip).strip():
                    continue

                clean_ip = str(ip).replace("http://", "").replace("/", "").strip()
                status_url = f"http://{clean_ip}/status"
                rssi_dbm = None
                online = False

                # 1. Quick probe to http://<ip>/status (2s timeout)
                try:
                    req = urllib.request.Request(status_url, headers={"User-Agent": "SmartStoragePoller/1.0"})
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
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
                if online:
                    node_health = {
                        "device_id": f"ESP32_{shelf_id.replace('SHELF_', '')}",
                        "shelf_id": shelf_id,
                        "ip_address": clean_ip,
                        "rssi_dbm": rssi_dbm,
                        "status": "ONLINE",
                        "last_seen_at": now_iso,
                    }
                    update_esp32_health_in_supabase(node_health)

                    # Update 30s baseline cache and Supabase compartments row
                    esp32_ref_manager.update_30s_baseline(shelf_id=shelf_id, esp32_ip=clean_ip)
                else:
                    # Check if node has a cached baseline; if not, do a synthetic or retry fetch
                    cached = esp32_ref_manager.baseline_cache.get(shelf_id)
                    if not cached or not cached.get("data_url"):
                        esp32_ref_manager.update_30s_baseline(shelf_id=shelf_id, esp32_ip=clean_ip)

        except Exception as exc:
            print(f"[CameraService Poller] Exception in baseline poller cycle: {exc}")

        time.sleep(30.0)


def run_service(host="0.0.0.0", port=8008):
    mqtt_bridge.connect_broker(timeout=1.5)

    # Start background 30s HTTP baseline poller daemon thread
    poller_thread = threading.Thread(target=esp32_http_baseline_poller, daemon=True, name="ESP32BaselinePoller")
    poller_thread.start()
    print("[CameraService] Started background 30s ESP32 HTTP baseline poller daemon.")

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, CameraServiceRequestHandler)
    print("=================================================================")
    print(f"  SMART STORAGE CAMERA CONTROL & TELEMETRY SERVICE (:{port})")
    print(f"  Available at: http://localhost:{port}/")
    print("  Endpoints:")
    print("    - GET    /status                  : Service & MQTT health status")
    print("    - GET    /test_feed               : Capture single CCTV test snapshot")
    print("    - GET    /esp32_nodes             : Shelf ROI, 30s baselines, & comparison logs")
    print("    - GET    /esp32_preview           : Live test snapshot from ESP32 camera")
    print("    - POST   /run_calibration         : Launch desktop OpenCV ROI calibrator")
    print("    - POST   /esp32_trigger           : Dispatch CAPTURE_STATE or CAPTURE_REFERENCE")
    print("    - GET    /esp32_reference_samples : List stored reference samples")
    print("    - POST   /esp32_reference_samples : Save reference sample for tested angles")
    print("    - DELETE /esp32_reference_samples : Remove reference sample")
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
    port = 8008
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_service(port=port)
