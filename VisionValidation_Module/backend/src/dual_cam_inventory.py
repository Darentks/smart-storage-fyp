"""Five-slot stacked inventory contract. No I/O or inference in this module."""
import math

FRONT_LABELS = {f"Front_{s}" for s in ("000", "010", "011", "100", "110", "111")}
BACK_LABELS = {f"Back_{s}" for s in ("000", "001", "100", "101", "110", "111")}
VERSION = "stacked-shared-m2-v1"


def decode_pair(front, back) -> tuple[int, int, int, int, int]:
    if front not in FRONT_LABELS or back not in BACK_LABELS:
        raise ValueError("Unexpected occupancy class")
    f1, m1, m2 = map(int, front[6:])
    b1, b2, back_m2 = map(int, back[5:])
    if m2 != back_m2:
        raise ValueError("MIDDLE_CONFLICT: cameras disagree on M2")
    if m2 > m1 or b2 > b1:
        raise ValueError("Unsupported upper box")
    return (f1, m1, m2, b1, b2)


def validate_evidence(evidence, count, now, captures, shelf_id) -> tuple[tuple[int, int, int, int, int], set[str]]:
    """Require three distinct recent server-issued captures with identical occupancy."""
    if not isinstance(evidence, dict) or evidence.get("version") != VERSION:
        raise ValueError("Live shared-M2 evidence is required; reload the updated dashboard")
    samples = evidence.get("samples")
    if not isinstance(samples, list) or not 3 <= len(samples) <= 5:
        raise ValueError("Three consistent live captures are required")
    if type(count) is not int or not 0 <= count <= 5:
        raise ValueError("Inventory count must be an integer from 0 to 5")
    previous = None
    seen: set[str] = set()
    slots = None
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("Invalid sample")
        current = decode_pair(sample.get("front_class"), sample.get("back_class"))
        if slots is not None and current != slots:
            raise ValueError("Occupancy changed between captures")
        slots = current
        for key in ("front_confidence", "back_confidence"):
            confidence = sample.get(key)
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 85 <= confidence <= 100:
                raise ValueError("Both cameras require at least 85% confidence")
        capture = sample.get("capture")
        if not isinstance(capture, dict):
            raise ValueError("Missing live capture timing")
        capture_id = capture.get("id")
        if not isinstance(capture_id, str):
            raise ValueError("Invalid capture ID")
        record = captures.get(capture_id)
        if not record or capture_id in seen or record.get("shelf_id") != shelf_id:
            raise ValueError("Unknown, reused or mismatched capture")
        completed = record["completed_at"]
        if not record["complete"] or not 0 <= now - completed <= 60 or not 0 <= record["duration_ms"] <= 2000:
            raise ValueError("Missing, stale or mismatched camera frames")
        if previous is not None and completed - previous < 1.5:
            raise ValueError("Confirmation captures must be separated in time")
        previous = completed
        seen.add(capture_id)
    # Explicitly narrow the loop accumulator before returning it to callers.
    if slots is None:
        raise ValueError("No validated occupancy samples")
    if sum(slots) != count:
        raise ValueError("Count does not match verified positions")
    return slots, seen
