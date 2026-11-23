import copy
import random
import subprocess
import sys
import unittest
from pathlib import Path

from paper_layers.manifest import (ManifestError, PreviousAssignmentConflictError,
                                   QuotaUnsatisfiableError, build_manifest)
from paper_layers.service import process

ROOT = Path(__file__).resolve().parent.parent


def sheet(sid, width, height):
    return {'id': sid, 'width': width, 'height': height}


def fold(fid, a, b, c):
    return {'id': fid, 'a': a, 'b': b, 'c': c}


def layout(sheets, folds=()):
    return {'sheets': list(sheets), 'folds': list(folds)}


def sample(sid, origin, channel, lay):
    return {'id': sid, 'origin': origin, 'channel': channel, 'layout': lay}


def request(samples, previous=None, total=4, field=2):
    return {'samples': samples, 'previous': previous,
            'quota': {'evaluation_total': total, 'evaluation_field': field}}


def small_batch(corrected=False):
    """Acceptance batch: A={a1,a2,a3}, B={b1,b2}, C={c1,c2}, D={d1,d2}.

    a1/a2 carry equivalent layouts (one 4x2 sheet folded at the same line,
    written as x=2 and 3x=6, with different sheet and fold ids); only a2/a3
    share a collection piece.  B, C, D group through their own origin ids.
    Every other layout is distinct.  With ``corrected`` the origin of b1 is
    re-pointed at a3's collection piece, leaving b2 on its own.
    """
    a1 = sample('a1', 'o-a1', 'field',
                layout([sheet('p1', '4', '2')], [fold('f1', '1', '0', '2')]))
    a2 = sample('a2', 'o-a23', 'field',
                layout([sheet('p2', '4', '2')], [fold('f2', '3', '0', '6')]))
    a3 = sample('a3', 'o-a23', 'lab', layout([sheet('p3', '4', '3')]))
    b1 = sample('b1', 'o-a23' if corrected else 'o-b', 'field',
                layout([sheet('p4', '3', '2')]))
    b2 = sample('b2', 'o-b', 'lab', layout([sheet('p5', '3', '3')]))
    c1 = sample('c1', 'o-c', 'field', layout([sheet('p6', '5', '2')]))
    c2 = sample('c2', 'o-c', 'lab', layout([sheet('p7', '5', '3')]))
    d1 = sample('d1', 'o-d', 'field', layout([sheet('p8', '6', '2')]))
    d2 = sample('d2', 'o-d', 'lab', layout([sheet('p9', '6', '3')]))
    return [a1, a2, a3, b1, b2, c1, c2, d1, d2]


