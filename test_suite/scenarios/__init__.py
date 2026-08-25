"""Shared scenario definitions: schema, loading, and role resolution.

Scenarios used to name concrete agent identifiers (``["current",
"python_v10", "go_v03"]``) and hand-written edge lists, duplicated across
five SDK repositories that drifted apart. The ``traversal/v1`` schema here
names *roles* instead — a SUT and a set of peers, described by SDK and
version line — and binds them to concrete repos and refs through
``matrix.yaml`` at run time.

The legacy format is still fully supported; see :mod:`.schema`.
"""

from test_suite.scenarios.loader import (
    ScenarioFileError,
    load_file,
    load_files,
    parse_tests,
)
from test_suite.scenarios.schema import (
    SUT_ID,
    TRAVERSAL_V1,
    Behavior,
    LegacyScenario,
    PeerRef,
    Roles,
    Tier,
    Transport,
    TraversalScenarioV1,
    is_traversal_v1,
)
from test_suite.scenarios.resolver import (
    ResolutionError,
    ResolutionReport,
    ResolvedScenario,
    resolve,
    resolve_all,
)
from test_suite.scenarios.topology import Topology, normalize_edges, topology_to_edges


__all__ = [
    'SUT_ID',
    'TRAVERSAL_V1',
    'Behavior',
    'LegacyScenario',
    'PeerRef',
    'ResolutionError',
    'ResolutionReport',
    'ResolvedScenario',
    'Roles',
    'ScenarioFileError',
    'Tier',
    'Topology',
    'Transport',
    'TraversalScenarioV1',
    'is_traversal_v1',
    'load_file',
    'load_files',
    'normalize_edges',
    'parse_tests',
    'resolve',
    'resolve_all',
    'topology_to_edges',
]
