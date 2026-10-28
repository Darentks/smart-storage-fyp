"""
Bidirectional MQTT Client Node Module for Smart Storage FYP
Handles event-driven messaging, ESP32 snapshot verification responses, 30-second health heartbeats,
30-second resting baseline tracking, and OpenCV inventory deduction.
Complies with Table 4.8, Section 4.7, FR-05, FR-07, FR-10, NFR-01, and NFR-08.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

try:
    import paho.mqtt.client as mqtt
    _PAHO_AVAILABLE = True
except ImportError:
    mqtt = None  # type: ignore
    _PAHO_AVAILABLE = False

try:
    from .config_loader import (
        update_esp32_health_in_supabase,
        apply_inventory_delta_in_supabase,
        list_esp32_cameras,
    )
    from .reference_validator import ESP32ReferenceManager
except ImportError:
    try:
        from src.config_loader import (
            update_esp32_health_in_supabase,
            apply_inventory_delta_in_supabase,
            list_esp32_cameras,
        )
        from src.reference_validator import ESP32ReferenceManager
    except ImportError:
        from config_loader import (
            update_esp32_health_in_supabase,
            apply_inventory_delta_in_supabase,
            list_esp32_cameras,
        )
        from reference_validator import ESP32ReferenceManager


class MQTTClientNode:
    """
    Asynchronous bidirectional MQTT client for:
    1. Publishing edge capture triggers (iotlab/storage/{rack_id}/trigger)
    2. Subscribing to ESP32 snapshot responses (iotlab/storage/+/response)
    3. Subscribing to 30-second resting baseline updates (iotlab/storage/+/baseline)
    4. Subscribing to 30-second ESP32 health heartbeats (iotlab/storage/health)
    5. Subscribing to live ROI reload signals (iotlab/storage/system/reload_roi)
    """

    def __init__(
        self,
        broker_address: str = "localhost",
        port: int = 1883,
        rack_id: str = "RACK_01",
        client_id_prefix: str = "VisionEngine",
        on_verification_response: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_health_ping: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_roi_reload: Optional[Callable[[Dict[str, Any]], None]] = None,
        sync_supabase: bool = True,
        esp32_validator: Optional[ESP32ReferenceManager] = None,
    ):
        self.broker_address = broker_address
        self.port = port
        self.rack_id = rack_id
        self.is_connected = False
        self.sync_supabase = sync_supabase
        self.esp32_validator = esp32_validator or ESP32ReferenceManager()

        self.on_verification_response = on_verification_response
        self.on_health_ping = on_health_ping
        self.on_roi_reload = on_roi_reload

        # Live in-memory state of discovered ESP32 nodes & recent verification responses
        self.edge_nodes: Dict[str, Dict[str, Any]] = {}
        self.recent_responses = []

        unique_suffix = int(time.time() * 1000) % 100000
        client_id = f"{client_id_prefix}_{self.rack_id}_{unique_suffix}"
        if _PAHO_AVAILABLE and mqtt is not None:
            cb_api_ver = getattr(mqtt, "CallbackAPIVersion", None)
            if cb_api_ver is not None:
                self.client = mqtt.Client(cb_api_ver.VERSION1, client_id=client_id, clean_session=True)
            else:
                self.client = mqtt.Client(client_id=client_id, clean_session=True)
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
        else:
            self.client = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.is_connected = True
            print(f"[MQTT] Connected successfully to Mosquitto broker at {self.broker_address}:{self.port}")
            # Subscriptions per specification:
            # - iotlab/storage/+/baseline (QoS 0)
            # - iotlab/storage/+/response (QoS 1)
            # - iotlab/storage/health (QoS 0)
            # - iotlab/storage/system/reload_roi (QoS 1)
            client.subscribe("iotlab/storage/+/baseline", qos=0)
            client.subscribe("iotlab/storage/+/response", qos=1)
            client.subscribe("iotlab/storage/health", qos=0)
            client.subscribe("iotlab/storage/system/reload_roi", qos=1)
            print("[MQTT] Subscribed to: baseline (QoS 0), response (QoS 1), health (QoS 0), reload_roi (QoS 1)")
        else:
            self.is_connected = False
            print(f"[MQTT] Connection failed with result code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.is_connected = False
        print(f"[MQTT] Disconnected from broker (rc={rc})")

    def _on_message(self, client, userdata, msg):
        """Handles incoming MQTT messages from ESP32 nodes and local system signals."""
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            print(f"[MQTT] Failed to parse JSON from topic {topic}: {exc}")
            return

        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. ESP32 30-Second Health Telemetry Heartbeat
        if topic == "iotlab/storage/health":
            device_id = payload.get("device_id") or payload.get("mqtt_client_id")
            if device_id:
                payload["last_seen_at"] = now_iso
                payload["mqtt_client_id"] = device_id
                self.edge_nodes[device_id] = payload
                print(
                    f"[MQTT Health] Node={device_id} | Shelf={payload.get('shelf_id', 'N/A')} | "
                    f"Status={payload.get('status', 'ALIVE')} | RSSI={payload.get('rssi_dbm')}dBm"
                )
                if self.sync_supabase:
                    update_esp32_health_in_supabase(payload)
            if self.on_health_ping:
                self.on_health_ping(payload)

        # 2. ESP32 30-Second Baseline Image Snapshot Broadcast
        elif topic.endswith("/baseline"):
            shelf_id = payload.get("shelf_id")
            if not shelf_id and len(topic.split("/")) >= 3:
                shelf_id = topic.split("/")[2]

            device_id = payload.get("device_id") or payload.get("mqtt_client_id") or f"ESP32_{shelf_id}"
            ip_address = payload.get("ip_address") or payload.get("ip") or (self.edge_nodes.get(device_id, {}).get("ip_address"))

            # Update edge node telemetry
            if device_id:
                if device_id not in self.edge_nodes:
                    self.edge_nodes[device_id] = {}
                self.edge_nodes[device_id].update({
                    "ip_address": ip_address,
                    "rssi_dbm": payload.get("rssi_dbm"),
                    "free_heap_bytes": payload.get("free_heap_bytes"),
                    "last_seen_at": now_iso,
                    "shelf_id": shelf_id,
                    "status": "ONLINE",
                })

            print(f"[MQTT Baseline] Received 30s baseline signal for {shelf_id} (IP: {ip_address})")

            # Update Supabase and baseline cache
            if self.sync_supabase and shelf_id:
                node_health = {
                    "device_id": device_id,
                    "shelf_id": shelf_id,
                    "ip_address": ip_address,
                    "rssi_dbm": payload.get("rssi_dbm"),
                    "free_heap_bytes": payload.get("free_heap_bytes"),
                    "status": "ONLINE",
                    "last_seen_at": now_iso,
                }
                update_esp32_health_in_supabase(node_health)

                # Resolve both CAM 1 (Back) and CAM 2 (Front) IPs so dual-camera baseline stitching is preserved
                is_cam2 = str(device_id).upper().endswith("_CAM2")
                base_mqtt_id = f"ESP32_{str(shelf_id).replace('SHELF_', '')}"
                cam1_node = self.edge_nodes.get(base_mqtt_id) or self.edge_nodes.get(f"{base_mqtt_id}_CAM1") or {}
                cam2_node = self.edge_nodes.get(f"{base_mqtt_id}_CAM2") or {}

                ip1 = cam1_node.get("ip_address") if is_cam2 else (ip_address or cam1_node.get("ip_address"))
                ip2 = (ip_address or cam2_node.get("ip_address")) if is_cam2 else cam2_node.get("ip_address")

                if not ip1 or not ip2:
                    try:
                        for comp_row in list_esp32_cameras():
                            if comp_row.get("shelf_id") == shelf_id:
                                ip1 = ip1 or comp_row.get("ip_address")
                                ip2 = ip2 or comp_row.get("secondary_ip_address")
                                break
                    except Exception:
                        pass

                self.esp32_validator.update_30s_baseline(shelf_id=shelf_id, esp32_ip=ip1, esp32_ip2=ip2)

        # 3. ESP32 Snapshot Verification / Capture Response
        elif topic.endswith("/response"):
            device_id = str(payload.get("device_id") or payload.get("mqtt_client_id") or "")
            shelf_id = payload.get("shelf_id")
            if not shelf_id and len(topic.split("/")) >= 3:
                shelf_id = topic.split("/")[2]

            node_entry = self.edge_nodes.get(device_id, {}) if device_id else {}
            ip_address = payload.get("ip_address") or node_entry.get("ip_address")
            payload["received_at"] = now_iso

            if device_id:
                if device_id not in self.edge_nodes:
                    self.edge_nodes[device_id] = {}
                self.edge_nodes[device_id]["last_seen_at"] = now_iso
                self.edge_nodes[device_id]["status"] = "ONLINE"
                if shelf_id:
                    self.edge_nodes[device_id]["shelf_id"] = shelf_id
                if ip_address:
                    self.edge_nodes[device_id]["ip_address"] = ip_address
                if "rssi_dbm" in payload:
                    self.edge_nodes[device_id]["rssi_dbm"] = payload.get("rssi_dbm")
                if "bus_vcc" in payload:
                    self.edge_nodes[device_id]["bus_vcc"] = payload.get("bus_vcc")

            # Check if this is a reference sample collection response
            if payload.get("capture_mode") == "REFERENCE":
                print(f"[MQTT Reference Capture] Received reference sample telemetry for {device_id} ({shelf_id}).")
                return

            # Only log hardware readiness and update node status on iotlab/storage/+/response.
            # Do NOT call compare_with_30s_baseline_and_references() from the MQTT callback so
            # we never write premature NO_CHANGE logs before the browser finishes TM inference.
            print(
                f"[MQTT Hardware Ready] Node={device_id} | Shelf={shelf_id} | IP={ip_address} | "
                f"Capture ready; awaiting browser 6-Class Teachable Machine validation (/esp32_validate)."
            )
            if self.on_verification_response:
                self.on_verification_response({
                    "device_id": device_id,
                    "shelf_id": shelf_id,
                    "ip_address": ip_address,
                    "state_delta": "HARDWARE_READY",
                    "status_code": payload.get("status_code", 200),
                })
            return

        # 4. Real-time ROI Configuration Reload Signal
        elif topic == "iotlab/storage/system/reload_roi":
            print(f"[MQTT System] Received ROI reload notification: {payload.get('reason', 'ROI update')}")
            if self.on_roi_reload:
                self.on_roi_reload(payload)

    def connect_broker(self, timeout: float = 2.0) -> bool:
        """Attempts to connect to the MQTT broker asynchronously."""
        if self.client is None:
            print("[MQTT] Warning: 'paho-mqtt' not installed; operating in simulation mode.")
            return False
        try:
            print(f"[MQTT] Connecting to broker at {self.broker_address}:{self.port}...")
            self.client.connect_async(self.broker_address, self.port, keepalive=60)
            self.client.loop_start()
            t0 = time.time()
            while time.time() - t0 < timeout:
                if self.is_connected:
                    return True
                time.sleep(0.05)
            print("[MQTT] Broker not reachable yet; continuing in background reconnect/simulation mode.")
            return False
        except Exception as e:
            print(f"[MQTT] Warning: Could not connect to MQTT broker ({e}). Continuing in standalone mode.")
            self.is_connected = False
            return False

    def publish_trigger(
        self,
        shelf_id: str,
        expected_action: str = "BORROW",
        confidence: float = 1.0,
        rack_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publishes hardware capture trigger to the localized ESP32 node.
        Topic: iotlab/storage/{rack_id}/trigger
        """
        target_rack = rack_id or self.rack_id
        topic = f"iotlab/storage/{target_rack}/trigger"
        payload = {
            "command": "CAPTURE_STATE",
            "shelf_id": shelf_id,
            "expected_action": expected_action,
            "confidence": round(float(confidence), 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        payload_json = json.dumps(payload)

        if self.is_connected and self.client is not None:
            self.client.publish(topic, payload_json, qos=1)
            print(f"[MQTT Published Trigger] Topic: {topic} -> {payload_json}")
        else:
            print(f"[MQTT Simulation Trigger] Topic: {topic} -> {payload_json}")

        return payload

    def publish_reference_capture(
        self,
        shelf_id: str,
        box_count: int = 4,
        sample_label: Optional[str] = None,
        rack_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publishes reference capture request to test ESP32 camera angles.
        Topic: iotlab/storage/{rack_id}/trigger
        """
        target_rack = rack_id or self.rack_id
        topic = f"iotlab/storage/{target_rack}/trigger"
        payload = {
            "command": "CAPTURE_REFERENCE",
            "shelf_id": shelf_id,
            "box_count": box_count,
            "sample_label": sample_label or f"{shelf_id}_{box_count}_boxes",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        payload_json = json.dumps(payload)

        if self.is_connected and self.client is not None:
            self.client.publish(topic, payload_json, qos=1)
            print(f"[MQTT Published Ref Capture] Topic: {topic} -> {payload_json}")
        else:
            print(f"[MQTT Simulation Ref Capture] Topic: {topic} -> {payload_json}")

        return payload

    def notify_roi_reload(self, reason: str = "ROI_CALIBRATED") -> None:
        """Notifies any running VisionValidationEngine process to reload roi_config.json immediately."""
        topic = "iotlab/storage/system/reload_roi"
        payload = {
            "command": "RELOAD_ROI",
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self.is_connected and self.client is not None:
            self.client.publish(topic, json.dumps(payload), qos=1)

    def disconnect(self):
        """Clean shutdown of MQTT network loop."""
        try:
            if self.client is not None:
                self.client.loop_stop()
                self.client.disconnect()
        except Exception:
            pass
