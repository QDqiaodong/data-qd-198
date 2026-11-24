"""Acceptance tests for the layout-regression evidence repository.

The scenarios follow the delivery contract end to end: late reference
revisions re-attribute already-ingested observations, increment packages are
the deterministic net change between two commits, an evidence-only receiver
rebuilds itself solely from plain-JSON packages, and every failure mode
rejects atomically.  Real JSON round trips, repository/receiver reopen and a
legacy ``process`` entry-point call are included.
"""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from paper_layers.evidence import (package_dumps, package_loads)
from paper_layers.receiver import (Receiver, ReceiverConflictError,
                                   ReceiverError, ReceiverInputError,
                                   ReceiverPositionError)
from paper_layers.repository import (BatchConflictError, RepositoryError,
                                     RepositoryRangeError, SourceRepository)
from paper_layers.service import process

EDGE_POINT = ['2', '1']
DOUBLE_HITS = [
    {'layer': 0, 'sheet': 'P', 'face': 'front', 'source_point': ['2', '1']},
    {'layer': 1, 'sheet': 'P', 'face': 'back', 'source_point': ['2', '1']},
]


def fold(fid):
    return {'id': fid, 'a': '1', 'b': '0', 'c': '2'}


def layout(fid):
    return {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
            'folds': [fold(fid)]}


def flat_layout():
    return {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
            'folds': []}


def reference(revision, effective_from, lay, probes):
    return {'case': 'C', 'revision': revision,
            'effective_from': effective_from, 'layout': lay,
            'probes': list(probes)}


def edge_probes():
    return [{'id': 'edge', 'point': EDGE_POINT}]


def observation(obs_id, observed_at, hits):
    return {'id': obs_id, 'case': 'C', 'observed_at': observed_at,
            'probe': 'edge', 'hits': hits}


def batch(bid, references=(), observations=()):
    return {'batch': bid, 'references': list(references),
            'observations': list(observations)}


def round_trip(package):
    """Plain-JSON handoff as it would arrive in a new offline process."""
    return package_loads(package_dumps(package))


def row_map(rows):
    return {row['id']: row for row in rows}


class EvidenceAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp)
        self.source_dir = Path(self.temp) / 'source'
        self.receiver_dir = Path(self.temp) / 'receiver'

    def build_first_commit(self):
        """Acceptance case C: r1@0 folded, r3@30 flat, observations at 15/25/35."""
        repo = SourceRepository.create(self.source_dir, 'dataset-c')
        first = batch(
            'b1',
            references=[
                reference('r1', 0, layout('f1'), edge_probes()),
                reference('r3', 30, flat_layout(), edge_probes()),
            ],
            observations=[
                observation('o15', 15, copy.deepcopy(DOUBLE_HITS)),
                observation('o25', 25, copy.deepcopy(DOUBLE_HITS)),
                observation('o35', 35, copy.deepcopy(DOUBLE_HITS)),
            ])
        self.assertEqual(repo.ingest(first), 1)
        self.assertEqual(repo.head(), 1)
        return repo, first

    def test_first_commit_attribution_and_double_hits(self):
        repo, _first = self.build_first_commit()
        package = repo.package(0, 1)
        self.assertEqual(
            (package['dataset'], package['after'], package['through']),
            ('dataset-c', 0, 1))
        self.assertEqual([c['id'] for c in package['changes']],
                         ['o15', 'o25', 'o35'])
        rows = {change['id']: change['after'] for change in package['changes']}
        for change in package['changes']:
            self.assertIsNone(change['before'])
        # Legacy process is the answer source: folded crease gives two hits,
        # the flat sheet only one.
        folded = process({'sheets': layout('f1')['sheets'],
                          'folds': layout('f1')['folds'],
                          'queries': edge_probes()})
        self.assertEqual([
            {key: hit[key] for key in ('layer', 'sheet', 'face', 'source_point')}
            for hit in folded['queries'][0]['hits']], DOUBLE_HITS)
        self.assertEqual(rows['o15']['revision'], 'r1')
        self.assertEqual(rows['o25']['revision'], 'r1')
        self.assertEqual(rows['o35']['revision'], 'r3')
        self.assertTrue(rows['o15']['matches'])
        self.assertTrue(rows['o25']['matches'])
        self.assertFalse(rows['o35']['matches'])
        # r3 has the probe but the flat sheet gives one hit, not two.
        self.assertEqual(len(rows['o35']['expected_hits']), 1)
        self.assertEqual(rows['o15']['expected_hits'], DOUBLE_HITS)
        repo.close()

    def test_late_revisions_replace_delete_and_reinsert(self):
        repo, _first = self.build_first_commit()
        receiver = Receiver.create(self.receiver_dir, 'dataset-c')
        first_package = round_trip(repo.package(0, 1))
        result = receiver.apply(first_package)
        self.assertEqual(result,
                         {'dataset': 'dataset-c', 'after': 0, 'through': 1,
                          'changes': 3, 'position': 1})
        self.assertEqual(receiver.position(), 1)
        saved_positions = receiver.position()
        saved_rows = receiver.rows()
        self.assertEqual([r['id'] for r in saved_rows], ['o15', 'o25', 'o35'])

        # Commit 2: r2@20 with the same layout/probes as r1 (new fold id).
        self.assertEqual(repo.ingest(batch(
            'b2', references=[reference('r2', 20, layout('f2'), edge_probes())])), 2)
        package_12 = repo.package(1, 2)
        self.assertEqual([c['id'] for c in package_12['changes']], ['o25'])
        change = package_12['changes'][0]
        self.assertEqual(change['before']['revision'], 'r1')
        self.assertEqual(change['after']['revision'], 'r2')
        self.assertEqual(change['before']['expected_hits'],
                         change['after']['expected_hits'])
        self.assertTrue(change['after']['matches'])
        receiver.apply(round_trip(package_12))
        rows = row_map(receiver.rows())
        self.assertEqual(rows['o25']['revision'], 'r2')
        self.assertTrue(rows['o25']['matches'])
        self.assertEqual(rows['o15']['revision'], 'r1')
        self.assertEqual(rows['o35']['revision'], 'r3')

        # Commit 3: r4@23 without the edge probe deletes o25's evidence row.
        self.assertEqual(repo.ingest(batch(
            'b3', references=[reference('r4', 23, layout('f3'), [])])), 3)
        package_23 = repo.package(2, 3)
        self.assertEqual(package_23['changes'],
                         [{'id': 'o25',
                           'before': rows['o25'], 'after': None}])
        receiver.apply(round_trip(package_23))
        self.assertNotIn('o25', receiver)
        self.assertIsNone(receiver.row('o25'))
        self.assertEqual(receiver.position(), 3)
        # o15/o35 never moved.
        self.assertEqual(row_map(receiver.rows())['o15']['revision'], 'r1')
        self.assertEqual(row_map(receiver.rows())['o35']['revision'], 'r3')

        # Commit 4: r5@24 restores edge; o25 returns without re-sending the
        # observation, and is attributed straight to r5.
        self.assertEqual(repo.ingest(batch(
            'b4', references=[reference('r5', 24, layout('f4'), edge_probes())])), 4)
        package_34 = repo.package(3, 4)
        self.assertEqual(len(package_34['changes']), 1)
        change = package_34['changes'][0]
        self.assertEqual(change['id'], 'o25')
        self.assertIsNone(change['before'])
        self.assertEqual(change['after']['revision'], 'r5')
        self.assertTrue(change['after']['matches'])
        receiver.apply(round_trip(package_34))
        self.assertEqual(receiver.row('o25')['revision'], 'r5')
        self.assertEqual(receiver.position(), 4)
        receiver.close()

        # A second receiver only ever sees segment 1->4: one r1->r5
        # replacement, never the intermediate delete/reinsert.
        direct = Receiver.create(Path(self.temp) / 'direct', 'dataset-c')
        direct.apply(round_trip(repo.package(0, 1)))
        package_14 = repo.package(1, 4)
        self.assertEqual([c['id'] for c in package_14['changes']], ['o25'])
        change = package_14['changes'][0]
        self.assertEqual(change['before']['revision'], 'r1')
        self.assertEqual(change['after']['revision'], 'r5')
        self.assertIsNotNone(change['before'])
        self.assertIsNotNone(change['after'])
        direct.apply(round_trip(package_14))
        self.assertEqual(direct.position(), 4)

        receiver = Receiver.open(self.receiver_dir)
        self.assertEqual(receiver.position(), 4)
        self.assertEqual(receiver.rows(), direct.rows())
        self.assertEqual(row_map(receiver.rows())['o15']['revision'], 'r1')
        self.assertEqual(row_map(receiver.rows())['o35']['revision'], 'r3')
        # Earlier saved state is still exactly what the first apply stored.
        self.assertEqual(saved_positions, 1)
        self.assertEqual(len(saved_rows), 3)
        receiver.close()
        direct.close()
        repo.close()

        # Reopening the source and re-fetching 1->2 yields the original
        # r1->r2-only package byte for byte.
        repo = SourceRepository.open(self.source_dir)
        self.assertEqual(repo.package(1, 2), package_12)
        repo.close()

    def test_tampered_before_rejects_whole_multi_change_package(self):
        repo, _first = self.build_first_commit()
        repo.ingest(batch(
            'b2', references=[reference('r2', 20, layout('f2'), edge_probes())]))
        repo.ingest(batch(
            'b3', references=[reference('r4', 23, layout('f3'), [])]))
        repo.ingest(batch(
            'b4', references=[reference('r5', 24, layout('f4'), edge_probes())]))
        # Two observations of a brand-new case arrive before any reference, so
        # they materialise nothing at commit 5.
        new_observations = [
            {'id': 'd1', 'case': 'D', 'observed_at': 0, 'probe': 'p',
             'hits': []},
            {'id': 'd2', 'case': 'D', 'observed_at': 1, 'probe': 'p',
             'hits': []},
        ]
        self.assertEqual(repo.ingest(batch('b5', observations=new_observations)), 5)

        receiver = Receiver.create(self.receiver_dir, 'dataset-c')
        receiver.apply(round_trip(repo.package(0, 4)))
        self.assertEqual(receiver.position(), 4)

        # The late reference makes both observations visible at once: a
        # genuinely multi-change package starting at the receiver position.
        new_ref = {'case': 'D', 'revision': 'd1', 'effective_from': 0,
                   'layout': flat_layout(),
                   'probes': [{'id': 'p', 'point': ['0', '0']}]}
        self.assertEqual(repo.ingest(batch('b6', references=[new_ref])), 6)
        package = round_trip(repo.package(4, 6))
        self.assertEqual([c['id'] for c in package['changes']], ['d1', 'd2'])
        tampered = copy.deepcopy(package)
        last = tampered['changes'][-1]
        # d2 did not exist locally, so a legally shaped non-null before row
        # clashes with the local absence.
        last['before'] = copy.deepcopy(last['after'])
        last['before']['revision'] = 'r1'
        before_rows = receiver.rows()
        before_position = receiver.position()
        with self.assertRaises(ReceiverConflictError):
            receiver.apply(tampered)
        # Nothing landed: rows and position are untouched.
        self.assertEqual(receiver.position(), before_position)
        self.assertEqual(receiver.rows(), before_rows)

        # The untampered package then succeeds and advances.
        result = receiver.apply(round_trip(package))
        self.assertEqual(result['position'], 6)
        self.assertEqual(receiver.position(), 6)
        receiver.close()

        # Reopen and re-send the same package: one success, no redo, no move.
        receiver = Receiver.open(self.receiver_dir)
        again = receiver.apply(round_trip(package))
        self.assertEqual(again, result)
        self.assertEqual(receiver.position(), 6)
        receiver.close()
        repo.close()

    def test_empty_package_advances_and_cannot_be_skipped(self):
        repo, _first = self.build_first_commit()
        # A reference nobody observes yet: the following package must be empty.
        unseen = {'case': 'Z', 'revision': 'z1', 'effective_from': 0,
                  'layout': flat_layout(), 'probes': []}
        self.assertEqual(repo.ingest(batch('b-empty', [unseen])), 2)
        empty_package = repo.package(1, 2)
        self.assertEqual(empty_package['changes'], [])

        ahead = Receiver.create(Path(self.temp) / 'ahead', 'dataset-c')
        ahead.apply(round_trip(repo.package(0, 1)))
        result = ahead.apply(round_trip(empty_package))
        self.assertEqual(result['changes'], 0)
        self.assertEqual(ahead.position(), 2)
        # Replaying the empty package does not advance again.
        replay = ahead.apply(round_trip(empty_package))
        self.assertEqual(replay, result)
        self.assertEqual(ahead.position(), 2)

        # Another commit after the empty one.
        repo.ingest(batch(
            'b3', references=[reference('r2', 20, layout('f2'), edge_probes())]))
        package_23 = repo.package(2, 3)
        ahead.apply(round_trip(package_23))

        stuck = Receiver.create(Path(self.temp) / 'stuck', 'dataset-c')
        stuck.apply(round_trip(repo.package(0, 1)))
        self.assertEqual(stuck.position(), 1)
        # Skipping the empty segment is rejected even though it changed no row.
        with self.assertRaises(ReceiverPositionError):
            stuck.apply(round_trip(package_23))
        self.assertEqual(stuck.position(), 1)
        # Catching up in order works.
        stuck.apply(round_trip(empty_package))
        stuck.apply(round_trip(package_23))
        self.assertEqual(stuck.position(), 3)
        self.assertEqual(stuck.rows(), ahead.rows())
        repo.close()

    def test_malformed_mixed_batch_is_atomic_and_retry_keeps_batch_id(self):
        repo = SourceRepository.create(self.source_dir, 'ds')
        good_obs = observation('ok1', 1, [])
        bad_layout = {'sheets': [{'id': 'P', 'width': '0', 'height': '2'}],
                      'folds': []}
        bad = batch('mix',
                    references=[reference('r1', 0, bad_layout, edge_probes())],
                    observations=[good_obs])
        with self.assertRaises(ValueError):
            repo.ingest(bad)
        self.assertEqual(repo.head(), 0)

        # Corrected content under the same batch id is the first success.
        second_ref = {'case': 'C', 'revision': 'r2', 'effective_from': 100,
                      'layout': flat_layout(), 'probes': []}
        fixed = batch('mix',
                      references=[reference('r1', 0, flat_layout(), edge_probes()),
                                  second_ref],
                      observations=[good_obs])
        self.assertEqual(repo.ingest(fixed), 1)
        # The rejected attempt consumed neither number nor identity.
        rows = {c['id']: c['after'] for c in repo.package(0, 1)['changes']}
        self.assertEqual(rows['ok1']['revision'], 'r1')

        # Exact retry returns the same commit and does not re-ingest.
        shuffled = {'observations': fixed['observations'],
                    'batch': fixed['batch'], 'references': fixed['references']}
        self.assertEqual(repo.ingest(shuffled), 1)
        self.assertEqual(repo.head(), 1)

        # Same batch id with changed content is refused (the new content is
        # itself valid; only the reused identity with different payload fails).
        changed = batch('mix',
                        references=[reference('r1', 0, flat_layout(),
                                              edge_probes()),
                                    {'case': 'C', 'revision': 'r3',
                                     'effective_from': 200,
                                     'layout': flat_layout(), 'probes': []}],
                        observations=[good_obs])
        with self.assertRaises(BatchConflictError):
            repo.ingest(changed)
        self.assertEqual(repo.head(), 1)

        # Array order is part of the retry content: same entries reversed is
        # different content even though every object is identical.
        reordered = batch('mix',
                          references=list(reversed(fixed['references'])),
                          observations=fixed['observations'])
        with self.assertRaises(BatchConflictError):
            repo.ingest(reordered)
        repo.close()

    def test_observation_first_then_reference_at_exact_effective_from(self):
        repo = SourceRepository.create(self.source_dir, 'ds')
        # Observation arrives first: no reference at all, no row materialises.
        obs = observation('early', 10, [])
        self.assertEqual(repo.ingest(batch('obs-only', observations=[obs])), 1)
        self.assertEqual(repo.package(0, 1)['changes'], [])

        # Reference whose effective_from equals the observed time makes it
        # visible; no fallback revision is involved.
        ref = reference('r1', 10, flat_layout(), edge_probes())
        self.assertEqual(repo.ingest(batch('ref-late', references=[ref])), 2)
        package = repo.package(1, 2)
        self.assertEqual([c['id'] for c in package['changes']], ['early'])
        self.assertIsNone(package['changes'][0]['before'])
        self.assertEqual(package['changes'][0]['after']['revision'], 'r1')

        # An observation strictly before the first effective_from stays row-less.
        before = observation('before', 9, [])
        repo.ingest(batch('obs-before', observations=[before]))
        self.assertEqual(repo.package(2, repo.head())['changes'], [])
        repo.close()

    def test_identity_conflicts_are_rejected_and_not_consumed(self):
        repo = SourceRepository.create(self.source_dir, 'ds')
        repo.ingest(batch('b1', references=[
            reference('r1', 0, flat_layout(), edge_probes())]))
        head = repo.head()
        # Duplicate revision id inside one case.
        with self.assertRaises(BatchConflictError):
            repo.ingest(batch('b2', references=[
                reference('r1', 5, layout('f2'), edge_probes())]))
        # Duplicate effective_from inside one case.
        with self.assertRaises(BatchConflictError):
            repo.ingest(batch('b3', references=[
                reference('r2', 0, layout('f2'), edge_probes())]))
        # Reused observation id.
        repo.ingest(batch('b4', observations=[observation('dup', 1, [])]))
        with self.assertRaises(BatchConflictError):
            repo.ingest(batch('b5', observations=[observation('dup', 2, [])]))
        # Revision ids may be reused across different cases.
        other = {'case': 'E', 'revision': 'r1', 'effective_from': 0,
                 'layout': flat_layout(), 'probes': []}
        self.assertEqual(repo.ingest(batch('b6', references=[other])),
                         repo.head())
        # Within-batch duplicate identity is rejected as well.
        with self.assertRaises(BatchConflictError):
            repo.ingest(batch('b7', observations=[
                observation('same', 1, []), observation('same', 2, [])]))
        self.assertEqual(repo.head(), head + 2)
        repo.close()

    def test_range_and_dataset_errors(self):
        repo, _first = self.build_first_commit()
        with self.assertRaises(RepositoryRangeError):
            repo.package(0, 0)
        with self.assertRaises(RepositoryRangeError):
            repo.package(1, 1)
        with self.assertRaises(RepositoryRangeError):
            repo.package(0, 2)
        with self.assertRaises(RepositoryRangeError):
            repo.package(2, 1)
        # A valid empty interval still returns the full package structure.
        repo.ingest(batch('empty-change', references=[
            {'case': 'Q', 'revision': 'q1', 'effective_from': 0,
             'layout': flat_layout(), 'probes': []}]))
        package = repo.package(1, 2)
        self.assertEqual((package['after'], package['through'],
                          package['changes']), (1, 2, []))

        receiver = Receiver.create(self.receiver_dir, 'dataset-c')
        foreign = round_trip(repo.package(0, 1))
        foreign['dataset'] = 'other-dataset'
        with self.assertRaises(ReceiverPositionError):
            receiver.apply(foreign)
        self.assertEqual(receiver.position(), 0)
        # A receiver created for another dataset rejects the real package.
        alien = Receiver.create(Path(self.temp) / 'alien', 'nope')
        with self.assertRaises(ReceiverPositionError):
            alien.apply(round_trip(repo.package(0, 1)))
        alien.close()
        repo.close()

    def test_receiver_input_validation(self):
        receiver = Receiver.create(self.receiver_dir, 'ds')
        good = {'format': 'paper-evidence-package', 'version': 1,
                'dataset': 'ds', 'after': 0, 'through': 1,
                'changes': []}
        # Bad format/version.
        bad = copy.deepcopy(good)
        bad['format'] = 'other'
        with self.assertRaises(ReceiverInputError):
            receiver.apply(bad)
        bad = copy.deepcopy(good)
        bad['version'] = 2
        with self.assertRaises(ReceiverInputError):
            receiver.apply(bad)
        # Unsorted or duplicate change ids.
        bad = copy.deepcopy(good)
        bad['changes'] = [
            {'id': 'b', 'before': None, 'after': None},
            {'id': 'a', 'before': None, 'after': None}]
        with self.assertRaises(ReceiverInputError):
            receiver.apply(bad)
        bad = copy.deepcopy(good)
        bad['changes'] = [
            {'id': 'a', 'before': None, 'after': None},
            {'id': 'a', 'before': None, 'after': None}]
        with self.assertRaises(ReceiverInputError):
            receiver.apply(bad)
        # Malformed row shape (missing field, bad face/layer/source point).
        row = {'id': 'a', 'case': 'C', 'observed_at': 1, 'probe': 'edge',
               'hits': [], 'revision': 'r1', 'expected_hits': [],
               'matches': True}
        for mutate, message in (
                (lambda r: r.pop('matches'), 'missing field'),
                (lambda r: r.update(matches='yes'), 'non-bool matches'),
                (lambda r: r.update(observed_at=True), 'boolean time'),
                (lambda r: r.update(hits=[{'layer': 0, 'sheet': 'P',
                                           'face': 'side',
                                           'source_point': ['1', '1']}]),
                 'bad face'),
                (lambda r: r.update(hits=[{'layer': -1, 'sheet': 'P',
                                           'face': 'front',
                                           'source_point': ['1', '1']}]),
                 'negative layer'),
                (lambda r: r.update(hits=[
                    {'layer': 1, 'sheet': 'P', 'face': 'front',
                     'source_point': ['1', '1']},
                    {'layer': 1, 'sheet': 'P', 'face': 'back',
                     'source_point': ['1', '1']}]), 'unsorted layers'),
                (lambda r: r.update(hits=[
                    {'layer': 0, 'sheet': 'P', 'face': 'front',
                     'source_point': ['2/4', '1']}]), 'unreduced fraction'),
                (lambda r: r.update(hits=[
                    {'layer': 0, 'sheet': 'P', 'face': 'front',
                     'source_point': ['1.5', '1']}]), 'decimal not output caliber'),
                (lambda r: r.update(extra=1), 'extra field')):
            candidate = copy.deepcopy(row)
            mutate(candidate)
            bad = copy.deepcopy(good)
            bad['changes'] = [{'id': 'a', 'before': None,
                               'after': candidate}]
            with self.assertRaises(ReceiverInputError, msg=message):
                receiver.apply(bad)
        self.assertEqual(receiver.position(), 0)
        self.assertEqual(receiver.rows(), [])
        receiver.close()

    def test_ingest_validation_rules(self):
        repo = SourceRepository.create(self.source_dir, 'ds')
        base_obs = observation('o', 1, [])
        base_ref = reference('r1', 0, flat_layout(), [])

        def expect_value_error(mutate):
            data = batch('x', [copy.deepcopy(base_ref)], [copy.deepcopy(base_obs)])
            mutate(data)
            with self.assertRaises(ValueError):
                repo.ingest(data)
            self.assertEqual(repo.head(), 0)

        expect_value_error(lambda d: d.pop('batch'))
        expect_value_error(lambda d: d.update(extra=1))
        expect_value_error(lambda d: d['observations'][0].update(observed_at=-1))
        expect_value_error(lambda d: d['observations'][0].update(observed_at=0.0))
        expect_value_error(lambda d: d['observations'][0].update(observed_at=True))
        expect_value_error(lambda d: d['references'][0].update(effective_from=False))
        expect_value_error(lambda d: d.update(references=[], observations=[]))

        # 1..1000 items in the two arrays together.
        big = batch('big',
                    references=[reference(f'r{i:04d}', i, flat_layout(), [])
                                for i in range(500)],
                    observations=[observation(f'o{i:04d}', i, [])
                                  for i in range(500)])
        self.assertEqual(repo.ingest(big), 1)
        too_big = batch('too-big',
                        references=[reference(f's{i:04d}', i, flat_layout(), [])
                                    for i in range(1001)],
                        observations=[])
        with self.assertRaises(ValueError):
            repo.ingest(too_big)
        self.assertEqual(repo.head(), 1)

        # Unknown sheet in observed hits is business evidence, not a bad batch.
        unknown_sheet = {'id': 'foreign', 'case': 'F', 'observed_at': 0,
                         'probe': 'edge',
                         'hits': [{'layer': 0, 'sheet': 'GHOST',
                                   'face': 'front',
                                   'source_point': ['0', '0']}]}
        foreign_ref = reference('f1', 0, flat_layout(), edge_probes())
        foreign_ref['case'] = 'F'
        repo.ingest(batch('foreign-batch',
                          references=[foreign_ref],
                          observations=[unknown_sheet]))
        package = repo.package(1, repo.head())
        row = {c['id']: c['after'] for c in package['changes']}['foreign']
        self.assertFalse(row['matches'])
        self.assertEqual(row['hits'][0]['sheet'], 'GHOST')
        repo.close()

    def test_real_json_roundtrip_and_reopen(self):
        repo, _first = self.build_first_commit()
        package = repo.package(0, 1)
        text = package_dumps(package)
        # Compact JSON, duplicate keys and non-finite values rejected on load.
        self.assertIsInstance(text, str)
        self.assertNotIn(' ', text.split(',', 1)[0])
        with self.assertRaises(ValueError):
            package_loads('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            package_loads('{"x":NaN}')
        with self.assertRaises(ValueError):
            package_loads('[1,2]')

        receiver = Receiver.create(self.receiver_dir, 'dataset-c')
        receiver.apply(package_loads(text))
        receiver.close()
        repo.close()

        # Both stores reopen from disk only and keep their state.
        repo = SourceRepository.open(self.source_dir)
        receiver = Receiver.open(self.receiver_dir)
        self.assertEqual(repo.head(), 1)
        self.assertEqual(receiver.position(), 1)
        self.assertEqual(repo.dataset, 'dataset-c')
        self.assertEqual(receiver.dataset, 'dataset-c')
        # Persisted rows equal the package endpoint rows exactly.
        endpoint = {c['id']: c['after'] for c in package['changes']}
        self.assertEqual(row_map(receiver.rows()), endpoint)
        # Reopening twice keeps idempotent replay semantics.
        receiver.close()
        receiver = Receiver.open(self.receiver_dir)
        self.assertEqual(receiver.apply(package_loads(text))['position'], 1)
        receiver.close()
        repo.close()

        # Context-manager close/reopen works too.
        with SourceRepository.open(self.source_dir) as repo:
            self.assertEqual(repo.head(), 1)
        with Receiver.open(self.receiver_dir) as receiver:
            self.assertEqual(receiver.position(), 1)

    def test_no_fallback_to_older_revision_and_empty_hits(self):
        repo = SourceRepository.create(self.source_dir, 'ds')
        # r1 knows edge; r2 later does not. The observation must not fall back.
        repo.ingest(batch('b1', references=[
            reference('r1', 0, flat_layout(), edge_probes())],
            observations=[observation('o1', 5, [])]))
        self.assertEqual(repo.head(), 1)
        repo.ingest(batch('b2', references=[
            reference('r2', 5, layout('f2'), [])]))
        # At commit 2 the observation is active under r2 which lacks the probe:
        # its row disappears instead of keeping r1's answer.
        package = repo.package(1, 2)
        self.assertEqual(package['changes'][0]['after'], None)
        # But history at commit 1 still shows r1.
        old = repo.package(0, 1)
        self.assertEqual(old['changes'][0]['after']['revision'], 'r1')

        # Empty observed hits match an empty reference answer exactly.
        empty_ref = {'case': 'N', 'revision': 'n1', 'effective_from': 0,
                     'layout': flat_layout(),
                     'probes': [{'id': 'none', 'point': ['9', '9']}]}
        empty_obs = {'id': 'n-obs', 'case': 'N', 'observed_at': 0,
                     'probe': 'none', 'hits': []}
        repo.ingest(batch('b3', [empty_ref], [empty_obs]))
        row = {c['id']: c['after'] for c in repo.package(2, repo.head())['changes']}
        self.assertTrue(row['n-obs']['matches'])
        self.assertEqual(row['n-obs']['expected_hits'], [])
        repo.close()

    def test_create_twice_and_tampered_delete_are_rejected(self):
        SourceRepository.create(self.source_dir, 'ds')
        with self.assertRaises(RepositoryError):
            SourceRepository.create(self.source_dir, 'ds')
        Receiver.create(self.receiver_dir, 'ds')
        with self.assertRaises(ReceiverError):
            Receiver.create(self.receiver_dir, 'ds')
        # Opening an unrelated directory fails rather than creating a store.
        with self.assertRaises(RepositoryError):
            SourceRepository.open(Path(self.temp) / 'missing-source')
        with self.assertRaises(ReceiverError):
            Receiver.open(Path(self.temp) / 'missing-receiver')

    def test_tampered_delete_before_row_is_conflict(self):
        repo, _first = self.build_first_commit()
        repo.ingest(batch(
            'b3', references=[reference('r4', 23, layout('f3'), [])]))
        receiver = Receiver.create(self.receiver_dir, 'dataset-c')
        receiver.apply(round_trip(repo.package(0, 1)))
        package = round_trip(repo.package(1, 2))
        # The real package deletes o25 with before=null; claim instead that a
        # well-formed row already exists locally.
        evil = copy.deepcopy(package)
        self.assertEqual(evil['changes'][0]['id'], 'o25')
        self.assertIsNone(evil['changes'][0]['after'])
        evil['changes'][0]['before'] = copy.deepcopy(receiver.row('o25'))
        evil['changes'][0]['before']['revision'] = 'r9'
        with self.assertRaises(ReceiverConflictError):
            receiver.apply(evil)
        self.assertEqual(receiver.position(), 1)
        self.assertIsNotNone(receiver.row('o25'))
        # Genuine delete then succeeds.
        receiver.apply(round_trip(package))
        self.assertIsNone(receiver.row('o25'))
        self.assertEqual(receiver.position(), 2)
        repo.close()

    def test_same_endpoint_rows_are_not_reported(self):
        repo = SourceRepository.create(self.source_dir, 'ds')
        repo.ingest(batch('b1', references=[
            reference('r1', 0, flat_layout(), edge_probes())],
            observations=[observation('o1', 5, [])]))
        # A revision inserted inside an unobserved window changes nothing
        # materialised at the endpoints.
        repo.ingest(batch('b2', references=[
            {'case': 'X', 'revision': 'x1', 'effective_from': 0,
             'layout': flat_layout(), 'probes': []}]))
        package = repo.package(0, repo.head())
        self.assertEqual([c['id'] for c in package['changes']], ['o1'])
        # And an interval whose net endpoint difference is empty stays empty.
        self.assertEqual(repo.package(1, 2)['changes'], [])
        repo.close()


if __name__ == '__main__':
    unittest.main()
