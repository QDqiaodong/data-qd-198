"""Acceptance tests for the offline re-verification probe manifest.

The scenarios follow the delivery contract end to end: the optimal probe
manifest is chosen so every version pair keeps three discriminating probes
with any one station down, the self-contained plain-JSON handoff is judged
offline without the layouts, and the 1024-layer handoff fits the byte limit
by sharing repeated answers.  Representative legacy entry-point regression
tests are included.
"""
import copy
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from paper_layers.archive import build_archive
from paper_layers.archive import dumps as archive_dumps
from paper_layers.archive import loads as archive_loads
from paper_layers.archive import rebuild_window
from paper_layers.evidence import package_dumps, package_loads
from paper_layers.manifest import build_manifest
from paper_layers.receiver import Receiver
from paper_layers.recheck import (FORMAT, MAX_BYTES, VERSION, HandoffInputError,
                                  HandoffSizeError, InfeasibleManifestError,
                                  RecheckError, build_handoff, handoff_dumps,
                                  handoff_loads, interpret_handoff)
from paper_layers.repository import SourceRepository
from paper_layers.service import process

ROOT = Path(__file__).resolve().parent.parent


def layout_u():
    """Bottom P 4x2, top Q 4x2, no folds."""
    return {'sheets': [{'id': 'P', 'width': '4', 'height': '2'},
                       {'id': 'Q', 'width': '4', 'height': '2'}],
            'folds': []}


def layout_v():
    """Bottom P 1x2, top Q 4x2, no folds."""
    return {'sheets': [{'id': 'P', 'width': '1', 'height': '2'},
                       {'id': 'Q', 'width': '4', 'height': '2'}],
            'folds': []}


def uv_versions():
    return [{'id': 'U', 'layout': layout_u()},
            {'id': 'V', 'layout': layout_v()}]


def uv_probes(b2_station='B'):
    """Acceptance probe set: a0 is required and sees nothing anywhere."""
    return [
        {'id': 'a0', 'station': 'A', 'point': ['5', '1'], 'cost': 1},
        {'id': 'a1', 'station': 'A', 'point': ['2', '1'], 'cost': 1},
        {'id': 'a2', 'station': 'A', 'point': ['2', '1'], 'cost': 1},
        {'id': 'a3', 'station': 'A', 'point': ['2', '1'], 'cost': 1},
        {'id': 'b1', 'station': 'B', 'point': ['2', '1'], 'cost': 2},
        {'id': 'b2', 'station': b2_station, 'point': ['2', '1'], 'cost': 2},
        {'id': 'c1', 'station': 'C', 'point': ['2', '1'], 'cost': 2},
        {'id': 'c2', 'station': 'C', 'point': ['2', '1'], 'cost': 2},
    ]


def uv_request(**kw):
    return {'versions': uv_versions(), 'probes': uv_probes(**kw),
            'required': ['a0']}


def selected_ids(handoff):
    return [probe['id'] for probe in handoff['probes']]


def expected(handoff, version_id, probe_id):
    """The standard answer of one selected probe for one version."""
    for version in handoff['versions']:
        if version['id'] == version_id:
            return handoff['answers'][version['expected'][probe_id]]
    raise KeyError(version_id)


def total_cost(handoff, probes):
    costs = {probe['id']: probe['cost'] for probe in probes}
    return sum(costs[pid] for pid in selected_ids(handoff))