class ReleaseAcceptance(unittest.TestCase):
    def test_first_release_picks_lexicographically_smallest(self):
        # Feasible evaluation sides: B+C, B+D or C+D (A never fits); the
        # sorted id list of B+C is the lexicographic minimum.  Picking A
        # first would strand the last slot; picking per-sample would leak.
        result = build_manifest(request(small_batch()))
        self.assertEqual(result, {'train': ['a1', 'a2', 'a3', 'd1', 'd2'],
                                  'evaluation': ['b1', 'b2', 'c1', 'c2']})

    def test_origin_correction_forces_regrouping(self):
        # a1 pinned to train drags the whole corrected group (a1,a2,a3,b1)
        # along; b2 stands alone.  Only C+D meets 4 total / 2 field.
        previous = {'train': ['a1'], 'evaluation': []}
        result = build_manifest(request(small_batch(corrected=True), previous))
        self.assertEqual(result, {'train': ['a1', 'a2', 'a3', 'b1', 'b2'],
                                  'evaluation': ['c1', 'c2', 'd1', 'd2']})

    def test_previous_assignment_conflict(self):
        previous = {'train': ['a1'], 'evaluation': ['b1']}
        with self.assertRaises(PreviousAssignmentConflictError) as ctx:
            build_manifest(request(small_batch(corrected=True), previous))
        self.assertIsInstance(ctx.exception, ManifestError)
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertNotIsInstance(ctx.exception, QuotaUnsatisfiableError)

    def test_pinned_evaluation_over_quota_is_unsatisfiable(self):
        previous = {'train': [], 'evaluation': ['b1', 'b2', 'c1', 'c2']}
        with self.assertRaises(QuotaUnsatisfiableError) as ctx:
            build_manifest(request(small_batch(), previous, total=3, field=2))
        self.assertIsInstance(ctx.exception, ManifestError)
        self.assertNotIsInstance(ctx.exception, PreviousAssignmentConflictError)

    def test_reflection_chain_map_distinguishes_samples(self):
        folds6 = [fold('x1', '1', '0', '-1'), fold('x2', '-1', '0', '0'),
                  fold('x3', '1', '0', '2'), fold('y1', '0', '1', '-1'),
                  fold('y2', '0', '-1', '0'), fold('y3', '0', '1', '2')]
        r1 = sample('r1', 'o-r1', 'lab', layout([sheet('q1', '2', '2')]))
        r2 = sample('r2', 'o-r2', 'lab', layout([sheet('q2', '2', '2')], folds6))
        # The folded stack of r2 is the same front square as r1 ...
        layers = process({'sheets': r2['layout']['sheets'],
                          'folds': r2['layout']['folds'],
                          'queries': []})['layers']
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]['face'], 'front')
        self.assertEqual({tuple(p) for p in layers[0]['polygon']},
                         {('0', '0'), ('2', '0'), ('2', '2'), ('0', '2')})
        # ... but the map back to the sheet is (2-x, 2-y), not the identity,
        # so grouping by outline and face alone would be a leak: with a
        # one-sample evaluation quota the two records must be separable.
        result = build_manifest(request([r1, r2], total=1, field=0))
        self.assertEqual(result, {'train': ['r2'], 'evaluation': ['r1']})

    def test_linkage_is_transitive(self):
        t1 = sample('t1', 'o-t1', 'lab', layout([sheet('s1', '7', '2')]))
        t2 = sample('t2', 'o-t23', 'lab', layout([sheet('s2', '7', '2')]))
        t3 = sample('t3', 'o-t23', 'lab', layout([sheet('s3', '8', '2')]))
        u1 = sample('u1', 'o-u1', 'lab', layout([sheet('s4', '9', '2')]))
        result = build_manifest(request([t1, t2, t3, u1], total=1, field=0))
        self.assertEqual(result, {'train': ['t1', 't2', 't3'],
                                  'evaluation': ['u1']})

    def test_equivalent_layouts_cannot_be_split(self):
        x1 = sample('x1', None, 'lab',
                    layout([sheet('p1', '4', '2')], [fold('f1', '1', '0', '2')]))
        x2 = sample('x2', None, 'lab',
                    layout([sheet('p2', '4', '2')], [fold('f2', '3', '0', '6')]))
        with self.assertRaises(QuotaUnsatisfiableError):
            build_manifest(request([x1, x2], total=1, field=0))
        result = build_manifest(request([x1, x2], total=2, field=0))
        self.assertEqual(result, {'train': [], 'evaluation': ['x1', 'x2']})

    def test_sheet_order_matters_but_sheet_ids_do_not(self):
        # Same ordered dimensions with renamed sheets: equivalent, so the
        # pair cannot be split to meet a one-sample quota.
        m1 = sample('m1', None, 'lab', layout([sheet('a', '2', '2'), sheet('b', '3', '3')]))
        m2 = sample('m2', None, 'lab', layout([sheet('x', '2', '2'), sheet('y', '3', '3')]))
        with self.assertRaises(QuotaUnsatisfiableError):
            build_manifest(request([m1, m2], total=1, field=0))
        # Swapped sheet order changes the ordered dimensions: separable.
        n1 = sample('n1', None, 'lab', layout([sheet('a', '2', '2'), sheet('b', '3', '3')]))
        n2 = sample('n2', None, 'lab', layout([sheet('a', '3', '3'), sheet('b', '2', '2')]))
        result = build_manifest(request([n1, n2], total=1, field=0))
        self.assertEqual(result, {'train': ['n2'], 'evaluation': ['n1']})

    def test_input_order_does_not_change_result(self):
        samples = small_batch(corrected=True)
        previous = {'train': ['a1'], 'evaluation': []}
        base = build_manifest(request(samples, previous))
        for seed in range(8):
            shuffled = copy.deepcopy(samples)
            random.Random(seed).shuffle(shuffled)
            prev = {'train': previous['train'][:],
                    'evaluation': previous['evaluation'][:]}
            random.Random(seed + 100).shuffle(prev['train'])
            random.Random(seed + 200).shuffle(prev['evaluation'])
            self.assertEqual(build_manifest(request(shuffled, prev)), base)

    def test_previous_result_can_be_fed_back(self):
        first = build_manifest(request(small_batch()))
        again = build_manifest(request(small_batch(), first))
        self.assertEqual(again, first)

    def test_result_is_a_partition_with_exact_keys(self):
        samples = small_batch()
        result = build_manifest(request(samples))
        self.assertEqual(set(result), {'train', 'evaluation'})
        ids = [s['id'] for s in samples]
        merged = result['train'] + result['evaluation']
        self.assertEqual(sorted(merged), sorted(ids))
        self.assertEqual(len(merged), len(set(merged)))
        self.assertEqual(result['train'], sorted(result['train']))
        self.assertEqual(result['evaluation'], sorted(result['evaluation']))

    def test_empty_samples(self):
        self.assertEqual(build_manifest(request([], None, 0, 0)),
                         {'train': [], 'evaluation': []})
        with self.assertRaises(QuotaUnsatisfiableError):
            build_manifest(request([], None, 1, 0))
        everything = [s['id'] for s in small_batch()]
        result = build_manifest(request(small_batch(),
                                        {'train': everything, 'evaluation': []},
                                        total=0, field=0))
        self.assertEqual(result, {'train': sorted(everything), 'evaluation': []})

    def test_documented_example(self):
        data = {
            'samples': [
                {'id': 's1', 'origin': 'o1', 'channel': 'field',
                 'layout': {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
                            'folds': [{'id': 'f', 'a': '1', 'b': '0', 'c': '2'}]}},
                {'id': 's2', 'origin': None, 'channel': 'lab',
                 'layout': {'sheets': [{'id': 'P', 'width': '3', 'height': '2'}],
                            'folds': []}},
            ],
            'previous': None,
            'quota': {'evaluation_total': 1, 'evaluation_field': 0},
        }
        self.assertEqual(build_manifest(data),
                         {'train': ['s1'], 'evaluation': ['s2']})

    def test_input_is_not_mutated(self):
        data = request(small_batch(corrected=True),
                       {'train': ['a1'], 'evaluation': []})
        snapshot = copy.deepcopy(data)
        build_manifest(data)
        self.assertEqual(data, snapshot)


