"""Acceptance and regression tests for exact revision impact zones."""
import copy
import json
import unittest
from fractions import Fraction
from unittest import mock

from paper_layers import (
    RevisionComplexityError,
    build_bonds,
    compare_revisions,
)
from paper_layers.polygon import area as polygon_area
from paper_layers.polygon import intersect, normalize
from paper_layers.revision import MAX_REVISION_POLYGONS
from paper_layers.service import process


def sheet(sid, width, height='2'):
    return {'id': sid, 'width': width, 'height': height}


def fold(fid, a='1', b='0', c='2'):
    return {'id': fid, 'a': a, 'b': b, 'c': c}


def layout(sheets, folds=None):
    return {'sheets': sheets, 'folds': folds or []}


def values(poly):
    return [(Fraction(x), Fraction(y)) for x, y in poly]


def normalized(poly):
    return normalize(values(poly))


def assert_partition(testcase, zones):
    """Check positive area, convexity and pairwise interior disjointness."""
    polygons = []
    for zone in zones:
        poly = normalized(zone['polygon'])
        testcase.assertTrue(poly)
        testcase.assertGreater(polygon_area(poly), 0)
        # A normalized polygon is CCW and every consecutive turn is left or
        # collinear-free left.  Clipping produces convex polygons.
        count = len(poly)
        for i in range(count):
            a, b, c = poly[i - 1], poly[i], poly[(i + 1) % count]
            cross = ((b[0] - a[0]) * (c[1] - b[1])
                     - (b[1] - a[1]) * (c[0] - b[0]))
            testcase.assertGreaterEqual(cross, 0)
        testcase.assertEqual(Fraction(zone['area']), polygon_area(poly))
        polygons.append(poly)

    for i, left in enumerate(polygons):
        for right in polygons[i + 1:]:
            common = intersect(left, right)
            if common:
                testcase.assertEqual(polygon_area(common), 0)


class SheetOrderAcceptance(unittest.TestCase):
    def test_swapping_two_sheets_is_a_full_area_hidden_stack_change(self):
        payload = {
            'reference': layout([sheet('A', '4'), sheet('B', '4')]),
            'candidate': layout([sheet('B', '4'), sheet('A', '4')]),
        }
        result = compare_revisions(payload)

        self.assertEqual(result['regions'], result['impact_zones'])
        self.assertEqual(len(result['regions']), 1)
        self.assertEqual(result['added_area'], 0)
        self.assertEqual(result['removed_area'], 0)
        self.assertEqual(result['unchanged_area'], 0)
        self.assertEqual(result['changed_area'], 8)
        self.assertEqual(result['areas']['changed'], 8)

        zone = result['regions'][0]
        self.assertEqual(zone['category'], 'changed')
        self.assertEqual(zone['polygon'], [[0, 0], [4, 0], [4, 2], [0, 2]])
        self.assertEqual([layer['sheet'] for layer in zone['reference_stack']],
                         ['A', 'B'])
        self.assertEqual([layer['sheet'] for layer in zone['candidate_stack']],
                         ['B', 'A'])
        for stack in (zone['reference_stack'], zone['candidate_stack']):
            self.assertEqual([layer['face'] for layer in stack],
                             ['front', 'front'])
            for layer in stack:
                self.assertEqual(layer['affine'], [1, 0, 0, 1, 0, 0])
                self.assertEqual(layer['transform'], [1, 0, 0, 1, 0, 0])
        assert_partition(self, result['regions'])

    def test_swapping_only_lower_sheets_keeps_same_visible_top(self):
        payload = {
            'reference': layout([sheet('A', '4'), sheet('B', '3'),
                                 sheet('C', '2')]),
            'candidate': layout([sheet('B', '3'), sheet('A', '4'),
                                 sheet('C', '2')]),
        }
        result = compare_revisions(payload)
        square = [[0, 0], [2, 0], [2, 2], [0, 2]]
        matching = [z for z in result['regions'] if z['polygon'] == square]
        self.assertEqual(len(matching), 1)
        zone = matching[0]
        self.assertEqual(zone['category'], 'changed')
        self.assertEqual(zone['reference_stack'][-1]['sheet'], 'C')
        self.assertEqual(zone['candidate_stack'][-1]['sheet'], 'C')
        self.assertEqual([x['sheet'] for x in zone['reference_stack']],
                         ['A', 'B', 'C'])
        self.assertEqual([x['sheet'] for x in zone['candidate_stack']],
                         ['B', 'A', 'C'])
        assert_partition(self, result['regions'])


