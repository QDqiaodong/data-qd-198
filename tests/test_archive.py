import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from paper_layers.archive import build_archive, dumps, loads, rebuild_window, serialize_window
from paper_layers.folding import fold_stack
from paper_layers.polygon import area, encloses, intersect, normalize
from paper_layers.service import process

ROOT = Path(__file__).resolve().parent.parent


def exchange(**layout):
    return {'layout': layout, 'batches': []}


def mark(ident, code, rect):
    return {'id': ident, 'code': code, 'rect': rect}


def request(sheet, face, rect, ignore=None):
    return {'sheet': sheet, 'face': face, 'rect': rect, 'ignore_batch': ignore}


def exact(value):
    return Fraction(value)


def polygons(result):
    return [[(exact(x), exact(y)) for x, y in r['polygon']] for r in result['regions']]


def total_area(result):
    return sum((area(p) for p in polygons(result)), Fraction(0))


def assert_region(test, result, expected):
    """Accept a split covering that is exactly ``expected`` with no extra area."""
    parts = polygons(result)
    expected = normalize([(exact(x), exact(y)) for x, y in expected])
    for part in parts:
        test.assertTrue(encloses(expected, part), f'{part} escapes {expected}')
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            test.assertEqual(area(intersect(parts[i], parts[j])), 0)
    test.assertEqual(total_area(result), area(expected))


HALF = [{'id': 'half', 'a': '1', 'b': '0', 'c': '2'}]
L_BATCH = {'id': 'L', 'priority': 1,
           'marks': [mark('l1', 'qc-l', ['-1', '0', '2', '2'])]}
H_BATCH = {'id': 'H', 'priority': 9,
           'marks': [mark('h1', 'qc-h', ['-0.5', '0.5', '1', '1.5'])]}


def layered(narrow=False):
    sheets = [{'id': 'S', 'width': '3' if narrow else '4', 'height': '2'},
              {'id': 'C', 'width': '5', 'height': '2'}]
    return {'layout': {'sheets': sheets, 'folds': HALF},
            'batches': [copy.deepcopy(L_BATCH), copy.deepcopy(H_BATCH)]}


