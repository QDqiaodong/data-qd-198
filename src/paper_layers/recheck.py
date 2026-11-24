"""Offline re-verification probe manifests and field interpretation.

A re-verification batch may have been produced by any of a few known layout
versions and the field station carries no trusted version label.  Before
dispatch the office computes the standard answer of every candidate probe
for every candidate version with the existing layout engine; the field team
then takes one probe manifest plus one offline-readable handoff and must
still identify the layout version when one whole station is down and at
most one of the remaining readings is misread.

``build_handoff(data)``
    Take ``{"versions": ..., "probes": ..., "required": ...}`` and return
    the self-contained plain-JSON handoff: the selected probe manifest
    (station and exact point per probe, ready for the measuring party) plus
    every candidate version id with the standard answer of each selected
    probe (for the interpreting party).  The manifest keeps every required
    probe and, for every pair of distinct versions, keeps at least three
    probes with different standard answers outside any single station.
    Among the feasible manifests it minimises the total cost, then the
    probe count, then the ascending probe-id list lexicographically; the
    order of the input arrays never changes the delivery.  When no manifest
    is feasible (versions that agree on every candidate probe cannot be
    merged either) it raises :class:`InfeasibleManifestError` and delivers
    nothing; when the handoff would exceed the byte limit it raises
    :class:`HandoffSizeError` instead of degrading the manifest or
    truncating the standard answers.

``interpret_handoff(handoff, request)``
    Take only the handoff and ``{"stopped": ..., "readings": ...}`` — no
    layout file and no re-folding — and judge the actually received
    readings.  A version survives while at most one received probe
    misreads: exactly one survivor is reported together with the probe to
    re-measure (``None`` when the readings are fully consistent), several
    survivors are reported as one ascending id list, and no survivor yields
    ``'unexplainable'``.  Malformed handoffs, unknown format versions,
    illegal references and invalid readings raise
    :class:`HandoffInputError`, which stays distinguishable from both
    business failures; a legal reading that simply does not match is field
    evidence, never bad input.  Neither argument is modified.

``handoff_dumps`` / ``handoff_loads``
    Compact UTF-8 plain-JSON round trip for the handoff, enforcing the
    250,000 byte single-piece limit.
"""
import json

from .checks import array, fields, ident, integer, unique, vector
from .evidence import output_number, reference_expected, validate_hits

__all__ = ['build_handoff', 'interpret_handoff', 'handoff_dumps',
           'handoff_loads', 'RecheckError', 'InfeasibleManifestError',
           'HandoffSizeError', 'HandoffInputError']

FORMAT = 'paper-recheck-handoff'
VERSION = 1
MAX_BYTES = 250000

MIN_VERSIONS = 2
MAX_VERSIONS = 12
MAX_PROBES = 22
MIN_STATIONS = 3
MAX_STATIONS = 8
MAX_COST = 1000000
MIN_DISCRIMINATORS = 3

HANDOFF_FIELDS = ('format', 'version', 'stations', 'probes', 'answers',
                  'versions')

# Per-version-pair station profiles are packed into one integer: five bits
# per station lane hold counts up to 22 probes without carry between lanes.
_LANE_BITS = 5
_LANE_MASK = (1 << _LANE_BITS) - 1


class RecheckError(ValueError):
    """Base class for the re-verification business failures.

    Invalid build input raises plain ``ValueError`` instead, so callers can
    tell bad requests apart from the infeasible and size-limit failures.
    """


class InfeasibleManifestError(RecheckError):
    """No probe list keeps three answers apart per version pair and station."""


class HandoffSizeError(RecheckError):
    """The handoff does not fit the plain-JSON byte limit."""


class HandoffInputError(RecheckError):
    """Malformed handoff, request or reading at interpretation time."""


# ------------------------------------------------------------------ selection


def _lane_ok(packed, station_count):
    """True when the packed per-station counts keep three outside any station."""
    total = 0
    top = 0
    for lane in range(station_count):
        value = (packed >> (_LANE_BITS * lane)) & _LANE_MASK
        total += value
        if value > top:
            top = value
    return total - top >= MIN_DISCRIMINATORS


