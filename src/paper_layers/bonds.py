"""Exact adhesive interfaces between the layers of a folded stack.

The sample-preparation team glues folded paper pieces along the actual
contact between neighbouring layers.  A work order names two original-paper
faces and a minimum contact area; the same material-face pair may touch at
several depths of the stack, and only the real local layer stack, the real
facing side and the exact overlap area can decide whether the order is met.

``build_bonds(data)``
    Take exactly ``{"layout": ..., "bonds": ...}`` — the legacy ``sheets`` /
    ``folds`` layout plus 0..1,024 adhesive specifications — and return the
    complete list of adjacent-layer interfaces together with one judgement
    per specification.  It reuses the existing layout parser, the exact
    half-plane clipping / reflection folding, the final bottom-to-top layer
    order and the source-coordinate reflection chain; no physical folding
    simulation, CLI or interactive surface is added.

Interfaces
----------
After folding, a layout position is covered by a bottom-to-top stack of
fragments; only two *locally adjacent* fragments of that stack touch there.
For every fragment ``i`` the part not covered by the fragments between it
and a later fragment ``j`` is intersected with ``j``: those positions are
exactly where ``i`` and ``j`` are locally adjacent.  Occluded overlap is
never counted, and fragments that are non-adjacent in the global layer order
can still touch where the fragments between them do not cover.  Edge- or
point-only contact has zero area and yields nothing.  Every interface keeps
both final layer numbers (zero-based, not necessarily consecutive) and is
never merged by sheet id across depths: two interfaces with identical
projection but different depths stay separate records.

The contact faces are physical: the lower fragment contributes the side it
currently presents upward, the upper fragment contributes the opposite of
its current upward side.  Each interface reports the contact polygon in
layout coordinates and that same polygon mapped back through each fragment's
reflection chain to its own original paper.  Mapped winding follows the
reflection, so mirror-related regions keep opposite orientation.  All
numbers are exact reduced integers or ``n/d`` strings.  Following the
annotation archive, a disconnected contact region becomes several interface
records — internally disjoint simple polygons that together cover exactly
the positive-area contact and never double-count area; a bounding box is
never substituted.

Judgements
----------
A specification is an unordered pair of material faces, matched regardless
of which side is upper.  All adjacent interfaces whose two contact faces
match contribute their areas, including identical projected regions at
different depths.  ``available_area >= min_area`` is the only satisfaction
condition; no match means zero available area.  Unsatisfied specifications
are normal results, never errors, and every computed interface is returned.
Different specifications may not declare the same unordered material-face
pair.

Invalid input (missing/extra fields, bad ids, duplicate identities, unknown
sheets, bad faces, non-positive thresholds, duplicate face pairs, invalid
layouts) raises ``ValueError`` and never returns a partial result.
"""
from fractions import Fraction

from .checks import array, fields, ident, number, unique
from .folding import fold_stack
from .geometry import reflect
from .model import layout_parts
from .polygon import (area as polygon_area, bbox, boxes_overlap,
                      difference, intersect, normalize)

FACES = ('front', 'back')
MAX_BONDS = 1024


def _flip(face):
    return 'back' if face == 'front' else 'front'


def _out(value):
    """Serialise an exact rational as an integer or a reduced ``n/d`` string."""
    value = Fraction(value)
    if value.denominator == 1:
        return value.numerator
    return f'{value.numerator}/{value.denominator}'


def _polygon_json(poly):
    return [[_out(x), _out(y)] for x, y in poly]


def _clean(poly):
    """Drop duplicate and collinear vertices while preserving vertex order.

    The winding is never reversed here: a reflection chain that mirrors a
    fragment must keep the mirrored orientation of its source-space region.
    """
    out = []
    for point in poly:
        if not out or point != out[-1]:
            out.append(point)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    changed = True
    while changed and len(out) >= 3:
        changed = False
        keep = []
        count = len(out)
        for index in range(count):
            a, b, c = out[index - 1], out[index], out[(index + 1) % count]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            between = (min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
                       and min(a[1], c[1]) <= b[1] <= max(a[1], c[1]))
            if cross == 0 and between:
                changed = True
            else:
                keep.append(b)
        out = keep
        if len(out) < 3:
            return []
    return out if len(out) >= 3 else []


def _canonical(poly):
    """Clean a polygon and rotate it to its lexicographically least vertex.

    The rotation is a cyclic shift, so the winding is preserved; it only
    makes the serialised order and the sort key independent of clipping.
    """
    poly = _clean(poly)
    if not poly or polygon_area(poly) == 0:
        return []
    start = min(range(len(poly)), key=lambda index: poly[index])
    return poly[start:] + poly[:start]


def _polygon_key(poly):
    return tuple(poly)


