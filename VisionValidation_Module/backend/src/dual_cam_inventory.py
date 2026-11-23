"""Five-slot stacked inventory contract. No I/O or inference in this module."""
import math
from typing import NamedTuple
from itertools import product

FRONT_STATES = {"Front_000": (0,0,0), "Front_010": (0,1,0),
                "Front_011": (0,1,1), "Front_100": (1,0,0),
                "Front_110": (1,1,0), "Front_111": (1,1,1)}
FRONT_LABELS = set(FRONT_STATES)
BACK_STATES = {"Back_000": (0,0,0), "Back_100": (1,0,0),
               "Back_X01": (None,0,1), "Back_11X": (1,1,None)}
BACK_LABELS = set(BACK_STATES)
VERSION = "stacked-visibility-v4-x01"


def decode_observation(front, back):
    """Partial occupancy is diagnostic evidence, never an exact inventory count."""
    if front not in FRONT_LABELS or back not in BACK_LABELS:
        raise ValueError("Model/version mismatch: unexpected occupancy class")
    f1, m1, m2 = FRONT_STATES[front]
    b1, b2, observed_m2 = BACK_STATES[back]
    if observed_m2 is not None and observed_m2 != m2:
        raise ValueError("M2 conflict; retain quantity and retry")
    if m2 > m1 or (b2 and b1 != 1):
        raise ValueError("Unsupported upper box")
    front_count = f1 + m1 + m2
    back_count = None if b1 is None else b1 + b2
    total = None if back_count is None else front_count + back_count
    return dict(slots=(f1,m1,m2,b1,b2), front_count=front_count, back_count=back_count,
                total=total, count_range=(front_count,front_count+1) if total is None else (total,total),
                observed_m2=observed_m2, m2_check="SKIPPED_OCCLUDED" if b2 else "PASS")


def decode_pair(front, back) -> tuple[int, int, int, int, int]:
    observation = decode_observation(front, back)
    if observation["total"] is None:
        raise ValueError("B1 unknown (Back_X01); exact inventory update held, retry a trusted observation")
    slots = observation["slots"]
    if not isinstance(slots, tuple) or len(slots) != 5:
        raise ValueError("Invalid occupancy slots")
    f1, m1, m2, b1, b2 = slots
    if b1 is None:
        raise ValueError("B1 unknown (Back_X01); exact inventory update held, retry a trusted observation")
    return (int(f1), int(m1), int(m2), int(b1), int(b2))


class CaptureTiming(NamedTuple):
    capture_id: str
    completed_at: float
    started_at: float


def validate_confidences(sample):
    """Shared raw per-role 80% floor; never round or renormalize here."""
    for key in ("front_confidence", "back_confidence"):
        confidence = sample.get(key)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 80 <= confidence <= 100:
            raise ValueError("Both cameras require at least 80% confidence")


def validate_capture(sample, now, captures, shelf_id, validation_token, seen,
                     previous=None, not_before=None, run_id=None) -> CaptureTiming:
    """Validate one server receipt; callers retain sequence/occupancy authority."""
    capture = sample.get("capture")
    if not isinstance(capture, dict):
        raise ValueError("Missing live capture timing")
    capture_id = capture.get("id")
    if not isinstance(capture_id, str):
        raise ValueError("Invalid capture ID")
    record = captures.get(capture_id)
    if not record or capture_id in seen or record.get("shelf_id") != shelf_id:
        raise ValueError("Unknown, reused or mismatched capture")
    if validation_token is not None and record.get("validation_token") != validation_token:
        raise ValueError("Capture belongs to another validation event")
    if run_id is not None and record.get("run_id") != run_id:
        raise ValueError("Capture belongs to another runtime generation")
    completed = record["completed_at"]
    if not_before is not None and completed - record["duration_ms"] / 1000 < not_before:
        raise ValueError("Capture predates the established inventory event")
    if not record["complete"] or not 0 <= now - completed <= 60 or not 0 <= record["duration_ms"] <= 2000:
        raise ValueError("Missing, stale or mismatched camera frames")
    if previous is not None and completed - previous < 1.5:
        raise ValueError("Confirmation captures must be separated in time")
    return CaptureTiming(capture_id, completed, completed-record["duration_ms"]/1000)


