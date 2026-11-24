"""Persistent evidence-only receiver for offline increment packages.

A :class:`Receiver` starts as the empty copy at position 0 of one named
dataset and holds nothing but evidence rows: no raw references, no layouts,
no separate observation store and no network access.  It advances solely by
applying the source repository's plain-JSON packages.

Only the next segment of the same dataset is accepted: the package ``after``
endpoint must equal the receiver's current position.  Every ``before`` row is
checked against the complete local row (``null`` means the id is absent)
before anything is written, and the whole package is applied in one
transaction, including packages whose change list is empty.  Re-sending the
exact package last applied successfully returns the original result without
redoing it and without advancing again.

Three failure classes stay distinguishable while all remaining
:class:`ValueError` subclasses::

    ReceiverInputError       bad package/row shape, keys or identities
    ReceiverPositionError    wrong dataset, stale, gap or overlapping segment
    ReceiverConflictError    a well-formed before row does not match local data
"""
import json
import sqlite3
from pathlib import Path

from .checks import ident
from .evidence import canonical_json, validate_package

DB_NAME = 'evidence_receiver.sqlite3'
META_FORMAT = 'paper-evidence-receiver'
META_VERSION = '1'


class ReceiverError(ValueError):
    """Base class for receiver failures (also a plain ``ValueError``)."""


class ReceiverInputError(ReceiverError):
    """Malformed package, change, row identity or row shape."""


class ReceiverPositionError(ReceiverError):
    """Wrong dataset, or the package segment does not start at the position."""


class ReceiverConflictError(ReceiverError):
    """A well-formed ``before`` row differs from the local complete row."""


_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE evidence_rows (
    obs_id TEXT PRIMARY KEY,
    content TEXT NOT NULL
);
"""


def _connect(path):
    # isolation_level=None puts the driver in autocommit mode so that the
    # explicit BEGIN IMMEDIATE/commit pair below owns transaction boundaries.
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


class Receiver:
    """Local single-writer evidence receiver; reopen with :meth:`open`."""

    def __init__(self, path, connection, dataset):
        self._path = Path(path)
        self._db = connection
        self._dataset = dataset

    # ------------------------------------------------------------------ open

    @classmethod
    def create(cls, path, dataset):
        """Create a new empty receiver directory bound to ``dataset`` at 0."""
        ident(dataset)
        directory = Path(path)
        target = directory / DB_NAME
        if target.exists():
            raise ReceiverError(f'receiver already exists: {directory}')
        directory.mkdir(parents=True, exist_ok=True)
        connection = _connect(target)
        try:
            connection.executescript(_SCHEMA)
            connection.execute('BEGIN IMMEDIATE')
            connection.executemany(
                'INSERT INTO meta(key, value) VALUES (?, ?)',
                (('format', META_FORMAT), ('version', META_VERSION),
                 ('dataset', dataset), ('position', '0'),
                 ('last_package', ''), ('last_result', '')))
            connection.commit()
        except Exception:
            connection.close()
            for suffix in ('', '-wal', '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            raise
        return cls(directory, connection, dataset)

    @classmethod
    def open(cls, path):
        """Reopen a previously created receiver after a normal shutdown."""
        directory = Path(path)
        target = directory / DB_NAME
        if not target.exists():
            raise ReceiverError(f'no receiver at: {directory}')
        connection = _connect(target)
        try:
            meta = dict(connection.execute('SELECT key, value FROM meta').fetchall())
        except Exception:
            connection.close()
            raise
        if meta.get('format') != META_FORMAT or meta.get('version') != META_VERSION:
            connection.close()
            raise ReceiverError(f'unsupported receiver metadata at: {directory}')
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

    def position(self):
        """Current commit position; 0 for the untouched empty copy."""
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = 'position'").fetchone()
        return int(row[0])

    def rows(self):
        """All current evidence rows as an id-ascending list of full dicts."""
        return [json.loads(content) for _obs_id, content in self._db.execute(
            'SELECT obs_id, content FROM evidence_rows ORDER BY obs_id')]

    def row(self, obs_id):
        """Return one complete local row, or ``None`` when the id is absent."""
        row = self._db.execute(
            'SELECT content FROM evidence_rows WHERE obs_id = ?',
            (obs_id,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def __contains__(self, obs_id):
        return self._db.execute(
            'SELECT 1 FROM evidence_rows WHERE obs_id = ?',
            (obs_id,)).fetchone() is not None

    # ---------------------------------------------------------------- apply

    def apply(self, package):
        """Validate and apply one increment package; return the apply result.

        The result records the dataset, both endpoints, the number of changes
        applied and the new position.  Re-sending the exact package last
        applied returns the stored result unchanged without redoing work.
        Any failure rejects the whole package: no row and the position change.
        """
        try:
            validate_package(package)
        except ValueError as exc:
            raise ReceiverInputError(f'invalid package: {exc}') from None

        text = canonical_json(package)
        stored_package = self._db.execute(
            "SELECT value FROM meta WHERE key = 'last_package'").fetchone()[0]
        if stored_package and stored_package == text:
            stored_result = self._db.execute(
                "SELECT value FROM meta WHERE key = 'last_result'").fetchone()[0]
            return json.loads(stored_result)

        if package['dataset'] != self._dataset:
            raise ReceiverPositionError(
                f"dataset {package['dataset']!r} does not match receiver "
                f"dataset {self._dataset!r}")

        position = self.position()
        if package['after'] != position:
            raise ReceiverPositionError(
                f"package starts at {package['after']} but receiver position is {position}")

        # All prerequisites are checked against the complete local rows before
        # the transaction starts, so a late conflict still changes nothing.
        local = {row['id']: row for row in self.rows()}
        for change in package['changes']:
            cid = change['id']
            actual = local.get(cid)
            if change['before'] != actual:
                raise ReceiverConflictError(
                    f'before row mismatch for observation {cid!r}')

        result = {
            'dataset': self._dataset,
            'after': package['after'],
            'through': package['through'],
            'changes': len(package['changes']),
            'position': package['through'],
        }
        try:
            self._db.execute('BEGIN IMMEDIATE')
            for change in package['changes']:
                cid = change['id']
                after = change['after']
                if after is None:
                    self._db.execute(
                        'DELETE FROM evidence_rows WHERE obs_id = ?', (cid,))
                else:
                    self._db.execute(
                        'INSERT INTO evidence_rows(obs_id, content) VALUES (?, ?)'
                        ' ON CONFLICT(obs_id) DO UPDATE SET content = excluded.content',
                        (cid, canonical_json(after)))
            self._db.execute(
                "UPDATE meta SET value = ? WHERE key = 'position'",
                (str(package['through']),))
            self._db.execute(
                'UPDATE meta SET value = ? WHERE key = ?',
                (text, 'last_package'))
            self._db.execute(
                'UPDATE meta SET value = ? WHERE key = ?',
                (canonical_json(result), 'last_result'))
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return result