class FoldAcceptance(unittest.TestCase):
    def test_half_fold_has_reflected_layer_and_removed_half(self):
        payload = {
            'reference': layout([sheet('A', '4')]),
            'candidate': layout([sheet('A', '4')], [fold('half')]),
        }
        result = compare_revisions(payload)
        self.assertEqual({z['category'] for z in result['regions']},
                         {'changed', 'removed'})
        self.assertEqual(result['added_area'], 0)
        self.assertEqual(result['unchanged_area'], 0)
        self.assertEqual(result['changed_area'], 4)
        self.assertEqual(result['removed_area'], 4)

        changed = next(z for z in result['regions']
                       if z['category'] == 'changed')
        removed = next(z for z in result['regions']
                       if z['category'] == 'removed')
        self.assertEqual(changed['polygon'],
                         [[0, 0], [2, 0], [2, 2], [0, 2]])
        self.assertEqual(removed['polygon'],
                         [[2, 0], [4, 0], [4, 2], [2, 2]])
        self.assertEqual(len(changed['reference_stack']), 1)
        self.assertEqual(len(changed['candidate_stack']), 2)
        stationary, reflected = changed['candidate_stack']
        self.assertEqual((stationary['face'], stationary['affine']),
                         ('front', [1, 0, 0, 1, 0, 0]))
        self.assertEqual(reflected['face'], 'back')
        self.assertEqual(reflected['affine'], [-1, 0, 0, 1, 4, 0])
        self.assertNotEqual(stationary['face'], reflected['face'])
        self.assertNotEqual(stationary['affine'], reflected['affine'])
        self.assertEqual(removed['reference_stack'][0]['sheet'], 'A')
        self.assertEqual(removed['candidate_stack'], [])
        assert_partition(self, result['regions'])

    def test_asymmetric_x1_fold_adds_extension_changes_overlap_and_removes_origin(self):
        payload = {
            'reference': layout([sheet('A', '3')]),
            'candidate': layout([sheet('A', '3')], [fold('at-one', c='1')]),
        }
        result = compare_revisions(payload)
        self.assertEqual(result['added_area'], 2)
        self.assertEqual(result['changed_area'], 2)
        self.assertEqual(result['removed_area'], 4)
        self.assertEqual(result['unchanged_area'], 0)

        by_category = {}
        for zone in result['regions']:
            by_category.setdefault(zone['category'], []).append(zone)
        self.assertEqual([z['polygon'] for z in by_category['added']],
                         [[[-1, 0], [0, 0], [0, 2], [-1, 2]]])
        self.assertEqual([z['polygon'] for z in by_category['changed']],
                         [[[0, 0], [1, 0], [1, 2], [0, 2]]])
        self.assertEqual([z['polygon'] for z in by_category['removed']],
                         [[[1, 0], [3, 0], [3, 2], [1, 2]]])
        assert_partition(self, result['regions'])

    def test_renamed_empty_fold_step_is_still_unchanged(self):
        # x=5 has no positive-side material on a [0,3] x [0,2] sheet.
        reference = layout([sheet('A', '3')], [fold('step-a', c='5')])
        candidate = layout([sheet('A', '3')], [fold('step-b-renamed', c='5')])
        result = compare_revisions({'reference': reference,
                                    'candidate': candidate})
        self.assertEqual(result['regions'], [])
        self.assertEqual(result['added_area'], 0)
        self.assertEqual(result['removed_area'], 0)
        self.assertEqual(result['changed_area'], 0)
        self.assertEqual(result['unchanged_area'], 6)
        self.assertEqual(
            Fraction(result['unchanged_area'])
            + Fraction(result['added_area'])
            + Fraction(result['removed_area'])
            + Fraction(result['changed_area']),
            6,
        )


    def test_hidden_non_top_source_map_is_compared(self):
        # This fixture has a positive-area cell whose visible top signature is
        # equal in both versions, while lower layers and their maps differ.
        payload = {
            'reference': {
                'sheets': [sheet('A', '3', '3'), sheet('B', '3', '3')],
                'folds': [fold('noop-y3', '0', '1', '3'),
                          fold('down-y2', '0', '1', '2')],
            },
            'candidate': {
                'sheets': [sheet('A', '4', '3'), sheet('B', '5', '3')],
                'folds': [fold('noop-x5', '1', '0', '5'),
                          fold('left-x1', '1', '0', '1'),
                          fold('down-y2', '0', '1', '2')],
            },
        }
        result = compare_revisions(payload)
        target = [[0, 1], [1, 1], [1, 2], [0, 2]]
        zone = next(z for z in result['regions'] if z['polygon'] == target)
        ref, cand = zone['reference_stack'], zone['candidate_stack']
        self.assertEqual((ref[-1]['sheet'], ref[-1]['face'],
                          ref[-1]['affine']),
                         (cand[-1]['sheet'], cand[-1]['face'],
                          cand[-1]['affine']))
        lower_maps_ref = [(x['sheet'], tuple(x['affine'])) for x in ref[:-1]]
        lower_maps_cand = [(x['sheet'], tuple(x['affine'])) for x in cand[:-1]]
        self.assertNotEqual(lower_maps_ref, lower_maps_cand)
        assert_partition(self, result['regions'])


