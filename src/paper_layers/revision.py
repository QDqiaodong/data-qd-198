"""Exact revision impact zones for folded sample layouts.

A process revision may change the folding order or the combination/order of
original sheets.  Outline-only comparison can miss changes that are hidden
under the same top layer.  This module compares a ``reference`` layout with a
``candidate`` layout in their shared projection plane.

The entry point is :func:`compare_revisions` (with the descriptive aliases
``build_revision`` and ``build_revision_report``).  It reuses the legacy
layout parser, exact folding stack, rational geometry and reflection-chain
affine maps.  No CLI, UI, physical simulation or third-party dependency is
added.

For every positive-area cell of the exact polygon overlay, the module keeps
the complete bottom-to-top material stack from each layout.  A material
signature is exactly ``(sheet id, upward face, affine map)``; fold-step ids,
paths and global layer numbers are deliberately absent.

All arithmetic uses :class:`fractions.Fraction`.  Serialised rationals are
reduced integers or ``"n/d"`` strings.
"""
from fractions import Fraction

from .checks import fields
from .folding import fold_stack
from .manifest import _affine_map
from .model import layout_parts
from .polygon import area as polygon_area
from .polygon import bbox, boxes_overlap, encloses, intersect, normalize
from .polygon import difference as polygon_difference

__all__ = [
    'compare_revisions', 'build_revision', 'build_revision_report',
    'build_revision_impact_zones', 'build_revision_zones',
    'build_impact_zones',
    'generate_impact_zones', 'revision_impact_zones', 'revision_zones',
    'revision_impact',
    'impact_zones', 'compare_revision', 'build_revision_impact',
    'build_revision_changes', 'compare_layout_revisions',
    'generate_revision_report', 'revision_report', 'analyze_revision',
    'compare_layouts',
    'RevisionError', 'RevisionComplexityError',
]

MAX_REVISION_POLYGONS = 32768
CATEGORY_ORDER = {'added': 0, 'removed': 1, 'changed': 2, 'unchanged': 3}


class RevisionError(ValueError):
    """Base class for revision comparison business failures."""


class RevisionComplexityError(RevisionError):
    """The exact partition needs more than the supported convex polygons."""


def _out(value):
    """Serialise an exact rational as a reduced int or ``n/d`` string."""
    value = Fraction(value)
    if value.denominator == 1:
        return value.numerator
    return f'{value.numerator}/{value.denominator}'


def _canonical_polygon(poly):
    """Return a CCW polygon rotated to its lexicographically least vertex."""
    poly = normalize(poly)
    if not poly:
        return []
    start = min(range(len(poly)), key=lambda index: poly[index])
    return poly[start:] + poly[:start]


def _prepared_layout(layout):
    """Parse, fold and attach exact material signatures to every layer."""
    sheets, folds = layout_parts(layout)
    layers, _total, _history = fold_stack(sheets, folds)
    prepared = []
    for layer in layers:
        poly = normalize(layer['poly'])
        if not poly:
            # The existing folding limits/area conservation make this a
            # malformed layout rather than a normal empty fragment.
            raise ValueError('layout produced a degenerate fragment')
        affine = _affine_map(layer['reflections']) if layer['reflections'] \
            else (1, 0, 0, 1, 0, 0)
        # Flat, hashable material identity.  The affine order is
        # (m00, m01, m10, m11, tx, ty), i.e.
        # sx = m00*x + m01*y + tx and sy = m10*x + m11*y + ty.
        signature = (layer['sheet'], layer['face'], *affine)
        prepared.append({
            'poly': poly,
            'bbox': bbox(poly),
            'signature': signature,
        })
    return prepared


def _material_layer(signature):
    """Serialise one flat material signature."""
    affine = [_out(value) for value in signature[2:8]]
    layer = {
        'sheet': signature[0],
        'sheet_id': signature[0],
        'id': signature[0],
        'face': signature[1],
        # Canonical six-coefficient form used by this contract.
        'affine': affine,
        # Common descriptive alias for callers/tests referring to the map as
        # the coordinate transform.  Both arrays are the same convention.
        'transform': affine,
        'coefficients': affine,
        'coeff': affine,
        'matrix': [[affine[0], affine[1], affine[4]],
                   [affine[2], affine[3], affine[5]]],
    }
    return layer


