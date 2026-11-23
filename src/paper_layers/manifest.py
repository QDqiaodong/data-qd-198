"""Leakage-free train/evaluation release manifests.

Offline entry point for the layout-regression sample pool: split the current
samples into a training list and an evaluation list that both pipelines can
load directly, without ever letting two linked samples land on opposite
sides.  The existing layout computation, query and annotation interfaces and
the CLI are untouched; this module only reads them.

``build_manifest(data)``
    Take ``{"samples": ..., "previous": ..., "quota": ...}`` and return
    ``{"train": [...], "evaluation": [...]}`` — ascending sample-ID lists
    that partition the input samples and hit the evaluation quotas exactly.

>>> from paper_layers.manifest import build_manifest
>>> data = {
...     'samples': [
...         {'id': 's1', 'origin': 'o1', 'channel': 'field',
...          'layout': {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
...                     'folds': [{'id': 'f', 'a': '1', 'b': '0', 'c': '2'}]}},
...         {'id': 's2', 'origin': None, 'channel': 'lab',
...          'layout': {'sheets': [{'id': 'P', 'width': '3', 'height': '2'}],
...                     'folds': []}},
...     ],
...     'previous': None,
...     'quota': {'evaluation_total': 1, 'evaluation_field': 0},
... }
>>> build_manifest(data)
{'train': ['s1'], 'evaluation': ['s2']}

Grouping
--------
Two samples are linked when they share a non-null ``origin`` or when their
layouts are equivalent; linkage is transitive, so whole groups are
indivisible.  Layout equivalence compares the complete ordered fragment
stack produced by the existing folding computation (bottom to top): the
sheet count and dimensions in input order, and per fragment the sheet
index, the upward face, the exact covering polygon (canonicalised for
starting vertex, winding direction and extra collinear points) and the
exact affine map from layout coordinates back to that sheet's coordinates.
Sheet ids, fold ids and step paths are ignored; no float tolerance is used.
Equivalence never merges or drops sample ids — it only constrains the
split.

Release rules
-------------
A group containing samples previously released to one side goes to that
side; one group mixing both previous sides raises
:class:`PreviousAssignmentConflictError`.  Remaining groups are assigned so
the evaluation totals hit the quota exactly; when no assignment works
(pinned evaluation samples over quota included) the entry point raises
:class:`QuotaUnsatisfiableError` and delivers nothing.  Among all feasible
solutions the one whose sorted evaluation ID list is lexicographically
smallest wins, so shuffling the input arrays never changes the result.

Errors
------
Invalid input (missing/extra fields, bad types or ids, duplicate
identities, unknown previous ids, invalid layouts, out-of-range quotas)
raises plain ``ValueError``.  Business failures raise
:class:`ManifestError` subclasses, which are also ``ValueError`` but stay
distinguishable from invalid input.
"""
from fractions import Fraction
from math import isqrt

from .checks import array, fields, ident, integer, unique
from .folding import fold_stack
from .geometry import reflect
from .model import layout_parts
from .polygon import normalize

__all__ = ['build_manifest', 'ManifestError', 'PreviousAssignmentConflictError',
           'QuotaUnsatisfiableError']

CHANNELS = ('field', 'lab')
MAX_SAMPLES = 40000
MAX_EVALUATION_TOTAL = 2048
MAX_EVALUATION_FIELD = 1024

# Affine map from layout coordinates back to original-sheet coordinates,
# stored as (m00, m01, m10, m11, tx, ty):
# (x, y) -> (m00*x + m01*y + tx, m10*x + m11*y + ty).
_IDENTITY_MAP = (1, 0, 0, 1, 0, 0)


class ManifestError(ValueError):
    """Base class for release-manifest business failures.

    Invalid input raises plain ``ValueError`` instead, so callers can tell
    bad requests apart from conflicts and quota failures.
    """


class PreviousAssignmentConflictError(ManifestError):
    """A leakage group mixes samples previously released to both sides."""


class QuotaUnsatisfiableError(ManifestError):
    """No assignment can meet the evaluation quotas exactly."""


def _canonical_polygon(poly):
    """Canonical form of one fragment polygon.

    Invariant to the starting vertex, the winding direction and extra
    collinear points on edges; exact rational coordinates, no tolerance.
    """
    points = normalize(poly)
    if not points:
        raise ValueError('layout produced a degenerate fragment')
    start = min(range(len(points)), key=points.__getitem__)
    return tuple(points[start:] + points[:start])


