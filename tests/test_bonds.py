"""Acceptance tests for the folded-stack adhesive interfaces.

The scenarios follow the delivery contract end to end: a folded stack is
reduced to its exact local layer stack per position, only locally adjacent
fragments form an interface, contact areas map back to each original paper,
and each bond specification sums the matching physical material-face pairs.
Zero-area folds, disconnected contact, separated sheets, empty bonds,
array-order independence, invalid input rejection and representative legacy
entry points are all covered.
"""
import copy
import json
import unittest
from fractions import Fraction
from pathlib import Path

from paper_layers import build_bonds
from paper_layers.archive import build_archive, dumps as archive_dumps, loads as archive_loads
from paper_layers.bonds import build_bonds as module_build_bonds
from paper_layers.manifest import build_manifest
from paper_layers.polygon import area, intersect, normalize
from paper_layers.recheck import build_handoff
from paper_layers.service import process

ROOT = Path(__file__).resolve().parent.parent


def as_poly(poly):
    return normalize([(Fraction(x), Fraction(y)) for x, y in poly])


def sheet(ident, width, height):
    return {'id': ident, 'width': width, 'height': height}


def fold(ident, a, b, c):
    return {'id': ident, 'a': a, 'b': b, 'c': c}


def side(sheet_id, face):
    return {'sheet': sheet_id, 'face': face}


def bond(ident, a, b, min_area):
    return {'id': ident, 'side_a': a, 'side_b': b, 'min_area': min_area}


def two_sheet_half():
    """A below B, both front up, folded at x=2 toward negative x."""
    return {'layout': {'sheets': [sheet('A', '4', '2'), sheet('B', '4', '2')],
                       'folds': [fold('half', '1', '0', '2')]},
            'bonds': []}


