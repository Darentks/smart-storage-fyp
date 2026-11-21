import ast
import json
import os
import tempfile
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('dual_cam_inventory', Path(__file__).parents[1] / 'src' / 'dual_cam_inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def test_all_18_states_and_deltas(self):
        count = unknown = 0
        for f1 in (0,1):
            for m1,m2 in ((0,0),(1,0),(1,1)):
                for b1,b2 in ((0,0),(1,0),(1,1)):
                    front=f'Front_{f1}{m1}{m2}'
                    back='Back_11X' if b2 else 'Back_X01' if m2 else f'Back_{b1}00'
                    obs=inventory.decode_observation(front,back)
                    if m2 and not b2:
                        self.assertEqual(obs['slots'],(f1,m1,m2,None,0))
                        self.assertIsNone(obs['back_count']); self.assertIsNone(obs['total'])
                        self.assertEqual(obs['count_range'],(f1+m1+m2,f1+m1+m2+1))
                        with self.assertRaisesRegex(ValueError,'B1 unknown'):
                            inventory.decode_pair(front,back)
                        unknown+=1
                    else:
                        slots=inventory.decode_pair(front,back)
                        self.assertEqual(slots,(f1,m1,m2,b1,b2))
                        self.assertEqual(sum(slots),obs['total'])
                    count+=1
        self.assertEqual((count,unknown),(18,4))

    def test_invalid_and_legacy_classes(self):
        for front, back in [('Front_001', 'Back_001'), ('Front_010', 'Back_010'),
                            ('Front_011', 'Back_100'), ('Front_010', 'Back_101'),
                            ('Front_1_Boxes', 'Back_1_Boxes'), ('Front_011', 'Back_110'),
                            ('Front_111', 'Back_111'), ('Front_000', 'Back_00'), ('Front_000', 'Back_01X')]:
            with self.assertRaises(ValueError):
                inventory.decode_pair(front, back)

    def test_model_metadata_contract(self):
        service=Path(__file__).parents[2]/'camera_service.py'
        tree=ast.parse(service.read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if
            (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in
                {'_FRONT_OCCUPANCY_LABELS','_BACK_OCCUPANCY_LABELS'} for t in n.targets)) or
            (isinstance(n,ast.FunctionDef) and n.name=='_has_valid_occupancy_metadata')]
        ns={'os':os,'json':json}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<metadata contract>','exec'),ns)
        with tempfile.TemporaryDirectory() as directory:
            for labels,expected in [(list(inventory.FRONT_LABELS),True),
                    (list(inventory.BACK_LABELS),True),
                    (list(inventory.FRONT_LABELS | inventory.BACK_LABELS),False),
                    (['Back_000','Back_001','Back_100','Back_101','Back_11X'],False),
                    (list(inventory.BACK_LABELS)+['Back_X01'],False)]:
                (Path(directory)/'metadata.json').write_text(json.dumps({'labels':labels}),encoding='utf-8')
                self.assertEqual(ns['_has_valid_occupancy_metadata'](directory),expected)

    def fixture(self):
        captures = {str(i): {'shelf_id': 's', 'complete': True, 'completed_at': 90 + i*2, 'duration_ms': 500} for i in range(3)}
        evidence = {'version': inventory.VERSION, 'samples': [{'front_class': 'Front_110', 'back_class': 'Back_100',
                    'front_confidence': 95, 'back_confidence': 94, 'capture': {'id': str(i)}} for i in range(3)]}
        return evidence, captures

    def test_ambiguous_evidence_rejects_guessed_counts(self):
        for count in (2,3):
            evidence,captures=self.fixture()
            for sample in evidence['samples']:
                sample.update(front_class='Front_011',back_class='Back_X01')
            with self.assertRaisesRegex(ValueError,'B1 unknown'):
                inventory.validate_evidence(evidence,count,100,captures,'s')

    def test_v3_evidence_rejected(self):
        evidence,captures=self.fixture()
        evidence['version']='stacked-visibility-v3'
        with self.assertRaises(ValueError):
            inventory.validate_evidence(evidence,3,100,captures,'s')

    def test_deprecated_back_labels_are_never_remapped(self):
        for back in ('Back_001','Back_101','Back_110','Back_111','Back_00','Back_10','Back_11'):
            with self.subTest(back=back), self.assertRaisesRegex(ValueError,'Model/version mismatch'):
                inventory.decode_observation('Front_011',back)

    def test_valid_evidence(self):
        evidence, captures = self.fixture()
        slots, ids = inventory.validate_evidence(evidence, 3, 100, captures, 's')
        self.assertEqual(sum(slots), 3)
        self.assertEqual(ids, {'0', '1', '2'})

    def test_confidence_floor_is_80_for_both_cameras(self):
        evidence,captures=self.fixture()
        for sample in evidence['samples']:
            sample.update(front_confidence=80,back_confidence=80)
        inventory.validate_evidence(evidence,3,100,captures,'s')
        for key in ('front_confidence','back_confidence'):
            evidence['samples'][0][key]=79.99
            with self.assertRaises(ValueError):
                inventory.validate_evidence(evidence,3,100,captures,'s')
            evidence['samples'][0][key]=80

    def test_fail_closed(self):
        for case in ('low', 'nan', 'invalid_back', 'changed', 'duplicate', 'missing', 'stale', 'skew', 'fast', 'count', 'shelf', 'short', 'legacy'):
            evidence, captures = self.fixture()
            count, shelf = 3, 's'
            if case == 'low': evidence['samples'][0]['front_confidence'] = 79
            if case == 'nan': evidence['samples'][0]['back_confidence'] = float('nan')
            if case == 'invalid_back': evidence['samples'][0]['back_class'] = 'Back_01'
            if case == 'changed': evidence['samples'][0]['front_class'] = 'Front_111'
            if case == 'duplicate': evidence['samples'][1]['capture']['id'] = '0'
            if case == 'missing': captures['0']['complete'] = False
            if case == 'stale': captures['0']['completed_at'] = 1
            if case == 'skew': captures['0']['duration_ms'] = 2100
            if case == 'fast': captures['1']['completed_at'] = 90.1
            if case == 'count': count = 4
            if case == 'shelf': shelf = 'other'
            if case == 'short': evidence['samples'].pop()
            if case == 'legacy': evidence = None
            with self.subTest(case=case), self.assertRaises(ValueError):
                inventory.validate_evidence(evidence, count, 100, captures, shelf)


