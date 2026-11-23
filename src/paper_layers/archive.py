"""Source-space annotation archive and window rebuild.

Two offline entry points live here:

``build_archive(data)``
    Take ``{"layout": {"sheets": ..., "folds": ...}, "batches": ...}``,
    resolve every rectangle mark against the folded layout exactly as the
    existing model does, transform the visible part of each mark back into
    original-paper coordinates, and return a self-contained plain JSON object.

``rebuild_window(archive, request)``
    Take only such an archive object and ``{"sheet", "face", "rect",
    "ignore_batch"}`` and return the exact winning regions inside the window.
    No layout file, raw mark rectangle or re-fold is consulted.

Marks never occlude one another while the archive is built: each mark is
clipped only by the folded fragments (the layout stack), so a mark keeps its
full footprint on the sheet even when a later batch paints over it.  This is
what allows a batch to be ignored later without losing the marks underneath.
Priority is resolved only during ``rebuild_window``.

Both functions work on plain dicts.  :func:`dumps` renders the compact UTF-8
JSON text used for handoff and enforces the 8,000,000 byte limit; :func:`loads`
reads that text back in a fresh process.
"""
import json
import re
from fractions import Fraction

from .checks import array, fields, ident, integer, number, unique
from .folding import fold_stack
from .geometry import reflect
from .model import layout_parts
from .polygon import bbox, boxes_overlap, difference, encloses, intersect, normalize

FORMAT = 'paper-annotation-archive'
VERSION = 1
MAX_BYTES = 8000000
FACES = ('front', 'back')

_FRACTION = re.compile(r'(-?(?:0|[1-9][0-9]*))/([1-9][0-9]*)')


def _exact(value):
    """Read an archive coordinate: JSON integer or reduced fraction string."""
    if type(value) is int:
        return Fraction(value)
    if isinstance(value, str):
        match = _FRACTION.fullmatch(value)
        if match:
            return Fraction(int(match.group(1)), int(match.group(2)))
        return number(value)
    raise ValueError('exact coordinate required')


def _out(value):
    """Serialise an exact rational as an integer or a reduced fraction string."""
    value = Fraction(value)
    if value.denominator == 1:
        return value.numerator
    return f'{value.numerator}/{value.denominator}'


def _compact(archive):
    try:
        text = json.dumps(archive, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'archive is not plain JSON: {exc}') from None
    return text.encode('utf-8')


def _check_size(archive):
    if len(_compact(archive)) > MAX_BYTES:
        raise ValueError(f'archive exceeds {MAX_BYTES} bytes and cannot be delivered')


def dumps(archive):
    """Return the compact UTF-8 JSON text of an archive, enforcing the limit."""
    payload = _compact(archive)
    if len(payload) > MAX_BYTES:
        raise ValueError(f'archive exceeds {MAX_BYTES} bytes and cannot be delivered')
    return payload.decode('utf-8')


def loads(text):
    """Deserialise archive text (a plain JSON object) with duplicate-key checks."""
    if isinstance(text, bytes):
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('archive text must be UTF-8') from None
    if not isinstance(text, str):
        raise ValueError('archive JSON text required')
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError(f'archive exceeds {MAX_BYTES} bytes')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('nonfinite JSON value')

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('archive object required')
    return value


def _point(value):
    return tuple(_exact(v) for v in array(value, 2, 2))


def _polygon(value):
    if not isinstance(value, list) or not 3 <= len(value) <= 4096:
        raise ValueError('polygon with at least three vertices required')
    poly = normalize([_point(p) for p in value])
    if not poly:
        raise ValueError('positive-area polygon required')
    return poly


def _parse_batches(value):
    batches = []
    total = 0
    for item in array(value, 0, 64):
        fields(item, ('id', 'priority', 'marks'))
        bid = ident(item['id'])
        priority = integer(item['priority'], 0, 1000000000)
        marks = []
        for mark in array(item['marks'], 0, 4096):
            fields(mark, ('id', 'code', 'rect'))
            mid = ident(mark['id'])
            code = ident(mark['code'])
            x0, y0, x1, y1 = (number(v) for v in array(mark['rect'], 4, 4))
            if not (x0 < x1 and y0 < y1):
                raise ValueError('mark rectangle must have positive width and height')
            marks.append((mid, code, (x0, y0), (x1, y1)))
        unique([m[0] for m in marks])
        total += len(marks)
        batches.append((bid, priority, marks))
    if total > 4096:
        raise ValueError('annotation count 0..4096 required')
    unique([b[0] for b in batches])
    return batches


def _rect_polygon(rect):
    x0, y0, x1, y1 = rect
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _source(poly, reflections):
    out = []
    for p in poly:
        for a, b, c in reversed(reflections):
            p = reflect(p, a, b, c)
        out.append(p)
    return normalize(out)


