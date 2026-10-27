"""
Pose Estimator Module for Smart Storage FYP
Extracts skeletal keypoints using Ultralytics YOLO11-Pose.
Complies with NFR-03 (Accuracy), NFR-04 (Hardware/Skeletal Constraints), and NFR-05 (Privacy Preservation).
"""

import os
import time
from typing import Any
import numpy as np
import cv2
from ultralytics import YOLO


def _to_numpy(val: Any) -> np.ndarray:
    """Safely converts a PyTorch Tensor or NumPy array to a CPU NumPy ndarray."""
    if val is None:
        return np.array([])
    if hasattr(val, "cpu"):
        return val.cpu().numpy()
    return np.asarray(val)


class PoseEstimator:
    """
    Skeletal Keypoint Extraction Engine using YOLO11-Pose.
    Focuses on upper-body keypoints: Wrists (9, 10), Elbows (7, 8), Shoulders (5, 6).
    """

    # COCO Keypoint standard upper-body indices
    KEYPOINT_NOSE = 0
    KEYPOINT_LEFT_EYE = 1
    KEYPOINT_RIGHT_EYE = 2
    KEYPOINT_LEFT_EAR = 3
    KEYPOINT_RIGHT_EAR = 4
    KEYPOINT_LEFT_SHOULDER = 5
    KEYPOINT_RIGHT_SHOULDER = 6
    KEYPOINT_LEFT_ELBOW = 7
    KEYPOINT_RIGHT_ELBOW = 8
    KEYPOINT_LEFT_WRIST = 9
    KEYPOINT_RIGHT_WRIST = 10
    KEYPOINT_LEFT_HIP = 11
    KEYPOINT_RIGHT_HIP = 12

    WRIST_INDICES = [KEYPOINT_LEFT_WRIST, KEYPOINT_RIGHT_WRIST]
    ELBOW_INDICES = [KEYPOINT_LEFT_ELBOW, KEYPOINT_RIGHT_ELBOW]
    SHOULDER_INDICES = [KEYPOINT_LEFT_SHOULDER, KEYPOINT_RIGHT_SHOULDER]

    # Upper body limb connections (pairs of indices)
    SKELETON_LIMBS = [
        (KEYPOINT_LEFT_SHOULDER, KEYPOINT_RIGHT_SHOULDER),
        (KEYPOINT_LEFT_SHOULDER, KEYPOINT_LEFT_ELBOW),
        (KEYPOINT_LEFT_ELBOW, KEYPOINT_LEFT_WRIST),
        (KEYPOINT_RIGHT_SHOULDER, KEYPOINT_RIGHT_ELBOW),
        (KEYPOINT_RIGHT_ELBOW, KEYPOINT_RIGHT_WRIST),
        (KEYPOINT_LEFT_SHOULDER, KEYPOINT_LEFT_HIP),
        (KEYPOINT_RIGHT_SHOULDER, KEYPOINT_RIGHT_HIP),
        (KEYPOINT_LEFT_HIP, KEYPOINT_RIGHT_HIP),
    ]

    def __init__(self, model_path="models/yolo11n-pose.pt", oks_threshold=0.50, device="cpu"):
        """
        Initializes the YOLO11-Pose inference engine.
        :param model_path: Path to the YOLO11 pose weights (default: models/yolo11n-pose.pt)
        :param oks_threshold: Confidence threshold for keypoints (COCO OKS threshold >= 0.50 per NFR-04)
        :param device: 'cpu' or 'cuda' / 0 if GPU available
        """
        self.oks_threshold = oks_threshold
        self.device = device
        self.model_path = self._resolve_model_path(model_path)
        
        print(f"[PoseEstimator] Loading model from: {self.model_path}")
        self.model = YOLO(self.model_path)
        self.last_inference_time_ms = 0.0

    def _resolve_model_path(self, model_path):
        """Resolves model path across common workspace directories."""
        candidate_paths = [
            model_path,
            os.path.join(os.path.dirname(__file__), "..", model_path),
            os.path.join(os.path.dirname(__file__), "..", "models", os.path.basename(model_path)),
            os.path.join("backend", "models", os.path.basename(model_path)),
            os.path.join("models", os.path.basename(model_path)),
            os.path.basename(model_path),
        ]
        for p in candidate_paths:
            if os.path.exists(p):
                return os.path.abspath(p)
        # Fall back to base filename for Ultralytics auto-download
        return os.path.basename(model_path)

    def extract_keypoints(self, frame):
        """
        Runs YOLO11-Pose on a single frame and extracts all detected person skeletal data.
        :param frame: BGR image from OpenCV
        :return: list of dicts, each containing:
                 - 'person_id': int index
                 - 'box': [x1, y1, x2, y2]
                 - 'box_conf': float
                 - 'keypoints': np.ndarray (17, 2) (x, y)
                 - 'confs': np.ndarray (17,)
                 - 'wrists': list of dicts [{'side': 'left'|'right', 'index': int, 'pt': (x,y), 'conf': float}]
                 - 'elbows': list of dicts [{'side': 'left'|'right', 'index': int, 'pt': (x,y), 'conf': float}]
                 - 'shoulders': list of dicts [{'side': 'left'|'right', 'index': int, 'pt': (x,y), 'conf': float}]
        """
        t_start = time.perf_counter()
        results = self.model(frame, verbose=False, conf=self.oks_threshold, device=self.device)
        self.last_inference_time_ms = (time.perf_counter() - t_start) * 1000.0

        detections = []
        for result in results:
            kpts_obj: Any = getattr(result, "keypoints", None)
            if kpts_obj is None or len(kpts_obj) == 0:
                continue

            xy_data = _to_numpy(getattr(kpts_obj, "xy", None))
            kpt_conf = getattr(kpts_obj, "conf", None)
            conf_data = _to_numpy(kpt_conf) if kpt_conf is not None else None

            boxes_obj: Any = getattr(result, "boxes", None)
            boxes = _to_numpy(getattr(boxes_obj, "xyxy", None)) if boxes_obj is not None else None
            box_confs = _to_numpy(getattr(boxes_obj, "conf", None)) if boxes_obj is not None else None

            for i, kpts in enumerate(xy_data):
                confs = conf_data[i] if conf_data is not None else np.ones(len(kpts))
                box = boxes[i] if boxes is not None and i < len(boxes) else [0, 0, 0, 0]
                box_conf = float(box_confs[i]) if box_confs is not None and i < len(box_confs) else 1.0

                # Extract wrists
                wrists = []
                for side, idx in [("left", self.KEYPOINT_LEFT_WRIST), ("right", self.KEYPOINT_RIGHT_WRIST)]:
                    wrists.append({
                        "side": side,
                        "index": idx,
                        "pt": (float(kpts[idx][0]), float(kpts[idx][1])),
                        "conf": float(confs[idx]),
                        "is_valid": float(confs[idx]) >= self.oks_threshold and (kpts[idx][0] > 0 or kpts[idx][1] > 0),
                    })

                # Extract elbows
                elbows = []
                for side, idx in [("left", self.KEYPOINT_LEFT_ELBOW), ("right", self.KEYPOINT_RIGHT_ELBOW)]:
                    elbows.append({
                        "side": side,
                        "index": idx,
                        "pt": (float(kpts[idx][0]), float(kpts[idx][1])),
                        "conf": float(confs[idx]),
                        "is_valid": float(confs[idx]) >= self.oks_threshold and (kpts[idx][0] > 0 or kpts[idx][1] > 0),
                    })

                # Extract shoulders
                shoulders = []
                for side, idx in [("left", self.KEYPOINT_LEFT_SHOULDER), ("right", self.KEYPOINT_RIGHT_SHOULDER)]:
                    shoulders.append({
                        "side": side,
                        "index": idx,
                        "pt": (float(kpts[idx][0]), float(kpts[idx][1])),
                        "conf": float(confs[idx]),
                        "is_valid": float(confs[idx]) >= self.oks_threshold and (kpts[idx][0] > 0 or kpts[idx][1] > 0),
                    })

                detections.append({
                    "person_id": i,
                    "box": box,
                    "box_conf": box_conf,
                    "keypoints": kpts,
                    "confs": confs,
                    "wrists": wrists,
                    "elbows": elbows,
                    "shoulders": shoulders,
                })

        return detections

    def draw_skeleton(self, frame, detections, privacy_mode=False):
        """
        Renders skeletal wireframes on the image.
        :param frame: BGR frame to draw on
        :param detections: Result from extract_keypoints()
        :param privacy_mode: If True, censors/blurs user head region to comply with NFR-05 privacy preservation
        :return: Annotated frame
        """
        annotated = frame.copy()

        for det in detections:
            kpts = det["keypoints"]
            confs = det["confs"]

            # Optional privacy preservation: anonymize head/face region
            if privacy_mode:
                x1, y1, x2, y2 = [int(v) for v in det["box"]]
                head_y2 = y1 + int((y2 - y1) * 0.25)
                head_roi = annotated[max(0, y1):min(frame.shape[0], head_y2), max(0, x1):min(frame.shape[1], x2)]
                if head_roi.size > 0:
                    blurred = cv2.GaussianBlur(head_roi, (51, 51), 30)
                    annotated[max(0, y1):min(frame.shape[0], head_y2), max(0, x1):min(frame.shape[1], x2)] = blurred

            # Draw limbs
            for pt1_idx, pt2_idx in self.SKELETON_LIMBS:
                if pt1_idx < len(kpts) and pt2_idx < len(kpts):
                    c1, c2 = confs[pt1_idx], confs[pt2_idx]
                    if c1 >= self.oks_threshold and c2 >= self.oks_threshold:
                        p1 = (int(kpts[pt1_idx][0]), int(kpts[pt1_idx][1]))
                        p2 = (int(kpts[pt2_idx][0]), int(kpts[pt2_idx][1]))
                        cv2.line(annotated, p1, p2, (0, 255, 255), 2, cv2.LINE_AA)

            # Draw joints with upper body color highlights
            for idx in range(min(13, len(kpts))):
                conf = confs[idx]
                if conf >= self.oks_threshold:
                    pt = (int(kpts[idx][0]), int(kpts[idx][1]))
                    if idx in self.WRIST_INDICES:
                        # Wrists: prominent red circle with white border
                        cv2.circle(annotated, pt, 7, (0, 0, 255), -1, cv2.LINE_AA)
                        cv2.circle(annotated, pt, 9, (255, 255, 255), 1, cv2.LINE_AA)
                        # Display wrist confidence score (NFR-04)
                        cv2.putText(
                            annotated, f"{conf:.2f}",
                            (pt[0] + 10, pt[1] - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1, cv2.LINE_AA
                        )
                    elif idx in self.ELBOW_INDICES:
                        cv2.circle(annotated, pt, 5, (0, 165, 255), -1, cv2.LINE_AA)
                    elif idx in self.SHOULDER_INDICES:
                        cv2.circle(annotated, pt, 5, (255, 200, 0), -1, cv2.LINE_AA)
                    else:
                        cv2.circle(annotated, pt, 4, (0, 255, 0), -1, cv2.LINE_AA)

        return annotated