class TransitionTests(unittest.TestCase):
    def test_initialization_matches_quantity_only(self):
        inventory.validate_transition(None, (0,1,1,1,0), 3, 'RECHECK')
        with self.assertRaises(ValueError):
            inventory.validate_transition(None, (0,1,1,1,0), 4, 'BORROW')

    def test_actions_and_relocations(self):
        previous = (0,1,1,1,0)
        inventory.validate_transition(previous, (0,1,0,1,0), 3, 'BORROW')
        inventory.validate_transition(previous, (1,1,1,1,0), 3, 'RETURN')
        inventory.validate_transition(previous, previous, 3, 'RECHECK')
        for current, quantity, action in [((1,1,0,1,0),3,'RETURN'),
                ((1,1,1,1,0),3,'BORROW'), ((0,1,0,1,0),3,'RETURN'),
                ((0,1,0,1,0),3,'RECHECK'), (previous,4,'RETURN')]:
            with self.subTest(current=current, action=action), self.assertRaises(ValueError):
                inventory.validate_transition(previous, current, quantity, action)

    def test_event_bound_receipts(self):
        evidence, captures = InventoryTests().fixture()
        for capture in captures.values():
            capture['validation_token'] = 'event-a'
        inventory.validate_evidence(evidence,3,100,captures,'s','event-a',89)
        for token, timestamp in [('event-b',89), ('event-a',90)]:
            with self.assertRaises(ValueError):
                inventory.validate_evidence(evidence,3,100,captures,'s',token,timestamp)


if __name__ == '__main__': unittest.main()
