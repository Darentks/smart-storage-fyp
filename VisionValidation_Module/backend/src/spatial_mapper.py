"""
Spatial Mapper Module for Smart Storage FYP
Dynamically maps and verifies digital Regions of Interest (ROIs) over physical storage racks.
Complies with FR-02 (Dynamic ROI Mapping), NFR-06 (Maintainability), and NFR-07 (Scalability).
"""

import json
import os
import cv2


class SpatialMapper:
    """
    Manages dynamic software-calibrated Regions of Interest (ROIs) for storage compartments.
    Coordinates are persisted in an external JSON configuration file to eliminate hardcoded values.
    """

    def __init__(self, config_file="config/roi_config.json"):
        self.config_file = self._resolve_config_path(config_file)
        self.rack_id = "RACK_01"
        self.compartments = []
        self.load_roi_config()

    def _resolve_config_path(self, config_file):
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
        # Default fallback to standard backend config path
        fallback = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "roi_config.json"))
        os.makedirs(os.path.dirname(fallback), exist_ok=True)
        return fallback

    def load_roi_config(self):
        """Loads ROI coordinates from the external JSON configuration file."""
        if not os.path.exists(self.config_file):
            print(f"[SpatialMapper] Config not found at {self.config_file}. Creating default schema.")
            self._create_default_config()
            return

        try:
            with open(self.config_file, "r") as f:
                data = json.load(f)
                self.rack_id = data.get("rack_id", "RACK_01")
                self.compartments = data.get("compartments", [])
            print(f"[SpatialMapper] Successfully loaded {len(self.compartments)} compartment ROIs from {self.config_file}")
        except Exception as e:
            print(f"[SpatialMapper] Error reading ROI config: {e}")
            self.compartments = []

    def save_roi_config(self):
        """Saves current ROI configuration to external JSON file."""
        data = {
            "rack_id": self.rack_id,
            "compartments": self.compartments,
        }
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[SpatialMapper] Saved {len(self.compartments)} compartment ROIs to {self.config_file}")

    def _create_default_config(self):
        """Creates a starter configuration if none exists."""
        self.rack_id = "RACK_01"
        self.compartments = [
            {
                "shelf_id": "SHELF_R1_C1",
                "x_min": 100,
                "y_min": 150,
                "x_max": 300,
                "y_max": 350,
                "dwell_threshold_seconds": 1.2,
            },
            {
                "shelf_id": "SHELF_R1_C2",
                "x_min": 320,
                "y_min": 150,
                "x_max": 520,
                "y_max": 350,
                "dwell_threshold_seconds": 1.2,
            },
        ]
        self.save_roi_config()

    def check_intersection(self, point_xy):
        """
        Evaluates whether a point (e.g. wrist coordinate) falls within any calibrated compartment ROI.
        :param point_xy: (x, y) float or int tuple
        :return: list of matching compartment dictionaries
        """
        x, y = point_xy
        matches = []
        for comp in self.compartments:
            if comp["x_min"] <= x <= comp["x_max"] and comp["y_min"] <= y <= comp["y_max"]:
                matches.append(comp)
        return matches

    def add_compartment(self, shelf_id, x_min, y_min, x_max, y_max, dwell_threshold_seconds=1.2):
        """Adds or updates a compartment ROI."""
        # Normalize bounds (ensure x_min < x_max and y_min < y_max)
        x1, x2 = min(x_min, x_max), max(x_min, x_max)
        y1, y2 = min(y_min, y_max), max(y_min, y_max)

        # Remove existing if shelf_id already present
        self.compartments = [c for c in self.compartments if c["shelf_id"] != shelf_id]

        new_comp = {
            "shelf_id": shelf_id,
            "x_min": int(x1),
            "y_min": int(y1),
            "x_max": int(x2),
            "y_max": int(y2),
            "dwell_threshold_seconds": float(dwell_threshold_seconds),
        }
        self.compartments.append(new_comp)
        return new_comp

    def remove_compartment(self, shelf_id):
        """Removes a compartment by shelf_id."""
        initial_len = len(self.compartments)
        self.compartments = [c for c in self.compartments if c["shelf_id"] != shelf_id]
        return len(self.compartments) < initial_len

    def draw_rois(self, frame, active_states=None):
        """
        Draws calibrated compartment boundaries, shelf labels, and live dwell status.
        :param frame: Image frame
        :param active_states: dict of {shelf_id: {'state': str, 'dwell_ratio': float, 'dwell_time': float}}
        :return: Annotated frame
        """
        active_states = active_states or {}
        overlay = frame.copy()

        for comp in self.compartments:
            shelf_id = comp["shelf_id"]
            x1, y1, x2, y2 = comp["x_min"], comp["y_min"], comp["x_max"], comp["y_max"]
            state_info = active_states.get(shelf_id, {})
            state = state_info.get("state", "IDLE")
            dwell_ratio = state_info.get("dwell_ratio", 0.0)
            dwell_time = state_info.get("dwell_time", 0.0)

            # Determine color theme based on intent state
            if state == "INTENT_CONFIRMED":
                box_color = (0, 255, 0)      # Bright Green (Triggered)
                fill_color = (0, 255, 0)
                alpha = 0.25
            elif state == "DWELLING":
                box_color = (0, 165, 255)    # Amber / Orange (Dwelling)
                fill_color = (0, 165, 255)
                alpha = 0.15
            else:
                box_color = (255, 120, 0)    # Blue / Cyan (Idle monitoring)
                fill_color = (255, 120, 0)
                alpha = 0.05

            # Semi-transparent fill
            cv2.rectangle(overlay, (x1, y1), (x2, y2), fill_color, -1)

            # Main ROI border
            cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)

            # Header tag pill
            header_text = f" {shelf_id} "
            (tw, th), _ = cv2.getTextSize(header_text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            cv2.rectangle(frame, (x1, max(0, y1 - th - 10)), (x1 + tw + 6, y1), box_color, -1)
            cv2.putText(
                frame, header_text,
                (x1 + 3, max(th + 2, y1 - 5)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA
            )

            # Dwell progress bar if hand is inside
            if state in ["DWELLING", "INTENT_CONFIRMED"]:
                bar_height = 6
                bar_y1 = y2 - bar_height - 2
                bar_y2 = y2 - 2
                total_bar_w = x2 - x1 - 4
                filled_w = int(total_bar_w * min(1.0, dwell_ratio))

                # Background bar
                cv2.rectangle(frame, (x1 + 2, bar_y1), (x2 - 2, bar_y2), (50, 50, 50), -1)
                # Filled progress
                cv2.rectangle(frame, (x1 + 2, bar_y1), (x1 + 2 + filled_w, bar_y2), box_color, -1)

                # Dwell time label
                time_lbl = f"{dwell_time:.1f}s / {comp.get('dwell_threshold_seconds', 1.2)}s"
                cv2.putText(
                    frame, time_lbl,
                    (x1 + 5, bar_y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, box_color, 1, cv2.LINE_AA
                )

        # Blend semi-transparent overlay
        cv2.addWeighted(overlay, 0.3, frame, 0.7, 0, frame)
        return frame