class ManifestSelection(unittest.TestCase):
    def test_acceptance_manifest_survives_one_station_down(self):
        handoff = build_handoff(uv_request())
        # Checking only the all-stations-online case would accept {a0,a1,a2,a3}
        # (three discriminators at A, cost 4); the station-down rule forces 9.
        self.assertEqual(selected_ids(handoff),
                         ['a0', 'a1', 'a2', 'b1', 'b2', 'c1'])
        self.assertEqual(total_cost(handoff, uv_probes()), 9)
        self.assertEqual(handoff['stations'], ['A', 'B', 'C'])
        self.assertEqual(handoff['format'], FORMAT)
        self.assertEqual(handoff['version'], VERSION)

        # The top hit agrees across versions; the complete lists differ.
        u_hits = expected(handoff, 'U', 'a1')
        v_hits = expected(handoff, 'V', 'a1')
        self.assertEqual(len(u_hits), 2)
        self.assertEqual(len(v_hits), 1)
        self.assertEqual(u_hits[-1], v_hits[-1])
        self.assertNotEqual(u_hits, v_hits)
        self.assertEqual(u_hits[-1], {'layer': 1, 'sheet': 'Q', 'face': 'front',
                                      'source_point': ['2', '1']})
        # The required probe sees an empty list in both versions.
        self.assertEqual(expected(handoff, 'U', 'a0'), [])
        self.assertEqual(expected(handoff, 'V', 'a0'), [])
        # Every selected probe keeps station and exact coordinates.
        by_id = {probe['id']: probe for probe in handoff['probes']}
        self.assertEqual(by_id['b2']['station'], 'B')
        self.assertEqual(by_id['b2']['point'], ['2', '1'])
        self.assertEqual(by_id['a0']['point'], ['5', '1'])

    def test_station_reassignment_changes_the_manifest(self):
        # Same layouts, probes and costs; only b2 moves from B to A.
        handoff = build_handoff(uv_request(b2_station='A'))
        self.assertEqual(selected_ids(handoff),
                         ['a0', 'a1', 'a2', 'b1', 'c1', 'c2'])
        self.assertEqual(total_cost(handoff, uv_probes(b2_station='A')), 9)

    def test_input_permutation_never_changes_the_delivery(self):
        base = handoff_dumps(build_handoff(uv_request()))
        rng = random.Random(20261124)
        for _trial in range(12):
            versions = uv_versions()
            probes = uv_probes()
            rng.shuffle(versions)
            rng.shuffle(probes)
            request = {'versions': versions, 'probes': probes,
                       'required': ['a0']}
            self.assertEqual(handoff_dumps(build_handoff(request)), base)

    def test_input_is_not_modified(self):
        request = uv_request()
        snapshot = copy.deepcopy(request)
        build_handoff(request)
        self.assertEqual(request, snapshot)

    def test_indistinguishable_versions_are_infeasible_not_merged(self):
        # The two versions differ only in the fold step id, so every
        # candidate probe answers identically; they still cannot be merged.
        def layout(fid):
            return {'sheets': [{'id': 'S', 'width': '4', 'height': '2'}],
                    'folds': [{'id': fid, 'a': '1', 'b': '0', 'c': '2'}]}

        probes = [{'id': f'p{i}', 'station': f's{i}', 'point': ['1', '1'],
                   'cost': 1} for i in range(3)]
        request = {'versions': [{'id': 'X', 'layout': layout('f1')},
                                {'id': 'Y', 'layout': layout('f2')}],
                   'probes': probes, 'required': []}
        with self.assertRaises(InfeasibleManifestError) as ctx:
            build_handoff(request)
        self.assertIsInstance(ctx.exception, RecheckError)
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertNotIsInstance(ctx.exception, HandoffSizeError)
        self.assertNotIsInstance(ctx.exception, HandoffInputError)

    def test_build_validation_errors_are_plain_value_error(self):
        def expect_invalid(mutate, message):
            request = uv_request()
            mutate(request)
            with self.assertRaises(ValueError, msg=message) as ctx:
                build_handoff(request)
            self.assertNotIsInstance(ctx.exception, RecheckError, msg=message)

        expect_invalid(lambda r: r.pop('versions'), 'missing field')
        expect_invalid(lambda r: r.update(extra=1), 'extra field')
        expect_invalid(lambda r: r.update(versions=r['versions'][:1]),
                       'fewer than two versions')
        expect_invalid(lambda r: r['versions'].append(
            {'id': 'U', 'layout': layout_v()}), 'duplicate version id')
        expect_invalid(lambda r: r['versions'][0].update(
            layout={'sheets': layout_u()['sheets'], 'folds': [],
                    'queries': []}), 'layout with queries')
        expect_invalid(lambda r: r['versions'][0]['layout'].update(
            folds=[{'id': 'f', 'a': '0', 'b': '0', 'c': '1'}]),
            'zero fold normal')
        expect_invalid(lambda r: r.update(probes=[]), 'no probes')
        expect_invalid(lambda r: r['probes'].append(
            dict(r['probes'][1])), 'duplicate probe id')
        expect_invalid(lambda r: r['probes'][1].update(station='*'),
                       'bad station id')
        expect_invalid(lambda r: r['probes'][1].update(cost=0), 'cost too low')
        expect_invalid(lambda r: r['probes'][1].update(cost=1000001),
                       'cost too high')
        expect_invalid(lambda r: r['probes'][1].update(cost=True),
                       'boolean cost')
        expect_invalid(lambda r: r['probes'][1].update(cost='1'),
                       'string cost')
        expect_invalid(lambda r: r['probes'][1].update(point=['2']),
                       'short point')
        expect_invalid(lambda r: r['probes'][1].update(point=['1/2', '1']),
                       'fraction point is not query syntax')
        expect_invalid(lambda r: r.update(required=['a0', 'a0']),
                       'duplicate required id')
        expect_invalid(lambda r: r.update(required=['zz']),
                       'unknown required id')
        expect_invalid(lambda r: r.update(required='a0'), 'required not array')

        def two_stations(request):
            request['probes'] = [p for p in request['probes']
                                 if p['station'] != 'C']
        expect_invalid(two_stations, 'only two stations')

        def nine_stations(request):
            request['probes'] = [
                {'id': f'p{i}', 'station': f's{i}', 'point': ['2', '1'],
                 'cost': 1} for i in range(9)]
            request['required'] = []
        expect_invalid(nine_stations, 'nine stations')

    def test_oversized_handoff_is_a_size_error_not_a_degraded_list(self):
        # 1024 layers over [0,1]; eight distinct probe points make every
        # selected answer unique, so sharing cannot save the delivery.
        folds = [{'id': f'f{i}', 'a': '1', 'b': '0', 'c': str(c)}
                 for i, c in enumerate([512, 256, 128, 64, 32, 16, 8, 4, 2, 1])]

        def layout(sheet):
            return {'sheets': [{'id': sheet, 'width': '1024', 'height': '1'}],
                    'folds': folds}

        points = ['0.25', '0.75', '0.125', '0.375',
                  '0.625', '0.875', '0.0625', '0.5625']
        probes = [{'id': f'p{i}', 'station': f's{i % 4}', 'point': [x, '0.5'],
                   'cost': 1} for i, x in enumerate(points)]
        request = {'versions': [{'id': 'VP', 'layout': layout('P')},
                                {'id': 'VQ', 'layout': layout('Q')}],
                   'probes': probes, 'required': []}
        with self.assertRaises(HandoffSizeError) as ctx:
            build_handoff(request)
        self.assertIsInstance(ctx.exception, RecheckError)
        self.assertNotIsInstance(ctx.exception, InfeasibleManifestError)
        self.assertNotIsInstance(ctx.exception, HandoffInputError)

    def test_dumps_and_loads_enforce_the_byte_limit(self):
        handoff = build_handoff(uv_request())
        text = handoff_dumps(handoff)
        self.assertLessEqual(len(text.encode('utf-8')), MAX_BYTES)
        self.assertNotIn(' ', text)
        self.assertEqual(handoff_loads(text), handoff)
        self.assertEqual(handoff_loads(text.encode('utf-8')), handoff)

        big = {'format': FORMAT, 'version': VERSION, 'pad': 'x' * MAX_BYTES}
        with self.assertRaises(HandoffSizeError):
            handoff_dumps(big)
        with self.assertRaises(HandoffSizeError):
            handoff_loads('{"pad":"' + 'x' * MAX_BYTES + '"}')
        for bad in ('{"a":1,"a":2}', '{"x":NaN}', '[1,2]', '{broken', 42):
            with self.assertRaises(HandoffInputError, msg=repr(bad)):
                handoff_loads(bad)
        self.assertIsInstance(handoff_loads(text), dict)


