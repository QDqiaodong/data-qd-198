"""Plural-module compatibility alias for :mod:`paper_layers.revision`."""
from .revision import *  # noqa: F401,F403
from .revision import (MAX_REVISION_POLYGONS, RevisionComplexityError,
                       RevisionError, compare_revisions)

__all__ = [
    'MAX_REVISION_POLYGONS', 'RevisionComplexityError', 'RevisionError',
    'compare_revisions',
]
