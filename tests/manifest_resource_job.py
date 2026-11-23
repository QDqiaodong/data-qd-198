"""Resource acceptance job for the release manifest entry point.

Standalone process: the address-space limit of 192 MiB is applied *before*
the fixture is generated, and the whole computation happens in this one
process — no subprocesses and no disk files are used to move data around.
Run directly (``python -I tests/manifest_resource_job.py``) or via
``test_manifest.ResourceAcceptance``.

Fixture (first release): 8,192 origin groups ordered by smallest sample id —
2,730 class-A groups (3 samples, 2 field), 2,731 class-B groups (2 samples,
1 field), 2,731 class-C groups (4 samples, 1 field).  Sample ids use a
letter prefix, a fixed-width group number and a member index so they sort
group-consecutively.  All layouts are distinct: one sheet, no folds, height
1, widths increasing from 1.  The evaluation side must be exactly 2,048
samples with 1,024 field samples: the first 408 A groups, the first 4 B
groups and the first 204 C groups; training is the complement.
"""
import resource
import sys
from pathlib import Path

LIMIT_BYTES = 192 * 1024 * 1024

GROUPS = (('a', 2730, 3, 2), ('b', 2731, 2, 1), ('c', 2731, 4, 1))
EXPECTED_PICKS = (('a', 408, 3), ('b', 4, 2), ('c', 204, 4))


def build_fixture():
    samples = []
    width = 0
    for prefix, group_count, size, field_count in GROUPS:
        for number in range(group_count):
            origin = f'o{prefix}{number:04d}'
            for member in range(size):
                width += 1
                samples.append({
                    'id': f'{prefix}{number:04d}m{member}',
                    'origin': origin,
                    'channel': 'field' if member < field_count else 'lab',
                    'layout': {'sheets': [{'id': 'P', 'width': str(width),
                                           'height': '1'}],
                               'folds': []},
                })
    return {'samples': samples, 'previous': None,
            'quota': {'evaluation_total': 2048, 'evaluation_field': 1024}}


def expected_evaluation():
    chosen = set()
    for prefix, picks, size in EXPECTED_PICKS:
        for number in range(picks):
            chosen.update(f'{prefix}{number:04d}m{member}' for member in range(size))
    return chosen


def main():
    resource.setrlimit(resource.RLIMIT_AS, (LIMIT_BYTES, LIMIT_BYTES))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
    from paper_layers.manifest import build_manifest

    data = build_fixture()
    result = build_manifest(data)

    evaluation = result['evaluation']
    train = result['train']
    channels = {sample['id']: sample['channel'] for sample in data['samples']}
    all_ids = {sample['id'] for sample in data['samples']}
    assert set(result) == {'train', 'evaluation'}, set(result)
    assert len(evaluation) == 2048, len(evaluation)
    assert sum(1 for sid in evaluation if channels[sid] == 'field') == 1024
    assert set(evaluation) == expected_evaluation()
    assert evaluation == sorted(evaluation)
    assert train == sorted(train)
    assert set(train) | set(evaluation) == all_ids
    assert not set(train) & set(evaluation)
    assert len(train) == len(all_ids) - 2048
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f'resource-ok evaluation={len(evaluation)} train={len(train)} '
          f'peak-rss-kib={peak}')


if __name__ == '__main__':
    main()