class InterfaceGeometry(unittest.TestCase):
    def test_two_sheet_fold_has_three_interfaces(self):
        result = build_bonds(two_sheet_half())
        interfaces = result['interfaces']
        self.assertEqual(len(interfaces), 3)
        self.assertEqual([(r['lower_layer'], r['upper_layer']) for r in interfaces],
                         [(0, 1), (1, 2), (2, 3)])
        self.assertEqual([r['area'] for r in interfaces], [4, 4, 4])
        for record in interfaces:
            self.assertEqual(record['lower_sheet'], record['lower_sheet'])
            self.assertTrue(record['polygon'])
            self.assertTrue(record['lower_polygon'])
            self.assertTrue(record['upper_polygon'])
        self.assertEqual(result['bonds'], [])

    def test_same_material_pair_touches_at_two_depths(self):
        result = build_bonds(two_sheet_half())
        pair = frozenset((('A', 'front'), ('B', 'back')))
        matching = [r for r in result['interfaces']
                    if frozenset(((r['lower_sheet'], r['lower_face']),
                                  (r['upper_sheet'], r['upper_face']))) == pair]
        self.assertEqual(len(matching), 2)
        self.assertEqual([(r['lower_layer'], r['upper_layer']) for r in matching],
                         [(0, 1), (2, 3)])

    def test_two_ab_interfaces_are_not_merged_or_mis_mapped(self):
        result = build_bonds(two_sheet_half())
        first, second = [r for r in result['interfaces']
                         if r['lower_sheet'] == 'A'] + \
                        [r for r in result['interfaces']
                         if r['upper_sheet'] == 'A']
        self.assertNotEqual(first['lower_layer'], second['lower_layer'])
        # Both A/B interfaces share the same projected contact polygon.
        self.assertEqual(as_poly(first['polygon']), as_poly(second['polygon']))
        # Yet their A-side source regions differ: the first is the stay half,
        # the second is the folded half, so their orientation is reversed.
        first_a = as_poly(first['lower_polygon'])
        second_a = as_poly(second['upper_polygon'])
        self.assertNotEqual(first_a, second_a)
        self.assertEqual(area(first_a), 4)
        self.assertEqual(area(second_a), 4)
        self.assertEqual(first_a, [(0, 0), (2, 0), (2, 2), (0, 2)])

    def test_contact_faces_follow_the_physical_rule(self):
        result = build_bonds(two_sheet_half())
        by_layers = {(r['lower_layer'], r['upper_layer']): r
                     for r in result['interfaces']}
        # Layer 0 is A.front (stay), layer 1 is B.front (stay): the lower shows
        # its front, the upper presents its back (the reverse of front).
        low = by_layers[(0, 1)]
        self.assertEqual((low['lower_face'], low['upper_face']), ('front', 'back'))
        # Layer 1 is B.front (stay), layer 2 is B.back (moved): B's front meets
        # B's front, because the upper fragment presents the reverse of its
        # upward back face.
        middle = by_layers[(1, 2)]
        self.assertEqual((middle['lower_face'], middle['upper_face']), ('front', 'front'))
        # Layer 2 is B.back, layer 3 is A.back.
        top = by_layers[(2, 3)]
        self.assertEqual((top['lower_face'], top['upper_face']), ('back', 'front'))

    def test_three_flat_sheets_see_through_the_narrow_middle(self):
        data = {'layout': {'sheets': [sheet('A', '4', '2'), sheet('B', '2', '2'),
                                      sheet('C', '4', '2')], 'folds': []},
                'bonds': []}
        result = build_bonds(data)
        ac = [r for r in result['interfaces']
              if {r['lower_sheet'], r['upper_sheet']} == {'A', 'C'}]
        self.assertEqual(len(ac), 1)
        record = ac[0]
        self.assertEqual((record['lower_layer'], record['upper_layer']), (0, 2))
        self.assertEqual((record['lower_sheet'], record['lower_face']), ('A', 'front'))
        self.assertEqual((record['upper_sheet'], record['upper_face']), ('C', 'back'))
        self.assertEqual(as_poly(record['polygon']),
                         [(2, 0), (4, 0), (4, 2), (2, 2)])
        self.assertEqual(record['area'], 4)
        # B fronts C only under [0,2]x[0,2], and A fronts B under the same box.
        for record in result['interfaces']:
            if record is not ac[0]:
                self.assertEqual(area(as_poly(record['polygon'])), 4)

    def test_zero_area_crease_adds_no_interface(self):
        data = {'layout': {'sheets': [sheet('P', '2', '2')],
                           'folds': [fold('half', '1', '0', '1')]}, 'bonds': []}
        result = build_bonds(data)
        self.assertEqual(len(result['interfaces']), 1)
        self.assertEqual(result['interfaces'][0]['area'], 2)

    def test_projected_overlap_hidden_by_a_middle_piece_is_not_contact(self):
        # A 4x4 bottom, B 2x2 middle, C 4x4 top; A and C overlap everywhere,
        # but only the ring outside B is direct contact.
        data = {'layout': {'sheets': [sheet('A', '4', '4'), sheet('B', '2', '2'),
                                      sheet('C', '4', '4')], 'folds': []},
                'bonds': []}
        result = build_bonds(data)
        ac = [r for r in result['interfaces']
              if (r['lower_layer'], r['upper_layer']) == (0, 2)]
        self.assertEqual(len(ac), 2)
        pieces = [as_poly(r['polygon']) for r in ac]
        self.assertEqual(sum(area(p) for p in pieces), 12)
        for i in range(len(pieces)):
            for j in range(i + 1, len(pieces)):
                self.assertEqual(area(intersect(pieces[i], pieces[j])), 0)
        # The footprint that B hides is A/C's blind spot: it belongs to B/C.
        hidden = normalize([(0, 0), (2, 0), (2, 2), (0, 2)])
        bc = [as_poly(r['polygon']) for r in result['interfaces']
              if (r['lower_layer'], r['upper_layer']) == (1, 2)]
        self.assertEqual(sum(area(intersect(p, hidden)) for p in bc), 4)
        self.assertEqual(sum(area(intersect(p, hidden)) for p in pieces), 0)

    def test_fractional_fold_line_and_reversed_bond_sides(self):
        data = {'layout': {'sheets': [sheet('A', '4', '2'), sheet('B', '4', '2')],
                           'folds': [fold('f', '2', '0', '3')]},
                'bonds': [bond('ab', side('B', 'back'), side('A', 'front'), '3')]}
        result = build_bonds(data)
        first = result['interfaces'][0]
        self.assertEqual(first['area'], 3)
        self.assertEqual(first['polygon'][1], ['3/2', 0])
        self.assertEqual(result['bonds'][0]['available_area'], 8)
        self.assertTrue(result['bonds'][0]['satisfied'])


