"""Offline business baseline plus the annotation archive entry points.

The baseline API (``process`` and the folder/query helpers) is unchanged.  The
new offline entry points for handing quality-check annotations to downstream
processing are exported here for convenience:

``build_archive(data)``      build a self-contained original-paper package
``dumps(archive)``           compact UTF-8 JSON text of that package
``loads(text)``              read such a package back in a fresh process
``rebuild_window(a, req)``   rebuild one original-paper window from the package
``serialize_window(result)`` compact JSON text for a rebuild result

The offline entry point for the train/evaluation release manifests:

``build_manifest(data)``     split samples into leakage-free train/evaluation
                             ID lists that meet the evaluation quotas exactly

Its business failures raise ``ManifestError`` subclasses
(``PreviousAssignmentConflictError``, ``QuotaUnsatisfiableError``); invalid
input raises plain ``ValueError``.

The offline layout-regression evidence store:

``SourceRepository.create/open``  persistent single-writer source repository
``Receiver.create/open``          evidence-only receiver for increment packages
``package_dumps/package_loads``   plain-JSON package round trip

Receiver failures split into ``ReceiverInputError`` (bad input/identity),
``ReceiverPositionError`` (dataset/interval/position) and
``ReceiverConflictError`` (before-row mismatch); source-repository failures
split into ``BatchConflictError`` and ``RepositoryRangeError``.

The offline re-verification probe manifest and field interpretation:

``build_handoff(data)``            optimal station-down-proof probe manifest
                                   plus per-version standard answers
``interpret_handoff(handoff, req)`` judge the received readings offline
``handoff_dumps/handoff_loads``    plain-JSON handoff round trip

Its business failures raise ``RecheckError`` subclasses
(``InfeasibleManifestError``, ``HandoffSizeError``); bad input at
interpretation time raises ``HandoffInputError`` (also a ``RecheckError``),
while invalid build input raises plain ``ValueError``.
"""

from .archive import build_archive, dumps, loads, rebuild_window, serialize_window
from .manifest import (ManifestError, PreviousAssignmentConflictError,
                       QuotaUnsatisfiableError, build_manifest)
from .receiver import (Receiver, ReceiverConflictError, ReceiverError,
                       ReceiverInputError, ReceiverPositionError)
from .recheck import (HandoffInputError, HandoffSizeError,
                      InfeasibleManifestError, RecheckError, build_handoff,
                      handoff_dumps, handoff_loads, interpret_handoff)
from .repository import (BatchConflictError, RepositoryError,
                         RepositoryRangeError, SourceRepository)
from .evidence import package_dumps, package_loads

__all__ = ['build_archive', 'dumps', 'loads', 'rebuild_window', 'serialize_window',
           'build_manifest', 'ManifestError', 'PreviousAssignmentConflictError',
           'QuotaUnsatisfiableError',
           'SourceRepository', 'BatchConflictError', 'RepositoryError',
           'RepositoryRangeError',
           'Receiver', 'ReceiverError', 'ReceiverInputError',
           'ReceiverPositionError', 'ReceiverConflictError',
           'package_dumps', 'package_loads',
           'build_handoff', 'interpret_handoff', 'handoff_dumps',
           'handoff_loads', 'RecheckError', 'InfeasibleManifestError',
           'HandoffSizeError', 'HandoffInputError']
