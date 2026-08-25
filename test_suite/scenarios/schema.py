"""Scenario schemas: the legacy per-SDK shape, and ``traversal/v1``.

Two formats are live at once, on purpose. Each SDK moves to the new one when
its team is ready, and until the last one has, ``/run`` has to accept both.
A scenario file carrying a top-level ``schema:`` key is parsed as the new
format; one without it is the legacy shape every SDK's ``scenarios.json``
uses today. Both end up as :class:`itk_runner.Scenario` before execution, so
nothing downstream knows the difference.

``schema:`` is a string rather than a bare version number so an unrelated
kind of scenario — ACTS conformance, which drives raw HTTP rather than an
agent traversal — can slot in as ``schema: acts/v1`` without either having to
know about the other.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from test_suite.scenarios.topology import Topology
from test_suite.transports import Transport


PEER_PLACEHOLDER = '{peer}'


TRAVERSAL_V1 = 'traversal/v1'

# The SUT's identifier. Unlike peers it doesn't resolve through matrix.yaml —
# it's whatever is bind-mounted into the container.
SUT_ID = 'current'


class Tier(str, enum.Enum):
    """Which trigger a scenario belongs to."""

    PR = 'pr'
    NIGHTLY = 'nightly'


class Behavior(str, enum.Enum):
    """What the agents are asked to do. Matches ``testlib``'s vocabulary."""

    SEND_MESSAGE = 'send_message'
    PUSH_NOTIFICATION = 'push_notification'
    RESUBSCRIBE = 'resubscribe'


class Expand(str, enum.Enum):
    """How a multi-peer role list becomes scenarios.

    ``TOGETHER``
        One scenario holding every peer. A peer that cannot speak the
        transport leaves the graph — the hop to it would fail.

    ``PER_PEER``
        One SUT-plus-one scenario per peer, so a partially-capable peer
        simply gets fewer scenarios instead of being dropped.
    """

    TOGETHER = 'together'
    PER_PEER = 'per_peer'


class PeerRef(BaseModel):
    """One peer, named by SDK and version line rather than by agent id.

    ``{sdk: go, line: v03}`` instead of ``go_v03``: the pair is what
    ``matrix.yaml`` is keyed by, so a scenario never has to know that the
    identifier is the two joined with an underscore.
    """

    model_config = ConfigDict(extra='forbid', frozen=True)

    sdk: str
    line: str
    instance: int | None = Field(
        default=None,
        ge=2,
        description='Run an Nth independent copy of this peer, on its own '
                    'ports. Yields agent id `<sdk>_<line>_<instance>`; used '
                    'for same-SDK-talks-to-itself scenarios.',
    )

    def agent_id(self) -> str:
        base = f'{self.sdk}_{self.line}'
        return base if self.instance is None else f'{base}_{self.instance}'


class Roles(BaseModel):
    """Who takes part, by role rather than by concrete agent id."""

    model_config = ConfigDict(extra='forbid')

    sut: Literal['current'] = SUT_ID
    peers: list[PeerRef] | Literal['all'] = Field(
        description="Explicit peer list, or 'all' for every line in "
                    'matrix.yaml that supports the requested transports. '
                    "'all' is what makes adding an SDK a matrix-only change.",
    )
    include_sut: bool = Field(
        default=True,
        description='Set false for a scenario that exercises peers against '
                    "each other without the SUT — a2a-itk's own smoke set "
                    'does this so it can run with nothing checked out.',
    )
    include_own_lines: bool = Field(
        default=False,
        description="Also test the SUT against its own SDK's released lines "
                    '— a different question from cross-SDK compatibility. '
                    'Needs sut_sdk; without one it adds nothing.',
    )


class TestWhen(BaseModel):
    """Restricts a shared scenario to certain SUTs.

    The PR sets are shared, but not every scenario is meaningful for every
    SUT: java's PR run has no reason to carry a rust peer, since the
    rust-vs-java pair is already covered from rust's side. Filtering here
    keeps one shared file instead of five near-copies.
    """

    model_config = ConfigDict(extra='forbid')

    sut_sdk: list[str] = Field(
        description='Only run when the SUT is one of these SDKs.',
    )


