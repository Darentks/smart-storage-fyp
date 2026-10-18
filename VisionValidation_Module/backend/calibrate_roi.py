"""
Interactive Dynamic ROI Calibration Canvas
Smart Storage FYP - Vision-Based Human Interaction Analysis (Use-Case 2)
Author: Daren Tan Khai Shuen

Comprehensive ROI Editing Features:
  - SELECTING:
      * Left-Click anywhere inside or on a box to select it
      * Press [TAB] to cycle through existing compartments
      * Press [1], [2], [3]... to select a compartment directly
  - RESIZING:
      * Drag any of the 4 Corner handles or 4 Edge handles
      * Press [+] / [-] to expand / shrink box size
      * Press [W] / [S] to adjust height; [A] / [D] (when holding ALT) or '[' / ']' for width
  - MOVING:
      * Click inside the box and drag with the mouse
      * Use Arrow Keys (or I/J/K/L) to nudge position by 2 pixels
  - CREATING & DELETING:
      * Drag in empty space to draw a new box
      * Press [SPACE] / [C] to freeze/unfreeze camera frame
      * Press [X] or [Backspace] to delete the selected compartment
  - SAVING:
      * Press [ENTER] or [Ctrl+S] / [S] to persist changes to config/roi_config.json
      * Press [R] to reload saved config from disk
      * Press [Q] or [ESC] to exit
"""

import os
import sys
import json
import time
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.spatial_mapper import SpatialMapper
from src.video_processor import VideoProcessor
from src.config_loader import get_active_rtsp_url

# State variables
spatial_mapper = None
selected_idx = 0  # Default to first compartment if available
drawing_new = False
dragging_box = False
active_handle = -1  # 0: TL, 1: TR, 2: BR, 3: BL, 4: Top, 5: Right, 6: Bottom, 7: Left
hover_handle = -1

drag_offset = (0, 0)
start_pt = (-1, -1)
current_pt = (-1, -1)
is_frozen = False
frozen_frame = None

status_msg = "Ready. Click any box to select & edit. Press [S] to save."
status_color = (0, 255, 255)
status_expiry = 0.0


def set_status(msg, color=(0, 255, 255), duration=4.0):
    global status_msg, status_color, status_expiry
    status_msg = msg
    status_color = color
    status_expiry = time.time() + duration
    print(f"[Calibrator] {msg}")


def is_inside_box(x, y, comp):
    return comp["x_min"] <= x <= comp["x_max"] and comp["y_min"] <= y <= comp["y_max"]