def _affine_map(reflections):
    """Exact affine map from layout coordinates back to sheet coordinates."""
    zero = Fraction(0)

    def apply(point):
        for a, b, c in reversed(reflections):
            point = reflect(point, a, b, c)
        return point

    ox, oy = apply((zero, zero))
    ux, uy = apply((Fraction(1), zero))
    vx, vy = apply((zero, Fraction(1)))
    return (ux - ox, vx - ox, uy - oy, vy - oy, ox, oy)


def _layout_fingerprint(layout):
    """Canonical fingerprint of the complete ordered fragment stack.

    Sheet ids, fold ids and step paths are ignored; sheets correspond by
    input position.  Two layouts are equivalent iff their fingerprints are
    equal.
    """
    sheets, folds = layout_parts(layout)
    layers, _total, _history = fold_stack(sheets, folds)
    index = {key: i for i, (key, _w, _h) in enumerate(sheets)}
    dimensions = tuple((w, h) for _key, w, h in sheets)
    fragments = []
    for layer in layers:
        reflections = layer['reflections']
        amap = _IDENTITY_MAP if not reflections else _affine_map(reflections)
        fragments.append((index[layer['sheet']], layer['face'],
                          _canonical_polygon(layer['poly']), amap))
    return dimensions, tuple(fragments)


def _branching(count):
    """Chunk count for one recursion level, keeping depth and memory bounded."""
    if count <= 64:
        return count
    if count <= 4096:
        root = isqrt(count)
        return root if root * root == count else root + 1
    root = round(count ** (1 / 3))
    while root ** 3 < count:
        root += 1
    while (root - 1) ** 3 >= count:
        root -= 1
    return root