class BondJudgements(unittest.TestCase):
    def test_two_ab_interfaces_sum_across_depths(self):
        data = two_sheet_half()
        data['bonds'] = [bond('ab8', side('A', 'front'), side('B', 'back'), '8')]
        result = build_bonds(data)
        self.assertEqual(result['bonds'][0]['available_area'], 8)
        self.assertTrue(result['bonds'][0]['satisfied'])

    def test_threshold_above_available_area_is_unsatisfied(self):
        data = two_sheet_half()
        data['bonds'] = [bond('ab9', side('A', 'front'), side('B', 'back'), '9')]
        result = build_bonds(data)
        self.assertEqual(len(result['interfaces']), 3)
        self.assertEqual(result['bonds'][0]['available_area'], 8)
        self.assertFalse(result['bonds'][0]['satisfied'])

    def test_unordered_material_faces_match_either_orientation(self):
        data = two_sheet_half()
        data['bonds'] = [bond('ab', side('B', 'back'), side('A', 'front'), '8')]
        result = build_bonds(data)
        self.assertEqual(result['bonds'][0]['available_area'], 8)
        self.assertTrue(result['bonds'][0]['satisfied'])

    def test_no_matching_interface_means_zero_and_unsatisfied(self):
        data = two_sheet_half()
        data['bonds'] = [bond('af', side('A', 'front'), side('B', 'front'), '1')]
        result = build_bonds(data)
        self.assertEqual(result['bonds'][0]['available_area'], 0)
        self.assertFalse(result['bonds'][0]['satisfied'])

    def test_non_matching_material_pair_is_zero_not_bad_input(self):
        # Two real sheets, but no interface ever joins A.front to A.back.
        data = two_sheet_half()
        data['bonds'] = [bond('self', side('A', 'front'), side('A', 'back'), '1')]
        result = build_bonds(data)
        self.assertEqual(len(result['interfaces']), 3)
        self.assertEqual(result['bonds'][0]['available_area'], 0)
        self.assertFalse(result['bonds'][0]['satisfied'])

    def test_sheet_without_any_contact_has_no_interface(self):
        data = {'layout': {'sheets': [sheet('A', '2', '2')], 'folds': []},
                'bonds': [bond('self', side('A', 'front'), side('A', 'back'), '1')]}
        result = build_bonds(data)
        self.assertEqual(result['interfaces'], [])
        self.assertEqual(result['bonds'][0]['available_area'], 0)
        self.assertFalse(result['bonds'][0]['satisfied'])

    def test_empty_bond_list_still_returns_interfaces(self):
        result = build_bonds(two_sheet_half())
        self.assertEqual(len(result['interfaces']), 3)
        self.assertEqual(result['bonds'], [])

    def test_all_specifications_are_returned_including_unsatisfied(self):
        data = two_sheet_half()
        data['bonds'] = [
            bond('b_ok', side('A', 'front'), side('B', 'back'), '8'),
            bond('b_no', side('A', 'front'), side('B', 'front'), '1'),
            bond('b_miss', side('A', 'back'), side('B', 'back'), '4'),
        ]
        result = build_bonds(data)
        self.assertEqual([b['id'] for b in result['bonds']], ['b_miss', 'b_no', 'b_ok'])
        status = {b['id']: b['satisfied'] for b in result['bonds']}
        self.assertEqual(status, {'b_ok': True, 'b_no': False, 'b_miss': False})

    def test_input_permutation_never_changes_the_delivery(self):
        data = two_sheet_half()
        data['bonds'] = [
            bond('b_no', side('A', 'front'), side('B', 'front'), '1'),
            bond('b_ok', side('A', 'front'), side('B', 'back'), '8'),
            bond('b_bb', side('B', 'front'), side('B', 'back'), '4'),
        ]
        base = build_bonds(data)
        shuffled = copy.deepcopy(data)
        shuffled['bonds'] = list(reversed(shuffled['bonds']))
        self.assertEqual(json.dumps(base, sort_keys=True),
                         json.dumps(build_bonds(shuffled), sort_keys=True))
        self.assertEqual([b['id'] for b in build_bonds(shuffled)['bonds']],
                         ['b_bb', 'b_no', 'b_ok'])

    def test_output_is_plain_json_with_exact_numbers(self):
        data = two_sheet_half()
        data['bonds'] = [bond('ab', side('A', 'front'), side('B', 'back'), '2.25')]
        text = json.dumps(build_bonds(data))
        parsed = json.loads(text)
        for record in parsed['interfaces']:
            for point in record['polygon']:
                for value in point:
                    self.assertIsInstance(value, (int, str))
        self.assertTrue(all(isinstance(v, (int, str)) for v in
                            (parsed['bonds'][0]['min_area'], parsed['bonds'][0]['available_area'])))
        # Both are output-caliber: exact reduced integer or ``n/d`` string.
        self.assertEqual(parsed['bonds'][0]['min_area'], '9/4')
        self.assertEqual(parsed['bonds'][0]['available_area'], 8)