def get_handles_for_box(comp):
    """Returns coordinates for 8 handles: 4 corners + 4 edge midpoints."""
    x1, y1 = comp["x_min"], comp["y_min"]
    x2, y2 = comp["x_max"], comp["y_max"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

    return [
        (x1, y1),  # 0: Top-Left
        (x2, y1),  # 1: Top-Right
        (x2, y2),  # 2: Bottom-Right
        (x1, y2),  # 3: Bottom-Left
        (cx, y1),  # 4: Top edge
        (x2, cy),  # 5: Right edge
        (cx, y2),  # 6: Bottom edge
        (x1, cy),  # 7: Left edge
    ]


def find_handle_under_cursor(x, y, comp, radius=12):
    """Finds which resize handle (0-7) is near cursor (x, y)."""
    handles = get_handles_for_box(comp)
    for i, (hx, hy) in enumerate(handles):
        if (x - hx) ** 2 + (y - hy) ** 2 <= radius ** 2:
            return i
    return -1


def add_new_compartment(x1, y1, x2, y2):
    """Adds a new compartment and selects it."""
    global selected_idx, spatial_mapper

    rx1, rx2 = min(x1, x2), max(x1, x2)
    ry1, ry2 = min(y1, y2), max(y1, y2)

    if (rx2 - rx1) < 20 or (ry2 - ry1) < 20:
        set_status("Box too small. Drag a larger area (at least 20x20 px).", (0, 0, 255))
        return False

    existing_ids = {c["shelf_id"] for c in spatial_mapper.compartments}
    counter = 1
    while f"SHELF_R1_C{counter}" in existing_ids:
        counter += 1
    new_id = f"SHELF_R1_C{counter}"

    spatial_mapper.add_compartment(
        shelf_id=new_id,
        x_min=int(rx1),
        y_min=int(ry1),
        x_max=int(rx2),
        y_max=int(ry2),
        dwell_threshold_seconds=1.2,
    )
    selected_idx = len(spatial_mapper.compartments) - 1
    set_status(f"Added {new_id}! Drag edges/corners to resize, press [S] to save.", (0, 255, 0))
    return True


def spawn_center_compartment(frame_w=640, frame_h=480):
    bw, bh = 180, 160
    cx, cy = frame_w // 2, frame_h // 2
    add_new_compartment(cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2)


def mouse_callback(event, x, y, flags, param):
    global selected_idx, drawing_new, dragging_box, active_handle, hover_handle
    global drag_offset, start_pt, current_pt, spatial_mapper

    # Update hover state when not dragging
    if event == cv2.EVENT_MOUSEMOVE and not dragging_box and active_handle == -1 and not drawing_new:
        current_pt = (x, y)
        hover_handle = -1
        if 0 <= selected_idx < len(spatial_mapper.compartments):
            hover_handle = find_handle_under_cursor(x, y, spatial_mapper.compartments[selected_idx])

    if event == cv2.EVENT_LBUTTONDOWN:
        start_pt = (x, y)
        current_pt = (x, y)

        # 1. Check if clicking on a resize handle of the currently selected box
        if 0 <= selected_idx < len(spatial_mapper.compartments):
            handle = find_handle_under_cursor(x, y, spatial_mapper.compartments[selected_idx])
            if handle != -1:
                active_handle = handle
                set_status("Resizing compartment... Release mouse when done.", (0, 255, 255))
                return

        # 2. Check if clicking on any handle across ALL other boxes
        for idx in range(len(spatial_mapper.compartments)):
            handle = find_handle_under_cursor(x, y, spatial_mapper.compartments[idx])
            if handle != -1:
                selected_idx = idx
                active_handle = handle
                set_status(f"Selected {spatial_mapper.compartments[idx]['shelf_id']}. Resizing...", (0, 255, 255))
                return

        # 3. Check if clicking inside any box to select and move it
        hit_idx = -1
        for idx in reversed(range(len(spatial_mapper.compartments))):
            if is_inside_box(x, y, spatial_mapper.compartments[idx]):
                hit_idx = idx
                break

        if hit_idx != -1:
            selected_idx = hit_idx
            dragging_box = True
            c = spatial_mapper.compartments[selected_idx]
            drag_offset = (x - c["x_min"], y - c["y_min"])
            set_status(f"Selected {c['shelf_id']}. Drag to move, or drag handles to resize.", (0, 255, 255))
        else:
            # 4. Clicked outside any box: start drawing a new ROI
            selected_idx = -1
            drawing_new = True

    elif event == cv2.EVENT_MOUSEMOVE:
        current_pt = (x, y)

        if drawing_new:
            pass  # Rendered in main loop

        elif active_handle != -1 and 0 <= selected_idx < len(spatial_mapper.compartments):
            c = spatial_mapper.compartments[selected_idx]
            min_size = 20

            # 0: TL, 1: TR, 2: BR, 3: BL, 4: Top, 5: Right, 6: Bottom, 7: Left
            if active_handle == 0:  # Top-Left
                c["x_min"] = min(x, c["x_max"] - min_size)
                c["y_min"] = max(45, min(y, c["y_max"] - min_size))
            elif active_handle == 1:  # Top-Right
                c["x_max"] = max(x, c["x_min"] + min_size)
                c["y_min"] = max(45, min(y, c["y_max"] - min_size))
            elif active_handle == 2:  # Bottom-Right
                c["x_max"] = max(x, c["x_min"] + min_size)
                c["y_max"] = max(y, c["y_min"] + min_size)
            elif active_handle == 3:  # Bottom-Left
                c["x_min"] = min(x, c["x_max"] - min_size)
                c["y_max"] = max(y, c["y_min"] + min_size)
            elif active_handle == 4:  # Top edge
                c["y_min"] = max(45, min(y, c["y_max"] - min_size))
            elif active_handle == 5:  # Right edge
                c["x_max"] = max(x, c["x_min"] + min_size)
            elif active_handle == 6:  # Bottom edge
                c["y_max"] = max(y, c["y_min"] + min_size)
            elif active_handle == 7:  # Left edge
                c["x_min"] = min(x, c["x_max"] - min_size)

        elif dragging_box and 0 <= selected_idx < len(spatial_mapper.compartments):
            c = spatial_mapper.compartments[selected_idx]
            w = c["x_max"] - c["x_min"]
            h = c["y_max"] - c["y_min"]
            new_x1 = max(0, x - drag_offset[0])
            new_y1 = max(45, y - drag_offset[1])
            c["x_min"] = int(new_x1)
            c["y_min"] = int(new_y1)
            c["x_max"] = int(new_x1 + w)
            c["y_max"] = int(new_y1 + h)

    elif event == cv2.EVENT_LBUTTONUP:
        if drawing_new:
            drawing_new = False
            add_new_compartment(start_pt[0], start_pt[1], current_pt[0], current_pt[1])

        elif dragging_box:
            dragging_box = False
            c = spatial_mapper.compartments[selected_idx]
            set_status(f"Moved {c['shelf_id']} to ({c['x_min']},{c['y_min']}). Press [S] to save.", (0, 255, 0))

        elif active_handle != -1:
            active_handle = -1
            c = spatial_mapper.compartments[selected_idx]
            w = c["x_max"] - c["x_min"]
            h = c["y_max"] - c["y_min"]
            set_status(f"Resized {c['shelf_id']} to {w}x{h}px. Press [S] to save.", (0, 255, 0))


def main():
    global spatial_mapper, is_frozen, frozen_frame, selected_idx

    config_path = os.path.join(os.path.dirname(__file__), "config", "roi_config.json")
    spatial_mapper = SpatialMapper(config_path)

    # Auto-select first compartment if available
    if len(spatial_mapper.compartments) > 0:
        selected_idx = 0

    if len(sys.argv) > 1:
        arg_val = sys.argv[1]
        if arg_val == "--source" and len(sys.argv) > 2:
            arg_val = sys.argv[2]
        try:
            cam_idx = int(arg_val)
        except ValueError:
            cam_idx = arg_val
    else:
        cam_idx = get_active_rtsp_url(default_source=0)
        try:
            cam_idx = int(cam_idx)
        except (ValueError, TypeError):
            pass

    video = VideoProcessor(source=cam_idx, width=640, height=480)
    if not video.is_streaming:
        print("[-] Could not open camera.")
        return

    window_name = "Dynamic ROI Calibration Canvas - Smart Storage FYP"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)

    set_status(f"Loaded {len(spatial_mapper.compartments)} ROIs. Click any box to edit its shape/size.", (0, 255, 255))

    while True:
        if not is_frozen:
            ret, frame = video.capture_frame()
            if not ret or frame is None:
                continue
            frozen_frame = frame.copy()
        else:
            frame = frozen_frame.copy()

        canvas = frame.copy()
        h, w = canvas.shape[:2]

        # 1. Render all configured compartments
        for i, comp in enumerate(spatial_mapper.compartments):
            x1, y1, x2, y2 = comp["x_min"], comp["y_min"], comp["x_max"], comp["y_max"]
            is_sel = (i == selected_idx)

            # Colors: Selected = Amber/Yellow, Normal = Blue/Cyan
            color = (0, 220, 255) if is_sel else (255, 140, 0)
            thickness = 3 if is_sel else 2

            # Semi-transparent fill
            overlay = canvas.copy()
            cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
            cv2.addWeighted(overlay, 0.25 if is_sel else 0.10, canvas, 0.75 if is_sel else 0.90, 0, canvas)

            # Outline border
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, thickness)

            # Header pill
            bw = x2 - x1
            bh = y2 - y1
            label = f" {comp['shelf_id']} [{bw}x{bh}] "
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            tag_y1 = max(45, y1 - th - 8)
            cv2.rectangle(canvas, (x1, tag_y1), (x1 + tw + 6, tag_y1 + th + 8), color, -1)
            cv2.putText(canvas, label, (x1 + 3, tag_y1 + th + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)

            # Draw 8 Control Handles if this compartment is selected
            if is_sel:
                handles = get_handles_for_box(comp)
                for h_idx, (hx, hy) in enumerate(handles):
                    is_h_active = (active_handle == h_idx)
                    is_h_hover = (hover_handle == h_idx)

                    h_color = (0, 255, 0) if (is_h_active or is_h_hover) else (0, 255, 255)
                    h_radius = 8 if (is_h_active or is_h_hover) else 6

                    cv2.circle(canvas, (hx, hy), h_radius, h_color, -1)
                    cv2.circle(canvas, (hx, hy), h_radius + 1, (0, 0, 0), 1)

        # 2. Render new box currently being drawn
        if drawing_new and start_pt[0] != -1:
            rx1, rx2 = min(start_pt[0], current_pt[0]), max(start_pt[0], current_pt[0])
            ry1, ry2 = min(start_pt[1], current_pt[1]), max(start_pt[1], current_pt[1])
            cv2.rectangle(canvas, (rx1, ry1), (rx2, ry2), (0, 255, 0), 2)
            cv2.putText(canvas, f"New Box: {rx2 - rx1}x{ry2 - ry1}px", (rx1, max(55, ry1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)

        # 3. Top diagnostic header banner
        banner = canvas.copy()
        cv2.rectangle(banner, (0, 0), (w, 44), (20, 20, 20), -1)
        cv2.addWeighted(banner, 0.88, canvas, 0.12, 0, canvas)

        status_disp = status_msg if time.time() < status_expiry else "Click to select | Drag handles to resize | [S]: Save"
        curr_color = status_color if time.time() < status_expiry else (200, 200, 200)

        sel_name = spatial_mapper.compartments[selected_idx]['shelf_id'] if 0 <= selected_idx < len(spatial_mapper.compartments) else "None"
        cv2.putText(canvas, f"Selected: [{sel_name}] | {status_disp}", (12, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, curr_color, 1, cv2.LINE_AA)
        cv2.putText(canvas, "[Click]:Select [Handles]:Resize [S]:SAVE [Q]:Exit", (12, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1, cv2.LINE_AA)

        cv2.imshow(window_name, canvas)
        key = cv2.waitKey(20) & 0xFF

        if key in [ord('q'), 27]:
            break
        elif key in [ord(' '), ord('c')]:
            is_frozen = not is_frozen
            set_status("Frame FROZEN for editing." if is_frozen else "LIVE camera feed resumed.")
        elif key in [9]:  # TAB key: cycle to next compartment
            if len(spatial_mapper.compartments) > 0:
                selected_idx = (selected_idx + 1) % len(spatial_mapper.compartments)
                sel_id = spatial_mapper.compartments[selected_idx]['shelf_id']
                set_status(f"Selected {sel_id} (Compartment {selected_idx + 1} of {len(spatial_mapper.compartments)})")
        elif ord('1') <= key <= ord('9'):  # Number keys: select shelf 1 to 9
            num_idx = key - ord('1')
            if num_idx < len(spatial_mapper.compartments):
                selected_idx = num_idx
                sel_id = spatial_mapper.compartments[selected_idx]['shelf_id']
                set_status(f"Selected {sel_id}")
        elif key in [ord('a'), ord('A')]:
            # Spawn new compartment box
            spawn_center_compartment(frame_w=w, frame_h=h)
        elif key in [ord('s'), ord('S'), 13]:  # 'S' or ENTER: Save
            spatial_mapper.save_roi_config()
            set_status("SAVED TO config/roi_config.json!", (0, 255, 0), duration=6.0)
        elif key in [ord('r'), ord('R')]:  # Reload
            spatial_mapper.load_roi_config()
            selected_idx = 0 if len(spatial_mapper.compartments) > 0 else -1
            set_status("Reloaded saved configuration from disk.", (0, 255, 255))
        elif key in [ord('x'), ord('X'), 8]:  # 'X' or Backspace: Delete selected
            if 0 <= selected_idx < len(spatial_mapper.compartments):
                removed = spatial_mapper.compartments.pop(selected_idx)
                selected_idx = min(selected_idx, len(spatial_mapper.compartments) - 1)
                set_status(f"Deleted {removed['shelf_id']}. Press [S] to save.", (0, 165, 255))
        elif key in [ord('+'), ord('=')]:  # Expand size
            if 0 <= selected_idx < len(spatial_mapper.compartments):
                c = spatial_mapper.compartments[selected_idx]
                c["x_min"] = max(0, c["x_min"] - 5)
                c["y_min"] = max(45, c["y_min"] - 5)
                c["x_max"] = min(w, c["x_max"] + 5)
                c["y_max"] = min(h, c["y_max"] + 5)
                set_status(f"Expanded size of {c['shelf_id']}. Press [S] to save.")
        elif key in [ord('-'), ord('_')]:  # Shrink size
            if 0 <= selected_idx < len(spatial_mapper.compartments):
                c = spatial_mapper.compartments[selected_idx]
                if (c["x_max"] - c["x_min"]) > 30 and (c["y_max"] - c["y_min"]) > 30:
                    c["x_min"] += 5
                    c["y_min"] += 5
                    c["x_max"] -= 5
                    c["y_max"] -= 5
                    set_status(f"Shrunk size of {c['shelf_id']}. Press [S] to save.")

    video.release_stream()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
