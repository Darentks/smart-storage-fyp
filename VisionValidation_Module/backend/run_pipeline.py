"""
Main Execution Script for Vision-Based Human Interaction Analysis Engine
Smart Storage FYP
Author: Daren Tan Khai Shuen
"""

import argparse
import os
import sys

# Ensure backend root is on Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from VisionValidation_Module.backend.src.vision_engine import VisionValidationEngine


def main():
    parser = argparse.ArgumentParser(
        description="Smart Storage - YOLO11-Pose Vision Validation Pipeline"
    )
    parser.add_argument(
        "--source",
        default="0",
        help="Camera index (0, 1) or RTSP stream URL or video file path (default: 0)",
    )
    parser.add_argument(
        "--model",
        default="models/yolo11n-pose.pt",
        help="Path to YOLO11-Pose weights (default: models/yolo11n-pose.pt)",
    )
    parser.add_argument(
        "--config",
        default="config/roi_config.json",
        help="Path to ROI configuration JSON (default: config/roi_config.json)",
    )
    parser.add_argument(
        "--broker",
        default="localhost",
        help="MQTT Broker address (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1883,
        help="MQTT Broker port (default: 1883)",
    )
    parser.add_argument(
        "--privacy",
        action="store_true",
        help="Enable face/head blurring for NFR-05 privacy preservation compliance",
    )

    args = parser.parse_args()

    # Convert source to int if numeric
    source = args.source
    try:
        source = int(source)
    except ValueError:
        pass

    engine = VisionValidationEngine(
        camera_source=source,
        model_path=args.model,
        roi_config_path=args.config,
        mqtt_broker=args.broker,
        mqtt_port=args.port,
        privacy_mode=args.privacy,
    )

    engine.initialize_system()
    engine.run_pipeline()


if __name__ == "__main__":
    main()

