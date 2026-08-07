"""Generic launcher: resolves (repo, sha) -> built agent dir, spawns.

Public types are re-exported here. The ``resolve()`` and ``spawn()``
functions live under submodules of the same name and are intentionally
NOT re-exported at the package level, because binding ``resolve`` at
``test_suite.launcher.resolve`` would shadow the submodule of the same
name (a well-known Python trap). Callers import them explicitly::

    from test_suite.launcher.resolve import resolve, spawn, LaunchSession
    from test_suite.launcher.cluster import Cluster, AgentHandle, StartOutcome
"""

from test_suite.launcher.cluster import AgentHandle, Cluster, StartOutcome
from test_suite.launcher.errors import InfraFailure, PermanentError, Stage
from test_suite.launcher.resolve import LaunchSession
from test_suite.launcher.spec import Kind, TargetSpec


__all__ = [
    'AgentHandle',
    'Cluster',
    'InfraFailure',
    'Kind',
    'LaunchSession',
    'PermanentError',
    'Stage',
    'StartOutcome',
    'TargetSpec',
]
