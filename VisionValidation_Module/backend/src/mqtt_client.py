"""
MQTT Client Node Module for Smart Storage FYP
Handles event-driven messaging to the Mosquitto broker according to Table 4.8 & Section 4.7 specifications.
Complies with FR-05, FR-07, and NFR-01 (Low latency MQTT < 500ms).
"""

import json
import time
from datetime import datetime, timezone
import paho.mqtt.client as mqtt


class MQTTClientNode:
    """
    Asynchronous MQTT publisher for edge capture triggers and operational telemetry.
    Gracefully handles disconnected state when running offline during early sprint testing.
    """

    def __init__(self, broker_address="localhost", port=1883, rack_id="RACK_01"):
        self.broker_address = broker_address
        self.port = port
        self.rack_id = rack_id
        self.is_connected = False

        self.client = mqtt.Client(client_id=f"VisionEngine_{self.rack_id}", clean_session=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.is_connected = True
            print(f"[MQTT] Connected successfully to Mosquitto broker at {self.broker_address}:{self.port}")
        else:
            self.is_connected = False
            print(f"[MQTT] Connection failed with result code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.is_connected = False
        print(f"[MQTT] Disconnected from broker (rc={rc})")

    def connect_broker(self, timeout=2.0):
        """
        Attempts to connect to the MQTT broker asynchronously.
        :param timeout: Maximum seconds to attempt connection
        :return: bool indicating initial connection dispatch
        """
        try:
            print(f"[MQTT] Connecting to broker at {self.broker_address}:{self.port}...")
            self.client.connect_async(self.broker_address, self.port, keepalive=60)
            self.client.loop_start()
            t0 = time.time()
            while time.time() - t0 < timeout:
                if self.is_connected:
                    return True
                time.sleep(0.05)
            print("[MQTT] Broker not reachable; operating in local simulation/logging mode.")
            return False
        except Exception as e:
            print(f"[MQTT] Warning: Could not connect to MQTT broker ({e}). Continuing in standalone mode.")
            self.is_connected = False
            return False

    def publish_trigger(self, shelf_id, expected_action="BORROW", confidence=1.0):
        """
        Publishes hardware capture trigger to the localized ESP32 node.
        Topic: iotlab/storage/{rack_id}/trigger
        Payload Schema complies with Section 4.7 Edge Capture Trigger Payload.
        """
        topic = f"iotlab/storage/{self.rack_id}/trigger"
        payload = {
            "command": "CAPTURE_STATE",
            "shelf_id": shelf_id,
            "expected_action": expected_action,
            "confidence": round(confidence, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        payload_json = json.dumps(payload)

        if self.is_connected:
            self.client.publish(topic, payload_json, qos=1)
            print(f"[MQTT Published] Topic: {topic} -> {payload_json}")
        else:
            print(f"[MQTT Simulation] Target Topic: {topic} -> {payload_json}")

        return payload

    def disconnect(self):
        """Clean shutdown of MQTT network loop."""
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