class ArchiveSamples(unittest.TestCase):
    def test_layered_handoff_round_trip(self):
        text = dumps(build_archive(layered()))
        # A new process only receives this JSON text, never the layout input.
        archive = loads(text)

        back_s = rebuild_window(archive, request('S', 'back', ['2', '0', '4', '2']))
        self.assertEqual({(r['batch'], r['mark'], r['code']) for r in back_s['regions']},
                         {('H', 'h1', 'qc-h'), ('L', 'l1', 'qc-l')})
        h = [r for r in back_s['regions'] if r['batch'] == 'H']
        self.assertEqual(len(h), 1)
        assert_region(self, {'regions': h}, [(3, '1/2'), (4, '1/2'), (4, '3/2'), (3, '3/2')])
        assert_region(self, back_s, [(2, 0), (4, 0), (4, 2), (2, 2)])

        back_c = rebuild_window(archive, request('C', 'back', ['4', '0', '5', '2']))
        h = [r for r in back_c['regions'] if r['batch'] == 'H']
        assert_region(self, {'regions': h}, [(4, '1/2'), ('9/2', '1/2'), ('9/2', '3/2'), (4, '3/2')])
        assert_region(self, back_c, [(4, 0), (5, 0), (5, 2), (4, 2)])

        self.assertEqual(rebuild_window(archive, request('C', 'back', ['2', '0', '4', '2']))['regions'], [])
        self.assertEqual(rebuild_window(archive, request('S', 'front', ['0', '0', '4', '2']))['regions'], [])

        # Retracting H restores the full L window; the hidden part of C stays empty.
        ignored = rebuild_window(archive, request('S', 'back', ['2', '0', '4', '2'], 'H'))
        self.assertEqual([(r['batch'], r['mark']) for r in ignored['regions']], [('L', 'l1')])
        assert_region(self, ignored, [(2, 0), (4, 0), (4, 2), (2, 2)])
        ignored_c = rebuild_window(archive, request('C', 'back', ['4', '0', '5', '2'], 'H'))
        assert_region(self, ignored_c, [(4, 0), (5, 0), (5, 2), (4, 2)])
        self.assertEqual(rebuild_window(archive, request('C', 'back', ['2', '0', '4', '2'], 'H'))['regions'], [])
        # ... and not ignoring H again yields the original assignment.
        again = rebuild_window(archive, request('S', 'back', ['2', '0', '4', '2']))
        self.assertEqual(serialize_window(again), serialize_window(back_s))

    def test_narrow_bottom_sheet_moves_h(self):
        archive = loads(dumps(build_archive(layered(narrow=True))))
        h_c = rebuild_window(archive, request('C', 'back', ['3', '0', '4.5', '1.5']))
        h = [r for r in h_c['regions'] if r['batch'] == 'H']
        assert_region(self, {'regions': h}, [(3, '1/2'), ('4.5', '1/2'), ('4.5', '3/2'), (3, '3/2')])
        s_back = rebuild_window(archive, request('S', 'back', ['2', '0', '3', '2']))
        self.assertEqual({r['batch'] for r in s_back['regions']}, {'L'})

    def test_diagonal_fold_region_is_not_its_bbox(self):
        data = {'layout': {'sheets': [{'id': 'P', 'width': '2', 'height': '2'}],
                           'folds': [{'id': 'd', 'a': '1', 'b': '1', 'c': '2'}]},
                'batches': [{'id': 'L', 'priority': 1,
                             'marks': [mark('m', 'k', ['0', '0', '1.5', '1.5'])]}]}
        archive = loads(dumps(build_archive(data)))
        back = rebuild_window(archive, request('P', 'back', ['0', '0', '2', '2']))
        expected = [(2, 2), (2, '1/2'), ('3/2', '1/2'), ('1/2', '3/2'), ('1/2', 2)]
        assert_region(self, back, expected)
        self.assertNotEqual(total_area(back), 2)  # not the bounding rectangle
        self.assertEqual(rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2']))['regions'], [])

    def test_cross_strip_delivery(self):
        n = 1024
        layout = {'sheets': [{'id': 'P', 'width': '2048', 'height': '2048'}], 'folds': []}
        low = {'id': 'L', 'priority': 1,
               'marks': [mark(f'l{i}', 'L', ['0', str(2 * i), '2048', str(2 * i + 1)]) for i in range(n)]}
        high = {'id': 'H', 'priority': 2,
                'marks': [mark(f'h{j}', 'H', [str(2 * j), '0', str(2 * j + 1), '2048']) for j in range(n)]}
        text = dumps(build_archive({'layout': layout, 'batches': [low, high]}))
        self.assertLessEqual(len(text.encode('utf-8')), 8000000)
        archive = loads(text)  # only the package is needed from here on

        first = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2']))
        assert_region(self, {'regions': [r for r in first['regions'] if r['batch'] == 'H']},
                      [(0, 0), (1, 0), (1, 2), (0, 2)])
        assert_region(self, {'regions': [r for r in first['regions'] if r['batch'] == 'L']},
                      [(1, 0), (2, 0), (2, 1), (1, 1)])
        # H covers [0,1] x [0,2]; L adds [1,2] x [0,1].  The rest of the window
        # has no mark at all, so it must not be filled with a bounding box.
        self.assertEqual(total_area(first), 3)
        ignored = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2'], 'H'))
        assert_region(self, {'regions': [r for r in ignored['regions'] if r['batch'] == 'L']},
                      [(0, 0), (2, 0), (2, 1), (0, 1)])
        self.assertEqual({r['batch'] for r in ignored['regions']}, {'L'})

        last = rebuild_window(archive, request('P', 'front', ['2046', '2046', '2048', '2048']))
        h = [r for r in last['regions'] if r['batch'] == 'H']
        l = [r for r in last['regions'] if r['batch'] == 'L']
        self.assertEqual(len(h), 1)
        self.assertEqual(len(l), 1)
        self.assertEqual(h[0]['mark'], 'h1023')
        self.assertEqual(l[0]['mark'], 'l1023')
        assert_region(self, {'regions': h}, [(2046, 2046), (2047, 2046), (2047, 2048), (2046, 2048)])
        assert_region(self, {'regions': l}, [(2047, 2046), (2048, 2046), (2048, 2047), (2047, 2047)])
        last_ignored = rebuild_window(archive, request('P', 'front', ['2046', '2046', '2048', '2048'], 'H'))
        assert_region(self, last_ignored, [(2046, 2046), (2048, 2046), (2048, 2047), (2046, 2047)])

    def test_fresh_process_reads_only_the_package(self):
        text = dumps(build_archive(layered()))
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'package.json'
            package.write_text(text, encoding='utf-8')
            script = (
                'import json,sys;'
                f'sys.path.insert(0,{str(ROOT / "src")!r});'
                'from paper_layers.archive import loads,rebuild_window;'
                f'archive=loads(open({str(package)!r},encoding="utf-8").read());'
                'print(json.dumps(rebuild_window(archive,{"sheet":"S","face":"back",'
                '"rect":["2","0","4","2"],"ignore_batch":"H"}),ensure_ascii=False,sort_keys=True))'
            )
            run = subprocess.run([sys.executable, '-I', '-c', script], cwd=tmp,
                                 capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout),
                         json.loads(json.dumps(rebuild_window(loads(text),
                                                              request('S', 'back', ['2', '0', '4', '2'], 'H')),
                                               ensure_ascii=False, sort_keys=True)))