def _region_key(entry):
    return (entry['sheet'], entry['face'], entry['batch'], entry['mark'],
            tuple(value for point in entry['polygon'] for value in point))


def build_archive(data):
    """Build the self-contained original-paper annotation package.

    ``data`` has exactly ``layout`` (the legacy ``sheets``/``folds`` pair) and
    ``batches``.  The result is a JSON-serialisable ``dict`` that satisfies the
    8,000,000 byte handoff limit; :func:`dumps` renders it as compact JSON.
    """
    fields(data, ('layout', 'batches'))
    sheets, folds = layout_parts(data['layout'])
    batches = _parse_batches(data['batches'])

    layers, _total, _history = fold_stack(sheets, folds)
    prepared = [(bbox(r['poly']), normalize(r['poly']), r['reflections'],
                 r['sheet'], r['face']) for r in layers]

    entries = []
    for bid, _priority, marks in batches:
        for mid, code, p0, p1 in marks:
            rect = (p0[0], p0[1], p1[0], p1[1])
            rect_poly = _rect_polygon(rect)
            # Only fragments that can intersect this rectangle matter; the
            # surviving order is still the layout's top-to-bottom order.
            involved = [layer for layer in prepared if boxes_overlap(layer[0], rect)]
            remaining = [(rect, rect_poly)]
            # Walk fragments from the top of the stack downwards; a position is
            # owned by the first (topmost) fragment that contains it.
            for box, poly, reflections, sheet, face in reversed(involved):
                if not remaining:
                    break
                rest = []
                for piece_box, piece in remaining:
                    if not boxes_overlap(piece_box, box):
                        rest.append((piece_box, piece))
                        continue
                    if encloses(poly, piece):
                        source = _source(piece, reflections)
                        if source:
                            entries.append({'sheet': sheet, 'face': face,
                                            'batch': bid, 'mark': mid,
                                            'code': code, 'polygon': source})
                        continue
                    part = intersect(piece, poly)
                    if part:
                        source = _source(part, reflections)
                        if source:
                            entries.append({'sheet': sheet, 'face': face,
                                            'batch': bid, 'mark': mid,
                                            'code': code, 'polygon': source})
                    for leftover in difference(piece, poly):
                        rest.append((bbox(leftover), leftover))
                remaining = rest
    sheets_json = [{'id': key, 'width': _out(w), 'height': _out(h)}
                   for key, w, h in sheets]
    batches_json = [{'id': bid, 'priority': priority}
                    for bid, priority, _ in sorted(batches, key=lambda b: b[0])]
    entries.sort(key=_region_key)
    regions = [{'sheet': e['sheet'], 'face': e['face'], 'batch': e['batch'],
                'mark': e['mark'], 'code': e['code'],
                'polygon': [[_out(x), _out(y)] for x, y in e['polygon']]}
               for e in entries]
    archive = {'format': FORMAT, 'version': VERSION, 'sheets': sheets_json,
               'batches': batches_json, 'regions': regions}
    _check_size(archive)
    return archive


def _parse_archive(value):
    if not isinstance(value, dict):
        raise ValueError('archive object required')
    _check_size(value)
    fields(value, ('format', 'version', 'sheets', 'batches', 'regions'))
    if value['format'] != FORMAT:
        raise ValueError(f'unsupported archive format: {value["format"]}')
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ValueError(f'unsupported archive version: {value["version"]}')
    sheets = {}
    order = []
    for item in array(value['sheets'], 1, 10):
        fields(item, ('id', 'width', 'height'))
        key = ident(item['id'])
        w = _exact(item['width'])
        h = _exact(item['height'])
        if w <= 0 or h <= 0:
            raise ValueError('positive sheet dimensions required')
        sheets[key] = (w, h)
        order.append(key)
    unique(order)
    batches = {}
    ids = []
    for item in array(value['batches'], 0, 64):
        fields(item, ('id', 'priority'))
        bid = ident(item['id'])
        batches[bid] = integer(item['priority'], 0, 1000000000)
        ids.append(bid)
    unique(ids)
    regions = []
    for item in array(value['regions'], 0, 4000000):
        fields(item, ('sheet', 'face', 'batch', 'mark', 'code', 'polygon'))
        sheet = item['sheet']
        if not isinstance(sheet, str) or sheet not in sheets:
            raise ValueError('unknown sheet in archive')
        face = item['face']
        if face not in FACES:
            raise ValueError('unknown face in archive')
        bid = item['batch']
        if not isinstance(bid, str) or bid not in batches:
            raise ValueError('unknown batch in archive')
        mid = ident(item['mark'])
        code = ident(item['code'])
        polygon = _polygon(item['polygon'])
        w, h = sheets[sheet]
        for x, y in polygon:
            if not (0 <= x <= w and 0 <= y <= h):
                raise ValueError('archive polygon outside sheet')
        regions.append({'sheet': sheet, 'face': face, 'batch': bid,
                        'mark': mid, 'code': code, 'polygon': polygon})
    return {'sheets': sheets, 'batches': batches, 'regions': regions}