class InvalidInput(unittest.TestCase):
    def assert_invalid(self, data):
        with self.assertRaises(ValueError) as ctx:
            build_manifest(data)
        self.assertNotIsInstance(ctx.exception, ManifestError)

    def test_root_shape(self):
        good = request(small_batch())
        self.assert_invalid({'samples': [], 'previous': None})
        self.assert_invalid({**good, 'extra': 1})
        self.assert_invalid([])
        self.assert_invalid(None)
        self.assert_invalid({**good, 'samples': {}})

    def test_quota(self):
        samples = small_batch()
        for quota in [
            {'evaluation_total': 4},
            {'evaluation_total': 4, 'evaluation_field': 2, 'x': 0},
            {'evaluation_total': -1, 'evaluation_field': 0},
            {'evaluation_total': 2049, 'evaluation_field': 0},
            {'evaluation_total': 4, 'evaluation_field': -1},
            {'evaluation_total': 4, 'evaluation_field': 1025},
            {'evaluation_total': 2, 'evaluation_field': 3},
            {'evaluation_total': True, 'evaluation_field': 0},
            {'evaluation_total': 4, 'evaluation_field': False},
            {'evaluation_total': '4', 'evaluation_field': 2},
            {'evaluation_total': 4.0, 'evaluation_field': 2},
        ]:
            self.assert_invalid({'samples': samples, 'previous': None,
                                 'quota': quota})
        self.assert_invalid({'samples': samples, 'previous': None, 'quota': []})

    def test_sample_records(self):
        good = small_batch()
        one = good[0]
        quota = {'evaluation_total': 1, 'evaluation_field': 0}
        bad_samples = [
            [{'id': 'a1', 'origin': None, 'channel': 'lab'}],
            [{**one, 'extra': 1}],
            [{**one, 'id': 1}],
            [{**one, 'id': 'bad id'}],
            [{**one, 'id': 'x' * 65}],
            [{**one, 'origin': 7}],
            [{**one, 'origin': 'bad origin'}],
            [{**one, 'channel': 'FIELD'}],
            [{**one, 'channel': ''}],
            [{**one, 'channel': None}],
            [{**one, 'layout': []}],
            [{**one, 'layout': {**one['layout'], 'queries': []}}],
            [{**one, 'layout': {'sheets': one['layout']['sheets']}}],
            [{**one, 'layout': layout([sheet('P', '0', '2')])}],
            [{**one, 'layout': layout([sheet('P', '4', '2')],
                                      [fold('f', '0', '0', '1')])}],
            [{**one, 'layout': layout([sheet(f'p{i}', '1', '1')
                                       for i in range(11)])}],
            [{**one, 'layout': layout([sheet('P', '4', '2'),
                                       sheet('P', '3', '2')])}],
            [one, one],
            [one, {**good[1], 'id': one['id']}],
        ]
        for samples in bad_samples:
            self.assert_invalid({'samples': samples, 'previous': None,
                                 'quota': quota})
        self.assert_invalid({'samples': [None] * 40001, 'previous': None,
                             'quota': quota})

    def test_previous_records(self):
        samples = small_batch()
        quota = {'evaluation_total': 4, 'evaluation_field': 2}
        bad_previous = [
            [],
            {'train': []},
            {'train': [], 'evaluation': [], 'x': []},
            {'train': {}, 'evaluation': []},
            {'train': [1], 'evaluation': []},
            {'train': ['a1', 'a1'], 'evaluation': []},
            {'train': ['a1'], 'evaluation': ['a1']},
            {'train': ['a1'], 'evaluation': ['a1', 'a1']},
            {'train': ['nope'], 'evaluation': []},
            {'train': [], 'evaluation': ['a1', 'gone']},
        ]
        for previous in bad_previous:
            self.assert_invalid({'samples': samples, 'previous': previous,
                                 'quota': quota})


class LegacyCompatibility(unittest.TestCase):
    def test_package_exports_still_work(self):
        import paper_layers
        for name in ('build_archive', 'dumps', 'loads', 'rebuild_window',
                     'serialize_window'):
            self.assertTrue(hasattr(paper_layers, name), name)
        for name in ('build_manifest', 'ManifestError',
                     'PreviousAssignmentConflictError', 'QuotaUnsatisfiableError'):
            self.assertTrue(hasattr(paper_layers, name), name)

    def test_baseline_process_unchanged(self):
        import json
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = process(data)
        self.assertEqual(result['summary'],
                         {'layers': 2, 'material_area': '8', 'queries': 3})


class ResourceAcceptance(unittest.TestCase):
    def test_release_fixture_fits_192_mib(self):
        job = ROOT / 'tests' / 'manifest_resource_job.py'
        run = subprocess.run([sys.executable, '-I', str(job)],
                             capture_output=True, text=True, timeout=900)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('resource-ok', run.stdout)


if __name__ == '__main__':
    unittest.main()