class _Cover:
    """Per-version-pair station profiles while searching probe subsets.

    ``cur`` holds the packed profile of the currently included probes per
    version pair; ``suffix[pair][i]`` holds the packed profile of the
    not-yet-decided probes at search positions ``i..`` per pair, so a branch
    is pruned as soon as some pair can no longer reach three discriminators
    outside every station.  A pair satisfied by the included probes alone is
    closed: adding more probes can never break it again.
    """

    def __init__(self, order, required, pair_count, station_count):
        self.pair_count = pair_count
        self.station_count = station_count
        self.cur = [0] * pair_count
        for probe in required:
            for pair in probe['pairs']:
                self.cur[pair] += probe['lane']
        self.closed = [_lane_ok(value, station_count) for value in self.cur]
        self.suffix = [[0] * (len(order) + 1) for _ in range(pair_count)]
        for i, probe in enumerate(order):
            for pair in probe['pairs']:
                column = self.suffix[pair]
                lane = probe['lane']
                for j in range(i + 1):
                    column[j] += lane

    def viable(self, i):
        """True when every open pair can still reach three outside any station."""
        cur = self.cur
        suffix = self.suffix
        closed = self.closed
        for pair in range(self.pair_count):
            if not closed[pair] and not _lane_ok(
                    cur[pair] + suffix[pair][i], self.station_count):
                return False
        return True

    def include(self, probe):
        """Add one probe; return the pairs it closed (for :meth:`exclude`)."""
        changed = []
        for pair in probe['pairs']:
            self.cur[pair] += probe['lane']
            if not self.closed[pair] and _lane_ok(self.cur[pair],
                                                  self.station_count):
                self.closed[pair] = True
                changed.append(pair)
        return changed

    def exclude(self, probe, changed):
        """Undo one :meth:`include` exactly."""
        for pair in probe['pairs']:
            self.cur[pair] -= probe['lane']
        for pair in changed:
            self.closed[pair] = False


def _select(probes, pair_count, station_count):
    """Pick the optimal feasible probe list; return its ascending id list.

    Required probes are always in; probes that discriminate no version pair
    and are not required never pay for a seat.  Phase one finds the best
    (total cost, probe count) by branch and bound; phase two replays the
    free probes in ascending id order, include-first, so the first feasible
    list hitting that optimum is the lexicographically smallest one.
    """
    required = [p for p in probes if p['required']]
    free = [p for p in probes if not p['required'] and p['pairs']]

    # Constraints are monotone in the selected set: if the full effective
    # probe set cannot keep three discriminators outside some station for
    # some version pair, no subset can, and nothing is delivered.
    full = [0] * pair_count
    for probe in required + free:
        for pair in probe['pairs']:
            full[pair] += probe['lane']
    for pair in range(pair_count):
        if not _lane_ok(full[pair], station_count):
            raise InfeasibleManifestError(
                'no probe list keeps three discriminating probes for every '
                'version pair with one station down')

    base_cost = sum(p['cost'] for p in required)
    base_count = len(required)

    # Phase 1: minimise (cost, count).  Expensive probes come first and the
    # exclude branch leads, so cheap incumbents appear early.
    order = sorted(free, key=lambda p: (-p['cost'], p['id']))
    cover = _Cover(order, required, pair_count, station_count)
    target = [base_cost + sum(p['cost'] for p in free),
              base_count + len(free)]

    def minimise(i, cost, count):
        if cost > target[0] or (cost == target[0] and count >= target[1]):
            return
        if not cover.viable(i):
            return
        if i == len(order):
            target[0] = cost
            target[1] = count
            return
        probe = order[i]
        minimise(i + 1, cost, count)
        changed = cover.include(probe)
        minimise(i + 1, cost + probe['cost'], count + 1)
        cover.exclude(probe, changed)

    minimise(0, base_cost, base_count)
    target_cost, target_count = target

    # Phase 2: among the optimal lists take the lexicographically smallest
    # ascending id list.  Equal-size subsets are visited in exactly that
    # order when ids ascend and the include branch leads.
    order = sorted(free, key=lambda p: p['id'])
    cover = _Cover(order, required, pair_count, station_count)
    chosen = []

    def lexicographic(i, cost, count):
        if cost > target_cost or count > target_count:
            return False
        if count + (len(order) - i) < target_count:
            return False
        if not cover.viable(i):
            return False
        if i == len(order):
            return True
        probe = order[i]
        changed = cover.include(probe)
        chosen.append(probe['id'])
        if lexicographic(i + 1, cost + probe['cost'], count + 1):
            return True
        cover.exclude(probe, changed)
        chosen.pop()
        return lexicographic(i + 1, cost, count)

    if not lexicographic(0, base_cost, base_count):
        # Unreachable: phase 1 proved an optimal feasible list exists.
        raise InfeasibleManifestError('no feasible probe list')
    return sorted([p['id'] for p in required] + chosen)


# ---------------------------------------------------------------------- json


def _compact(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'handoff is not plain JSON: {exc}') from None
    return text.encode('utf-8')


def handoff_dumps(handoff):
    """Return the compact UTF-8 JSON text of a handoff, enforcing the limit."""
    payload = _compact(handoff)
    if len(payload) > MAX_BYTES:
        raise HandoffSizeError(
            f'handoff exceeds {MAX_BYTES} bytes and cannot be delivered')
    return payload.decode('utf-8')


