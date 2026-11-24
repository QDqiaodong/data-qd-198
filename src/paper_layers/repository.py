"""Persistent source repository for the offline layout-regression evidence set.

A :class:`SourceRepository` owns one immutable ``dataset`` id and stores
ingested references and observations in a local single-writer SQLite file.
Commit numbers are assigned consecutively starting at 1 and are independent of
the field sequence numbers carried by observations.  A batch becomes visible
atomically: validation or identity conflicts reject the whole batch without
leaving a reference, an observation or a consumed commit number.

The repository only needs its on-disk directory to reopen.  Reference answers
(the legacy ``process`` four-field hit lists) are precomputed while a batch is
ingested, so reading an increment package never re-runs a historical layout
and never needs the original input files.
"""
import json
import sqlite3
from pathlib import Path

from .checks import ident
from .evidence import (canonical_json, build_package, diff_rows,
                       materialize, validate_batch)

DB_NAME = 'source_evidence.sqlite3'
META_FORMAT = 'paper-evidence-source'
META_VERSION = '1'


class RepositoryError(ValueError):
    """Base class for source-repository business failures."""


class BatchConflictError(RepositoryError):
    """A batch id is reused with different content, or an identity collides."""


class RepositoryRangeError(RepositoryError):
    """A requested package interval does not fit the committed history."""


_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE batches (
    seq INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL
);
CREATE TABLE refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_seq INTEGER NOT NULL,
    case_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    effective_from INTEGER NOT NULL,
    expected TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_refs_case_revision ON refs(case_id, revision);
