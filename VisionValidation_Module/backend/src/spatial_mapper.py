"""
Spatial Mapper Module for Smart Storage FYP
Dynamically maps and verifies digital Regions of Interest (ROIs) over physical storage racks,
including CRUD bindings between each Shelf Compartment and its assigned ESP32-CAM node.
Complies with FR-02 (Dynamic ROI Mapping), NFR-06 (Maintainability), and NFR-07 (Scalability).
"""

import json
import os
from typing import Any, Dict, List, Optional
import cv2


class SpatialMapper:
    """
    Manages dynamic software-calibrated Regions of Interest (ROIs) and ESP32 node mappings
    for storage compartments. Coordinates and ESP32 device bindings are persisted in
    config/roi_config.json and synchronized with Supabase ('esp32_cameras').
    """

    def __init__(self, config_file: str = "config/roi_config.json"):
        self.config_file = self._resolve_config_path(config_file)
        self.rack_id = "RACK_01"
        self.compartments: List[Dict[str, Any]] = []
        self.load_roi_config()

    def _resolve_config_path(self, config_file: str) -> str:
        """Resolves config path across common project root and backend working directories."""
        candidates = [
            config_file,
            os.path.join(os.path.dirname(__file__), "..", config_file),
            os.path.join("backend", config_file),
            os.path.join(os.path.dirname(__file__), "..", "config", os.path.basename(config_file)),
        ]
        for c in candidates:
            if os.path.exists(c):
                return os.path.abspath(c)
        fallback = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "roi_config.json"))
        os.makedirs(os.path.dirname(fallback), exist_ok=True)
        return fallback

    def load_roi_config(self) -> List[Dict[str, Any]]:
        """Loads ROI coordinates and ESP32 mappings from the external JSON configuration file."""
        if not os.path.exists(self.config_file):
            print(f"[SpatialMapper] Config not found at {self.config_file}. Creating default schema.")
            self._create_default_config()
            return self.compartments

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.rack_id = data.get("rack_id", "RACK_01")
                raw_comps = data.get("compartments", [])
                # Ensure each compartment has standard ESP32 mapping keys
                self.compartments = []
                for idx, comp in enumerate(raw_comps, start=1):
                    shelf_id = comp.get("shelf_id", f"SHELF_R1_C{idx}")
                    default_esp_id = shelf_id.replace("SHELF_", "ESP32_")
                    self.compartments.append({
                        "shelf_id": shelf_id,
                        "mqtt_client_id": comp.get("mqtt_client_id") or default_esp_id,
                        "camera_name": comp.get("camera_name") or f"ESP32 Node ({shelf_id})",
                        "container_id": comp.get("container_id"),
                        "x_min": int(comp.get("x_min", 100)),
                        "y_min": int(comp.get("y_min", 150)),
                        "x_max": int(comp.get("x_max", 300)),
                        "y_max": int(comp.get("y_max", 350)),
                        "dwell_threshold_seconds": float(comp.get("dwell_threshold_seconds", 1.2)),
                    })
            print(f"[SpatialMapper] Loaded {len(self.compartments)} compartment ROIs from {self.config_file}")
        except Exception as e:
            print(f"[SpatialMapper] Error reading ROI config: {e}")
            self.compartments = []
        return self.compartments

    def save_roi_config(self) -> None:
        """Saves current ROI configuration and ESP32 bindings to external JSON file."""
        data = {
            "rack_id": self.rack_id,
            "compartments": self.compartments,
        }
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[SpatialMapper] Saved {len(self.compartments)} compartment ROIs to {self.config_file}")

    def _create_default_config(self) -> None:
        """Creates a starter configuration if none exists."""
        self.rack_id = "RACK_01"
        self.compartments = [
            {
                "shelf_id": f"SHELF_R1_C{i}",
                "mqtt_client_id": f"ESP32_R1_C{i}",
                "camera_name": f"ESP32 Node R1-C{i}",
                "container_id": None,
                "x_min": 60 + ((i - 1) % 3) * 185,
                "y_min": 70 + ((i - 1) // 3) * 180,
                "x_max": 220 + ((i - 1) % 3) * 185,
                "y_max": 220 + ((i - 1) // 3) * 180,
                "dwell_threshold_seconds": 1.2,
            }
            for i in range(1, 7)
        ]
        self.save_roi_config()

    def check_intersection(self, point_xy) -> List[Dict[str, Any]]:
        """
        Evaluates whether a point (e.g. wrist coordinate) falls within any calibrated compartment ROI.
        """
        x, y = point_xy
        matches = []
        for comp in self.compartments:
            if comp["x_min"] <= x <= comp["x_max"] and comp["y_min"] <= y <= comp["y_max"]:
                matches.append(comp)
        return matches

    def get_compartment(self, shelf_id: str) -> Optional[Dict[str, Any]]:
        """READ: Retrieves a single compartment dictionary by shelf_id."""
        for comp in self.compartments:
            if comp["shelf_id"] == shelf_id:
                return comp
        return None

    def add_compartment(
        self,
        shelf_id: str,
        x_min: int,
        y_min: int,
        x_max: int,
        y_max: int,
        dwell_threshold_seconds: float = 1.2,
        mqtt_client_id: Optional[str] = None,
        camera_name: Optional[str] = None,
        container_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        CREATE / UPDATE: Adds a new compartment ROI or updates an existing compartment by shelf_id.
        Preserves existing ESP32 node bindings if mqtt_client_id is not explicitly overridden.
        """
        x1, x2 = min(int(x_min), int(x_max)), max(int(x_min), int(x_max))
        y1, y2 = min(int(y_min), int(y_max)), max(int(y_min), int(y_max))

        existing = self.get_compartment(shelf_id)
        default_esp_id = shelf_id.replace("SHELF_", "ESP32_")

        resolved_mqtt_id = (
            mqtt_client_id
            or (existing.get("mqtt_client_id") if existing else None)
            or default_esp_id
        )
        resolved_cam_name = (
            camera_name
            or (existing.get("camera_name") if existing else None)
            or f"ESP32 Node ({shelf_id})"
        )
        resolved_container_id = (
            container_id
            if container_id is not None
            else (existing.get("container_id") if existing else None)
        )

        new_comp = {
            "shelf_id": shelf_id,
            "mqtt_client_id": resolved_mqtt_id,
            "camera_name": resolved_cam_name,
            "container_id": resolved_container_id,
            "x_min": x1,
            "y_min": y1,
            "x_max": x2,
            "y_max": y2,
            "dwell_threshold_seconds": float(dwell_threshold_seconds),
        }

        updated = False
        for i, c in enumerate(self.compartments):
            if c["shelf_id"] == shelf_id:
                self.compartments[i] = new_comp
                updated = True
                break

        if not updated:
            self.compartments.append(new_comp)

        return new_comp

    def remove_compartment(self, shelf_id: str) -> bool:
        """DELETE: Removes a compartment by shelf_id."""
        initial_len = len(self.compartments)
        self.compartments = [c for c in self.compartments if c["shelf_id"] != shelf_id]
        return len(self.compartments) < initial_len

    def draw_rois(self, frame, active_states=None):
        """
        Draws calibrated compartment boundaries, shelf labels, mapped ESP32 IDs, and live dwell status.
        """
        active_states = active_states or {}
        overlay = frame.copy()

        for comp in self.compartments:
            shelf_id = comp["shelf_id"]
            esp_id = comp.get("mqtt_client_id", "")
            x1, y1, x2, y2 = comp["x_min"], comp["y_min"], comp["x_max"], comp["y_max"]
            state_info = active_states.get(shelf_id, {})
            state = state_info.get("state", "IDLE")
            dwell_ratio = state_info.get("dwell_ratio", 0.0)
            dwell_time = state_info.get("dwell_time", 0.0)

            if state == "INTENT_CONFIRMED":
                box_color = (0, 255, 0)
                fill_color = (0, 255, 0)
            elif state == "DWELLING":
                box_color = (0, 165, 255)
                fill_color = (0, 165, 255)
            else:
                box_color = (255, 120, 0)
                fill_color = (255, 120, 0)

            cv2.rectangle(overlay, (x1, y1), (x2, y2), fill_color, -1)
            cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)

            header_text = f" {shelf_id} [{esp_id}] " if esp_id else f" {shelf_id} "
            (tw, th), _ = cv2.getTextSize(header_text, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            cv2.rectangle(frame, (x1, max(0, y1 - th - 10)), (x1 + tw + 6, y1), box_color, -1)
            cv2.putText(
                frame, header_text,
                (x1 + 3, max(th + 2, y1 - 5)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 1, cv2.LINE_AA
            )

            if state in ["DWELLING", "INTENT_CONFIRMED"]:
                bar_height = 6
                bar_y1 = y2 - bar_height - 2
                bar_y2 = y2 - 2
                total_bar_w = x2 - x1 - 4
                filled_w = int(total_bar_w * min(1.0, dwell_ratio))

                cv2.rectangle(frame, (x1 + 2, bar_y1), (x2 - 2, bar_y2), (50, 50, 50), -1)
                cv2.rectangle(frame, (x1 + 2, bar_y1), (x1 + 2 + filled_w, bar_y2), box_color, -1)

                time_lbl = f"{dwell_time:.1f}s / {comp.get('dwell_threshold_seconds', 1.2)}s"
                cv2.putText(
                    frame, time_lbl,
                    (x1 + 5, bar_y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, box_color, 1, cv2.LINE_AA
                )

        cv2.addWeighted(overlay, 0.3, frame, 0.7, 0, frame)
        return frame
