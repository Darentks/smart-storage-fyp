"""
ESP32-CAM 2x2 Feature Signature, Rolling Baseline Cache, & Hybrid Reference Validator
Smart Storage FYP
Author: Daren Tan Khai Shuen

Features:
1. extract_2x2_feature_signature:
   Extracts color (BGR, HSV), luminance, and edge density per 2x2 quadrant (TL, TR, BL, BR).
2. compute_signature_similarity:
   Computes similarity metric (0..100%) between two 2x2 signatures or against reference states.
3. Base64 data URL encode / decode helpers for OpenCV BGR images.
4. ESP32ReferenceManager:
   - Manages 30-second rolling baseline cache in memory.
   - Updates 30s baseline in memory and syncs with Supabase public.compartments.
   - Performs hybrid OpenCV comparison (30s baseline absdiff + reference sample signature matching)
     to determine item removal (-1), return (+1), or no change (0).
"""

import base64
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

cv2: Any
np: Any
try:
    import cv2 as _cv2_module
    import numpy as _np_module
    cv2 = _cv2_module
    np = _np_module
    _OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    _OPENCV_AVAILABLE = False

try:
    from .config_loader import (
        get_supabase_client,
        update_compartment_baseline_in_supabase,
        list_esp32_reference_samples,
        insert_esp32_reference_sample,
    )
except ImportError:
    from config_loader import (
        get_supabase_client,
        update_compartment_baseline_in_supabase,
        list_esp32_reference_samples,
        insert_esp32_reference_sample,
    )


# =============================================================================
# SCENARIO CONSTANTS & MAPS
# =============================================================================

SCENARIO_MAP: Dict[int, str] = {
    4: "Scenario 1: 4/4 Boxes Present",
    3: "Scenario 2: 3/4 Boxes Present",
    2: "Scenario 3: 2/4 Boxes Present",
    1: "Scenario 4: 1/4 Box Present",
    0: "Scenario 5: 0/4 No Box Present",
}


# =============================================================================
# IMAGE ENCODING & DECODING HELPERS
# =============================================================================

def encode_bgr_to_data_url(frame_bgr: Any, quality: int = 80) -> str:
    """Encodes an OpenCV BGR image array into a base64 JPEG data URL."""
    if not _OPENCV_AVAILABLE or frame_bgr is None:
        return ""
    try:
        success, buffer = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not success:
            return ""
        b64_str = base64.b64encode(buffer).decode("utf-8")
        return f"data:image/jpeg;base64,{b64_str}"
    except Exception:
        return ""


def decode_data_url_to_bgr(data_url: str) -> Optional[Any]:
    """Decodes a base64 JPEG/PNG data URL or raw base64 string into an OpenCV BGR array."""
    if not _OPENCV_AVAILABLE or not data_url or not isinstance(data_url, str):
        return None
    try:
        if "," in data_url:
            base64_data = data_url.split(",", 1)[1]
        else:
            base64_data = data_url
        img_bytes = base64.b64decode(base64_data)
        arr = np.asarray(bytearray(img_bytes), dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame
    except Exception as exc:
        print(f"[ReferenceValidator] Failed to decode data URL to BGR: {exc}")
        return None


def enhance_low_light_frame(frame_bgr: Any, boost_mode: str = "auto") -> Any:
    """
    Automatic low-light shadow enhancement for ESP32-CAM images.
    Applies CLAHE on the L-channel and adaptive gamma correction to lift dark shadows.
    """
    if not _OPENCV_AVAILABLE or frame_bgr is None or boost_mode == "off":
        return frame_bgr
    try:
        lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        mean_l = float(np.mean(l))

        if boost_mode == "auto" and mean_l >= 115.0:
            return frame_bgr

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        l_clahe = clahe.apply(l)
        enhanced = cv2.cvtColor(cv2.merge((l_clahe, a, b)), cv2.COLOR_LAB2BGR)

        if mean_l < 55:
            gamma = 0.42
        elif mean_l < 85:
            gamma = 0.55
        elif boost_mode != "high":
            gamma = 0.72
        else:
            gamma = 0.45

        lut = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)]).astype("uint8")
        enhanced = cv2.LUT(enhanced, lut)
        return enhanced
    except Exception as exc:
        print(f"[ReferenceValidator] Low-light frame enhancement failed: {exc}")
        return frame_bgr