class StabilityAndGeometry(unittest.TestCase):
    def test_result_is_stable_to_key_order_and_fold_id_rename(self):
        three_width = {
            'reference': layout([sheet('A', '4', '3'), sheet('B', '3', '3'),
                                  sheet('C', '2', '2')],
                                 [fold('r1', c='2'),
                                  fold('r2', '0', '1', '1')]),
            'candidate': layout([sheet('C', '2', '2'), sheet('A', '4', '3'),
                                  sheet('B', '3', '3')],
                                 [fold('c1', c='3'),
                                  fold('c2', '1', '1', '4')]),
        }
        first = compare_revisions(copy.deepcopy(three_width))

        renamed = copy.deepcopy(three_width)
        for side in ('reference', 'candidate'):
            for i, item in enumerate(renamed[side]['folds']):
                item['id'] = f'completely-different-{side}-{i}'
        second = compare_revisions(renamed)
        self.assertEqual(second, first)

        # Rebuild the same JSON object with reversed insertion/key order.
        text = json.dumps(three_width, sort_keys=True)
        reordered = json.loads(text)
        third = compare_revisions(reordered)
        self.assertEqual(third, first)
        # The plain result is JSON serializable with exact rational output.
        restored = json.loads(json.dumps(first, sort_keys=True))
        self.assertEqual(restored['changed_area'], first['changed_area'])
        assert_partition(self, first['regions'])

    def test_area_identity_and_pairwise_partition_on_diverse_fixture(self):
        payload = {
            'reference': layout(
                [sheet('A', '5', '3'), sheet('B', '3', '3'),
                 sheet('C', '2', '2')],
                [fold('x2', c='2'), fold('y1', '0', '1', '1')]),
            'candidate': layout(
                [sheet('C', '2', '2'), sheet('B', '4', '2'),
                 sheet('A', '6', '4')],
                [fold('x1', c='1'), fold('diag', '1', '1', '3')]),
        }
        result = compare_revisions(payload)
        assert_partition(self, result['regions'])
        total = sum(Fraction(result[key]) for key in (
            'unchanged_area', 'added_area', 'removed_area', 'changed_area'))
        # Independent sum of returned impact polygons.
        impact_area = sum(polygon_area(normalized(z['polygon']))
                          for z in result['regions'])
        returned_impact = sum(Fraction(z['area']) for z in result['regions'])
        self.assertEqual(impact_area, returned_impact)
        self.assertEqual(total - Fraction(result['unchanged_area']),
                         impact_area)
        self.assertGreater(total, 0)

    def test_no_floats_occur_in_serialized_report(self):
        payload = {
            'reference': layout([sheet('A', '4')], [fold('f', c='2')]),
            'candidate': layout([sheet('A', '4')],
                                [fold('g', '1', '1', '3')]),
        }
        result = compare_revisions(payload)
        json.dumps(result)

        # A recursive type check is stronger than searching text.
        def walk(value):
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            else:
                self.assertNotIsInstance(value, float)
        walk(result)


