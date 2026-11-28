"""Run: python -m unittest discover -s VisionValidation_Module/backend/tests -v"""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import numpy as np
import runpy
SHELF_SET=runpy.run_path(str(Path(__file__).resolve().parents[1]/'src'/'shelves.py'))['SHELF_SET']  # for AST-isolated bridge functions

# Load only the tested backend modules, without starting application managers.
package = types.ModuleType("polygon_test_backend")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "src")]
sys.modules[package.__name__] = package
geometry = importlib.import_module(package.__name__ + ".roi_geometry")
mapper_module = importlib.import_module(package.__name__ + ".spatial_mapper")
behavior = importlib.import_module(package.__name__ + ".behavior_classifier")


class PolygonRoiTests(unittest.TestCase):
    def mapper(self, compartments):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "rois.json"
        path.write_text(json.dumps({"camera_resolution":[640,480],"rack_id":"RACK_01","compartments":compartments}))
        return mapper_module.SpatialMapper(str(path))

    def test_perspective_polygon_and_boundary(self):
        roi = {"vertices":[[0,0],[100,20],[80,100],[20,80]]}
        self.assertTrue(geometry.contains_point(roi,(50,50)))
        self.assertTrue(geometry.contains_point(roi,(0,0)))
        self.assertFalse(geometry.contains_point(roi,(99,99)))
        self.assertFalse(geometry.contains_point(roi,(float("nan"),1)))
        for points in [[[0,0],[1,1],[0,1],[1,0]],[[0,0]]*4,[[0,0],[1,0],[.5,.1],[0,1]]]:
            with self.assertRaises(ValueError): geometry.validate_vertices(points)

    def test_legacy_and_polygon_round_trip(self):
        mapper = self.mapper([{"shelf_id":"A","x_min":0,"y_min":0,"x_max":100,"y_max":100},
                              {"shelf_id":"B","vertices":[[200,0],[300,20],[280,100],[220,80]]}])
        self.assertEqual([c["shelf_id"] for c in mapper.check_intersection((50,50))],["A"])
        self.assertEqual(mapper.check_intersection((299,99)),[])
        mapper.save_roi_config();mapper.load_roi_config()
        self.assertEqual(len(mapper.get_compartment("B")["vertices"]),4)
        self.assertEqual(json.loads(Path(mapper.config_file).read_text())["camera_resolution"],[640,480])
        frame = np.zeros((480,640,3),dtype=np.uint8)
        self.assertTrue(mapper.draw_rois(frame).any())

    def test_dictionary_schema_and_normalized_coordinates(self):
        mapper = self.mapper({"A":{"vertices":[[0,0],[.5,0],[.5,.5],[0,.5]],"coordinate_space":"normalized"}})
        self.assertTrue(mapper.check_intersection((300,200)))
        self.assertFalse(mapper.check_intersection((400,300)))
        self.assertEqual(mapper.get_compartment("A")["x_max"],320)

    def test_ambiguity_cancels_confirmed_dwell_without_withdrawal_vote(self):
        mapper = self.mapper([{"shelf_id":sid,"x_min":lo,"y_min":0,"x_max":hi,"y_max":100}
                              for sid,lo,hi in [("A",0,100),("B",80,180),("C",300,400)]])
        events=[]
        classifier = behavior.BehaviorClassifier(mapper,events.append)
        def person(x): return {"person_id":1,"wrists":[{"pt":(x,50),"is_valid":True,"conf":.9,"side":"Left"}]}
        with patch.object(behavior.time,"time",return_value=100): classifier.process_detections([person(50)])
        with patch.object(behavior.time,"time",return_value=101.3): classifier.process_detections([person(50)])
        self.assertEqual(classifier.trackers["A"].state,"INTENT_CONFIRMED")
        with patch.object(behavior.time,"time",return_value=102): classifier.process_detections([person(90),person(350)])
        self.assertTrue(classifier.get_all_states()["A"]["ambiguous"])
        self.assertEqual(classifier.trackers["A"].state,"IDLE")
        self.assertEqual(classifier.trackers["C"].state,"DWELLING")
        with patch.object(behavior.time,"time",return_value=103): classifier.process_detections([])
        self.assertEqual(events,[])

    def test_polygon_survives_binding_update(self):
        mapper=self.mapper([{"shelf_id":"A","vertices":[[0,0],[100,20],[80,100],[20,80]]}])
        mapper.add_compartment("A",0,0,100,100,mqtt_client_id="test")
        self.assertEqual(mapper.get_compartment("A")["vertices"][1],[100,20])

    def test_desktop_calibrator_preserves_polygon_overlays_and_isolates_save(self):
        import ast
        source = (Path(__file__).resolve().parents[1] / "calibrate_roi.py").read_text(encoding="utf-8")
        selected = {"set_polygon_vertices", "transform_polygon_bounds", "get_handles_for_box", "is_inside_box", "load_calibration_mapper", "save_calibration_rois"}
        tree = ast.parse(source)
        namespace = dict(np=np, os=__import__("os"), json=json, SpatialMapper=mapper_module.SpatialMapper,
                         roi_vertices=geometry.roi_vertices, contains_point=geometry.contains_point, validate_vertices=geometry.validate_vertices)
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in selected],type_ignores=[]),"<calibrator>","exec"),namespace)
        master = self.mapper([{"shelf_id":sid,"x_min":0,"y_min":0,"x_max":100,"y_max":100} for sid in ["A","B"]])
        camera = Path(master.config_file).with_name("roi_config_CCTV_02.json")
        camera.write_text(json.dumps({"compartments":[{"shelf_id":"A","vertices":[[10,10],[90,20],[80,90],[20,80]]}]}))
        before = Path(master.config_file).read_bytes()
        merged = namespace["load_calibration_mapper"](master.config_file,str(camera))
        self.assertEqual(len(merged.compartments),2)
        comp=merged.get_compartment("A")
        self.assertEqual(len(namespace["get_handles_for_box"](comp)),4)
        self.assertFalse(namespace["is_inside_box"](89,89,comp))
        old_bounds=tuple(comp[k] for k in ("x_min","y_min","x_max","y_max"))
        comp["x_max"]+=80
        namespace["transform_polygon_bounds"](comp,old_bounds)
        namespace["save_calibration_rois"](merged,[str(camera)])
        self.assertEqual(Path(master.config_file).read_bytes(),before)
        self.assertEqual(len(json.loads(camera.read_text())["compartments"][0]["vertices"]),4)

    def test_strict_three_camera_consensus_still_requires_exact_grid_agreement(self):
        import ast, math, threading, time, uuid
        service = Path(__file__).resolve().parents[2] / "camera_service.py"
        tree = ast.parse(service.read_text(encoding="utf-8"))
        namespace = dict(SHELF_SET=SHELF_SET,math=math,threading=threading,time=time,uuid=uuid,
            CCTV_CAMERA_CONFIG={"CCTV_01":{"camera_role":"GRID_6_ROI","is_active":True},
                "CCTV_02":{"camera_role":"GRID_6_ROI","is_active":True},
                "CCTV_03":{"camera_role":"SIDE_PRESENCE","is_active":True}},
            CCTV_CONSENSUS_LOCK=threading.RLock(),CCTV_ALIGN_STATE={},CCTV_SEEN_TRIGGER_IDS={},
            LAST_SHELF_TRIGGER_TS={},PENDING_CCTV_TRIGGERS=[],CONSENSUS_WINDOW_SEC=4.,CONSENSUS_COOLDOWN_SEC=4.)
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="record_cctv_consensus"],type_ignores=[]),"<consensus>","exec"),namespace)
        vote=namespace["record_cctv_consensus"]
        self.assertFalse(vote("SHELF_R1_C2","CCTV_01","BORROW",.9)["consensus_met"])
        self.assertFalse(vote("ANY_SHELF","CCTV_03","BORROW",.9)["consensus_met"])
        self.assertFalse(vote("SHELF_R1_C3","CCTV_02","BORROW",.9)["consensus_met"])
        self.assertEqual(namespace["PENDING_CCTV_TRIGGERS"],[])
        result=vote("SHELF_R1_C2","CCTV_02","BORROW",.9)
        self.assertTrue(result["consensus_met"])
        self.assertEqual(result["confirmed_cameras"],["CCTV_01","CCTV_02","CCTV_03"])
        self.assertEqual(len(namespace["PENDING_CCTV_TRIGGERS"]),1)


if __name__ == "__main__": unittest.main()