class PriorityRules(unittest.TestCase):
    def archive(self, batches):
        return loads(dumps(build_archive({'layout': {'sheets': [{'id': 'P', 'width': '2', 'height': '2'}],
                                          'folds': []},
                         'batches': batches})))

    def test_priority_beats_batch_order(self):
        archive = self.archive([
            {'id': 'Z', 'priority': 1, 'marks': [mark('m', 'low', ['0', '0', '1', '1'])]},
            {'id': 'A', 'priority': 7, 'marks': [mark('m', 'high', ['0', '0', '1', '1'])]},
        ])
        result = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2']))
        self.assertEqual([(r['batch'], r['mark'], r['code']) for r in result['regions']], [('A', 'm', 'high')])
        self.assertEqual(rebuild_window(archive, request('P', 'front', ['1', '0', '2', '1'], 'A'))['regions'], [])
        restored = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2'], 'A'))
        self.assertEqual([(r['batch'], r['mark']) for r in restored['regions']], [('Z', 'm')])

    def test_priority_tie_uses_smallest_batch_then_mark(self):
        archive = self.archive([
            {'id': 'B', 'priority': 5, 'marks': [mark('m', 'b', ['0', '0', '1', '1'])]},
            {'id': 'A', 'priority': 5, 'marks': [mark('m', 'a', ['0', '0', '1', '1'])]},
        ])
        result = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2']))
        self.assertEqual([(r['batch'], r['code']) for r in result['regions']], [('A', 'a')])

        marks = self.archive([
            {'id': 'A', 'priority': 5,
             'marks': [mark('b', 'second', ['0', '0', '1', '1']),
                       mark('a', 'first', ['0', '0', '1', '1'])]},
        ])
        tie = rebuild_window(marks, request('P', 'front', ['0', '0', '2', '2']))
        self.assertEqual([(r['mark'], r['code']) for r in tie['regions']], [('a', 'first')])
        self.assertEqual(rebuild_window(marks, request('P', 'front', ['0', '0', '2', '2'], 'A'))['regions'], [])

    def test_zero_area_contact_emits_nothing(self):
        archive = self.archive([
            {'id': 'A', 'priority': 1, 'marks': [mark('a', 'a', ['0', '0', '1', '1'])]},
            {'id': 'B', 'priority': 1, 'marks': [mark('b', 'b', ['1', '0', '2', '1'])]},
        ])
        full = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '1']))
        self.assertEqual({(r['batch'], r['mark']) for r in full['regions']}, {('A', 'a'), ('B', 'b')})
        self.assertEqual(total_area(full), 2)
        # Sharing only the boundary x=1 with the window yields no region.
        self.assertEqual(rebuild_window(archive, request('P', 'front', ['1', '0', '2', '1'], 'B'))['regions'], [])
        # Touching a mark only at a corner yields no region.
        self.assertEqual(rebuild_window(archive, request('P', 'front', ['1', '1', '2', '2'], 'B'))['regions'], [])

    def test_regions_are_disjoint_and_stable(self):
        archive = self.archive([
            {'id': 'A', 'priority': 5, 'marks': [mark('a', 'a', ['0', '0', '1.2', '1.2'])]},
            {'id': 'B', 'priority': 5, 'marks': [mark('b', 'b', ['0.4', '0.4', '1.6', '1.6'])]},
            {'id': 'C', 'priority': 1, 'marks': [mark('c', 'c', ['0', '0', '2', '2'])]},
        ])
        result = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2']))
        parts = polygons(result)
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                self.assertEqual(area(intersect(parts[i], parts[j])), 0)
        self.assertEqual(total_area(result), 4)
        self.assertEqual(serialize_window(result),
                         serialize_window(rebuild_window(loads(dumps(archive)),
                                                         request('P', 'front', ['0', '0', '2', '2']))))

    def test_reordering_batches_and_marks_is_stable(self):
        data = layered()
        base = loads(dumps(build_archive(data)))
        shuffled = copy.deepcopy(data)
        random.Random(11).shuffle(shuffled['batches'])
        for batch in shuffled['batches']:
            random.Random(12).shuffle(batch['marks'])
        other = loads(dumps(build_archive(shuffled)))
        for sheet, face, rect, ignore in [
            ('S', 'back', ['2', '0', '4', '2'], None),
            ('S', 'back', ['2', '0', '4', '2'], 'H'),
            ('C', 'back', ['4', '0', '5', '2'], 'L'),
            ('S', 'front', ['0', '0', '4', '2'], None),
        ]:
            self.assertEqual(serialize_window(rebuild_window(base, request(sheet, face, rect, ignore))),
                             serialize_window(rebuild_window(other, request(sheet, face, rect, ignore))))

    def test_mark_parts_go_to_their_own_sheet_and_face(self):
        archive = loads(dumps(build_archive(layered())))
        self.assertEqual(rebuild_window(archive, request('C', 'front', ['0', '0', '5', '2']))['regions'], [])
        self.assertEqual(rebuild_window(archive, request('S', 'front', ['0', '0', '4', '2']))['regions'], [])
        result = rebuild_window(archive, request('S', 'back', ['2', '0', '4', '2']))
        self.assertEqual((result['sheet'], result['face']), ('S', 'back'))
        self.assertEqual(result['rect'], [2, 0, 4, 2])

    def test_mark_outside_paper_is_discarded(self):
        archive = self.archive([
            {'id': 'A', 'priority': 1, 'marks': [
                mark('off', 'off', ['-5', '-5', '-1', '-1']),
                mark('part', 'part', ['-1', '-1', '1', '1'])]},
        ])
        result = rebuild_window(archive, request('P', 'front', ['0', '0', '2', '2']))
        self.assertEqual([r['mark'] for r in result['regions']], ['part'])
        assert_region(self, result, [(0, 0), (1, 0), (1, 1), (0, 1)])

    def test_empty_inputs(self):
        archive = loads(dumps(build_archive(exchange(
            sheets=[{'id': 'P', 'width': '2', 'height': '2'}], folds=[]))))
        self.assertEqual(archive['regions'], [])
        self.assertEqual(archive['batches'], [])
        self.assertEqual(rebuild_window(archive, request('P', 'back', ['0', '0', '2', '2']))['regions'], [])
        with_marks = self.archive([{'id': 'A', 'priority': 0, 'marks': []}])
        self.assertEqual(rebuild_window(with_marks, request('P', 'back', ['0', '0', '2', '2']))['regions'], [])


