"""Pure evidence logic for the layout-regression offline evidence set.

This module never opens a database or touches the filesystem: it validates
ingest batches against the legacy entry points, attributes one observation to
exactly one reference revision, materialises evidence rows, diffs two
materialised row sets and builds/validates the plain-JSON increment packages.
The persistent source repository and the receiver store the results.

Attribution rule for one observation at field sequence number ``observed_at``:
take the reference of the same case whose ``effective_from`` is the largest
value not greater than ``observed_at``; then look up the observation's probe in
that revision only.  Without a revision or without the probe in that revision
no evidence row is produced and older revisions are never consulted as a
fallback.  Reference answers reuse the complete ordered hit list of the legacy
``process`` entry point (including multiple hits on a crease), keeping only
``layer, sheet, face, source_point`` per hit; step paths are not compared,
sheet numbers are not rewritten and no float tolerance is used.
"""
import json
import re
from fractions import Fraction

from .checks import array, fields, ident
from .service import process

PACKAGE_FORMAT = 'paper-evidence-package'
PACKAGE_VERSION = 1
MAX_BATCH_ITEMS = 1000
FACES = ('front', 'back')
HIT_FIELDS = ('layer', 'sheet', 'face', 'source_point')
OBSERVATION_FIELDS = ('id', 'case', 'observed_at', 'probe', 'hits')
REFERENCE_FIELDS = ('case', 'revision', 'effective_from', 'layout', 'probes')
ROW_FIELDS = ('id', 'case', 'observed_at', 'probe', 'hits',
              'revision', 'expected_hits', 'matches')
CHANGE_FIELDS = ('id', 'before', 'after')
PACKAGE_FIELDS = ('format', 'version', 'dataset', 'after', 'through', 'changes')

# Output-caliber exact numbers: an integer or a reduced ``n/d`` fraction,
# optionally negative, matching ``str(Fraction(...))``.
_OUTPUT_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?')


def nonneg_int(value):
    """A non-negative integer; booleans are rejected even though ``bool`` is ``int``."""
    if type(value) is not int or value < 0:
        raise ValueError('non-negative integer required')
    return value


def output_number(value):
    """Validate one reduced integer/fraction string in the legacy output caliber."""
    if not isinstance(value, str) or not _OUTPUT_NUMBER.fullmatch(value):
        raise ValueError('reduced integer or fraction string required')
    if str(Fraction(value)) != value:
        raise ValueError('reduced integer or fraction string required')
    return value


def validate_hit(item):
    """Validate one four-field hit (an observation hit or a package row hit)."""
    fields(item, HIT_FIELDS)
    nonneg_int(item['layer'])
    ident(item['sheet'])
    if item['face'] not in FACES:
        raise ValueError('face must be front or back')
    point = array(item['source_point'], 2, 2)
    for coordinate in point:
        output_number(coordinate)
    return item


def validate_hits(hits):
    """Validate a hit list: strictly ascending non-negative layer numbers."""
    if not isinstance(hits, list):
        raise ValueError('hits array required')
    previous = None
    for item in hits:
        validate_hit(item)
        if previous is not None and item['layer'] <= previous:
            raise ValueError('hits must be strictly ascending by layer')
        previous = item['layer']
    return hits


def validate_observation(item):
    """Validate one observation record (exactly five fields)."""
    fields(item, OBSERVATION_FIELDS)
    ident(item['id'])
    ident(item['case'])
    ident(item['probe'])
    nonneg_int(item['observed_at'])
    validate_hits(item['hits'])
    return item


def reference_expected(layout, probes):
    """Run the legacy ``process`` answer and keep the four evidence fields.

    The call also performs the legacy layout/probe validation (sheets/folds
    limits, unique ids, query point syntax, the 4096-fragment limit, ...).
    """
    result = process({'sheets': layout['sheets'],
                      'folds': layout['folds'],
                      'queries': probes})
    return {
        answer['id']: [
            {'layer': hit['layer'], 'sheet': hit['sheet'],
             'face': hit['face'], 'source_point': list(hit['source_point'])}
            for hit in answer['hits']
        ]
        for answer in result['queries']
    }


def validate_reference(item):
    """Validate one reference declaration and return its probe answer map."""
    fields(item, REFERENCE_FIELDS)
    ident(item['case'])
    ident(item['revision'])
    nonneg_int(item['effective_from'])
    fields(item['layout'], ('sheets', 'folds'))
    if not isinstance(item['probes'], list):
        raise ValueError('probes array required')
    for probe in item['probes']:
        fields(probe, ('id', 'point'))
    # ``process`` is the single source of layout/probe validation and answers.
    expected = reference_expected(item['layout'], item['probes'])
    return expected


def validate_batch(batch):
    """Validate one ingest batch; return ``(batch_id, [probe answer maps])``.

    The two arrays together must carry 1..1000 items.  Identity conflict
    checks against stored data and the within-batch uniqueness checks live in
    the repository, so that a rejected batch never touches the database.
    """
    fields(batch, ('batch', 'references', 'observations'))
    bid = ident(batch['batch'])
    references = array(batch['references'], 0, MAX_BATCH_ITEMS)
    observations = array(batch['observations'], 0, MAX_BATCH_ITEMS)
    total = len(references) + len(observations)
    if not 1 <= total <= MAX_BATCH_ITEMS:
        raise ValueError('batch must carry 1..1000 references and observations together')
    expected_maps = [validate_reference(item) for item in references]
    for item in observations:
        validate_observation(item)
    return bid, expected_maps