def validate_evidence(evidence, count, now, captures, shelf_id, validation_token=None, not_before=None) -> tuple[tuple[int, int, int, int, int], set[str]]:
    """Require three distinct recent server-issued captures with identical occupancy."""
    if not isinstance(evidence, dict) or evidence.get("version") != VERSION:
        raise ValueError("Live visibility-aware evidence is required; reload the updated dashboard")
    samples = evidence.get("samples")
    if not isinstance(samples, list) or not 3 <= len(samples) <= 5:
        raise ValueError("Three consistent live captures are required")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 5:
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
        validate_confidences(sample)
        timing = validate_capture(sample, now, captures, shelf_id, validation_token,
                                  seen, previous, not_before)
        completed, capture_id = timing.completed_at, timing.capture_id
        previous = completed
        seen.add(capture_id)
    # Explicitly narrow the loop accumulator before returning it to callers.
    if slots is None:
        raise ValueError("No validated occupancy samples")
    if sum(slots) != count:
        raise ValueError("Count does not match verified positions")
    return slots, seen


def validate_transition(previous, current, quantity, action):
    """RAM snapshot bootstraps without a write; only monotone authorized actions commit."""
    if previous is None:
        if sum(current) != quantity:
            raise ValueError(f"Prior occupancy unavailable: cameras observed {sum(current)} boxes but stored quantity is {quantity}. First complete live Full Validation with a fully observed shelf matching {quantity}; Preview does not initialize occupancy.")
        return
    if sum(previous) != quantity:
        raise ValueError("Stored quantity changed outside this snapshot; restart and recount before validation")
    changes = [new - old for old, new in zip(previous, current)]
    if not any(changes):
        return
    if action == "BORROW" and all(change <= 0 for change in changes):
        return
    if action == "RETURN" and all(change >= 0 for change in changes):
        return
    raise ValueError("Occupancy change conflicts with expected action; relocation/recheck requires review")


class ActionProof(NamedTuple):
    accepted: bool
    code: str | None
    possible_deltas: tuple[int, ...]


def feasible_completions(slots):
    """Enumerate stack-supported states, without assuming hidden-slot continuity."""
    if len(slots)!=5 or any(bit is not None and (type(bit) is not int or bit not in (0,1)) for bit in slots):
        raise ValueError('Five 0/1/null slots required')
    states=tuple(state for state in product(*[(0,1) if bit is None else (bit,) for bit in slots])
                 if state[2]<=state[1] and state[4]<=state[3])
    if not states:raise ValueError('No physically feasible completion')
    return states


def prove_unit_action(before, after, action) -> ActionProof:
    """Accept iff every independent completion proves one same-direction slot move.

    This is runtime policy; persistent compatibility retains validate_transition.
    No prior quantity, repeated X01 or direction candidate resolves a hidden bit.
    """
    if action not in ('BORROW','RETURN'):raise ValueError('BORROW or RETURN required')
    expected=-1 if action=='BORROW' else 1
    deltas=set();all_proven=True
    before_states=feasible_completions(before);after_states=feasible_completions(after)
    for previous in before_states:
        for current in after_states:
            changes=tuple(new-old for old,new in zip(previous,current));net=sum(changes)
            deltas.add(net)
            all_proven=all_proven and net==expected and sum(abs(change) for change in changes)==1
    if all_proven:return ActionProof(True,None,tuple(sorted(deltas)))
    unknown=any(bit is None for bit in (*before,*after))
    visible=any(a is not None and b is not None and a!=b for a,b in zip(before,after))
    code=('VISIBLE_CHANGE_NET_UNPROVEN' if visible else 'UNKNOWN_B1') if unknown else 'OCCUPANCY_CHANGED'
    return ActionProof(False,code,tuple(sorted(deltas)))
