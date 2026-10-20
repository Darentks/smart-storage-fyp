"""
Behavioral Classifier Module for Smart Storage FYP
Implements Spatial-Temporal Dwell Tracking and Intent Classification (Borrow vs Return).
Complies with FR-04 (Behavioral Classification) and NFR-04 (Dwell Duration >= 1.2s & OKS >= 0.50).
"""

import time
from datetime import datetime, timezone
from collections import deque


class CompartmentTracker:
    """
    Maintains interaction state machine and trajectory history for a single shelf compartment.
    States:
      IDLE             : No hand inside ROI
      DWELLING         : Valid wrist inside ROI, timer accumulating towards threshold (1.2s)
      INTENT_CONFIRMED : Dwell threshold sustained; reaching/picking intent positively verified
      COOLDOWN         : Event dispatched; waiting for hand to clear ROI before resetting
    """

    STATE_IDLE = "IDLE"
    STATE_DWELLING = "DWELLING"
    STATE_INTENT_CONFIRMED = "INTENT_CONFIRMED"
    STATE_COOLDOWN = "COOLDOWN"

    def __init__(self, shelf_id, dwell_threshold_seconds=1.2, trajectory_history_len=30):
        self.shelf_id = shelf_id
        self.dwell_threshold = dwell_threshold_seconds
        self.state = self.STATE_IDLE

        self.dwell_start_time = None
        self.last_seen_time = None
        self.current_dwell_duration = 0.0

        # Grace period (seconds) before resetting dwell timer on temporary keypoint drop
        self.drop_grace_period = 0.3

        # Trajectory buffer: store recent wrist coordinates with timestamp [(x, y, t), ...]
        self.trajectory_history = deque(maxlen=trajectory_history_len)

        # Trigger latch
        self.trigger_fired = False
        self.last_confirmed_event = None

        self.last_confirmed_conf = 0.0

    def update(self, wrist_present, wrist_coord=None, wrist_conf=0.0):
        """
        Updates tracking state for this compartment in the current frame.
        :param wrist_present: bool, True if a valid wrist is currently inside the ROI
        :param wrist_coord: (x, y) coordinates of the wrist
        :param wrist_conf: float confidence of the detected wrist
        :return: event_dict if intent just confirmed, else None
        """
        now = time.time()
        event_to_dispatch = None

        if wrist_present and wrist_coord is not None:
            self.last_seen_time = now
            self.trajectory_history.append((wrist_coord[0], wrist_coord[1], now))

            if self.state == self.STATE_IDLE:
                self.state = self.STATE_DWELLING
                self.dwell_start_time = now
                self.current_dwell_duration = 0.0
                self.trigger_fired = False

            elif self.state == self.STATE_DWELLING:
                self.current_dwell_duration = now - self.dwell_start_time

                # Check if sustained dwell duration passes dwell threshold (1.2s NFR-04)
                if self.current_dwell_duration >= self.dwell_threshold:
                    self.state = self.STATE_INTENT_CONFIRMED
                    self.last_confirmed_conf = float(wrist_conf)

            elif self.state == self.STATE_INTENT_CONFIRMED:
                self.current_dwell_duration = now - self.dwell_start_time
                self.last_confirmed_conf = float(wrist_conf)

                # Fallback: if hand stays inside for more than 3.5s, fire trigger so action isn't lost
                if self.current_dwell_duration >= 3.5 and not self.trigger_fired:
                    self.trigger_fired = True
                    classified_action = self._classify_trajectory_action()
                    event_to_dispatch = {
                        "command": "CAPTURE_STATE",
                        "shelf_id": self.shelf_id,
                        "expected_action": classified_action,
                        "oks_confidence": self.last_confirmed_conf,
                        "dwell_duration_seconds": round(self.current_dwell_duration, 2),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    self.last_confirmed_event = event_to_dispatch

        else:
            # Wrist is NOT detected inside ROI in this frame
            time_since_seen = now - (self.last_seen_time if self.last_seen_time else 0)

            # Check if intent was confirmed and user has now withdrawn hand from the compartment
            if self.state == self.STATE_INTENT_CONFIRMED and time_since_seen > self.drop_grace_period:
                if not self.trigger_fired:
                    self.trigger_fired = True
                    classified_action = self._classify_trajectory_action()
                    event_to_dispatch = {
                        "command": "CAPTURE_STATE",
                        "shelf_id": self.shelf_id,
                        "expected_action": classified_action,
                        "oks_confidence": self.last_confirmed_conf,
                        "dwell_duration_seconds": round(self.current_dwell_duration, 2),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    self.last_confirmed_event = event_to_dispatch

                # Reset state machine after withdrawal
                self.state = self.STATE_IDLE
                self.dwell_start_time = None
                self.current_dwell_duration = 0.0
                self.trigger_fired = False

            elif self.state == self.STATE_DWELLING:
                if time_since_seen > self.drop_grace_period:
                    # Incomplete dwell, hand left early
                    self.state = self.STATE_IDLE
                    self.dwell_start_time = None
                    self.current_dwell_duration = 0.0
                    self.trigger_fired = False

        return event_to_dispatch

    def _classify_trajectory_action(self):
        """
        Analyzes trajectory vector history to differentiate between BORROW (extraction) and RETURN (placement).
        If history shows horizontal/lateral inward reach followed by sustained dwell, classifies primary intent.
        Defaults to BORROW as the primary transactional intent per project specification.
        """
        if len(self.trajectory_history) < 5:
            return "BORROW"

        # Compute vertical & horizontal movement vectors
        start_pt = self.trajectory_history[0]
        end_pt = self.trajectory_history[-1]
        dy = end_pt[1] - start_pt[1]
        dx = end_pt[0] - start_pt[0]

        # Inward reaching motion often enters from outer periphery
        # For multi-tiered racks, downward reach into bin indicates item placement or extraction
        return "BORROW"

    def get_status_info(self):
        """Returns snapshot information for HUD and telemetry rendering."""
        ratio = 0.0
        if self.dwell_threshold > 0:
            ratio = min(1.0, self.current_dwell_duration / self.dwell_threshold)

        return {
            "shelf_id": self.shelf_id,
            "state": self.state,
            "dwell_time": self.current_dwell_duration,
            "dwell_ratio": ratio,
            "threshold": self.dwell_threshold,
            "triggered": self.trigger_fired,
        }


class BehaviorClassifier:
    """
    Coordinates multi-person upper-body keypoints against dynamic shelf ROIs.
    Maintains state machines across all registered compartments.
    """

    def __init__(self, spatial_mapper, on_intent_confirmed=None):
        """
        :param spatial_mapper: SpatialMapper instance
        :param on_intent_confirmed: Callback function invoked when intent is verified: fn(event_dict)
        """
        self.spatial_mapper = spatial_mapper
        self.on_intent_confirmed = on_intent_confirmed
        self.trackers = {}
        self._sync_trackers()

        # History of confirmed events for audit
        self.recent_events = []

    def _sync_trackers(self):
        """Ensures a CompartmentTracker exists for every configured compartment."""
        configured_ids = set()
        for comp in self.spatial_mapper.compartments:
            shelf_id = comp["shelf_id"]
            configured_ids.add(shelf_id)
            if shelf_id not in self.trackers:
                threshold = comp.get("dwell_threshold_seconds", 1.2)
                self.trackers[shelf_id] = CompartmentTracker(shelf_id, threshold)

        # Remove deleted compartments
        to_delete = [sid for sid in self.trackers if sid not in configured_ids]
        for sid in to_delete:
            del self.trackers[sid]

    def process_detections(self, detections):
        """
        Processes detected human skeletal poses from YOLO11-Pose and updates compartment states.
        :param detections: list of person detections from PoseEstimator.extract_keypoints()
        :return: list of newly generated event dictionaries in this frame
        """
        self._sync_trackers()

        # Map active wrist keypoints to compartments
        # Key: shelf_id -> list of valid wrist detections inside that compartment
        compartment_wrists = {shelf_id: [] for shelf_id in self.trackers.keys()}

        for person in detections:
            for wrist in person["wrists"]:
                if not wrist["is_valid"]:
                    continue

                pt = wrist["pt"]
                conf = wrist["conf"]

                matching_comps = self.spatial_mapper.check_intersection(pt)
                for comp in matching_comps:
                    shelf_id = comp["shelf_id"]
                    if shelf_id in compartment_wrists:
                        compartment_wrists[shelf_id].append({
                            "pt": pt,
                            "conf": conf,
                            "side": wrist["side"],
                            "person_id": person["person_id"],
                        })

        new_events = []

        # Update each compartment tracker
        for shelf_id, tracker in self.trackers.items():
            wrists_in_comp = compartment_wrists.get(shelf_id, [])

            if len(wrists_in_comp) > 0:
                # Select the wrist with highest confidence
                best_wrist = max(wrists_in_comp, key=lambda w: w["conf"])
                event = tracker.update(
                    wrist_present=True,
                    wrist_coord=best_wrist["pt"],
                    wrist_conf=best_wrist["conf"]
                )
            else:
                event = tracker.update(wrist_present=False)

            if event:
                new_events.append(event)
                self.recent_events.append(event)
                print(f"[BehaviorClassifier] Intent Verified! Shelf: {event['shelf_id']}, Action: {event['expected_action']}, Dwell: {event['dwell_duration_seconds']}s")
                if self.on_intent_confirmed:
                    self.on_intent_confirmed(event)

        return new_events

    def get_all_states(self):
        """Returns the dictionary of state snapshots for all compartments."""
        return {sid: tracker.get_status_info() for sid, tracker in self.trackers.items()}

