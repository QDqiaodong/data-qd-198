"""Exact convex-polygon helpers used by the annotation archive.

The folding model only ever produces convex fragments: a rectangle clipped by
half-planes stays convex, and reflections are isometries.  A rectangle mark is
convex too, so every piece we have to overlay is convex.  Keeping that
invariant lets us implement intersection and "remove a convex region" with
simple half-plane clips instead of a general polygon-boolean library.
"""
from fractions import Fraction

from .geometry import clip


def signed_area2(poly):
    return sum((p[0] * q[1] - p[1] * q[0] for p, q in zip(poly, poly[1:] + poly[:1])), Fraction(0))


def area(poly):
    return abs(signed_area2(poly)) / 2


def _clean(poly):
    out = []
    for p in poly:
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    # Drop exactly collinear interior vertices.  This does not change the
    # region, but keeps archive and window output small and stable.
    changed = True
    while changed and len(out) >= 3:
        changed = False
        keep = []
        n = len(out)
        for i in range(n):
            a, b, c = out[i - 1], out[i], out[(i + 1) % n]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if cross == 0 and min(a[0], c[0]) <= b[0] <= max(a[0], c[0]) and min(a[1], c[1]) <= b[1] <= max(a[1], c[1]):
                changed = True
            else:
                keep.append(b)
        out = keep
        if len(out) < 3:
            break
    return out


def normalize(poly):
    """Return a de-duplicated, collinear-free, counter-clockwise polygon."""
    out = _clean(list(poly))
    if len(out) < 3:
        return []
    out = _clean(out)
    if len(out) < 3 or area(out) == 0:
        return []
    if signed_area2(out) < 0:
        out.reverse()
    return out


def bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a, b):
    """Bounding-box test for precomputed (xmin, ymin, xmax, ymax) tuples."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def bbox_overlap(a, b):
    """Bounding-box test for two polygons."""
    return boxes_overlap(bbox(a), bbox(b))


def encloses(outer, inner):
    """True when the convex polygon ``inner`` lies inside convex ``outer``."""
    ox0, oy0, ox1, oy1 = bbox(outer)
    ix0, iy0, ix1, iy1 = bbox(inner)
    if not (ox0 <= ix0 and oy0 <= iy0 and ix1 <= ox1 and iy1 <= oy1):
        return False
    for a, b, c in halfplanes(outer):
        for p in inner:
            if a * p[0] + b * p[1] - c < 0:
                return False
    return True


def halfplanes(poly):
    """Inside half-planes (a*x+b*y >= c) of a counter-clockwise polygon."""
    out = []
    for p, q in zip(poly, poly[1:] + poly[:1]):
        a = p[1] - q[1]
        b = q[0] - p[0]
        c = q[0] * p[1] - q[1] * p[0]
        out.append((a, b, c))
    return out


def intersect(p, q):
    """Exact intersection of two convex polygons; [] when only edges touch."""
    p = normalize(p)
    q = normalize(q)
    if not p or not q or not bbox_overlap(p, q):
        return []
    for a, b, c in halfplanes(q):
        p = clip(p, a, b, c, True)
        if not p:
            return []
    return normalize(p)


def difference(p, q):
    """Return disjoint convex pieces covering p \\ q (interiors only)."""
    p = normalize(p)
    q = normalize(q)
    if not p:
        return []
    if not q or not bbox_overlap(p, q):
        return [p]
    remaining = [p]
    pieces = []
    for a, b, c in halfplanes(q):
        nxt = []
        for r in remaining:
            outside = clip(r, a, b, c, False)
            if outside:
                pieces.append(normalize(outside))
            inside = clip(r, a, b, c, True)
            if inside:
                nxt.append(inside)
        remaining = nxt
        if not remaining:
            break
    return [piece for piece in pieces if piece]
