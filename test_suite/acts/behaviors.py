"""The SUT behaviour contract file (spec §11.1).

Each SDK repo declares, in `acts/sut-behaviors.yaml`, which `tck-*` prefixes
its ITK agent implements. The runner reads it and **fails** a test needing a
prefix the SUT does not declare — not skips it. That is deliberate and comes
from the Phase 4 breakdown: a skip would let an SDK's missing support vanish
from its own conformance report, which is the opposite of what the report is
for.

A SUT with no contract file at all is a different case: nothing has been
claimed, so there is nothing to check against, and gating is off. That is what
`Runner(sut_behaviors=None)` means, and it is how a repo behaves before it
adopts §11.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


#: Where the contract lives inside an SDK checkout.
CONTRACT_PATH = Path('acts') / 'sut-behaviors.yaml'


class BehaviorsFileError(ValueError):
    """The contract file is missing a field, or is not a contract at all."""


class Behavior(BaseModel):
    """One declared behaviour. §11.1's `behavior`."""

    model_config = ConfigDict(extra='allow')

    prefix: str = Field(min_length=1)
    description: str = ''
    response_type: str | None = None
    terminal_state: str | None = None
    artifacts: list[dict[str, Any]] | None = None
    delay_ms: int | None = None
    streaming: bool | None = None


class SutBehaviors(BaseModel):
    """A parsed `sut-behaviors.yaml`."""

    model_config = ConfigDict(extra='forbid')

    acts_version: str
    behaviors: list[Behavior] = Field(min_length=1)

    def prefixes(self) -> frozenset[str]:
        return frozenset(b.prefix for b in self.behaviors)


def parse(raw: object, *, source: str = '<memory>') -> SutBehaviors:
    try:
        return SutBehaviors.model_validate(raw)
    except ValidationError as exc:
        raise BehaviorsFileError(f'{source}: {exc}') from exc


def load(path: Path) -> SutBehaviors:
    """Read and validate a contract file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise BehaviorsFileError(f'cannot read {path}: {exc}') from exc
    except yaml.YAMLError as exc:
        raise BehaviorsFileError(f'{path} is not valid YAML: {exc}') from exc
    return parse(raw, source=str(path))


def declared_by(repo_root: Path) -> frozenset[str] | None:
    """The prefixes an SDK checkout declares, or ``None`` if it declares none.

    ``None`` and an empty set mean different things and must not be conflated:
    no file is "this repo has not adopted §11, do not gate", while a file
    listing nothing is "this repo implements nothing", which fails every
    behaviour test. Returning the wrong one silently turns a conformance gate
    on or off.
    """
    path = repo_root / CONTRACT_PATH
    if not path.is_file():
        return None
    return load(path).prefixes()


__all__ = [
    'CONTRACT_PATH',
    'Behavior',
    'BehaviorsFileError',
    'SutBehaviors',
    'declared_by',
    'load',
    'parse',
]
