"""
Standalone ESP32-CAM Reference Capture & Sub-ROI Slot Calibrator
Smart Storage FYP
Author: Daren Tan Khai Shuen

Features:
1. Connects to ESP32-CAM via HTTP (/capture) to fetch live preview & snapshots.
2. Captures reference states:
   - '4': ref_4_boxes.jpg (Full baseline)
   - '3': ref_3_boxes.jpg (3 boxes)
   - '2': ref_2_boxes.jpg (2 boxes)
   - '1': ref_1_box.jpg   (1 box)
   - '0': ref_0_boxes.jpg (Empty baseline)
3. Interactive Sub-ROI Slot definition:
   - Click-and-drag 4 bounding boxes over:
     'front_top', 'front_bottom', 'back_top', 'back_bottom'
4. Press 'S':
   - Computes slot-wise reference statistics (mean BGR, HSV, luminance, and edge density)
     from ref_4_boxes.jpg and ref_0_boxes.jpg.
   - Saves coordinates & baseline signatures to reference_esp32/{shelf_id}/sub_rois.json.
5. Press 'Q' or ESC: Exit.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import cv2
import numpy as np

SLOT_KEYS = ["front_top", "front_bottom", "back_top", "back_bottom"]
SLOT_LABELS = {
    "front_top": "1. Front-Top Slot",
    "front_bottom": "2. Front-Bottom Slot",
    "back_top": "3. Back-Top Slot",
    "back_bottom": "4. Back-Bottom Slot",
}
SLOT_COLORS = {
    "front_top": (0, 255, 255),     # Yellow
    "front_bottom": (0, 255, 0),    # Green
    "back_top": (255, 165, 0),      # Cyan/Blue-Orange
    "back_bottom": (255, 0, 255),   # Magenta
}

# Mouse interaction globals
current_slot_idx = 0
drawing = False
start_pt = (-1, -1)
current_pt = (-1, -1)
sub_rois = {}  # { "front_top": [x1, y1, x2, y2], ... }
status_message = "Ready. Press 4,3,2,1,0 to capture reference states."
status_color = (0, 255, 255)
status_expiry = 0.0


def set_status(msg, color=(0, 255, 255), duration=4.0):
    global status_message, status_color, status_expiry
    status_message = msg
    status_color = color
    status_expiry = time.time() + duration
    print(f"[ESP32 Calibrator] {msg}")


def fetch_esp32_frame(ip_address, timeout=2.5):
    """Fetches a JPEG frame from ESP32-CAM http://{ip}/capture or synthesizes fallback."""
    url = f"http://{ip_address}/capture"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            arr = np.asarray(bytearray(data), dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None:
                return frame, True
    except Exception as exc:
        pass

    # Offline simulation canvas if hardware ESP32 is not reachable
    synth = np.full((480, 640, 3), (30, 35, 45), dtype=np.uint8)
    cv2.putText(
        synth,
        f"ESP32-CAM Offline ({ip_address})",
        (50, 220),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (0, 0, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        synth,
        "Simulated frame active for Sub-ROI mapping test",
        (50, 260),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (180, 180, 180),
        1,
        cv2.LINE_AA,
    )
    return synth, False


def mouse_callback(event, x, y, flags, param):
    global drawing, start_pt, current_pt, current_slot_idx, sub_rois

    if event == cv2.EVENT_LBUTTONDOWN:
        drawing = True
        start_pt = (x, y)
        current_pt = (x, y)

    elif event == cv2.EVENT_MOUSEMOVE:
        if drawing:
            current_pt = (x, y)

    elif event == cv2.EVENT_LBUTTONUP:
        if drawing:
            drawing = False
            x1, x2 = min(start_pt[0], x), max(start_pt[0], x)
            y1, y2 = min(start_pt[1], y), max(start_pt[1], y)

            if (x2 - x1) >= 15 and (y2 - y1) >= 15:
                slot_key = SLOT_KEYS[current_slot_idx]
                sub_rois[slot_key] = [int(x1), int(y1), int(x2), int(y2)]
                set_status(
                    f"Set {SLOT_LABELS[slot_key]} -> [{x1},{y1},{x2},{y2}].",
                    (0, 255, 0),
                )
                if current_slot_idx < len(SLOT_KEYS) - 1:
                    current_slot_idx += 1
            else:
                set_status("Drawn box is too small. Try again.", (0, 0, 255))


def extract_slot_stats(frame_bgr, bbox):
    """Calculates mean color, HSV stats, mean luminance, and edge density for a slot."""
    x1, y1, x2, y2 = bbox
    h, w = frame_bgr.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    crop = frame_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return {
            "mean_bgr": [0.0, 0.0, 0.0],
            "mean_hsv": [0.0, 0.0, 0.0],
            "luminance": 0.0,
            "edge_density": 0.0,
        }

    mean_bgr = [round(float(v), 2) for v in cv2.mean(crop)[:3]]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mean_hsv = [round(float(v), 2) for v in cv2.mean(hsv)[:3]]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edge_density = round(float(np.mean(edges > 0)), 4)
    lum = round(float(np.mean(gray)), 2)

    return {
        "mean_bgr": mean_bgr,
        "mean_hsv": mean_hsv,
        "luminance": lum,
        "edge_density": edge_density,
    }


def save_sub_rois_config(shelf_dir, shelf_id, ip_address):
    """Saves sub-ROI coordinates and reference metrics to sub_rois.json."""
    if len(sub_rois) < 4:
        missing = [k for k in SLOT_KEYS if k not in sub_rois]
        set_status(f"Cannot save: Missing slots {missing}.", (0, 0, 255), duration=5.0)
        return False

    ref_4_path = os.path.join(shelf_dir, "ref_4_boxes.jpg")
    ref_0_path = os.path.join(shelf_dir, "ref_0_boxes.jpg")

    ref_4_img = cv2.imread(ref_4_path) if os.path.exists(ref_4_path) else None
    ref_0_img = cv2.imread(ref_0_path) if os.path.exists(ref_0_path) else None

    slots_data = {}
    for key in SLOT_KEYS:
        coords = sub_rois[key]
        full_stats = extract_slot_stats(ref_4_img, coords) if ref_4_img is not None else None
        empty_stats = extract_slot_stats(ref_0_img, coords) if ref_0_img is not None else None

        slots_data[key] = {
            "bbox_xyxy": coords,
            "width": coords[2] - coords[0],
            "height": coords[3] - coords[1],
            "baseline_full_4": full_stats,
            "baseline_empty_0": empty_stats,
        }

    config_payload = {
        "shelf_id": shelf_id,
        "esp32_ip": ip_address,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slots": slots_data,
    }

    out_file = os.path.join(shelf_dir, "sub_rois.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(config_payload, f, indent=2)

    set_status(f"Saved Sub-ROIs & Baselines -> {out_file}", (0, 255, 0), duration=6.0)
    return True


def main():
    global current_slot_idx, sub_rois

    parser = argparse.ArgumentParser(description="Capture ESP32-CAM box state reference photos and Sub-ROIs.")
    parser.add_argument("--ip", default="192.168.1.120", help="ESP32-CAM IP address (e.g. 192.168.1.120)")
    parser.add_argument("--shelf", default="SHELF_R1_C1", help="Shelf Compartment ID (default: SHELF_R1_C1)")
    args = parser.parse_args()

    shelf_id = args.shelf.strip()
    esp_ip = args.ip.strip()

    # Create target directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    shelf_dir = os.path.join(base_dir, "reference_esp32", shelf_id)
    os.makedirs(shelf_dir, exist_ok=True)

    # Load existing sub_rois.json if present
    sub_rois_path = os.path.join(shelf_dir, "sub_rois.json")
    if os.path.isfile(sub_rois_path):
        try:
            with open(sub_rois_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                loaded_slots = data.get("slots", {})
                for k in SLOT_KEYS:
                    if k in loaded_slots and "bbox_xyxy" in loaded_slots[k]:
                        sub_rois[k] = loaded_slots[k]["bbox_xyxy"]
            print(f"[ESP32 Calibrator] Loaded existing Sub-ROIs for {shelf_id}: {list(sub_rois.keys())}")
        except Exception as e:
            print(f"[ESP32 Calibrator] Warning loading existing sub_rois.json: {e}")

    window_name = f"ESP32-CAM Reference & Sub-ROI Calibrator - {shelf_id}"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)

    print("=================================================================")
    print(f"  ESP32-CAM REFERENCE CAPTURE & SUB-ROI CALIBRATOR ({shelf_id})")
    print(f"  ESP32 Address: http://{esp_ip}/capture")
    print(f"  Storage Path:  {shelf_dir}")
    print("  Controls:")
    print("    [4, 3, 2, 1, 0]: Capture snapshot for corresponding box state")
    print("    [TAB]          : Cycle target slot to draw (1 to 4)")
    print("    [C]            : Clear currently selected slot bounding box")
    print("    [S]            : Save Sub-ROI boxes & baseline signatures to sub_rois.json")
    print("    [Q] / [ESC]    : Exit")
    print("=================================================================")

    last_frame = None
    last_fetch_time = 0.0

    while True:
        now = time.time()
        # Fetch fresh frame from ESP32 every 250ms
        if now - last_fetch_time > 0.25 or last_frame is None:
            last_frame, is_live = fetch_esp32_frame(esp_ip)
            last_fetch_time = now

        canvas = last_frame.copy()
        h, w = canvas.shape[:2]

        # 1. Draw existing Sub-ROI slots
        for idx, key in enumerate(SLOT_KEYS):
            if key in sub_rois:
                bx1, by1, bx2, by2 = sub_rois[key]
                color = SLOT_COLORS[key]
                is_selected = idx == current_slot_idx
                thickness = 3 if is_selected else 2

                # Semi-transparent overlay
                overlay = canvas.copy()
                cv2.rectangle(overlay, (bx1, by1), (bx2, by2), color, -1)
                cv2.addWeighted(overlay, 0.22 if is_selected else 0.10, canvas, 0.78 if is_selected else 0.90, 0, canvas)
                cv2.rectangle(canvas, (bx1, by1), (bx2, by2), color, thickness)

                # Pill label
                label = f"{key} [{bx2-bx1}x{by2-by1}]"
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
                cv2.rectangle(canvas, (bx1, max(0, by1 - th - 6)), (bx1 + tw + 6, max(0, by1)), color, -1)
                cv2.putText(canvas, label, (bx1 + 3, max(12, by1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)

        # 2. Draw current dragging box
        if drawing and start_pt[0] != -1:
            rx1, rx2 = min(start_pt[0], current_pt[0]), max(start_pt[0], current_pt[0])
            ry1, ry2 = min(start_pt[1], current_pt[1]), max(start_pt[1], current_pt[1])
            curr_col = SLOT_COLORS[SLOT_KEYS[current_slot_idx]]
            cv2.rectangle(canvas, (rx1, ry1), (rx2, ry2), curr_col, 2)
            cv2.putText(
                canvas,
                f"Drawing {SLOT_KEYS[current_slot_idx]}: {rx2-rx1}x{ry2-ry1}px",
                (rx1, max(20, ry1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                curr_col,
                1,
                cv2.LINE_AA,
            )

        # 3. Top Banner UI
        banner = canvas.copy()
        cv2.rectangle(banner, (0, 0), (w, 54), (20, 24, 30), -1)
        cv2.addWeighted(banner, 0.88, canvas, 0.12, 0, canvas)

        active_slot_name = SLOT_LABELS[SLOT_KEYS[current_slot_idx]]
        curr_msg = status_message if time.time() < status_expiry else f"Draw box for: {active_slot_name} | [S]: Save"
        msg_col = status_color if time.time() < status_expiry else (220, 220, 220)

        cv2.putText(canvas, f"Target: [{active_slot_name}] | {curr_msg}", (12, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, msg_col, 1, cv2.LINE_AA)
        cv2.putText(canvas, "[4,3,2,1,0]:Snap Ref  [Drag]:Set Slot  [TAB]:Next Slot  [C]:Clear Slot  [S]:Save  [Q]:Exit", (12, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (160, 160, 160), 1, cv2.LINE_AA)

        # Check existing reference photos presence
        ref_status = []
        for n, fname in [(4, "ref_4_boxes.jpg"), (3, "ref_3_boxes.jpg"), (2, "ref_2_boxes.jpg"), (1, "ref_1_box.jpg"), (0, "ref_0_boxes.jpg")]:
            exists = os.path.exists(os.path.join(shelf_dir, fname))
            ref_status.append(f"{n}B:{'OK' if exists else '-'}")

        cv2.putText(canvas, f"Refs: {' '.join(ref_status)}", (w - 220, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 255, 200), 1, cv2.LINE_AA)

        cv2.imshow(window_name, canvas)
        key = cv2.waitKey(30) & 0xFF

        if key in [ord("q"), 27]:
            break

        # Capture states 0..4
        elif key in [ord("4"), ord("3"), ord("2"), ord("1"), ord("0")]:
            count = chr(key)
            fname = "ref_1_box.jpg" if count == "1" else f"ref_{count}_boxes.jpg"
            save_path = os.path.join(shelf_dir, fname)

            # Capture snapshot from ESP32 directly
            snap, success = fetch_esp32_frame(esp_ip, timeout=4.0)
            if snap is not None:
                cv2.imwrite(save_path, snap)
                set_status(f"Saved reference for {count} boxes -> {fname}!", (0, 255, 0), duration=4.5)
            else:
                set_status(f"Failed to grab frame from http://{esp_ip}/capture", (0, 0, 255))

        elif key == 9:  # TAB key: cycle target slot
            current_slot_idx = (current_slot_idx + 1) % len(SLOT_KEYS)
            set_status(f"Selected target: {SLOT_LABELS[SLOT_KEYS[current_slot_idx]]}")

        elif key in [ord("c"), ord("C")]:  # Clear selected slot
            target_key = SLOT_KEYS[current_slot_idx]
            if target_key in sub_rois:
                del sub_rois[target_key]
                set_status(f"Cleared box for {target_key}.", (0, 165, 255))

        elif key in [ord("s"), ord("S")]:  # Save Sub-ROIs
            save_sub_rois_config(shelf_dir, shelf_id, esp_ip)

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()