class ArchivedValidation(unittest.TestCase):
    def test_generator_rejects_bad_input(self):
        base = lambda **kw: {'layout': {'sheets': [{'id': 'P', 'width': '2', 'height': '2'}], 'folds': []},
                             'batches': [], **kw}
        cases = [
            {'layout': {'sheets': [{'id': 'P', 'width': '2', 'height': '2'}], 'folds': []}},
            {'layout': {'sheets': [{'id': 'P', 'width': '2', 'height': '2'}], 'folds': [], 'queries': []},
             'batches': []},
            base(extra=1),
            base(batches=[{'id': 'A', 'priority': 0, 'marks': []},
                          {'id': 'A', 'priority': 1, 'marks': []}]),
            base(batches=[{'id': 'A', 'priority': -1, 'marks': []}]),
            base(batches=[{'id': 'A', 'priority': 0, 'marks': [
                mark('m', 'c', ['0', '0', '1', '1']), mark('m', 'c', ['0', '0', '1', '1'])]}]),
            base(batches=[{'id': 'A', 'priority': 0, 'marks': [mark('m', 'c', ['0', '0', '0', '1'])]}]),
            base(batches=[{'id': 'A', 'priority': 0, 'marks': [mark('m', 'c', ['0', '1', '1', '1'])]}]),
            base(batches=[{'id': 'A', 'priority': 0, 'marks': [{'id': 'm', 'code': 'c',
                                                               'rect': ['0', '0', '1', '1'], 'x': 1}]}]),
            base(batches=[{'id': f'b{i}', 'priority': 0, 'marks': []} for i in range(65)]),
            base(batches=[{'id': 'A', 'priority': 0, 'marks': [
                mark(f'm{i}', 'c', ['0', '0', '1', '1']) for i in range(4097)]}]),
            {'layout': {'sheets': [{'id': 'P', 'width': '0', 'height': '2'}], 'folds': []}, 'batches': []},
        ]
        for data in cases:
            with self.assertRaises(ValueError):
                build_archive(data)

    def test_rebuild_rejects_bad_requests(self):
        archive = loads(dumps(build_archive(layered())))
        bad = [
            request('Q', 'back', ['2', '0', '3', '2']),
            request('S', 'top', ['2', '0', '3', '2']),
            request('S', 'back', ['2', '0', '5', '2']),
            request('S', 'back', ['-1', '0', '3', '2']),
            request('S', 'back', ['3', '0', '2', '2']),
            request('S', 'back', ['2', '0', '3', '2'], 'nope'),
            {'sheet': 'S', 'face': 'back', 'rect': ['2', '0', '3', '2']},
            {'sheet': 'S', 'face': 'back', 'rect': ['2', '0', '3', '2'],
             'ignore_batch': None, 'extra': True},
            {'sheet': 'S', 'face': 'back', 'rect': ['2', '0', '3'], 'ignore_batch': None},
        ]
        for req in bad:
            with self.assertRaises(ValueError, msg=str(req)):
                rebuild_window(archive, req)

    def test_rebuild_rejects_bad_archives(self):
        archive = loads(dumps(build_archive(layered())))
        for mutate in [
            lambda a: a.update(version=2),
            lambda a: a.update(format='something-else'),
            lambda a: a.pop('version'),
            lambda a: a.pop('regions'),
            lambda a: a.update(extra=1),
            lambda a: a['regions'][0].update(sheet='Q'),
            lambda a: a['regions'][0].update(face='top'),
            lambda a: a['regions'][0].update(batch='Q'),
            lambda a: a['regions'][0].update(polygon=[[0, 0], [1, 0], [2, 0]]),
            lambda a: a['regions'][0].update(polygon=[[0, 0], [-1, 0], [-1, 1]]),
            lambda a: a['regions'][0].update(polygon=[[0, 0], [1, 0]]),
            lambda a: a['regions'][0].update(extra=1),
        ]:
            broken = copy.deepcopy(archive)
            mutate(broken)
            with self.assertRaises(ValueError):
                rebuild_window(broken, request('S', 'back', ['2', '0', '4', '2']))

    def test_json_reader_rejects_duplicates_and_non_objects(self):
        with self.assertRaises(ValueError):
            loads('{"format":"a","format":"b"}')
        with self.assertRaises(ValueError):
            loads('NaN')
        archive = loads(dumps(build_archive(layered())))
        with self.assertRaises(ValueError):
            rebuild_window([], request('S', 'back', ['2', '0', '3', '2']))
        broken = copy.deepcopy(archive)
        broken['regions'][0]['polygon'][0][0] = 0.5
        with self.assertRaises(ValueError):
            rebuild_window(broken, request('S', 'back', ['2', '0', '4', '2']))

    def test_oversized_package_is_refused(self):
        from paper_layers import archive as module
        big = {'format': module.FORMAT, 'version': module.VERSION,
               'sheets': [{'id': 'P', 'width': 1, 'height': 1}],
               'batches': [], 'regions': [{'sheet': 'P', 'face': 'front', 'batch': 'A',
                                           'mark': 'm', 'code': 'c',
                                           'polygon': [[0, 0], [1, 0], [0, 1]]}] * 100000}
        with self.assertRaises(ValueError):
            module.dumps(big)
        with self.assertRaises(ValueError):
            module._check_size(big)
        with self.assertRaises(ValueError):
            module.loads(' ' * (module.MAX_BYTES + 1))
        # Non-plain objects cannot be serialised into a delivery package.
        with self.assertRaises(ValueError):
            module.dumps({**big, 'regions': [{'polygon': object()}]})


class LegacyCompatibility(unittest.TestCase):
    def test_process_and_query_unchanged(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = process(data)
        self.assertEqual(result['summary'], {'layers': 2, 'material_area': '8', 'queries': 3})
        self.assertEqual(result['queries'][0]['top']['source_point'], ['3', '1'])
        self.assertEqual(result['queries'][0]['top']['face'], 'back')
        self.assertEqual(result['queries'][1]['hits'], [])
        self.assertEqual(len(result['queries'][2]['hits']), 2)  # closed-crease multiplicity

    def test_layout_parser_accepts_legacy_shape_only(self):
        sheets, folds = __import__('paper_layers.model', fromlist=['layout_parts']).layout_parts(
            {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
             'folds': [{'id': 'f', 'a': '1', 'b': '0', 'c': '2'}]})
        self.assertEqual([s[0] for s in sheets], ['P'])
        self.assertEqual([f[0] for f in folds], ['f'])
        layers, total, history = fold_stack(sheets, folds)
        self.assertEqual((len(layers), str(total), len(history)), (2, '8', 1))


if __name__ == '__main__':
    unittest.main()