def validate_row(row, expected_id=None):
    """Validate one materialised evidence row as carried inside a package."""
    fields(row, ROW_FIELDS)
    ident(row['id'])
    ident(row['case'])
    ident(row['probe'])
    ident(row['revision'])
    nonneg_int(row['observed_at'])
    if type(row['matches']) is not bool:
        raise ValueError('matches must be a boolean')
    validate_hits(row['hits'])
    validate_hits(row['expected_hits'])
    if expected_id is not None and row['id'] != expected_id:
        raise ValueError('row id does not match its change id')
    return row


def validate_package(package):
    """Full structural validation of one increment package.

    Raises plain ``ValueError``; the receiver wraps the failure into its own
    distinguishable error classes.
    """
    fields(package, PACKAGE_FIELDS)
    if package['format'] != PACKAGE_FORMAT:
        raise ValueError(f'unsupported package format: {package["format"]}')
    if type(package['version']) is not int or package['version'] != PACKAGE_VERSION:
        raise ValueError(f'unsupported package version: {package["version"]}')
    ident(package['dataset'])
    nonneg_int(package['after'])
    nonneg_int(package['through'])
    if package['after'] >= package['through']:
        raise ValueError('package interval must satisfy after < through')
    changes = package['changes']
    if not isinstance(changes, list):
        raise ValueError('package changes array required')
    seen = set()
    previous = None
    for change in changes:
        fields(change, CHANGE_FIELDS)
        cid = ident(change['id'])
        if cid in seen:
            raise ValueError('duplicate change id in package')
        if previous is not None and cid <= previous:
            raise ValueError('package changes must appear once in ascending id order')
        seen.add(cid)
        previous = cid
        before, after = change['before'], change['after']
        if before is not None:
            validate_row(before, cid)
        if after is not None:
            validate_row(after, cid)
    return package


def materialize(references, observations):
    """Materialise the evidence rows visible from one committed state.

    ``references`` are mappings carrying ``case, revision, effective_from``
    and the precomputed ``expected`` probe-answer map; ``observations`` carry
    the stored observation fields.  Only data introduced up to that commit may
    be passed in, which makes late references change attribution inside their
    affected time window without rewriting history visible to earlier commits.
    """
    by_case = {}
    for reference in references:
        by_case.setdefault(reference['case'], []).append(reference)
    rows = {}
    for observation in observations:
        versions = by_case.get(observation['case'])
        if not versions:
            continue
        candidates = [r for r in versions
                      if r['effective_from'] <= observation['observed_at']]
        if not candidates:
            # No revision is effective yet; keep the observation row-less.
            continue
        chosen = max(candidates, key=lambda r: r['effective_from'])
        expected = chosen['expected'].get(observation['probe'])
        if expected is None:
            # The active revision does not know this probe: no fallback to
            # older revisions, so the observation stays without a row.
            continue
        hits = observation['hits']
        rows[observation['id']] = {
            'id': observation['id'],
            'case': observation['case'],
            'observed_at': observation['observed_at'],
            'probe': observation['probe'],
            'hits': hits,
            'revision': chosen['revision'],
            'expected_hits': expected,
            'matches': hits == expected,
        }
    return rows


def diff_rows(before_rows, after_rows):
    """Net row changes between two endpoint row sets, ids ascending once.

    Ids absent on one side carry ``null`` there; ids that are equal at both
    endpoints (including ids that only changed inside the interval) are
    omitted.
    """
    changes = []
    for cid in sorted(set(before_rows) | set(after_rows)):
        before = before_rows.get(cid)
        after = after_rows.get(cid)
        if before != after:
            changes.append({'id': cid, 'before': before, 'after': after})
    return changes


def build_package(dataset, after, through, changes):
    """Assemble a package dict with the fixed format/version header."""
    ident(dataset)
    nonneg_int(after)
    nonneg_int(through)
    if after >= through:
        raise ValueError('package interval must satisfy after < through')
    return {'format': PACKAGE_FORMAT, 'version': PACKAGE_VERSION,
            'dataset': dataset, 'after': after, 'through': through,
            'changes': changes}


def canonical_json(value):
    """Deterministic compact JSON text used for retry/identity comparisons.

    Object key order is irrelevant to retry identity (hence ``sort_keys``),
    while array order is part of the content and is preserved exactly.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def package_dumps(package):
    """Compact UTF-8 JSON text of an increment package (plain JSON round trip)."""
    return canonical_json(package)


def package_loads(text):
    """Read package JSON text, rejecting duplicate keys and non-finite values."""
    if isinstance(text, bytes):
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('package text must be UTF-8') from None
    if not isinstance(text, str):
        raise ValueError('package JSON text required')

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
        raise ValueError('package object required')
    return value