def _source(poly, reflections):
    """Map a layout-space polygon back to its original-paper coordinates."""
    out = []
    for point in poly:
        for a, b, c in reversed(reflections):
            point = reflect(point, a, b, c)
        out.append(point)
    return _canonical(out)


def _side(value, sheet_ids):
    fields(value, ('sheet', 'face'))
    sheet = value['sheet']
    if not isinstance(sheet, str) or sheet not in sheet_ids:
        raise ValueError('unknown sheet in bond side')
    face = value['face']
    if face not in FACES:
        raise ValueError('face must be front or back')
    return sheet, face


def _parse_bonds(value, sheet_ids):
    specs = []
    ids = []
    declared = []
    for item in array(value, 0, MAX_BONDS):
        fields(item, ('id', 'side_a', 'side_b', 'min_area'))
        bid = ident(item['id'])
        a = _side(item['side_a'], sheet_ids)
        b = _side(item['side_b'], sheet_ids)
        if a == b:
            raise ValueError('bond sides must not be the same material face')
        threshold = number(item['min_area'])
        if threshold <= 0:
            raise ValueError('positive min_area required')
        pair = frozenset((a, b))
        if pair in declared:
            raise ValueError('duplicate material-face pair in bonds')
        ids.append(bid)
        declared.append(pair)
        specs.append((bid, a, b, item['min_area'], threshold, pair))
    unique(ids)
    return specs


def _contacts(layers):
    """All positive-area locally adjacent contact pieces, in final layer order.

    Yields ``(lower_index, upper_index, polygon)`` with the polygon in layout
    coordinates, wound counter-clockwise.  Fragments separated only by an
    edge or a point produce no record.  A disconnected contact between two
    fragments yields one record per simple polygon.
    """
    prepared = []
    for layer in layers:
        poly = normalize(layer['poly'])
        prepared.append((bbox(poly), poly) if poly else None)
    count = len(layers)
    found = []
    for lower in range(count - 1):
        if prepared[lower] is None:
            continue
        # ``free`` is the part of fragment ``lower`` not covered by the
        # fragments between it and the current upper layer.  Intersecting it
        # with fragment ``upper`` yields exactly where the two are adjacent.
        free = [prepared[lower]]
        for upper in range(lower + 1, count):
            if not free:
                break
            if prepared[upper] is None:
                continue
            ubox, upoly = prepared[upper]
            rest = []
            for fbox, fpoly in free:
                if not boxes_overlap(fbox, ubox):
                    rest.append((fbox, fpoly))
                    continue
                part = intersect(fpoly, upoly)
                if part:
                    part = _canonical(part)
                    if part:
                        found.append((lower, upper, part))
                for leftover in difference(fpoly, upoly):
                    rest.append((bbox(leftover), leftover))
            free = rest
    found.sort(key=lambda item: (item[0], item[1], _polygon_key(item[2])))
    return found


def build_bonds(data):
    """Return the complete adjacent-layer interface list and bond judgements.

    ``data`` has exactly ``layout`` (legacy ``sheets``/``folds``) and
    ``bonds`` (0..1,024 ``{id, side_a, side_b, min_area}`` specifications).
    The result is a plain JSON-serialisable dict with ``interfaces`` and
    ``bonds``; the order of the ``bonds`` array never changes the delivery.
    """
    fields(data, ('layout', 'bonds'))
    sheets, folds = layout_parts(data['layout'])
    specs = _parse_bonds(data['bonds'], {sheet[0] for sheet in sheets})
    layers, _total, _history = fold_stack(sheets, folds)

    interfaces = []
    available = {}
    for lower, upper, poly in _contacts(layers):
        low = layers[lower]
        high = layers[upper]
        low_face = low['face']
        high_face = _flip(high['face'])
        contact_area = polygon_area(poly)
        interfaces.append({
            'lower_layer': lower,
            'upper_layer': upper,
            'lower_sheet': low['sheet'],
            'lower_face': low_face,
            'upper_sheet': high['sheet'],
            'upper_face': high_face,
            'polygon': _polygon_json(poly),
            'lower_polygon': _polygon_json(_source(poly, low['reflections'])),
            'upper_polygon': _polygon_json(_source(poly, high['reflections'])),
            'area': _out(contact_area),
        })
        pair = frozenset(((low['sheet'], low_face), (high['sheet'], high_face)))
        available[pair] = available.get(pair, Fraction(0)) + contact_area

    judgements = []
    for bid, a, b, raw_min, threshold, pair in specs:
        covered = available.get(pair, Fraction(0))
        judgements.append({
            'id': bid,
            'side_a': {'sheet': a[0], 'face': a[1]},
            'side_b': {'sheet': b[0], 'face': b[1]},
            'min_area': _out(raw_min),
            'available_area': _out(covered),
            'satisfied': covered >= threshold,
        })
    judgements.sort(key=lambda item: item['id'])
    return {'interfaces': interfaces, 'bonds': judgements}