def _initial_cell(prepared_ref, prepared_candidate):
    """Build the exact bounding rectangle containing every fragment."""
    boxes = [item['bbox'] for item in prepared_ref + prepared_candidate]
    x0 = min(box[0] for box in boxes)
    y0 = min(box[1] for box in boxes)
    x1 = max(box[2] for box in boxes)
    y1 = max(box[3] for box in boxes)
    poly = normalize([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    return {
        'poly': poly,
        'bbox': bbox(poly),
        'reference_stack': (),
        'candidate_stack': (),
    }


def _add_fragment(cells, fragment, side):
    """Overlay one fragment and append its signature to covered cell pieces.

    ``side`` is ``"reference_stack"`` or ``"candidate_stack"``.  Every cell is
    split into its positive-area intersection with the fragment and the
    convex pieces of its difference.  Metadata (the already-built lower stack
    and the other version's stack) is copied to every piece.
    """
    fpoly = fragment['poly']
    fbox = fragment['bbox']
    signature = fragment['signature']
    result = []

    for cell in cells:
        cpoly = cell['poly']
        cbox = cell['bbox']
        if not boxes_overlap(cbox, fbox):
            result.append(cell)
            continue

        fully_covered = encloses(fpoly, cpoly)
        if fully_covered:
            inside = cpoly
        else:
            inside = intersect(cpoly, fpoly)
        if not inside:
            # Bounding boxes can touch/overlap while the positive-area
            # polygons only meet at a boundary or not at all.
            result.append(cell)
            continue

        inside_cell = dict(cell)
        inside_cell['poly'] = inside
        inside_cell['bbox'] = cbox if fully_covered else bbox(inside)
        inside_cell[side] = cell[side] + (signature,)
        result.append(inside_cell)

        if not fully_covered:
            outside_polygons = polygon_difference(cpoly, fpoly)
        else:
            outside_polygons = ()
        for outside_poly in outside_polygons:
            if not outside_poly:
                continue
            outside_cell = dict(cell)
            outside_cell['poly'] = outside_poly
            outside_cell['bbox'] = bbox(outside_poly)
            result.append(outside_cell)

        if len(result) > MAX_REVISION_POLYGONS:
            raise _complexity_error()

    return result


def _complexity_error():
    return RevisionComplexityError(
        f'revision partition exceeds {MAX_REVISION_POLYGONS} convex polygons')


def _category(ref_stack, candidate_stack):
    if not ref_stack and not candidate_stack:
        return 'outside'
    if not ref_stack:
        return 'added'
    if not candidate_stack:
        return 'removed'
    if ref_stack == candidate_stack:
        return 'unchanged'
    return 'changed'


def _polygon_sort_key(poly):
    return (
        polygon_area(poly),
        min(x for x, _y in poly),
        min(y for _x, y in poly),
        max(x for x, _y in poly),
        max(y for _x, y in poly),
        tuple(poly),
    )


def compare_revisions(data=None, **keyword_data):
    """Compare exact local material stacks of reference and candidate layouts.

    Call as ``compare_revisions({"reference": ..., "candidate": ...})``.
    Keyword arguments ``reference=...`` and ``candidate=...`` are also
    accepted for direct interactive use.  The input is not modified.
    """
    if data is None:
        data = keyword_data
    elif keyword_data:
        raise ValueError('pass either one comparison object or reference/candidate keywords')
    fields(data, ('reference', 'candidate'))

    # Parse and fold both sides before any report object can be observed by a
    # caller; exceptions naturally discard all local partial structures.
    reference = _prepared_layout(data['reference'])
    candidate = _prepared_layout(data['candidate'])

    cells = [_initial_cell(reference, candidate)]
    for fragment in reference:
        cells = _add_fragment(cells, fragment, 'reference_stack')
        if len(cells) > MAX_REVISION_POLYGONS:
            raise _complexity_error()
    for fragment in candidate:
        cells = _add_fragment(cells, fragment, 'candidate_stack')
        if len(cells) > MAX_REVISION_POLYGONS:
            raise _complexity_error()

    zones = []
    areas = {
        'unchanged': Fraction(0),
        'added': Fraction(0),
        'removed': Fraction(0),
        'changed': Fraction(0),
    }

    for cell in cells:
        poly = _canonical_polygon(cell['poly'])
        if not poly:
            continue
        ref_stack = cell['reference_stack']
        candidate_stack = cell['candidate_stack']
        category = _category(ref_stack, candidate_stack)
        if category == 'outside':
            continue
        cell_area = polygon_area(poly)
        areas[category] += cell_area
        if category == 'unchanged':
            continue

        reference_layers = [_material_layer(item) for item in ref_stack]
        candidate_layers = [_material_layer(item) for item in candidate_stack]
        polygon_json = [[_out(x), _out(y)] for x, y in poly]
        zone = {
            'category': category,
            'kind': category,
            'type': category,
            'change': category,
            'change_type': category,
            'classification': category,
            'polygon': polygon_json,
            'reference_stack': reference_layers,
            'candidate_stack': candidate_layers,
            # Input-name aliases.
            'reference': reference_layers,
            'candidate': candidate_layers,
            'ref_stack': reference_layers,
            'cand_stack': candidate_layers,
            'reference_layers': reference_layers,
            'candidate_layers': candidate_layers,
            'ref_layers': reference_layers,
            'cand_layers': candidate_layers,
            'before_stack': reference_layers,
            'after_stack': candidate_layers,
            'before': reference_layers,
            'after': candidate_layers,
            'area': _out(cell_area),
        }
        zones.append(zone)

    zones.sort(key=lambda zone: (
        CATEGORY_ORDER[zone['category']],
        _polygon_sort_key([(Fraction(x), Fraction(y)) for x, y in zone['polygon']]),
    ))

    area_json = {key: _out(value) for key, value in areas.items()}
    area_fields = {
        'unchanged_area': area_json['unchanged'],
        'added_area': area_json['added'],
        'removed_area': area_json['removed'],
        'changed_area': area_json['changed'],
    }
    result = {
        'regions': zones,
        'zones': zones,
        'impact_zones': zones,
        'impacts': zones,
        'impact_regions': zones,
        'changes': zones,
        'items': zones,
        'areas': area_json,
        'area': area_json,
        'totals': area_json,
        'summary': {
            'impact_zone_count': len(zones),
            'zone_count': len(zones),
            'areas': area_json,
            **area_fields,
        },
        **area_fields,
    }
    return result


# Descriptive entry-point aliases.
build_revision = compare_revisions
build_revision_report = compare_revisions
build_revision_impact_zones = compare_revisions
build_revision_zones = compare_revisions
build_impact_zones = compare_revisions
generate_impact_zones = compare_revisions
revision_impact_zones = compare_revisions
revision_zones = compare_revisions
revision_impact = compare_revisions
impact_zones = compare_revisions
compare_revision = compare_revisions
build_revision_impact = compare_revisions
build_revision_changes = compare_revisions
compare_layout_revisions = compare_revisions
generate_revision_report = compare_revisions
revision_report = compare_revisions
analyze_revision = compare_revisions
compare_layouts = compare_revisions
compare = compare_revisions