class BuildValidation(unittest.TestCase):
    def invalid(self, data):
        with self.assertRaises(ValueError):
            build_bonds(data)

    def test_bad_top_level_shapes(self):
        good = two_sheet_half()
        for mutate in [
            lambda d: d.pop('layout'),
            lambda d: d.pop('bonds'),
            lambda d: d.update(extra=1),
            lambda d: d.update(layout={'sheets': [sheet('A', '2', '2')]}),
            lambda d: d.update(bonds={}),
        ]:
            broken = copy.deepcopy(good)
            mutate(broken)
            self.invalid(broken)

    def test_bad_bond_specifications(self):
        good = two_sheet_half()
        cases = [
            [{'id': 'b', 'side_a': side('A', 'front'), 'side_b': side('B', 'back')}],
            [dict(bond('b', side('A', 'front'), side('B', 'back'), '1'), extra=1)],
            [bond('', side('A', 'front'), side('B', 'back'), '1')],
            [bond('b', side('A', 'front'), side('B', 'back'), '0')],
            [bond('b', side('A', 'front'), side('B', 'back'), '-1')],
            [bond('b', side('A', 'front'), side('B', 'back'), '1.0000001')],
            [bond('b', {'sheet': 'A', 'face': 'top'}, side('B', 'back'), '1')],
            [bond('b', {'sheet': 'Q', 'face': 'front'}, side('B', 'back'), '1')],
            [bond('b', {'sheet': 'A'}, side('B', 'back'), '1')],
            [bond('b', side('A', 'front'), side('A', 'front'), '1')],
            [bond('b', side('A', 'front'), side('B', 'back'), '1'),
             bond('b', side('A', 'front'), side('B', 'back'), '1')],
            [bond('b', side('A', 'front'), side('B', 'back'), '1'),
             bond('c', side('B', 'back'), side('A', 'front'), '2')],
        ]
        for bonds in cases:
            data = copy.deepcopy(good)
            data['bonds'] = bonds
            self.invalid(data)

    def test_bond_count_limit(self):
        data = two_sheet_half()
        data['bonds'] = [bond(f'b{i}', side('A', 'front'), side('B', 'back'), '1')
                         for i in range(1025)]
        self.invalid(data)

    def test_invalid_layout_is_rejected(self):
        data = two_sheet_half()
        data['layout']['folds'].append({'id': 'half', 'a': '0', 'b': '0', 'c': '1'})
        self.invalid(data)
        data = two_sheet_half()
        data['layout']['sheets'][0]['width'] = '0'
        self.invalid(data)

    def test_partial_result_is_never_returned(self):
        data = two_sheet_half()
        data['bonds'] = [bond('ok', side('A', 'front'), side('B', 'back'), '1'),
                         bond('bad', side('A', 'front'), side('Q', 'back'), '1')]
        with self.assertRaises(ValueError):
            build_bonds(data)


class EntryPointWiring(unittest.TestCase):
    def test_module_and_package_export_agree(self):
        self.assertIs(module_build_bonds, build_bonds)
        self.assertEqual(build_bonds(two_sheet_half()),
                         module_build_bonds(two_sheet_half()))

    def test_result_is_fresh_per_call(self):
        first = build_bonds(two_sheet_half())
        second = build_bonds(two_sheet_half())
        self.assertEqual(first, second)
        first['interfaces'][0]['polygon'][0][0] = 99
        self.assertNotEqual(first, build_bonds(two_sheet_half()))


class LegacyCompatibility(unittest.TestCase):
    def test_process_fixture(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = process(data)
        self.assertEqual(result['summary'],
                         {'layers': 2, 'material_area': '8', 'queries': 3})
        self.assertEqual(result['queries'][0]['top']['source_point'], ['3', '1'])

    def test_archive_round_trip(self):
        from paper_layers.archive import rebuild_window
        data = {'layout': two_sheet_half()['layout'], 'batches': []}
        archive = archive_loads(archive_dumps(build_archive(data)))
        self.assertEqual(rebuild_window(archive, {
            'sheet': 'A', 'face': 'back', 'rect': ['2', '0', '4', '2'],
            'ignore_batch': None})['regions'], [])

    def test_manifest_sample(self):
        result = build_manifest({
            'samples': [
                {'id': 's1', 'origin': 'o1', 'channel': 'field',
                 'layout': {'sheets': [sheet('P', '4', '2')],
                            'folds': [fold('f', '1', '0', '2')]}},
                {'id': 's2', 'origin': None, 'channel': 'lab',
                 'layout': {'sheets': [sheet('P', '3', '2')], 'folds': []}},
            ],
            'previous': None,
            'quota': {'evaluation_total': 1, 'evaluation_field': 0},
        })
        self.assertEqual(result, {'train': ['s1'], 'evaluation': ['s2']})

    def test_recheck_build_still_works(self):
        handoff = build_handoff({
            'versions': [
                {'id': 'U', 'layout': {'sheets': [sheet('P', '4', '2')], 'folds': []}},
                {'id': 'V', 'layout': {'sheets': [sheet('P', '4', '2')],
                                       'folds': [fold('f', '1', '0', '2')]}},
            ],
            'probes': [{'id': f'p{i}', 'station': 'ABC'[i % 3],
                        'point': ['1', '1'], 'cost': 1} for i in range(6)],
            'required': [],
        })
        self.assertEqual(handoff['format'], 'paper-recheck-handoff')


if __name__ == '__main__':
    unittest.main()