def _decide(items, total, field):
    """Pick the lex-max membership vector meeting (total, field) exactly.

    ``items`` are the (size, field) pairs of the free groups ordered by
    smallest sample id; every item fits inside (total, field).  Returns one
    bool per item, True meaning the group goes to evaluation.  Preferring
    earlier groups inside the evaluation side yields the lexicographically
    smallest sorted evaluation id list.

    Reachability over (total, field) is one big integer bitset: bit
    ``t * stride + f`` records that some suffix of the items contributes
    exactly ``t`` samples of which ``f`` are field samples.  Suffix states
    are checkpointed per chunk, so peak memory stays at a few dozen bitsets
    while each item is revisited only a constant number of times.
    """
    count = len(items)
    if count == 0:
        if total == 0 and field == 0:
            return []
        raise QuotaUnsatisfiableError('evaluation quota cannot be met exactly')
    stride = field + 1
    full_mask = (1 << ((total + 1) * stride)) - 1
    unit = full_mask // ((1 << stride) - 1)
    masks = {}
    for _size, f0 in items:
        if f0 not in masks:
            masks[f0] = unit * ((1 << (stride - f0)) - 1) if f0 else full_mask
    shifts = [size * stride + f0 for size, f0 in items]

    def transition(state, i):
        f0 = items[i][1]
        if f0:
            state |= (state & masks[f0]) << shifts[i]
        else:
            state |= state << shifts[i]
        return state & full_mask

    chosen = [False] * count

    def rec(lo, hi, suffix, rem_total, rem_field):
        if hi - lo == 1:
            size, f0 = items[lo]
            if size <= rem_total and f0 <= rem_field:
                pos = (rem_total - size) * stride + rem_field - f0
                if (suffix >> pos) & 1:
                    chosen[lo] = True
                    return rem_total - size, rem_field - f0
            if (suffix >> (rem_total * stride + rem_field)) & 1:
                return rem_total, rem_field
            raise QuotaUnsatisfiableError('evaluation quota cannot be met exactly')
        step = -(-(hi - lo) // _branching(hi - lo))
        bounds = list(range(lo, hi, step)) + [hi]
        states = {hi: suffix}
        state = suffix
        for i in range(len(bounds) - 1, 0, -1):
            for j in range(bounds[i] - 1, bounds[i - 1] - 1, -1):
                state = transition(state, j)
            states[bounds[i - 1]] = state
        if not (states[lo] >> (rem_total * stride + rem_field)) & 1:
            raise QuotaUnsatisfiableError('evaluation quota cannot be met exactly')
        for i in range(len(bounds) - 1):
            rem_total, rem_field = rec(bounds[i], bounds[i + 1],
                                       states[bounds[i + 1]], rem_total, rem_field)
        return rem_total, rem_field

    rec(0, count, 1, total, field)
    return chosen


def build_manifest(data):
    """Split the current samples into train/evaluation ID manifests.

    ``data`` has exactly ``samples`` (0..40,000 records with ``id``,
    ``origin``, ``channel``, ``layout``), ``previous`` (null or the last
    delivered ``{"train", "evaluation"}`` pair) and ``quota``
    (``evaluation_total`` 0..2048, ``evaluation_field`` 0..1024, not
    exceeding the total).  Returns ``{"train": [...], "evaluation": [...]}``
    with ascending ids covering every input sample exactly once.  The input
    is only read, never modified.
    """
    fields(data, ('samples', 'previous', 'quota'))
    quota = data['quota']
    fields(quota, ('evaluation_total', 'evaluation_field'))
    total = integer(quota['evaluation_total'], 0, MAX_EVALUATION_TOTAL)
    field_quota = integer(quota['evaluation_field'], 0, MAX_EVALUATION_FIELD)
    if field_quota > total:
        raise ValueError('evaluation_field must not exceed evaluation_total')

    samples = []
    for item in array(data['samples'], 0, MAX_SAMPLES):
        fields(item, ('id', 'origin', 'channel', 'layout'))
        sid = ident(item['id'])
        origin = item['origin']
        if origin is not None:
            origin = ident(origin)
        channel = item['channel']
        if channel not in CHANNELS:
            raise ValueError('channel must be field or lab')
        samples.append((sid, origin, channel, item['layout']))
    unique([sid for sid, _origin, _channel, _layout in samples])
    samples.sort(key=lambda item: item[0])

    pinned = {}
    previous = data['previous']
    if previous is not None:
        fields(previous, ('train', 'evaluation'))
        for side in ('train', 'evaluation'):
            for value in array(previous[side], 0, MAX_SAMPLES):
                sid = ident(value)
                if sid in pinned:
                    raise ValueError('duplicate identity in previous release')
                pinned[sid] = side
    known = {sid for sid, _origin, _channel, _layout in samples}
    for sid in pinned:
        if sid not in known:
            raise ValueError(f'unknown id in previous release: {sid}')

    count = len(samples)
    parent = list(range(count))
    weight = [1] * count

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if weight[ra] < weight[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        weight[ra] += weight[rb]

    first_origin = {}
    first_layout = {}
    for i, (_sid, origin, _channel, layout) in enumerate(samples):
        if origin is not None:
            j = first_origin.setdefault(origin, i)
            if j != i:
                union(i, j)
        fingerprint = _layout_fingerprint(layout)
        j = first_layout.setdefault(fingerprint, i)
        if j != i:
            union(i, j)
    del first_origin, first_layout

    clusters = {}
    for i in range(count):
        clusters.setdefault(find(i), []).append(i)

    groups = []
    for members in clusters.values():
        ids = [samples[i][0] for i in members]
        sides = {pinned[sid] for sid in ids if sid in pinned}
        if len(sides) > 1:
            raise PreviousAssignmentConflictError(
                'one leakage group mixes previous train and evaluation releases')
        field_count = sum(1 for i in members if samples[i][2] == 'field')
        groups.append((ids, len(members), field_count, sides.pop() if sides else None))
    groups.sort(key=lambda group: group[0][0])

    evaluation = []
    train = []
    free = []
    used_total = 0
    used_field = 0
    for ids, group_size, field_count, side in groups:
        if side == 'evaluation':
            used_total += group_size
            used_field += field_count
            evaluation.extend(ids)
        elif side == 'train':
            train.extend(ids)
        else:
            free.append((ids, group_size, field_count))
    rem_total = total - used_total
    rem_field = field_quota - used_field
    if rem_total < 0 or rem_field < 0:
        raise QuotaUnsatisfiableError(
            'pinned evaluation samples already exceed the quota')

    items = []
    for ids, group_size, field_count in free:
        if group_size <= rem_total and field_count <= rem_field:
            items.append((ids, group_size, field_count))
        else:
            train.extend(ids)
    chosen = _decide([(size, f) for _ids, size, f in items], rem_total, rem_field)
    for (ids, _size, _f), take in zip(items, chosen):
        (evaluation if take else train).extend(ids)
    evaluation.sort()
    train.sort()
    return {'train': train, 'evaluation': evaluation}
