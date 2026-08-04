"""Generic launcher: resolves (repo, sha) -> built agent dir, spawns.

Public types are re-exported here. The ``resolve()`` and ``spawn()``
functions live under submodules of the same name and are intentionally
NOT re-exported at the package level, because binding ``resolve`` at
``v2.launcher.resolve`` would shadow the ``v2.launcher.resolve`` submodule
(a well-known Python trap). Callers import them explicitly::

    from v2.launcher.resolve import resolve, spawn, LaunchSession
"""

from v2.launcher.errors import InfraFailure, PermanentError, Stage
from v2.launcher.resolve import LaunchSession
from v2.launcher.spec import Kind, TargetSpec


__all__ = [
    'InfraFailure',
    'Kind',
    'LaunchSession',
    'PermanentError',
    'Stage',
    'TargetSpec',
]
