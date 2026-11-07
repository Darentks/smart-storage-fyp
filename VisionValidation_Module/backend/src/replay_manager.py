"""Bridge-clock synchronized, isolated consensus rehearsal. No MQTT or inventory access."""
import math
import re
import threading
import time
import uuid


class CctvReplayManager:
    def __init__(self, vote_handler, clock=time.time, window_seconds=4.0):
        self.vote_handler = vote_handler
        self.clock = clock
        self.window_seconds = window_seconds
        self.sessions = {}
        self.lock = threading.RLock()

    @staticmethod
    def context():
        return {"cameras": {f"CCTV_0{i}": {"is_active": True, "camera_role": "SIDE_PRESENCE" if i == 3 else "GRID_6_ROI"} for i in (1,2,3)},
                "votes": {}, "seen_ids": {}, "last_triggers": {}, "events": []}

    def request(self, body):
        if not isinstance(body, dict):
            raise ValueError("Replay request must be an object")
        session_id = body.get("session_id", "")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise ValueError("Replay session must use 1-64 letters, digits, underscores or hyphens")
        camera_id, client_id = body.get("camera_id"), body.get("client_id")
        if camera_id not in {"CCTV_01", "CCTV_02", "CCTV_03"} or not isinstance(client_id,str) or not 1 <= len(client_id) <= 100:
            raise ValueError("Replay needs a valid camera ID and browser client ID")
        action = body.get("action", "status")
        if action not in {"ready", "status", "start", "pause", "reset", "leave", "vote"}:
            raise ValueError("Unknown replay action")
        with self.lock:
            now = self.clock()
            for key, old in list(self.sessions.items()):
                if now-old["updated"] > 3600:
                    del self.sessions[key]
            if session_id not in self.sessions:
                if len(self.sessions) >= 100:
                    raise ValueError("Too many replay sessions; use an existing session")
                self.sessions[session_id] = {"session_token":uuid.uuid4().hex, "status":"waiting", "epoch":0, "start_at":None, "clients":{}, "context":self.context(), "updated":now, "elapsed":0.0}
            session = self.sessions[session_id]
            session["updated"] = now
            clients = session["clients"]
            mine = clients.get(camera_id)
            owned = mine is not None and mine["client_id"] == client_id
            if owned:
                mine["last_seen"] = now
            if action == "ready":
                if mine and not owned and now-mine["last_seen"] <= 10:
                    raise ValueError(f"{camera_id} is already loaded in another window; stop that tracker first")
                offset, duration = float(body.get("offset",0)), float(body.get("duration",0))
                if not math.isfinite(offset) or not math.isfinite(duration) or not 0 <= offset < duration:
                    raise ValueError("Clip start offset must be inside a finite playable video")
                session.update(status="waiting",epoch=session["epoch"]+1,start_at=None,elapsed=0.0,context=self.context())
                clients[camera_id] = {"client_id":client_id,"last_seen":now,"offset":offset,"duration":duration}
                mine = clients[camera_id]
                owned = True
            elif action != "status" and not owned:
                raise ValueError("Load and ready this camera's clip before controlling replay")
            for cid, vote in list(session["context"]["votes"].items()):
                if now-vote["ts"] > self.window_seconds:
                    del session["context"]["votes"][cid]
            stale = [cid for cid,c in clients.items() if now-c["last_seen"] > 10]
            if session["status"] == "playing" and (stale or len(clients) != 3):
                session.update(status="waiting",epoch=session["epoch"]+1,start_at=None,elapsed=0.0)
                session["context"]["votes"].clear()
            if action == "start":
                if set(clients) != {"CCTV_01","CCTV_02","CCTV_03"} or stale:
                    raise ValueError("All three camera clips must be ready and connected")
                session.update(status="playing",epoch=session["epoch"]+1,start_at=now+2.0,elapsed=0.0,context=self.context())
            elif action in {"pause","reset","leave"}:
                elapsed = max(0.0,now-session["start_at"]) if session["start_at"] else session["elapsed"]
                session.update(status="paused" if action=="pause" else "waiting",epoch=session["epoch"]+1,start_at=None,elapsed=elapsed if action=="pause" else 0.0)
                session["context"]["votes"].clear()
                if action in {"reset","leave"}:
                    session["context"] = self.context()
                if action == "leave":
                    del clients[camera_id]
                    owned = False
            elif action == "vote":
                if session["status"] != "playing" or body.get("epoch") != session["epoch"] or now < session["start_at"]:
                    raise ValueError("Replay is not running or this vote belongs to an old run")
                if now-session["start_at"] >= min(c["duration"]-c["offset"] for c in clients.values()):
                    session.update(status="finished",elapsed=min(c["duration"]-c["offset"] for c in clients.values()),start_at=None)
                    session["context"]["votes"].clear()
                    raise ValueError("Replay has finished; restart all three clips for another test")
                camera_role = session["context"]["cameras"][camera_id]["camera_role"]
                if body.get("camera_role") != camera_role:
                    raise ValueError("Replay requires CCTV_01/02 grid roles and CCTV_03 side presence")
                # Invoke the same consensus gate, but with isolated test-only buffers.
                result = self.vote_handler(body.get("shelf_id"), camera_id, body.get("expected_action"),
                    float(body.get("oks_confidence",0)), camera_role=camera_role,
                    trigger_id=body.get("trigger_id"), dwell_duration_seconds=float(body.get("dwell_duration_seconds",0)),
                    consensus_context=session["context"])
                return {**result,"test_only":True}
            if session["status"] == "playing":
                limit = min(c["duration"]-c["offset"] for c in clients.values())
                if now-session["start_at"] >= limit:
                    session.update(status="finished",elapsed=limit,start_at=None)
                    session["context"]["votes"].clear()
            return {"session_id":session_id,"session_token":session["session_token"],"server_time":now,"status":session["status"],"epoch":session["epoch"],
                    "start_at":session["start_at"],"elapsed":session["elapsed"],"test_only":True,"owned":owned,
                    "ready_cameras":[cid for cid,c in clients.items() if now-c["last_seen"] <= 10],
                    "clip":{k:mine[k] for k in ("offset","duration")} if owned else None,
                    "votes":dict(session["context"]["votes"]),"results":list(session["context"]["events"])}
