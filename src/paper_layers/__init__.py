"""Offline business baseline plus the annotation archive entry points.

The baseline API (``process`` and the folder/query helpers) is unchanged.  The
new offline entry points for handing quality-check annotations to downstream
processing are exported here for convenience:

``build_archive(data)``      build a self-contained original-paper package
``dumps(archive)``           compact UTF-8 JSON text of that package
``loads(text)``              read such a package back in a fresh process
``rebuild_window(a, req)``   rebuild one original-paper window from the package
``serialize_window(result)`` compact JSON text for a rebuild result
"""

from .archive import build_archive, dumps, loads, rebuild_window, serialize_window

__all__ = ['build_archive', 'dumps', 'loads', 'rebuild_window', 'serialize_window']
