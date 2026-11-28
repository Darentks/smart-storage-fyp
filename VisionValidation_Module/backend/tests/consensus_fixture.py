"""Shared fixture: the real live record_cctv_consensus from camera_service.py in an isolated namespace.

Previously provided by test_replay_consensus.ReplayTests; kept after recorded-clip replay was removed.
Not a test module itself (no test_ prefix), so unittest discovery does not collect it.
"""
import ast
import math
from pathlib import Path
import threading
import types
import uuid

from src.shelves import SHELF_SET  # same import camera_service.py uses; previously leaked in from another test

ROOT = Path(__file__).resolve().parents[3]
# Three enabled cameras: CCTV_01/02 shelf (grid) view, CCTV_03 side presence.
THREE_CAMERAS = {f"CCTV_0{i}": {"is_active": True, "camera_role": "SIDE_PRESENCE" if i == 3 else "GRID_6_ROI"} for i in (1, 2, 3)}


class ConsensusFixture:
    def setUp(self):
        self.now = 100.
        self.ns = dict(math=math, time=types.SimpleNamespace(time=lambda: self.now), uuid=uuid,
                       CCTV_CAMERA_CONFIG={k: dict(v) for k, v in THREE_CAMERAS.items()}, CCTV_CONSENSUS_LOCK=threading.RLock(),
                       CCTV_ALIGN_STATE={}, CCTV_SEEN_TRIGGER_IDS={}, LAST_SHELF_TRIGGER_TS={}, PENDING_CCTV_TRIGGERS=[],
                       CONSENSUS_WINDOW_SEC=4., CONSENSUS_COOLDOWN_SEC=4., SHELF_SET=SHELF_SET)
        tree = ast.parse((ROOT / "VisionValidation_Module/camera_service.py").read_text(encoding="utf-8"))
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "record_cctv_consensus"],
                                type_ignores=[]), "<consensus>", "exec"), self.ns)