class TraversalScenarioV1(BaseModel):
    """A role-based traversal scenario.

    The plural fields (``behaviors``, ``transport_sets``,
    ``streaming_variants``) expand as a Cartesian product into several
    executable scenarios. They exist because the nightly sets are already a
    hand-written product — 32 near-identical entries per SDK — and writing
    that out by hand is how the five repos drifted apart in the first place.
    Generated matrices build on the same mechanism rather than adding one.
    """

    model_config = ConfigDict(extra='forbid')

    schema_: Literal['traversal/v1'] = Field(alias='schema')
    name: str
    roles: Roles
    topology: Topology = Topology.STAR
    tier: Tier = Tier.NIGHTLY
    test_when: TestWhen | None = None
    expand: Expand = Field(
        default=Expand.TOGETHER,
        description='TOGETHER puts every peer in one graph; PER_PEER emits '
                    'one SUT-plus-one scenario per peer. Use {peer} in the '
                    'name to label each.',
    )

    # Singular or plural, never both — see the validator below.
    behavior: Behavior | None = None
    behaviors: list[Behavior] | None = None
    transports: list[Transport] | None = Field(
        default=None,
        description='Transports to cover. Each becomes its OWN scenario, so '
                    'one failing transport reports as one failure and names '
                    'itself, instead of sinking every transport in the batch. '
                    'Use `transport_sets` to deliberately run several in one '
                    'scenario.',
    )
    transport_sets: list[list[Transport]] | None = Field(
        default=None,
        description='Explicit groupings, one scenario per group. Only for '
                    'when several transports genuinely belong in one '
                    'traversal; prefer `transports`.',
    )
    streaming: bool | None = None
    streaming_variants: list[bool] | None = None

    edges: list[str] | None = Field(
        default=None,
        description='Explicit edge list, overriding `topology`. For graphs '
                    'the named topologies cannot express.',
    )
    build_subtests: bool = Field(
        default=False,
        description='Also run every traversable induced subgraph containing '
                    'the first agent.',
    )

    @model_validator(mode='after')
    def _exactly_one_of_each_pair(self) -> 'TraversalScenarioV1':
        """Reject giving both the singular and plural form of a field.

        Silently preferring one would make a scenario's coverage depend on a
        rule nobody reading the file can see.
        """
        for singular, plural in (
            ('behavior', 'behaviors'),
            ('transports', 'transport_sets'),
            ('streaming', 'streaming_variants'),
        ):
            if getattr(self, singular) is not None and getattr(self, plural) is not None:
                raise ValueError(
                    f'set either `{singular}` or `{plural}`, not both'
                )
        if self.behavior is None and self.behaviors is None:
            raise ValueError('one of `behavior` or `behaviors` is required')
        if self.transports is None and self.transport_sets is None:
            raise ValueError('one of `transports` or `transport_sets` is required')
        for field, value in (
            ('behaviors', self.behaviors),
            ('transport_sets', self.transport_sets),
            ('streaming_variants', self.streaming_variants),
        ):
            if value is not None and not value:
                raise ValueError(f'`{field}` must not be empty')
        if self.transports is not None and not self.transports:
            raise ValueError('`transports` must not be empty')
        if self.transport_sets is not None and any(not s for s in self.transport_sets):
            raise ValueError('`transport_sets` entries must not be empty')
        return self

    # -- normalised accessors, so the resolver never branches on which form
    #    the author used --------------------------------------------------

    def behavior_variants(self) -> list[Behavior]:
        return self.behaviors if self.behaviors is not None else [self.behavior]  # type: ignore[list-item]

    def transport_variants(self) -> list[list[Transport]]:
        """One entry per scenario to emit.

        ``transports`` splits — ``[jsonrpc, grpc]`` is two scenarios, not one
        carrying both. A traversal runs a separate circuit per transport and
        asserts the union of their trace tokens, so bundling them means any
        one transport failing marks the whole scenario failed, and the result
        name doesn't say which. Splitting makes each transport its own
        pass/fail, which is also what lets a known failure be excluded at
        transport granularity.
        """
        if self.transport_sets is not None:
            return self.transport_sets
        return [[t] for t in self.transports]  # type: ignore[union-attr]

    def streaming_options(self) -> list[bool]:
        if self.streaming_variants is not None:
            return self.streaming_variants
        return [bool(self.streaming)]


class LegacyScenario(BaseModel):
    """The shape every SDK's ``scenarios.json`` uses today.

    Frozen: these files live in five repos we don't all own, and they keep
    working unchanged until each one is migrated.
    """

    model_config = ConfigDict(extra='ignore')

    name: str
    sdks: list[str]
    behavior: str
    edges: list[str] | None = None
    protocols: list[str] | None = None
    streaming: bool = False
    build_subtests: bool = False


def is_traversal_v1(raw: object) -> bool:
    """Does this raw mapping declare the new schema?

    The absence of ``schema:`` is what identifies a legacy scenario, so this
    is the whole discriminator.
    """
    return isinstance(raw, dict) and raw.get('schema') == TRAVERSAL_V1
