"""A2A protocol versions.

What a version is and how two compare, defined once for everything that reads
one: the matrix parser, the traversal runner and the ACTS runner. A leaf module
with no package imports of its own, like ``transports.py``, so any of them can
depend on it without creating a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


_VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def parse_spec_version(text: str) -> tuple[int, int]:
    """An A2A version as ``(major, minor)``.

    Versions are compared as numbers, component by component: compared as
    text, ``1.10`` would sort before ``1.9``.

    Raises:
        ValueError: ``text`` is not a ``Major.Minor`` string.
    """
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError(
            f'{text!r} is not an A2A version; expected "Major.Minor", such as "1.1"'
        )
    return int(match[1]), int(match[2])


def newest_first(versions: Sequence[str]) -> tuple[str, ...]:
    """A list of A2A versions, sorted newest first.

    Raises:
        ValueError: An entry is not a ``Major.Minor`` string, or one is listed
            twice.
    """
    ordered = sorted(versions, key=parse_spec_version, reverse=True)
    if len(set(versions)) != len(versions):
        raise ValueError(f'lists a version twice: {list(versions)}')
    return tuple(ordered)


def since_1_1(version: str | None) -> bool:
    """Is ``version`` A2A 1.1 or later?"""
    return version is not None and parse_spec_version(version) >= (1, 1)


def call_version(
    caller: Sequence[str], callee: Sequence[str], forced: str | None
) -> str | None:
    """The A2A version a call is made at, from what the two agents speak.

    ``caller`` and ``callee`` list the versions each speaks, newest first. A
    caller that speaks A2A 1.1 reads a forced version from its instruction and
    uses it. Any other caller negotiates the newest version both speak.
    ``None`` when they speak no version in common: the call cannot be made.
    """
    if forced and since_1_1(caller[0]):
        return forced
    return next((v for v in caller if v in callee), None)
