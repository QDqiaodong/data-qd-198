#!/usr/bin/env python3
"""Directly runnable exact revision-impact example.

Run from the repository root with either::

    python examples/revision_example.py

or, without installing the package::

    PYTHONPATH=src python examples/revision_example.py

The script intentionally uses only the standard library.
"""
import json
import sys
from pathlib import Path

# Allow direct execution from a source checkout without installation.
SRC = Path(__file__).resolve().parents[1] / 'src'
if SRC.exists():
    sys.path.insert(0, str(SRC))

from paper_layers.revision import compare_revisions  # noqa: E402


def sheet(sheet_id, width, height):
    return {'id': sheet_id, 'width': width, 'height': height}


def main():
    payload = {
        'reference': {
            'sheets': [sheet('A', '4', '2'), sheet('B', '4', '2')],
            'folds': [],
        },
        'candidate': {
            # Only the bottom-to-top sheet order changes.
            'sheets': [sheet('B', '4', '2'), sheet('A', '4', '2')],
            'folds': [],
        },
    }
    report = compare_revisions(payload)
    print(json.dumps({
        'impact_zones': report['impact_zones'],
        'areas': report['areas'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
