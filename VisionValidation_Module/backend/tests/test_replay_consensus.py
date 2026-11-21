"""Isolated replay integration against the real live consensus function."""
import ast
import importlib.util
import math
from pathlib import Path
import threading
import types
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("replay_manager_test", ROOT/"VisionValidation_Module/backend/src/replay_manager.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.now=100.
        self.ns=dict(math=math,time=types.SimpleNamespace(time=lambda:self.now),uuid=uuid,
            CCTV_CAMERA_CONFIG=module.CctvReplayManager.context()["cameras"],CCTV_CONSENSUS_LOCK=threading.RLock(),
            CCTV_ALIGN_STATE={},CCTV_SEEN_TRIGGER_IDS={},LAST_SHELF_TRIGGER_TS={},PENDING_CCTV_TRIGGERS=[],
            CONSENSUS_WINDOW_SEC=4.,CONSENSUS_COOLDOWN_SEC=4.)
        tree=ast.parse((ROOT/"VisionValidation_Module/camera_service.py").read_text(encoding="utf-8"))
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="record_cctv_consensus"],type_ignores=[]),"<consensus>","exec"),self.ns)
        self.manager=module.CctvReplayManager(self.ns["record_cctv_consensus"],clock=lambda:self.now)

    def request(self,action,camera=1,session="test",**kwargs):
        return self.manager.request(dict(session_id=session,camera_id=f"CCTV_0{camera}",client_id=f"client-{camera}",action=action,**kwargs))

    def ready(self):
        for i in (1,2,3): self.request("ready",i,offset=i,duration=30.)

    def vote(self,camera,shelf="SHELF_R1_C2",**kwargs):
        return self.request("vote",camera,shelf_id="ANY_SHELF" if camera==3 else shelf,
            camera_role="SIDE_PRESENCE" if camera==3 else "GRID_6_ROI",expected_action="BORROW",
            oks_confidence=.9,dwell_duration_seconds=1.3,**kwargs)

    def test_barrier_and_common_clock(self):
        self.request("ready",1,offset=1.,duration=30.)
        with self.assertRaises(ValueError):self.request("start")
        self.request("ready",2,offset=2.,duration=30.)
        self.request("ready",3,offset=3.,duration=30.)
        state=self.request("start")
        self.assertEqual(state["start_at"],102.)
        self.assertEqual(self.request("status",2)["start_at"],102.)
        self.assertEqual(self.request("status",3)["clip"]["offset"],3.)
        with self.assertRaises(ValueError):self.vote(1,epoch=state["epoch"])

    def test_test_consensus_is_broadcast_and_cannot_enter_live_queue(self):
        self.ready();state=self.request("start");self.now=103.
        self.assertFalse(self.vote(1,epoch=state["epoch"])["consensus_met"])
        self.assertFalse(self.vote(2,epoch=state["epoch"])["consensus_met"])
        result=self.vote(3,epoch=state["epoch"])
        self.assertTrue(result["consensus_met"]);self.assertTrue(result["test_only"])
        self.assertEqual(result["confirmed_cameras"],["CCTV_01","CCTV_02","CCTV_03"])
        for i in (1,2,3):self.assertEqual(self.request("status",i)["results"][0]["trigger_id"],result["trigger_id"])
        self.assertEqual(self.ns["PENDING_CCTV_TRIGGERS"],[])
        self.assertEqual(self.ns["CCTV_ALIGN_STATE"],{})
        self.assertEqual(self.ns["LAST_SHELF_TRIGGER_TS"],{})

    def test_pause_reset_and_old_epoch_votes(self):
        self.ready();state=self.request("start");self.now=103.
        self.vote(1,epoch=state["epoch"])
        paused=self.request("pause",2);self.assertEqual(paused["status"],"paused")
        self.assertEqual(paused["votes"],{})
        with self.assertRaises(ValueError):self.vote(2,epoch=state["epoch"])
        new=self.request("start",3);self.now+=3.
        with self.assertRaises(ValueError):self.vote(2,epoch=state["epoch"])
        self.assertNotEqual(new["epoch"],state["epoch"])
        reset=self.request("reset");self.assertEqual(reset["results"],[])

    def test_duplicates_disconnect_and_session_isolation(self):
        self.ready()
        with self.assertRaises(ValueError):self.manager.request(dict(action="ready",session_id="test",camera_id="CCTV_01",client_id="other",offset=0,duration=30))
        self.request("start");self.now+=11.
        stale=self.request("status");self.assertEqual(stale["status"],"waiting")
        self.assertEqual(stale["ready_cameras"],["CCTV_01"])
        other=self.request("status",session="other");self.assertEqual(other["ready_cameras"],[])
        self.request("leave");self.assertEqual(self.request("status")["ready_cameras"],[])

    def test_disagreement_and_expired_votes_never_pass(self):
        self.ready();state=self.request("start");self.now+=3.
        self.vote(1,epoch=state["epoch"]);self.vote(2,"SHELF_R1_C5",epoch=state["epoch"])
        self.assertFalse(self.vote(3,epoch=state["epoch"])["consensus_met"])
        self.now+=5.
        self.assertFalse(self.vote(2,epoch=state["epoch"])["consensus_met"])
        self.assertEqual(self.request("status")["results"],[])

    def test_clip_completion_rejects_late_votes(self):
        for i in (1,2,3):self.request("ready",i,offset=0,duration=4.)
        state=self.request("start");self.now=106.
        with self.assertRaises(ValueError):self.vote(1,epoch=state["epoch"])
        self.assertEqual(self.request("status")["status"],"finished")


if __name__=="__main__":unittest.main()
