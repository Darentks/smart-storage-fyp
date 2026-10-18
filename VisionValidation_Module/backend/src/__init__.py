"""
Smart Storage FYP - Vision-Based Human Interaction Analysis Engine
Author: Daren Tan Khai Shuen
"""

from .spatial_mapper import SpatialMapper
from .behavior_classifier import BehaviorClassifier
from .video_processor import VideoProcessor
from .mqtt_client import MQTTClientNode
from .opencv_comparator import OpenCVComparator
from .reference_validator import ESP32ReferenceManager
from .config_loader import (
    get_supabase_client,
    get_active_rtsp_url,
    get_active_camera_record,
    build_hikvision_rtsp_url,
    list_esp32_cameras,
    update_esp32_health_in_supabase,
    update_compartment_baseline_in_supabase,
    apply_inventory_delta_in_supabase,
    list_comparison_logs,
    insert_esp32_reference_sample,
    list_esp32_reference_samples,
    delete_esp32_reference_sample,
)

try:
    from .pose_estimator import PoseEstimator
    from .vision_engine import VisionValidationEngine
except ImportError:
    PoseEstimator = None  # type: ignore
    VisionValidationEngine = None  # type: ignore

__all__ = [
    "PoseEstimator",
    "SpatialMapper",
    "BehaviorClassifier",
    "VideoProcessor",
    "VisionValidationEngine",
    "MQTTClientNode",
    "OpenCVComparator",
    "ESP32ReferenceManager",
    "get_supabase_client",
    "get_active_rtsp_url",
    "get_active_camera_record",
    "build_hikvision_rtsp_url",
    "list_esp32_cameras",
    "update_esp32_health_in_supabase",
    "update_compartment_baseline_in_supabase",
    "apply_inventory_delta_in_supabase",
    "list_comparison_logs",
    "insert_esp32_reference_sample",
    "list_esp32_reference_samples",
    "delete_esp32_reference_sample",
]
