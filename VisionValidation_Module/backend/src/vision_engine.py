"""
Vision Validation Engine for Smart Storage FYP
Orchestrates macro CCTV ingestion, YOLO11-Pose tracking, spatial ROI mapping,
and bidirectional MQTT hardware triggering & verification.
Complies with Figure 4.11 (Class Diagram), Figure 4.7 (Sequence Diagram), and Figure 4.8 (Activity Diagram).
"""

import json
import threading
import time
import urllib.error
import urllib.request
import cv2
from .pose_estimator import PoseEstimator
from .spatial_mapper import SpatialMapper
from .behavior_classifier import BehaviorClassifier
from .video_processor import VideoProcessor
from .mqtt_client import MQTTClientNode


class VisionValidationEngine:
    """
    Central orchestration engine executed on the backend processing server.
    """

    def __init__(
        self,
        camera_source=0,
        model_path="models/yolo11n-pose.pt",
        roi_config_path="config/roi_config.json",
        mqtt_broker="localhost",
        mqtt_port=1883,
        privacy_mode=False,
    ):
        self.camera_source = camera_source
        self.model_path = model_path
        self.roi_config_path = roi_config_path
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.privacy_mode = privacy_mode

        self.current_state = "INITIALIZING"
        self.is_running = False

        # Subsystems
        self.video_processor = None
        self.pose_estimator = None
        self.spatial_mapper = None
        self.behavior_classifier = None
        self.mqtt_client = None

        # Notification banners for trigger & ESP32 verification response
        self.active_alert = None
        self.alert_expiry_time = 0.0
        self.alert_color = (0, 180, 0)

    def initialize_system(self):
        """Initializes all sub-modules including bidirectional MQTT listeners."""
        print("=================================================================")
        print("    VISION-BASED HUMAN INTERACTION ANALYSIS ENGINE INITIALIZING  ")
        print("    Final Year Project: RFID-Vision Integrated Smart Storage     ")
        print("=================================================================")

        # 1. Initialize Spatial Mapper
        self.spatial_mapper = SpatialMapper(self.roi_config_path)

        # 2. Initialize Pose Estimator
        self.pose_estimator = PoseEstimator(model_path=self.model_path, oks_threshold=0.50)

        # 3. Initialize Bidirectional MQTT Client
        self.mqtt_client = MQTTClientNode(
            broker_address=self.mqtt_broker,
            port=self.mqtt_port,
            rack_id=self.spatial_mapper.rack_id,
            on_verification_response=self._on_esp32_verification_response,
            on_health_ping=self._on_esp32_health_ping,
            on_roi_reload=self._on_mqtt_roi_reload,
            sync_supabase=False,
        )
        self.mqtt_client.connect_broker()

        # 4. Initialize Behavior Classifier
        self.behavior_classifier = BehaviorClassifier(
            spatial_mapper=self.spatial_mapper,
            on_intent_confirmed=self._on_intent_confirmed
        )

        # 5. Initialize Video Processor
        self.video_processor = VideoProcessor(source=self.camera_source)

        self.current_state = "READY"
        print("[VisionValidationEngine] System initialized and ready.")

    def _on_intent_confirmed(self, event):
        """Callback invoked when 1.2s dwell threshold is sustained."""
        shelf_id = event["shelf_id"]
        action = event["expected_action"]
        conf = event.get("oks_confidence", 1.0)

        # Publish MQTT trigger to wake compartment ESP32-CAM
        self.mqtt_client.publish_trigger(shelf_id=shelf_id, expected_action=action, confidence=conf)

        # Also notify local bridge about CCTV trigger so browser UI TM model can process dual-cam inference
        def _notify_bridge_trigger():
            try:
                trig_url = "http://localhost:8008/esp32_trigger"
                t_payload = json.dumps({
                    "shelf_id": shelf_id,
                    "command": "CAPTURE_STATE",
                    "expected_action": action,
                    "source": "CCTV",
                }).encode("utf-8")
                req = urllib.request.Request(trig_url, data=t_payload, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    print(f"[VisionEngine] CCTV wrist interaction (1.2s dwell) queued in local bridge: {shelf_id} ({action})")
            except Exception as e:
                print(f"[VisionEngine] Notice: Local bridge not responding to CCTV trigger: {e}")

        threading.Thread(target=_notify_bridge_trigger, daemon=True).start()

        # Set HUD visual alert banner for 3 seconds
        self.active_alert = f"TRIGGER SENT: [{action}] at {shelf_id} (Dwell: {event['dwell_duration_seconds']}s)"
        self.alert_color = (0, 165, 255)
        self.alert_expiry_time = time.time() + 3.0

        # Spawn background thread to wait for Teachable Machine dual-cam validation from web dashboard
        def _wait_for_tm_validation():
            url = "http://localhost:8008/esp32_nodes"
            prev_log_id = None
            first_poll = True

            for _ in range(12):
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "VisionEngine/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        raw = resp.read().decode("utf-8")
                        res = json.loads(raw) if raw else {}
                        logs = res.get("comparison_logs") or []

                        if first_poll:
                            first_poll = False
                            if logs:
                                prev_log_id = logs[0].get("log_id")

                        # Check if a new log arrived for this shelf
                        if logs:
                            latest_log = logs[0]
                            curr_log_id = latest_log.get("log_id")
                            if curr_log_id != prev_log_id and latest_log.get("shelf_id") == shelf_id:
                                state_delta = latest_log.get("state_delta", "NO_CHANGE")
                                qty_before = latest_log.get("quantity_before", 4)
                                qty_after = latest_log.get("quantity_after", 4)
                                delta_int = qty_after - qty_before
                                scenario = latest_log.get("scenario_label") or f"Boxes: {qty_after}/4"

                                self.active_alert = f"TM VERIFIED: {state_delta} ({delta_int:+d}) @ {shelf_id} | {scenario} | Stock: {qty_before}->{qty_after}"

                                if state_delta == "ITEM_RETURNED":
                                    self.alert_color = (0, 190, 0)
                                elif state_delta == "ITEM_REMOVED":
                                    self.alert_color = (0, 110, 255)
                                else:
                                    self.alert_color = (120, 120, 120)

                                self.alert_expiry_time = time.time() + 5.0
                                return
                except Exception as exc:
                    print(f"[VisionEngine] Polling {url} for TM validation: {exc}")

                time.sleep(1.0)

        val_thread = threading.Thread(target=_wait_for_tm_validation, daemon=True, name=f"WaitTM_{shelf_id}")
        val_thread.start()

    def _on_esp32_verification_response(self, payload):
        """Callback invoked when ESP32-CAM publishes snapshot verification to iotlab/storage/{shelf_id}/response."""
        device_id = payload.get("device_id", "ESP32")
        shelf_id = payload.get("shelf_id", "UNKNOWN")
        delta = payload.get("state_delta", "VERIFIED")
        code = payload.get("status_code", 200)

        self.active_alert = f"ESP32 CONFIRMED ({device_id}): {delta} @ {shelf_id} [HTTP {code}]"
        self.alert_color = (0, 190, 0) if code == 200 else (0, 0, 220)
        self.alert_expiry_time = time.time() + 4.0

    def _on_esp32_health_ping(self, payload):
        """Callback invoked when an ESP32 node publishes its 30s health ping or CRUD ack."""
        pass

    def _on_mqtt_roi_reload(self, payload):
        """Automatically reloads ROI config when CRUD operations occur via camera_service.py."""
        if self.spatial_mapper:
            self.spatial_mapper.load_roi_config()
            print("[VisionValidationEngine] Hot-reloaded ROI configuration after MQTT CRUD update.")

    def run_pipeline(self, window_name="Smart Storage - Vision Validation Engine"):
        """
        Executes the continuous video processing and inference pipeline.
        Press 'q' to quit, 'p' to toggle privacy anonymization, 'r' to reload ROI config.
        """
        if self.video_processor is None or not self.video_processor.is_streaming:
            print("[-] Error: Video stream is not active. Attempting reconnection...")
            if not self.video_processor.connect():
                print("[-] Could not start pipeline. Exiting.")
                return

        self.is_running = True
        self.current_state = "MONITORING"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

        print("[VisionValidationEngine] Pipeline running. Press 'q' to quit, 'p' to toggle privacy mode.")

        try:
            while self.is_running:
                ret, frame = self.video_processor.capture_frame()
                if not ret or frame is None:
                    time.sleep(0.5)
                    self.video_processor.connect()
                    continue

                # 1. Pose estimation keypoint extraction
                detections = self.pose_estimator.extract_keypoints(frame)

                # 2. Behavioral dwell tracking and intent classification
                self.behavior_classifier.process_detections(detections)

                # 3. Render skeleton and privacy anonymization
                annotated = self.pose_estimator.draw_skeleton(frame, detections, privacy_mode=self.privacy_mode)

                # 4. Render Dynamic ROIs and live dwell progress
                active_states = self.behavior_classifier.get_all_states()
                annotated = self.spatial_mapper.draw_rois(annotated, active_states)

                # 5. Render HUD telemetry bar
                annotated = self._render_hud(annotated, detections)

                # Display frame
                cv2.imshow(window_name, annotated)

                # Key controls
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    print("[VisionValidationEngine] Stop command received. Terminating...")
                    break
                elif key == ord('p'):
                    self.privacy_mode = not self.privacy_mode
                    print(f"[VisionValidationEngine] Privacy mode toggled: {self.privacy_mode}")
                elif key == ord('r'):
                    self.spatial_mapper.load_roi_config()
                    print("[VisionValidationEngine] Reloaded ROI configuration from disk.")

        finally:
            self.stop()
            cv2.destroyAllWindows()

    def _render_hud(self, frame, detections):
        """Renders high-fidelity telemetry bar matching thesis UI specifications."""
        h, w = frame.shape[:2]

        bar_height = 42
        hud_overlay = frame.copy()
        cv2.rectangle(hud_overlay, (0, 0), (w, bar_height), (20, 20, 20), -1)
        cv2.addWeighted(hud_overlay, 0.75, frame, 0.25, 0, frame)

        fps_text = f"FPS: {self.video_processor.fps:.1f}"
        latency_text = f"Inference: {self.pose_estimator.last_inference_time_ms:.1f}ms"
        persons_text = f"Subjects: {len(detections)}"
        rois_text = f"ROIs: {len(self.spatial_mapper.compartments)}"
        nodes_online = len(self.mqtt_client.edge_nodes) if self.mqtt_client else 0
        mqtt_text = f"MQTT: {'ONLINE' if (self.mqtt_client and self.mqtt_client.is_connected) else 'SIM'} (Nodes:{nodes_online})"

        cv2.putText(frame, "SMART STORAGE VALIDATION", (12, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(frame, f"{fps_text} | {latency_text} | {persons_text} | {rois_text} | {mqtt_text}", (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (220, 220, 220), 1, cv2.LINE_AA)

        if self.active_alert and time.time() < self.alert_expiry_time:
            alert_bar_y = h - 45
            pill_w = min(w - 40, 580)
            cv2.rectangle(frame, (20, alert_bar_y), (20 + pill_w, h - 10), self.alert_color, -1)
            cv2.rectangle(frame, (20, alert_bar_y), (20 + pill_w, h - 10), (255, 255, 255), 1)
            cv2.putText(
                frame, self.active_alert,
                (30, alert_bar_y + 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA
            )
        elif self.active_alert and time.time() >= self.alert_expiry_time:
            self.active_alert = None

        return frame

    def stop(self):
        """Stops pipeline and frees hardware resources."""
        self.is_running = False
        self.current_state = "STOPPED"
        if self.video_processor:
            self.video_processor.release_stream()
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        print("[VisionValidationEngine] Pipeline stopped successfully.")