class FieldInterpretation(unittest.TestCase):
    def setUp(self):
        # Only the compact JSON text crosses over; no layout is available.
        self.handoff = handoff_loads(handoff_dumps(build_handoff(uv_request())))
        self.u = expected(self.handoff, 'U', 'b1')
        self.v = expected(self.handoff, 'V', 'b1')

    def interpret(self, stopped, readings, handoff=None):
        return interpret_handoff(handoff or self.handoff,
                                 {'stopped': stopped, 'readings': readings})

    def test_station_down_determines_version_and_remeasure_probe(self):
        result = self.interpret('A', [
            {'probe': 'b1', 'hits': copy.deepcopy(self.u)},
            {'probe': 'b2', 'hits': copy.deepcopy(self.u)},
            {'probe': 'c1', 'hits': copy.deepcopy(self.v)}])
        self.assertEqual(result, {'status': 'determined', 'version': 'U',
                                  'remeasure': 'c1'})

    def test_single_reading_leaves_both_versions_possible(self):
        result = self.interpret('A', [
            {'probe': 'b1', 'hits': copy.deepcopy(self.u)}])
        self.assertEqual(result, {'status': 'ambiguous', 'versions': ['U', 'V']})

    def test_empty_lists_everywhere_cannot_be_explained(self):
        result = self.interpret('A', [{'probe': 'b1', 'hits': []},
                                      {'probe': 'b2', 'hits': []},
                                      {'probe': 'c1', 'hits': []}])
        self.assertEqual(result, {'status': 'unexplainable'})

    def test_fully_consistent_readings_need_no_remeasure(self):
        result = self.interpret(None, [
            {'probe': 'a0', 'hits': []},
            {'probe': 'a1', 'hits': copy.deepcopy(self.u)},
            {'probe': 'b1', 'hits': copy.deepcopy(self.u)}])
        self.assertEqual(result, {'status': 'determined', 'version': 'U',
                                  'remeasure': None})

    def test_reassigned_handoff_judges_by_the_new_manifest(self):
        handoff = handoff_loads(handoff_dumps(
            build_handoff(uv_request(b2_station='A'))))
        self.assertEqual(selected_ids(handoff),
                         ['a0', 'a1', 'a2', 'b1', 'c1', 'c2'])
        # b1, c1, c2 readings are accepted and judged by the new manifest.
        result = self.interpret('A', [
            {'probe': 'b1', 'hits': copy.deepcopy(self.u)},
            {'probe': 'c1', 'hits': copy.deepcopy(self.u)},
            {'probe': 'c2', 'hits': copy.deepcopy(self.v)}], handoff=handoff)
        self.assertEqual(result, {'status': 'determined', 'version': 'U',
                                  'remeasure': 'c2'})
        # The old manifest's b2 reading is refused: b2 is not selected here.
        with self.assertRaises(HandoffInputError):
            self.interpret('A', [{'probe': 'b2', 'hits': copy.deepcopy(self.u)}],
                           handoff=handoff)

    def test_legal_misread_is_evidence_not_bad_input(self):
        # A well-formed hit list naming a sheet no candidate layout knows is
        # one misread for every version, never bad input.
        ghost = [{'layer': 0, 'sheet': 'GHOST', 'face': 'front',
                  'source_point': ['0', '0']}]
        result = self.interpret('A', [{'probe': 'b1', 'hits': ghost}])
        self.assertEqual(result, {'status': 'ambiguous', 'versions': ['U', 'V']})

    def test_stopped_station_without_selected_probes_is_legal(self):
        request = uv_request()
        request['probes'].append(
            {'id': 'd1', 'station': 'D', 'point': ['2', '1'], 'cost': 100})
        handoff = handoff_loads(handoff_dumps(build_handoff(request)))
        self.assertNotIn('d1', selected_ids(handoff))
        result = self.interpret('D', [
            {'probe': 'a1', 'hits': copy.deepcopy(self.u)},
            {'probe': 'b1', 'hits': copy.deepcopy(self.u)},
            {'probe': 'b2', 'hits': copy.deepcopy(self.u)},
            {'probe': 'c1', 'hits': copy.deepcopy(self.u)}], handoff=handoff)
        self.assertEqual(result, {'status': 'determined', 'version': 'U',
                                  'remeasure': None})

    def test_interpretation_input_is_not_modified(self):
        request = {'stopped': 'A',
                   'readings': [{'probe': 'b1', 'hits': copy.deepcopy(self.u)}]}
        handoff_snapshot = copy.deepcopy(self.handoff)
        request_snapshot = copy.deepcopy(request)
        interpret_handoff(self.handoff, request)
        self.assertEqual(self.handoff, handoff_snapshot)
        self.assertEqual(request, request_snapshot)

    def test_bad_interpretation_input_is_distinguishable(self):
        def expect_bad(handoff, request, message):
            with self.assertRaises(HandoffInputError, msg=message) as ctx:
                interpret_handoff(handoff, request)
            self.assertIsInstance(ctx.exception, RecheckError)
            self.assertNotIsInstance(ctx.exception, InfeasibleManifestError)
            self.assertNotIsInstance(ctx.exception, HandoffSizeError)

        good_request = {'stopped': 'A', 'readings': []}
        # Unknown format and version.
        bad = copy.deepcopy(self.handoff)
        bad['format'] = 'paper-annotation-archive'
        expect_bad(bad, good_request, 'unknown format')
        bad = copy.deepcopy(self.handoff)
        bad['version'] = 2
        expect_bad(bad, good_request, 'unknown version')
        bad = copy.deepcopy(self.handoff)
        bad['version'] = True
        expect_bad(bad, good_request, 'boolean version')
        # Illegal references inside the handoff.
        bad = copy.deepcopy(self.handoff)
        bad['versions'][0]['expected']['zz'] = 0
        expect_bad(bad, good_request, 'expected references unknown probe')
        bad = copy.deepcopy(self.handoff)
        bad['versions'][0]['expected'].pop('b1')
        expect_bad(bad, good_request, 'expected misses a selected probe')
        bad = copy.deepcopy(self.handoff)
        bad['versions'][0]['expected']['b1'] = len(bad['answers'])
        expect_bad(bad, good_request, 'answer index out of range')
        bad = copy.deepcopy(self.handoff)
        bad['probes'][0]['station'] = 'zzz'
        expect_bad(bad, good_request, 'probe at unknown station')
        bad = copy.deepcopy(self.handoff)
        bad['answers'][0] = [{'layer': 0, 'sheet': 'P', 'face': 'sideways',
                              'source_point': ['2', '1']}]
        expect_bad(bad, good_request, 'bad face inside an answer')
        # Request shape and stopped station.
        expect_bad(self.handoff, {'stopped': 'A'}, 'missing readings')
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [], 'x': 1},
                   'extra request field')
        expect_bad(self.handoff, {'stopped': 'Q9', 'readings': []},
                   'unknown stopped station')
        expect_bad(self.handoff, {'stopped': 9, 'readings': []},
                   'non-string stopped station')
        # Readings: unselected probe, stopped-station probe, duplicates.
        expect_bad(self.handoff, {'stopped': None, 'readings': [
            {'probe': 'a3', 'hits': []}]}, 'a3 is not selected')
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [
            {'probe': 'a1', 'hits': []}]}, 'a1 sits at the stopped station')
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [
            {'probe': 'b1', 'hits': []}, {'probe': 'b1', 'hits': []}]},
            'duplicate reading')
        # Invalid readings (but a matching-format wrong list stays legal).
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [
            {'probe': 'b1', 'hits': [{'layer': 0, 'sheet': 'P', 'face': 'up',
                                      'source_point': ['2', '1']}]}]},
            'bad face in reading')
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [
            {'probe': 'b1', 'hits': [
                {'layer': 1, 'sheet': 'P', 'face': 'front',
                 'source_point': ['2', '1']},
                {'layer': 1, 'sheet': 'Q', 'face': 'front',
                 'source_point': ['2', '1']}]}]}, 'layers not ascending')
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [
            {'probe': 'b1', 'hits': [
                {'layer': 0, 'sheet': 'P', 'face': 'front',
                 'source_point': ['2/4', '1']}]}]}, 'unreduced fraction')
        expect_bad(self.handoff, {'stopped': 'A', 'readings': [
            {'probe': 'b1', 'hits': [
                {'layer': 0, 'sheet': 'P', 'face': 'front',
                 'source_point': ['2', '1'], 'path': []}]}]}, 'extra hit field')