class InvalidInputAndComplexity(unittest.TestCase):
    valid = {
        'reference': layout([sheet('A', '4')]),
        'candidate': layout([sheet('A', '4')]),
    }

    def assert_bad_input(self, payload):
        with self.assertRaises(ValueError) as context:
            compare_revisions(payload)
        self.assertNotIsInstance(context.exception, RevisionComplexityError)

    def test_extra_top_level_field_is_rejected(self):
        self.assert_bad_input({**self.valid, 'extra': True})

    def test_duplicate_identity_is_rejected(self):
        bad = copy.deepcopy(self.valid)
        bad['reference']['sheets'].append(sheet('A', '2'))
        self.assert_bad_input(bad)

    def test_zero_normal_fold_is_rejected(self):
        bad = copy.deepcopy(self.valid)
        bad['candidate']['folds'] = [fold('zero', a='0', b='0', c='1')]
        self.assert_bad_input(bad)

    def test_illegal_coordinate_is_rejected(self):
        bad = copy.deepcopy(self.valid)
        bad['reference']['folds'] = [fold('bad', c='2/4')]
        self.assert_bad_input(bad)

    def test_either_side_invalid_rejects_whole_comparison(self):
        bad_side = copy.deepcopy(self.valid)
        bad_side['candidate'] = {'sheets': [], 'folds': []}
        self.assert_bad_input(bad_side)

    def test_input_is_not_mutated(self):
        payload = copy.deepcopy(self.valid)
        expected = copy.deepcopy(payload)
        compare_revisions(payload)
        self.assertEqual(payload, expected)

    def test_complexity_error_is_distinguishable_and_whole_failure(self):
        self.assertEqual(MAX_REVISION_POLYGONS, 32768)
        complex_layout = {
            'reference': layout(
                [sheet('A', '8', '8')],
                [fold('rx', c='4'), fold('ry', '0', '1', '4')]),
            'candidate': layout(
                [sheet('A', '8', '8')],
                [fold('cx', c='3'), fold('cy', '0', '1', '3')]),
        }
        with mock.patch('paper_layers.revision.MAX_REVISION_POLYGONS', 4):
            with self.assertRaises(RevisionComplexityError) as context:
                compare_revisions(complex_layout)
        self.assertIsInstance(context.exception, ValueError)
        # A normal bad input remains distinguishable from complexity.
        with self.assertRaises(ValueError) as bad:
            compare_revisions({'reference': {}, 'candidate': {}})
        self.assertNotIsInstance(bad.exception, RevisionComplexityError)


class LegacyEntryRegression(unittest.TestCase):
    def test_process_still_reports_baseline_fold(self):
        result = process({
            'sheets': [sheet('S', '4')],
            'folds': [fold('half')],
            'queries': [{'id': 'q', 'point': ['1', '1']}],
        })
        self.assertEqual(result['summary']['layers'], 2)
        self.assertEqual(result['summary']['material_area'], '8')
        self.assertEqual(result['queries'][0]['top']['source_point'],
                         ['3', '1'])

    def test_bonds_entry_still_works(self):
        result = build_bonds({'layout': {
            'sheets': [sheet('A', '4'), sheet('B', '4')],
            'folds': [fold('half')],
        }, 'bonds': []})
        self.assertEqual(len(result['interfaces']), 3)
        self.assertEqual(result['bonds'], [])


if __name__ == '__main__':
    unittest.main()