def synthesize_compartment_frame(label: str = "ESP32-CAM", box_count: int = 4) -> Any:
    """Synthesizes an offline placeholder frame for testing when hardware is disconnected."""
    if not _OPENCV_AVAILABLE:
        return None
    synth = np.full((480, 640, 3), (30, 32, 38), dtype=np.uint8)
    # Draw 2x2 grid divider
    cv2.line(synth, (320, 40), (320, 440), (70, 75, 85), 2)
    cv2.line(synth, (40, 240), (600, 240), (70, 75, 85), 2)

    quads = [
        ("Slot 1 (TL)", (60, 80, 280, 220), (0, 180, 255)),
        ("Slot 2 (TR)", (360, 80, 580, 220), (0, 220, 100)),
        ("Slot 3 (BL)", (60, 260, 280, 400), (255, 160, 0)),
        ("Slot 4 (BR)", (360, 260, 580, 400), (220, 80, 255)),
    ]

    for idx, (qname, bbox, qcolor) in enumerate(quads):
        x1, y1, x2, y2 = bbox
        if idx < box_count:
            cv2.rectangle(synth, (x1, y1), (x2, y2), qcolor, -1)
            cv2.putText(synth, f"{qname} [BOX]", (x1 + 15, y1 + 75), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2)
        else:
            cv2.rectangle(synth, (x1, y1), (x2, y2), (45, 48, 55), -1)
            cv2.rectangle(synth, (x1, y1), (x2, y2), (90, 95, 105), 1)
            cv2.putText(synth, f"{qname} [EMPTY]", (x1 + 15, y1 + 75), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (160, 160, 160), 1)

    cv2.putText(synth, f"{label} | Boxes: {box_count}/4", (30, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (240, 240, 240), 2)
    return synth


# =============================================================================
# 2X2 FEATURE SIGNATURE & SIMILARITY COMPUTATION
# =============================================================================

QUADRANT_KEYS = ["slot_1_tl", "slot_2_tr", "slot_3_bl", "slot_4_br"]


def extract_2x2_feature_signature(frame_bgr: Any) -> Dict[str, Any]:
    """
    Extracts color (BGR, HSV), luminance, and edge density per 2x2 quadrant
    from an ESP32-CAM compartment image.
    """
    if not _OPENCV_AVAILABLE or frame_bgr is None:
        return {}

    h, w = frame_bgr.shape[:2]
    mx, my = w // 2, h // 2

    quadrants = {
        "slot_1_tl": (0, 0, mx, my),
        "slot_2_tr": (mx, 0, w, my),
        "slot_3_bl": (0, my, mx, h),
        "slot_4_br": (mx, my, w, h),
    }

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    signature: Dict[str, Any] = {}
    for q_key, (x1, y1, x2, y2) in quadrants.items():
        crop_bgr = frame_bgr[y1:y2, x1:x2]
        crop_hsv = hsv[y1:y2, x1:x2]
        crop_gray = gray[y1:y2, x1:x2]
        crop_edges = edges[y1:y2, x1:x2]

        if crop_bgr.size == 0:
            signature[q_key] = {
                "mean_bgr": [0.0, 0.0, 0.0],
                "mean_hsv": [0.0, 0.0, 0.0],
                "luminance": 0.0,
                "edge_density": 0.0,
            }
            continue

        mean_bgr = [round(float(v), 2) for v in cv2.mean(crop_bgr)[:3]]
        mean_hsv = [round(float(v), 2) for v in cv2.mean(crop_hsv)[:3]]
        luminance = round(float(np.mean(crop_gray)), 2)
        edge_density = round(float(np.mean(crop_edges > 0)), 4)

        signature[q_key] = {
            "mean_bgr": mean_bgr,
            "mean_hsv": mean_hsv,
            "luminance": luminance,
            "edge_density": edge_density,
        }

    return signature


def compute_signature_similarity(sig_a: Dict[str, Any], sig_b: Dict[str, Any]) -> float:
    """
    Computes a normalized similarity score (0..100%) between two 2x2 signatures.
    Evaluates color distance, luminance contrast, and edge density across all 4 slots.
    """
    if not sig_a or not sig_b:
        return 0.0

    slot_scores: List[float] = []
    for k in QUADRANT_KEYS:
        qa = sig_a.get(k)
        qb = sig_b.get(k)
        if not qa or not qb:
            continue

        bgr_a = np.array(qa.get("mean_bgr", [0, 0, 0]), dtype=np.float32)
        bgr_b = np.array(qb.get("mean_bgr", [0, 0, 0]), dtype=np.float32)
        bgr_dist = float(np.linalg.norm(bgr_a - bgr_b))
        # Max BGR Euclidean distance is sqrt(255^2 * 3) ~ 441.67
        bgr_sim = max(0.0, 1.0 - (bgr_dist / 441.67))

        lum_a = float(qa.get("luminance", 0.0))
        lum_b = float(qb.get("luminance", 0.0))
        lum_sim = max(0.0, 1.0 - (abs(lum_a - lum_b) / 255.0))

        edge_a = float(qa.get("edge_density", 0.0))
        edge_b = float(qb.get("edge_density", 0.0))
        edge_sim = max(0.0, 1.0 - abs(edge_a - edge_b))

        slot_score = (bgr_sim * 0.5) + (lum_sim * 0.3) + (edge_sim * 0.2)
        slot_scores.append(slot_score)

    if not slot_scores:
        return 0.0

    avg_similarity = float(np.mean(slot_scores)) * 100.0
    return round(avg_similarity, 2)


def render_annotated_2x2_preview(frame_bgr: Any, signature: Dict[str, Any]) -> Any:
    """Renders annotated 2x2 bounding boxes with slot statistics onto the image."""
    if not _OPENCV_AVAILABLE or frame_bgr is None:
        return frame_bgr

    annotated = frame_bgr.copy()
    h, w = annotated.shape[:2]
    mx, my = w // 2, h // 2

    cv2.line(annotated, (mx, 0), (mx, h), (0, 255, 255), 1)
    cv2.line(annotated, (0, my), (w, my), (0, 255, 255), 1)

    labels = {
        "slot_1_tl": ("Slot 1 (TL)", 10, 25),
        "slot_2_tr": ("Slot 2 (TR)", mx + 10, 25),
        "slot_3_bl": ("Slot 3 (BL)", 10, my + 25),
        "slot_4_br": ("Slot 4 (BR)", mx + 10, my + 25),
    }

    for k, (title, tx, ty) in labels.items():
        stats = signature.get(k, {})
        lum = stats.get("luminance", 0.0)
        edge = stats.get("edge_density", 0.0)
        cv2.putText(annotated, f"{title}: Y={lum:.1f} E={edge:.3f}", (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)

    return annotated


# =============================================================================
# ESP32 REFERENCE MANAGER & HYBRID BASELINE COMPARATOR
# =============================================================================

class ESP32ReferenceManager:
    """
    Manages tested ESP32-CAM reference samples and handles 30-second resting baseline caching
    plus hybrid OpenCV difference comparisons against reference signatures.
    """

    def __init__(self):
        # 30-second rolling baseline cache per shelf_id:
        # self.baseline_cache[shelf_id] = { "frame_bgr": frame_bgr, "data_url": data_url, "updated_at": now_iso }
        self.baseline_cache: Dict[str, Dict[str, Any]] = {}
        # Guardrail 1: 30-Second Baseline Freeze Lock timestamp per shelf_id
        self.baseline_freeze_until: Dict[str, float] = {}
        self.vision_boost_mode: str = "auto"

    def freeze_baseline(self, shelf_id: str, duration_sec: float = 10.0) -> None:
        """Sets freeze lock on shelf baseline so 30s poller won't overwrite during active user hand interaction."""
        self.baseline_freeze_until[shelf_id.strip()] = time.time() + duration_sec

    def fetch_esp32_frame(
        self,
        esp32_ip: Optional[str] = None,
        mode: str = "live",
        timeout: float = 3.0,
        box_count_fallback: int = 4,
        flash: Optional[int] = None,
    ) -> Tuple[Any, str]:
        """
        Fetches an image from http://{esp32_ip}/capture?mode={mode}.
        Returns (frame_bgr, data_url). If unreachable or cv2 unavailable, returns synthetic frame.
        """
        if esp32_ip and _OPENCV_AVAILABLE:
            clean_ip = esp32_ip.replace("http://", "").replace("/", "").strip()
            flash_val = flash if flash is not None else 0
            url = f"http://{clean_ip}/capture?mode={mode}&flash={flash_val}&t={int(time.time() * 1000)}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "SmartStorageVision/1.0",
                        "Cache-Control": "no-cache, no-store",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw_bytes = resp.read()
                    arr = np.asarray(bytearray(raw_bytes), dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        frame = enhance_low_light_frame(frame, boost_mode=self.vision_boost_mode)
                        data_url = encode_bgr_to_data_url(frame)
                        return frame, data_url
            except Exception:
                pass

        # Offline synthetic frame fallback
        synth = synthesize_compartment_frame(f"ESP32-CAM ({mode.upper()})", box_count=box_count_fallback)
        data_url = encode_bgr_to_data_url(synth)
        return synth, data_url

    def fetch_esp32_http_frame(self, esp32_ip: Optional[str] = None, mode: str = "live") -> Tuple[Any, str]:
        """Alias for fetch_esp32_frame to fetch a frame from http://{esp32_ip}/capture."""
        return self.fetch_esp32_frame(esp32_ip=esp32_ip, mode=mode)

    def fetch_dual_esp32_frame(
        self,
        esp32_ip1: Optional[str],
        esp32_ip2: Optional[str] = None,
        mode: str = "live",
        timeout: float = 3.5,
        flash: Optional[int] = None,
    ) -> Tuple[Any, str, Optional[str], Optional[str]]:
        """
        Fetches Back Cam (esp32_ip1) and Front Cam (esp32_ip2) with &flash=0,
        retrying failed HTTP requests once (timeout=2.0).
        Returns (combined_bgr, combined_data_url, url1, url2).
        Stitches the two images side-by-side horizontally for logging & comparison.
        """
        def _fetch_single_with_retry(ip: str) -> Tuple[Optional[Any], Optional[str]]:
            clean = ip.replace("http://", "").replace("/", "").strip()
            flash_val = flash if flash is not None else 0
            f_url = f"http://{clean}/capture?mode={mode}&flash={flash_val}&t={int(time.time() * 1000)}"
            for attempt in range(2):
                req_timeout = timeout if attempt == 0 else 2.0
                try:
                    req = urllib.request.Request(
                        f_url,
                        headers={"User-Agent": "SmartStorageVision/1.0", "Cache-Control": "no-cache"},
                    )
                    with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                        raw = resp.read()
                        arr = np.asarray(bytearray(raw), dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR) if _OPENCV_AVAILABLE else None
                        if frame is not None:
                            frame = enhance_low_light_frame(frame, boost_mode=self.vision_boost_mode)
                            return frame, encode_bgr_to_data_url(frame)
                except Exception as exc:
                    if attempt == 0:
                        time.sleep(0.1)
                        continue
                    print(f"[ReferenceValidator] Snapshot fetch failed for {clean} (attempt {attempt + 1}): {exc}")
            # Return (None, None) when hardware camera is unreachable so degraded camera guardrail triggers properly
            return None, None

        frame1 = None
        url1 = None
        frame2 = None
        url2 = None

        if esp32_ip1:
            frame1, url1 = _fetch_single_with_retry(esp32_ip1)

        if esp32_ip2:
            frame2, url2 = _fetch_single_with_retry(esp32_ip2)

        if frame1 is None and frame2 is None:
            synth = synthesize_compartment_frame(f"DUAL-CAM ({mode.upper()})", box_count=4)
            combined_url = encode_bgr_to_data_url(synth)
            return synth, combined_url, None, None

        if frame1 is not None and frame2 is not None and _OPENCV_AVAILABLE:
            h1, w1 = frame1.shape[:2]
            h2, w2 = frame2.shape[:2]
            target_h = max(h1, h2, 240)
            target_w = int(w1 * (target_h / float(h1))) if h1 > 0 else 320
            target_w2 = int(w2 * (target_h / float(h2))) if h2 > 0 else 320

            r1 = cv2.resize(frame1, (target_w, target_h))
            r2 = cv2.resize(frame2, (target_w2, target_h))

            # Overlay angle labels
            cv2.putText(r1, "BACK CAM", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(r2, "FRONT CAM", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

            combined_bgr = np.hstack([r1, r2])
            combined_url = encode_bgr_to_data_url(combined_bgr)
            return combined_bgr, combined_url, url1, url2

        single_frame = frame1 if frame1 is not None else frame2
        single_url = url1 if url1 is not None else (url2 or "")
        return single_frame, single_url, url1, url2

    def update_30s_baseline(
        self,
        shelf_id: str,
        esp32_ip: Optional[str] = None,
        esp32_ip2: Optional[str] = None,
        image_data_url: Optional[str] = None,
        image_2_data_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Updates the 30-second resting baseline cache for shelf_id and syncs to Supabase.
        Fetches both esp32_ip (CAM 1 Back) and esp32_ip2 (CAM 2 Front) or uses provided data URLs,
        stitches them into combined_url [BACK CAM | FRONT CAM], caches "data_url" and "data_url_2"
        in self.baseline_cache[shelf_id], and updates baseline_image_url and baseline_image_2_url in public.compartments.
        """
        shelf_id = shelf_id.strip()
        # Guardrail 1: Skip updating if baseline is currently frozen during active hand interaction
        if time.time() < self.baseline_freeze_until.get(shelf_id, 0.0):
            print(f"[ESP32ReferenceManager] Baseline update frozen for {shelf_id} (active interaction lock).")
            return self.baseline_cache.get(shelf_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        frame_bgr = None
        data_url = None
        url_back = None
        url_front = image_2_data_url

        if image_data_url:
            frame_bgr = decode_data_url_to_bgr(image_data_url)
            data_url = image_data_url
            url_back = image_data_url
        elif esp32_ip and esp32_ip2:
            frame_bgr, data_url, url_back, url_front = self.fetch_dual_esp32_frame(
                esp32_ip1=esp32_ip,
                esp32_ip2=esp32_ip2,
                mode="baseline",
            )
        elif esp32_ip:
            frame_bgr, data_url = self.fetch_esp32_frame(esp32_ip, mode="baseline")
            url_back = data_url

        if frame_bgr is None:
            synth = synthesize_compartment_frame(f"{shelf_id} (BASELINE)", box_count=4)
            frame_bgr = synth
            data_url = encode_bgr_to_data_url(synth)

        cached_entry = {
            "frame_bgr": frame_bgr,
            "data_url": data_url or "",
            "data_url_2": url_front or "",
            "url_back": url_back or data_url or "",
            "url_front": url_front or "",
            "updated_at": now_iso,
        }
        self.baseline_cache[shelf_id] = cached_entry

        # Sync to Supabase public.compartments
        update_compartment_baseline_in_supabase(
            shelf_id=shelf_id,
            baseline_image_url=data_url or "",
            ip_address=esp32_ip,
            baseline_image_2_url=url_front,
        )

        print(f"[ESP32ReferenceManager] Updated 30s resting baseline for {shelf_id} at {now_iso}.")
        return cached_entry

    def _compute_absdiff_score(self, img1: Any, img2: Any) -> float:
        """
        Computes cv2.absdiff between two frames after grayscale and GaussianBlur,
        thresholds at 28, and returns the percentage of changed pixels (0..100%).
        """
        if not _OPENCV_AVAILABLE or img1 is None or img2 is None:
            return 0.0

        h1, w1 = img1.shape[:2]
        h2, w2 = img2.shape[:2]
        if (h1, w1) != (h2, w2):
            img2 = cv2.resize(img2, (w1, h1))

        g1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

        g1 = cv2.GaussianBlur(g1, (5, 5), 0)
        g2 = cv2.GaussianBlur(g2, (5, 5), 0)

        diff = cv2.absdiff(g1, g2)
        _, thresh = cv2.threshold(diff, 28, 255, cv2.THRESH_BINARY)
        non_zero = int(np.count_nonzero(thresh))
        total = int(thresh.size)
        pct = (non_zero / float(total)) * 100.0 if total > 0 else 0.0
        return round(float(pct), 2)

    def compare_with_30s_baseline_and_references(
        self,
        mqtt_client_id: str,
        shelf_id: str,
        expected_action: str = "BORROW",
        current_quantity: int = 4,
        esp32_ip: Optional[str] = None,
        esp32_ip2: Optional[str] = None,
        after_data_url: Optional[str] = None,
        baseline_box_count: Optional[int] = None,
        predicted_box_count: Optional[int] = None,
        tm_predicted_box_count: Optional[int] = None,
        scenario_label: Optional[str] = None,
        tm_confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Pure 30s Baseline vs. Teachable Machine Dual-Cam Validation:
        Step A: Fetch post-trigger Dual-Cam frame (CAM 1 Back + CAM 2 Front stitched).
        Step B: Retrieve latest 30-second baseline frame from self.baseline_cache[shelf_id].
        Step C: Compute diff_score = self._compute_absdiff_score(before_bgr, after_bgr).
        Step D: Evaluate Teachable Machine Dual-Cam classification result (0..4 boxes):
               - matched_box_count = int(predicted_box_count)
               - matched_scenario_label = SCENARIO_MAP[matched_box_count]
               - matched_box_count < ref_quantity -> ITEM_REMOVED (delta = matched - ref)
               - matched_box_count > ref_quantity -> ITEM_RETURNED (delta = matched - ref)
               - matched_box_count == ref_quantity -> NO_CHANGE (delta = 0)
        Step E: Update baseline cache if delta != 0 and return comparison details.
        """
        shelf_id = shelf_id.strip()
        mqtt_client_id = mqtt_client_id.strip()
        exp_action = expected_action.upper() if expected_action.upper() in ["BORROW", "RETURN"] else "BORROW"

        # Step A: Fetch Post-Trigger Dual-Cam Frame
        after_bgr = None
        after_url = None
        url_back = None
        url_front = None

        if after_data_url:
            after_bgr = decode_data_url_to_bgr(after_data_url)
            after_url = after_data_url

        if after_bgr is None:
            if esp32_ip and esp32_ip2:
                after_bgr, after_url, url_back, url_front = self.fetch_dual_esp32_frame(
                    esp32_ip1=esp32_ip,
                    esp32_ip2=esp32_ip2,
                    mode="live",
                )
            else:
                target_boxes = max(0, min(4, current_quantity - 1 if exp_action == "BORROW" else current_quantity + 1))
                after_bgr, after_url = self.fetch_esp32_frame(
                    esp32_ip=esp32_ip,
                    mode="live",
                    box_count_fallback=target_boxes,
                )

        # Step B: Retrieve 30s Baseline Frame
        before_bgr = None
        before_url = None
        cached_baseline = self.baseline_cache.get(shelf_id)
        if cached_baseline and cached_baseline.get("frame_bgr") is not None:
            before_bgr = cached_baseline["frame_bgr"]
            before_url = cached_baseline.get("data_url")

        if before_bgr is None:
            before_bgr = synthesize_compartment_frame(f"{shelf_id} (PRE-TRIGGER)", box_count=current_quantity)
            before_url = encode_bgr_to_data_url(before_bgr)

        # Step C: OpenCV Frame Delta between 30s Baseline & After Frame (strictly passive diagnostic metric)
        diff_score = self._compute_absdiff_score(before_bgr, after_bgr)

        # Step D: Teachable Machine Dual-Cam Classification Logic
        # Require predicted_box_count is not None before inserting into public.comparison_logs
        # or updating public.compartments.current_quantity.
        target_tm_count = tm_predicted_box_count if tm_predicted_box_count is not None else predicted_box_count

        matched_box_count: Optional[int] = None
        matched_scenario_label: Optional[str] = scenario_label
        highest_similarity: float = float(tm_confidence if tm_confidence is not None else 95.0)

        state_delta = "NO_CHANGE"
        delta = 0

        if target_tm_count is not None:
            matched_box_count = max(0, min(4, int(target_tm_count)))
            if not matched_scenario_label:
                matched_scenario_label = SCENARIO_MAP.get(matched_box_count, f"Scenario {5 - matched_box_count}: {matched_box_count}/4 Boxes")

            if matched_box_count < current_quantity:
                state_delta = "ITEM_REMOVED"
                delta = matched_box_count - current_quantity
            elif matched_box_count > current_quantity:
                state_delta = "ITEM_RETURNED"
                delta = matched_box_count - current_quantity
            else:
                state_delta = "NO_CHANGE"
                delta = 0

            # Step E: If confirmed change, update rolling 30s baseline cache with after_bgr
            if delta != 0 or shelf_id not in self.baseline_cache:
                now_iso = datetime.now(timezone.utc).isoformat()
                self.baseline_cache[shelf_id] = {
                    "frame_bgr": after_bgr,
                    "data_url": after_url or "",
                    "data_url_2": url_front or "",
                    "url_back": url_back or after_url or "",
                    "url_front": url_front or "",
                    "updated_at": now_iso,
                }
        else:
            # predicted_box_count is None: do NOT mutate current_quantity or insert a comparison_log
            matched_box_count = None
            state_delta = "SKIPPED_NO_TM"
            delta = 0

        return {
            "shelf_id": shelf_id,
            "mqtt_client_id": mqtt_client_id,
            "before_image": before_url,
            "after_image": after_url,
            "diff_score": diff_score,
            "similarity_score": highest_similarity if highest_similarity > 0 else None,
            "matched_box_count": matched_box_count,
            "matched_scenario_label": matched_scenario_label,
            "scenario_label": matched_scenario_label,
            "state_delta": state_delta,
            "delta": delta,
            "tm_validated": target_tm_count is not None,
        }