CREATE UNIQUE INDEX ux_refs_case_effective ON refs(case_id, effective_from);
CREATE TABLE obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_seq INTEGER NOT NULL,
    obs_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    probe TEXT NOT NULL,
    hits TEXT NOT NULL
);
"""


def _connect(path):
    # isolation_level=None puts the driver in autocommit mode so that the
    # explicit BEGIN IMMEDIATE/commit pairs below own transaction boundaries.
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


class SourceRepository:
    """Local single-writer source repository; reopen with :meth:`open`."""

    def __init__(self, path, connection, dataset):
        self._path = Path(path)
        self._db = connection
        self._dataset = dataset

    # ------------------------------------------------------------------ open

    @classmethod
    def create(cls, path, dataset):
        """Create a new empty repository directory bound to ``dataset``."""
        ident(dataset)
        directory = Path(path)
        target = directory / DB_NAME
        if target.exists():
            raise RepositoryError(f'repository already exists: {directory}')
        directory.mkdir(parents=True, exist_ok=True)
        connection = _connect(target)
        try:
            connection.executescript(_SCHEMA)
            connection.execute('BEGIN IMMEDIATE')
            connection.executemany(
                'INSERT INTO meta(key, value) VALUES (?, ?)',
                (('format', META_FORMAT), ('version', META_VERSION),
                 ('dataset', dataset), ('head', '0')))
            connection.commit()
        except Exception:
            connection.close()
            for suffix in ('', '-wal', '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            raise
        return cls(directory, connection, dataset)

    @classmethod
    def open(cls, path):
        """Reopen a previously created repository after a normal shutdown."""
        directory = Path(path)
        target = directory / DB_NAME
        if not target.exists():
            raise RepositoryError(f'no repository at: {directory}')
        connection = _connect(target)
        try:
            meta = dict(connection.execute('SELECT key, value FROM meta').fetchall())
        except Exception:
            connection.close()
            raise
        if meta.get('format') != META_FORMAT or meta.get('version') != META_VERSION:
            connection.close()
            raise RepositoryError(f'unsupported repository metadata at: {directory}')
        return cls(directory, connection, meta['dataset'])

    def close(self):
        """Flush and close the underlying SQLite database."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    # ------------------------------------------------------------- properties

    @property
    def dataset(self):
        return self._dataset

    @property
    def path(self):
        return self._path

    def head(self):
        """Current commit number; 0 while the repository is empty."""
        row = self._db.execute("SELECT value FROM meta WHERE key = 'head'").fetchone()
        return int(row[0])

    # ---------------------------------------------------------------- ingest

    def ingest(self, batch):
        """Atomically ingest one batch and return its commit number.

        Re-sending the same batch id with the same content after a reopen
        returns the original commit number without re-ingesting anything.
        The same id with different content raises :class:`BatchConflictError`;
        malformed batches raise plain ``ValueError`` and consume no number.
        """
        bid, expected_maps = validate_batch(batch)
        content = canonical_json(batch)

        existing = self._db.execute(
            'SELECT seq, content FROM batches WHERE batch_id = ?',
            (bid,)).fetchone()
        if existing is not None:
            if existing[1] != content:
                raise BatchConflictError(
                    f'batch id {bid!r} reused with different content')
            return existing[0]

        references = batch['references']
        observations = batch['observations']

        # Identity pre-checks in Python keep the atomic failure mode explicit:
        # unique indexes below are the durable backstop.
        seen_revisions = set()
        seen_effective = set()
        seen_obs = set()
        for reference in references:
            revision_key = (reference['case'], reference['revision'])
            if revision_key in seen_revisions:
                raise BatchConflictError('duplicate revision identity in batch')
            seen_revisions.add(revision_key)
            effective_key = (reference['case'], reference['effective_from'])
            if effective_key in seen_effective:
                raise BatchConflictError('duplicate effective_from in batch')
            seen_effective.add(effective_key)
        for observation in observations:
            if observation['id'] in seen_obs:
                raise BatchConflictError('duplicate observation id in batch')
            seen_obs.add(observation['id'])

        stored_revisions = {
            (case_id, revision)
            for case_id, revision in self._db.execute(
                'SELECT case_id, revision FROM refs')}
        stored_effective = {
            (case_id, effective_from)
            for case_id, effective_from in self._db.execute(
                'SELECT case_id, effective_from FROM refs')}
        for key in seen_revisions:
            if key in stored_revisions:
                raise BatchConflictError(f'reference revision already recorded: {key}')
        for key in seen_effective:
            if key in stored_effective:
                raise BatchConflictError(
                    f'reference effective_from already recorded: {key}')
        stored_observations = {
            row[0] for row in self._db.execute('SELECT obs_id FROM obs')}
        for obs_id in seen_obs:
            if obs_id in stored_observations:
                raise BatchConflictError(f'observation id already recorded: {obs_id}')

        try:
            self._db.execute('BEGIN IMMEDIATE')
            next_seq = self.head() + 1
            self._db.execute(
                'INSERT INTO batches(seq, batch_id, content) VALUES (?, ?, ?)',
                (next_seq, bid, content))
            self._db.executemany(
                'INSERT INTO refs(commit_seq, case_id, revision, effective_from, expected)'
                ' VALUES (?, ?, ?, ?, ?)',
                ((next_seq, reference['case'], reference['revision'],
                  reference['effective_from'], canonical_json(expected))
                 for reference, expected in zip(references, expected_maps)))
            self._db.executemany(
                'INSERT INTO obs(commit_seq, obs_id, case_id, observed_at, probe, hits)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                ((next_seq, observation['id'], observation['case'],
                  observation['observed_at'], observation['probe'],
                  canonical_json(observation['hits']))
                 for observation in observations))
            self._db.execute("UPDATE meta SET value = ? WHERE key = 'head'",
                             (str(next_seq),))
            self._db.commit()
        except sqlite3.IntegrityError as exc:
            self._db.rollback()
            raise BatchConflictError(f'identity conflict: {exc}') from None
        except Exception:
            self._db.rollback()
            raise
        return next_seq

    # ---------------------------------------------------------------- reads

    def _state(self, commit):
        """Materialised evidence rows visible at exactly ``commit``."""
        references = [
            {'case': case_id, 'revision': revision,
             'effective_from': effective_from,
             'expected': json.loads(expected)}
            for case_id, revision, effective_from, expected in self._db.execute(
                'SELECT case_id, revision, effective_from, expected FROM refs'
                ' WHERE commit_seq <= ?', (commit,))]
        observations = [
            {'id': obs_id, 'case': case_id, 'observed_at': observed_at,
             'probe': probe, 'hits': json.loads(hits)}
            for obs_id, case_id, observed_at, probe, hits in self._db.execute(
                'SELECT obs_id, case_id, observed_at, probe, hits FROM obs'
                ' WHERE commit_seq <= ?', (commit,))]
        return materialize(references, observations)

    def package(self, after, through):
        """Build the deterministic plain-JSON increment package for an interval.

        ``0 <= after < through <= head``; 0 denotes the empty store.  The same
        interval always returns the same content, including after a reopen,
        because endpoints are derived from durable rows without re-folding.
        """
        if type(after) is not int or type(through) is not int \
                or after < 0 or through < 0:
            raise RepositoryRangeError(
                'package interval needs non-negative integer endpoints')
        head = self.head()
        if not (after < through <= head):
            raise RepositoryRangeError(
                f'require 0 <= after < through <= {head}, '
                f'got after={after} through={through}')
        before_rows = self._state(after)
        after_rows = self._state(through)
        changes = diff_rows(before_rows, after_rows)
        return build_package(self._dataset, after, through, changes)