class SizeLimitedHandoff(unittest.TestCase):
    """The 1024-layer acceptance: five probes, shared answers, JSON round trip."""

    @staticmethod
    def folds():
        return [{'id': f'f{i}', 'a': '1', 'b': '0', 'c': str(c)}
                for i, c in enumerate([512, 256, 128, 64, 32, 16, 8, 4, 2, 1])]

    @staticmethod
    def layout(sheet):
        return {'sheets': [{'id': sheet, 'width': '1024', 'height': '1'}],
                'folds': SizeLimitedHandoff.folds()}

    @staticmethod
    def request():
        probes = [{'id': pid, 'station': station, 'point': ['0.5', '0.5'],
                   'cost': 1}
                  for pid, station in [('p1a', 's1'), ('p1b', 's1'),
                                       ('p2a', 's2'), ('p2b', 's2'),
                                       ('p3a', 's3'), ('p3b', 's3')]]
        return {'versions': [{'id': 'VP', 'layout': SizeLimitedHandoff.layout('P')},
                             {'id': 'VQ', 'layout': SizeLimitedHandoff.layout('Q')}],
                'probes': probes, 'required': []}

    def setUp(self):
        self.text = handoff_dumps(build_handoff(self.request()))
        self.handoff = handoff_loads(self.text)

    def test_five_probe_manifest_with_1024_ordered_hits(self):
        self.assertEqual(selected_ids(self.handoff),
                         ['p1a', 'p1b', 'p2a', 'p2b', 'p3a'])
        # Both versions share one answer table entry each: every probe sits
        # at the same point, and the sheet id is the only difference.
        self.assertEqual(len(self.handoff['answers']), 2)
        for version_id, sheet in (('VP', 'P'), ('VQ', 'Q')):
            for pid in selected_ids(self.handoff):
                hits = expected(self.handoff, version_id, pid)
                self.assertEqual(len(hits), 1024)
                self.assertEqual([hit['layer'] for hit in hits],
                                 list(range(1024)))
                self.assertTrue(all(hit['sheet'] == sheet for hit in hits))
        first = expected(self.handoff, 'VP', 'p1a')[0]
        self.assertEqual(first, {'layer': 0, 'sheet': 'P', 'face': 'front',
                                 'source_point': ['1/2', '1/2']})
        self.assertEqual(expected(self.handoff, 'VP', 'p1a')[-1]['layer'], 1023)

    def test_handoff_fits_the_byte_limit_and_round_trips(self):
        payload = self.text.encode('utf-8')
        self.assertLessEqual(len(payload), MAX_BYTES)
        self.assertEqual(handoff_dumps(self.handoff), self.text)

    def test_any_single_station_down_still_determines(self):
        for stopped in ('s1', 's2', 's3'):
            readings = [{'probe': probe['id'],
                         'hits': expected(self.handoff, 'VP', probe['id'])}
                        for probe in self.handoff['probes']
                        if probe['station'] != stopped]
            result = interpret_handoff(
                self.handoff, {'stopped': stopped, 'readings': readings})
            self.assertEqual(result, {'status': 'determined', 'version': 'VP',
                                      'remeasure': None}, msg=stopped)

    def test_same_coordinate_probes_are_distinct_field_records(self):
        # p1a and p1b share station and point but are two measurements:
        # p1a's correct reading never covers p1b's misread.
        readings = [{'probe': 'p1a', 'hits': expected(self.handoff, 'VP', 'p1a')},
                    {'probe': 'p1b', 'hits': expected(self.handoff, 'VQ', 'p1b')},
                    {'probe': 'p2a', 'hits': expected(self.handoff, 'VP', 'p2a')},
                    {'probe': 'p2b', 'hits': expected(self.handoff, 'VP', 'p2b')}]
        result = interpret_handoff(self.handoff,
                                   {'stopped': 's3', 'readings': readings})
        self.assertEqual(result, {'status': 'determined', 'version': 'VP',
                                  'remeasure': 'p1b'})
        # Two readings for the same probe id are bad input, not one record.
        with self.assertRaises(HandoffInputError):
            interpret_handoff(self.handoff, {'stopped': 's3', 'readings': [
                {'probe': 'p1a', 'hits': []}, {'probe': 'p1a', 'hits': []}]})


