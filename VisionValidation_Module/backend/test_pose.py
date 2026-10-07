"""
Smart Storage FYP - YOLO11-Pose & Dynamic ROI Intent Verification Test
Author: Daren Tan Khai Shuen


"""

import os
import sys
import time
import json
import cv2

# Ensure backend root is in import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from VisionValidation_Module.backend.src.pose_estimator import PoseEstimator
from VisionValidation_Module.backend.src.spatial_mapper import SpatialMapper
from VisionValidation_Module.backend.src.behavior_classifier import BehaviorClassifier
from VisionValidation_Module.backend.src.video_processor import VideoProcessor
from VisionValidation_Module.backend.src.mqtt_client import MQTTClientNode


def on_intent_trigger(event_payload):
    """Callback executed when a user sustains reaching intent for >= 1.2s."""
    print("\n" + "=" * 60)
    print(" [!] REPLACING PROXIMITY WITH VISUAL INTENT VERIFIED!")
    print(f"     Target Shelf: {event_payload['shelf_id']}")
    print(f"     Action:       {event_payload['expected_action']}")
    print(f"     Dwell Time:   {event_payload['dwell_duration_seconds']}s (Threshold: >=1.2s)")
    print(f"     Confidence:   {event_payload['oks_confidence']:.2f} (Threshold: >0.50)")
    print(f"     Timestamp:    {event_payload['timestamp']}")
    print(" [>] MQTT Dispatch Topic: iotlab/storage/RACK_01/trigger")
    print(f"     Payload: {json.dumps(event_payload, indent=2)}")
    print("=" * 60 + "\n")


def main():
    print("=" * 65)
    print("   SMART STORAGE FYP - YOLO11-POSE & DYNAMIC ROI VALIDATION")
    print("=" * 65)

    # 1. Load Dynamic ROI configuration
    config_file = os.path.join(os.path.dirname(__file__), "config", "roi_config.json")
    print(f"[+] Step 1: Loading dynamic ROI configuration from {config_file}...")
    spatial_mapper = SpatialMapper(config_file)
    print(f"[+] Loaded {len(spatial_mapper.compartments)} compartment ROIs for Rack '{spatial_mapper.rack_id}'.")

    # 2. Initialize YOLO11-Pose model
    model_path = os.path.join(os.path.dirname(__file__), "models", "yolo11n-pose.pt")
    print(f"[+] Step 2: Loading YOLO11-Pose engine from {model_path}...")
    pose_estimator = PoseEstimator(model_path=model_path, oks_threshold=0.50)
    print("[+] Model loaded successfully.")

    # 3. Initialize Behavioral State Classifier (with 1.2s dwell requirement)
    print("[+] Step 3: Initializing Behavior Classifier (Dwell Threshold: 1.2s, OKS > 0.50)...")
    behavior_classifier = BehaviorClassifier(
        spatial_mapper=spatial_mapper,
        on_intent_confirmed=on_intent_trigger
    )

    # 4. Initialize MQTT publisher (runs in simulation/local mode if broker offline)
    mqtt_node = MQTTClientNode(rack_id=spatial_mapper.rack_id)
    mqtt_node.connect_broker()

    # 5. Open Macro Camera Feed
    CAMERA_INDEX = 0
    print(f"[+] Step 5: Ingesting video stream from camera index {CAMERA_INDEX}...")
    video = VideoProcessor(source=CAMERA_INDEX, width=640, height=480)

    if not video.is_streaming:
        print(f"[-] ERROR: Could not open camera {CAMERA_INDEX}.")
        print("    -> Check if another app is using the webcam.")
        print("    -> Run 'python calibrate_roi.py' or check device permissions.")
        return

    window_name = "Smart Storage FYP - YOLO11-Pose & Dynamic ROI Tracking"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    print("\n[+] System running!")
    print("    -> Reach into any colored shelf box with your hand.")
    print("    -> Hold for 1.2 seconds to confirm reaching intent.")
    print("    -> Press 'p' to toggle Privacy Anonymization.")
    print("    -> Press 'r' to reload ROI config from disk.")
    print("    -> Press 'q' to exit.\n")

    privacy_mode = False
    alert_banner = None
    alert_time = 0.0

    try:
        while True:
            ret, frame = video.capture_frame()
            if not ret or frame is None:
                continue

            t_start = time.perf_counter()

            # 1. Extract skeletal keypoints (wrists, elbows, shoulders)
            detections = pose_estimator.extract_keypoints(frame)

            # 2. Update behavioral state machine and dwell timers
            new_events = behavior_classifier.process_detections(detections)
            for event in new_events:
                alert_banner = f"INTENT VERIFIED: [{event['expected_action']}] -> {event['shelf_id']}"
                alert_time = time.time() + 3.0
                mqtt_node.publish_trigger(
                    shelf_id=event["shelf_id"],
                    expected_action=event["expected_action"],
                    confidence=event["oks_confidence"]
                )

            # 3. Draw skeletal wireframe
            annotated = pose_estimator.draw_skeleton(frame, detections, privacy_mode=privacy_mode)

            # 4. Draw dynamic ROIs with dwell progress bars
            active_states = behavior_classifier.get_all_states()
            annotated = spatial_mapper.draw_rois(annotated, active_states)

            # 5. Render HUD diagnostic bar
            h, w = annotated.shape[:2]
            cv2.rectangle(annotated, (0, 0), (w, 38), (25, 25, 25), -1)

            inf_ms = pose_estimator.last_inference_time_ms
            hud_str = f"FPS: {video.fps:.1f} | Latency: {inf_ms:.1f}ms | People: {len(detections)} | Privacy: {'ON' if privacy_mode else 'OFF'}"
            cv2.putText(annotated, hud_str, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)

            # Alert notification pill
            if alert_banner and time.time() < alert_time:
                cv2.rectangle(annotated, (20, h - 45), (min(w - 20, 520), h - 12), (0, 180, 0), -1)
                cv2.rectangle(annotated, (20, h - 45), (min(w - 20, 520), h - 12), (255, 255, 255), 1)
                cv2.putText(annotated, alert_banner, (30, h - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (255, 255, 255), 1, cv2.LINE_AA)

            cv2.imshow(window_name, annotated)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('p'):
                privacy_mode = not privacy_mode
                print(f"[+] Privacy Anonymization: {'ENABLED' if privacy_mode else 'DISABLED'}")
            elif key == ord('r'):
                spatial_mapper.load_roi_config()
                print("[+] Reloaded ROI configuration from disk.")

    finally:
        video.release_stream()
        mqtt_node.disconnect()
        cv2.destroyAllWindows()
        print("[+] Test terminated cleanly.")


if __name__ == "__main__":
    main()