def _parse_request(value, archive):
    fields(value, ('sheet', 'face', 'rect', 'ignore_batch'))
    sheet = value['sheet']
    if not isinstance(sheet, str) or sheet not in archive['sheets']:
        raise ValueError('unknown sheet')
    face = value['face']
    if face not in FACES:
        raise ValueError('face must be front or back')
    x0, y0, x1, y1 = (number(v) for v in array(value['rect'], 4, 4))
    if not (x0 < x1 and y0 < y1):
        raise ValueError('request rectangle must have positive width and height')
    w, h = archive['sheets'][sheet]
    if not (0 <= x0 and x1 <= w and 0 <= y0 and y1 <= h):
        raise ValueError('request rectangle outside sheet')
    ignore = value['ignore_batch']
    if ignore is not None and (not isinstance(ignore, str) or ignore not in archive['batches']):
        raise ValueError('unknown ignore_batch')
    return sheet, face, (x0, y0, x1, y1), ignore


def _cells(rect, window_box, n):
    """Grid cells touched by ``rect`` (clamped to the window)."""
    x0, y0, x1, y1 = rect
    wx0, wy0, wx1, wy1 = window_box
    sx = (wx1 - wx0) / n
    sy = (wy1 - wy0) / n
    i0 = min(n - 1, max(0, int((x0 - wx0) / sx)))
    i1 = min(n - 1, max(0, int((x1 - wx0) / sx)))
    j0 = min(n - 1, max(0, int((y0 - wy0) / sy)))
    j1 = min(n - 1, max(0, int((y1 - wy0) / sy)))
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def rebuild_window(archive, request):
    """Rebuild one original-paper window using only the archive and request.

    The winning mark at each position is the participating mark with the
    highest ``priority``; ties are broken by the lexicographically smallest
    ``(batch id, mark id)``.  The returned ``regions`` are disjoint simple
    polygons covering exactly the positive-area winning area inside the window.
    """
    state = _parse_archive(archive)
    sheet, face, rect, ignore = _parse_request(request, state)
    priorities = state['batches']
    window = _rect_polygon(rect)
    window_box = bbox(window)
    candidates = []
    for r in state['regions']:
        if r['sheet'] != sheet or r['face'] != face or r['batch'] == ignore:
            continue
        box = bbox(r['polygon'])
        if boxes_overlap(box, window_box):
            candidates.append((r, box))
    candidates.sort(key=lambda c: (-priorities[c[0]['batch']], c[0]['batch'], c[0]['mark'],
                                   tuple(v for point in c[0]['polygon'] for v in point)))

    # Uncovered pieces are indexed by a coarse uniform grid so each candidate is
    # only tested against pieces that can actually touch it.
    n = min(48, max(1, int(len(candidates) ** 0.5)))
    grid = {}

    def add(ident, box):
        for cell in _cells(box, window_box, n):
            grid.setdefault(cell, set()).add(ident)

    def drop(ident, box):
        for cell in _cells(box, window_box, n):
            bucket = grid.get(cell)
            if bucket is not None:
                bucket.discard(ident)

    pieces = {0: (window_box, window)}
    add(0, window_box)
    next_id = 1
    winners = []
    for entry, entry_box in candidates:
        if not pieces:
            break
        touched = set()
        for cell in _cells(entry_box, window_box, n):
            touched.update(grid.get(cell, ()))
        if not touched:
            continue
        for ident in sorted(touched):
            if ident not in pieces:
                continue
            box, piece = pieces[ident]
            if not boxes_overlap(box, entry_box):
                continue
            part = intersect(piece, entry['polygon'])
            if part:
                winners.append({'batch': entry['batch'], 'mark': entry['mark'],
                                'code': entry['code'], 'polygon': part})
            leftovers = difference(piece, entry['polygon'])
            if len(leftovers) == 1 and leftovers[0] == piece:
                continue
            del pieces[ident]
            drop(ident, box)
            for leftover in leftovers:
                new_box = bbox(leftover)
                pieces[next_id] = (new_box, leftover)
                add(next_id, new_box)
                next_id += 1
    winners.sort(key=lambda r: (r['batch'], r['mark'],
                                tuple(v for point in r['polygon'] for v in point)))
    return {'sheet': sheet, 'face': face,
            'rect': [_out(v) for v in rect],
            'ignore_batch': ignore,
            'regions': [{'batch': r['batch'], 'mark': r['mark'], 'code': r['code'],
                         'polygon': [[_out(x), _out(y)] for x, y in r['polygon']]}
                        for r in winners]}


def serialize_window(result):
    """Compact JSON text for a rebuild result (comma and colon separators)."""
    return json.dumps(result, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)