class LegacyRegression(unittest.TestCase):
    """Representative old entry points keep their behaviour."""

    def test_process_fixture(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = process(data)
        self.assertEqual(result['summary'],
                         {'layers': 2, 'material_area': '8', 'queries': 3})
        top = result['queries'][0]['top']
        self.assertEqual((top['face'], top['source_point']),
                         ('back', ['3', '1']))
        self.assertEqual(len(result['queries'][2]['hits']), 2)

    def test_archive_round_trip(self):
        archive = build_archive({
            'layout': {'sheets': [{'id': 'S', 'width': '4', 'height': '2'}],
                       'folds': [{'id': 'half', 'a': '1', 'b': '0', 'c': '2'}]},
            'batches': [{'id': 'L', 'priority': 1, 'marks': [
                {'id': 'l1', 'code': 'qc-l', 'rect': ['-1', '0', '2', '2']}]}]})
        result = rebuild_window(archive_loads(archive_dumps(archive)), {
            'sheet': 'S', 'face': 'back', 'rect': ['2', '0', '4', '2'],
            'ignore_batch': None})
        self.assertEqual([(r['batch'], r['mark']) for r in result['regions']],
                         [('L', 'l1')])

    def test_manifest_sample(self):
        result = build_manifest({
            'samples': [
                {'id': 's1', 'origin': 'o1', 'channel': 'field',
                 'layout': {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
                            'folds': [{'id': 'f', 'a': '1', 'b': '0', 'c': '2'}]}},
                {'id': 's2', 'origin': None, 'channel': 'lab',
                 'layout': {'sheets': [{'id': 'P', 'width': '3', 'height': '2'}],
                            'folds': []}}],
            'previous': None,
            'quota': {'evaluation_total': 1, 'evaluation_field': 0}})
        self.assertEqual(result, {'train': ['s1'], 'evaluation': ['s2']})

    def test_evidence_package_round_trip(self):
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp)
        repo = SourceRepository.create(Path(temp) / 'source', 'ds')
        repo.ingest({'batch': 'b1',
                     'references': [{'case': 'C', 'revision': 'r1',
                                     'effective_from': 0,
                                     'layout': {'sheets': [
                                         {'id': 'P', 'width': '4',
                                          'height': '2'}], 'folds': []},
                                     'probes': [{'id': 'p',
                                                 'point': ['1', '1']}]}],
                     'observations': [{'id': 'o1', 'case': 'C', 'observed_at': 0,
                                       'probe': 'p', 'hits': []}]})
        package = repo.package(0, 1)
        self.assertEqual(package_loads(package_dumps(package)), package)
        receiver = Receiver.create(Path(temp) / 'receiver', 'ds')
        result = receiver.apply(package_loads(package_dumps(package)))
        self.assertEqual(result['position'], 1)
        self.assertEqual([row['id'] for row in receiver.rows()], ['o1'])
        receiver.close()
        repo.close()


if __name__ == '__main__':
    unittest.main()