def handoff_loads(text):
    """Read handoff JSON text back, with duplicate-key and size checks."""
    if isinstance(text, bytes):
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            raise HandoffInputError('handoff text must be UTF-8') from None
    if not isinstance(text, str):
        raise HandoffInputError('handoff JSON text required')
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise HandoffSizeError(f'handoff exceeds {MAX_BYTES} bytes')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HandoffInputError(f'duplicate JSON key: {key}')
            result[key] = value
        return result

    def constant(value):
        raise HandoffInputError('nonfinite JSON value')

    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=constant)
    except HandoffInputError:
        raise
    except ValueError as exc:
        raise HandoffInputError(f'invalid handoff JSON: {exc}') from None
    if not isinstance(value, dict):
        raise HandoffInputError('handoff object required')
    return value


# ---------------------------------------------------------------------- build


def build_handoff(data):
    """Build the self-contained re-verification handoff.

    ``data`` has exactly ``versions`` (2..12 records ``id``/``layout``),
    ``probes`` (1..22 records ``id``/``station``/``point``/``cost`` spanning
    3..8 stations) and ``required`` (probe ids that must stay on the list,
    possibly empty).  The result is a JSON-serialisable ``dict`` within the
    250,000 byte handoff limit; :func:`handoff_dumps` renders it as compact
    JSON.  The input is only read, never modified.
    """
    fields(data, ('versions', 'probes', 'required'))
    versions = []
    for item in array(data['versions'], MIN_VERSIONS, MAX_VERSIONS):
        fields(item, ('id', 'layout'))
        vid = ident(item['id'])
        fields(item['layout'], ('sheets', 'folds'))
        versions.append((vid, item['layout']))
    unique([vid for vid, _layout in versions])

    probes = []
    for item in array(data['probes'], 1, MAX_PROBES):
        fields(item, ('id', 'station', 'point', 'cost'))
        pid = ident(item['id'])
        station = ident(item['station'])
        x, y = vector(item['point'], 2)
        cost = integer(item['cost'], 1, MAX_COST)
        probes.append({'id': pid, 'station': station, 'raw_point': item['point'],
                       'point': [str(x), str(y)], 'cost': cost})
    unique([p['id'] for p in probes])
    stations = sorted({p['station'] for p in probes})
    if not MIN_STATIONS <= len(stations) <= MAX_STATIONS:
        raise ValueError(f'station count {MIN_STATIONS}..{MAX_STATIONS} required')

    required_ids = [ident(value)
                    for value in array(data['required'], 0, MAX_PROBES)]
    unique(required_ids)
    known = {p['id'] for p in probes}
    for pid in required_ids:
        if pid not in known:
            raise ValueError(f'unknown required probe: {pid}')
    required_set = set(required_ids)

    # Standard answers come from the existing layout engine once per version,
    # keeping the complete ordered hit list down to the four evidence fields.
    versions.sort(key=lambda item: item[0])
    queries = [{'id': p['id'], 'point': p['raw_point']} for p in probes]
    answers = {vid: reference_expected(layout, queries)
               for vid, layout in versions}
    vids = [vid for vid, _layout in versions]
    version_pairs = [(i, j) for i in range(len(vids))
                     for j in range(i + 1, len(vids))]
    for probe in probes:
        pid = probe['id']
        probe['pairs'] = [k for k, (i, j) in enumerate(version_pairs)
                          if answers[vids[i]][pid] != answers[vids[j]][pid]]
    lane_of = {station: 1 << (_LANE_BITS * k)
               for k, station in enumerate(stations)}
    for probe in probes:
        probe['lane'] = lane_of[probe['station']]
        probe['required'] = probe['id'] in required_set

    selected = _select(probes, len(version_pairs), len(stations))
    selected_set = set(selected)

    # Repeated answers (same probe across versions, same point across
    # probes) are shared through one plain-JSON answer table.
    table = []
    index_of = {}
    versions_out = []
    for vid in vids:
        expected = {}
        for pid in selected:
            hits = answers[vid][pid]
            key = json.dumps(hits, sort_keys=True, separators=(',', ':'))
            index = index_of.get(key)
            if index is None:
                index = len(table)
                index_of[key] = index
                table.append(hits)
            expected[pid] = index
        versions_out.append({'id': vid, 'expected': expected})
    probes_out = [{'id': p['id'], 'station': p['station'], 'point': p['point']}
                  for p in sorted(probes, key=lambda p: p['id'])
                  if p['id'] in selected_set]
    handoff = {'format': FORMAT, 'version': VERSION, 'stations': stations,
               'probes': probes_out, 'answers': table,
               'versions': versions_out}
    if len(_compact(handoff)) > MAX_BYTES:
        raise HandoffSizeError(
            f'handoff exceeds {MAX_BYTES} bytes and cannot be delivered')
    return handoff


# ------------------------------------------------------------------ interpret


def _check(call, *args):
    """Run a shared validator, reclassifying its failure as bad handoff input."""
    try:
        return call(*args)
    except ValueError as exc:
        raise HandoffInputError(str(exc)) from None


def _parse_handoff(value):
    """Full structural validation of one handoff; return its indexed content."""
    if not isinstance(value, dict):
        raise HandoffInputError('handoff object required')
    _check(fields, value, HANDOFF_FIELDS)
    if value['format'] != FORMAT:
        raise HandoffInputError(
            f'unsupported handoff format: {value["format"]!r}')
    if type(value['version']) is not int or value['version'] != VERSION:
        raise HandoffInputError(
            f'unsupported handoff version: {value["version"]!r}')
    station_ids = [_check(ident, station) for station in _check(
        array, value['stations'], MIN_STATIONS, MAX_STATIONS)]
    _check(unique, station_ids)
    station_set = set(station_ids)

    probes = {}
    probe_ids = []
    for item in _check(array, value['probes'], 1, MAX_PROBES):
        _check(fields, item, ('id', 'station', 'point'))
        pid = _check(ident, item['id'])
        station = item['station']
        if not isinstance(station, str) or station not in station_set:
            raise HandoffInputError(f'unknown station in handoff: {station!r}')
        point = [_check(output_number, coordinate)
                 for coordinate in _check(array, item['point'], 2, 2)]
        probes[pid] = {'station': station, 'point': point}
        probe_ids.append(pid)
    _check(unique, probe_ids)

    answers = _check(array, value['answers'], 1, MAX_VERSIONS * MAX_PROBES)
    for hits in answers:
        _check(validate_hits, hits)

    versions = {}
    version_ids = []
    for item in _check(array, value['versions'], MIN_VERSIONS, MAX_VERSIONS):
        _check(fields, item, ('id', 'expected'))
        vid = _check(ident, item['id'])
        expected = item['expected']
        if not isinstance(expected, dict) or set(expected) != set(probe_ids):
            raise HandoffInputError(
                'expected answers must reference exactly the selected probes')
        for index in expected.values():
            if type(index) is not int or not 0 <= index < len(answers):
                raise HandoffInputError('answer reference out of range')
        versions[vid] = expected
        version_ids.append(vid)
    _check(unique, version_ids)
    return {'stations': station_set, 'probes': probes, 'answers': answers,
            'versions': versions, 'version_ids': sorted(version_ids)}


def interpret_handoff(handoff, request):
    """Judge the received readings with only the handoff and the request.

    ``request`` has exactly ``stopped`` (``None`` or one of the original
    candidate station ids recorded in the handoff, whether or not that
    station made the manifest) and ``readings`` (``[{probe, hits}]``, at
    most one per selected probe that is not at the stopped station).  Only
    the received probes are compared; a missing reading is neither a match
    nor a misread, while a received empty hit list is a normal reading.
    """
    state = _parse_handoff(handoff)
    if not isinstance(request, dict):
        raise HandoffInputError('request object required')
    _check(fields, request, ('stopped', 'readings'))
    stopped = request['stopped']
    if stopped is not None and (not isinstance(stopped, str)
                                or stopped not in state['stations']):
        raise HandoffInputError(f'unknown stopped station: {stopped!r}')

    received = {}
    for item in _check(array, request['readings'], 0, MAX_PROBES):
        _check(fields, item, ('probe', 'hits'))
        pid = _check(ident, item['probe'])
        probe = state['probes'].get(pid)
        if probe is None:
            raise HandoffInputError(f'reading for unselected probe: {pid!r}')
        if pid in received:
            raise HandoffInputError(f'duplicate reading for probe: {pid!r}')
        if stopped is not None and probe['station'] == stopped:
            raise HandoffInputError(f'reading from stopped station: {pid!r}')
        _check(validate_hits, item['hits'])
        received[pid] = item['hits']

    answers = state['answers']
    kept = []
    for vid in state['version_ids']:
        expected = state['versions'][vid]
        misread = [pid for pid, hits in received.items()
                   if hits != answers[expected[pid]]]
        if len(misread) <= 1:
            kept.append((vid, misread))
    if len(kept) == 1:
        vid, misread = kept[0]
        return {'status': 'determined', 'version': vid,
                'remeasure': misread[0] if misread else None}
    if kept:
        return {'status': 'ambiguous', 'versions': [vid for vid, _ in kept]}
    return {'status': 'unexplainable'}
