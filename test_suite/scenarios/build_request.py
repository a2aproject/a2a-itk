#!/usr/bin/env python3
"""Turn a scenario file into a ``/run`` request body.

``run_itk.sh`` used to ``curl -d @scenarios.json`` straight at the service,
which works only while every scenario file is JSON in the legacy shape. The
shared sets are YAML, so something has to convert. Doing it here rather than
in the shell keeps the one-line legacy path intact — a ``scenarios.json``
comes out the other side unchanged — while letting the same script post a
shared YAML set.

Deliberately does no resolving: roles are bound by the service, against the
``matrix.yaml`` inside the container, so a scenario means the same thing
whichever host posts it.

Runs inside the ITK container: reading YAML needs PyYAML, which the image has
and a bare CI runner may not. ``-`` means stdin/stdout, so the file is piped
in through ``docker exec`` without being copied anywhere. It lives under
``test_suite/`` rather than ``scripts/`` because ``.dockerignore`` keeps the
latter out of the image.

Usage::

    python -m test_suite.scenarios.build_request \\
        --scenarios scenarios.json --sut-sdk python --output run_request.json

    docker exec -i -w /app itk-service uv run python \\
        -m test_suite.scenarios.build_request \\
        --scenarios - --sut-sdk python --output - \\
        < scenarios.yaml > run_request.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def build(text: str, sut_sdk: str | None, label: str) -> dict:
    """Wrap a scenario document as a ``/run`` request body.

    Raises:
        SystemExit: The document is empty or unparseable. These are operator
            errors and a traceback in CI logs helps nobody.
    """
    try:
        # YAML 1.1 is a superset of JSON, so this reads both formats.
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        sys.exit(f'{label}: could not parse: {e}')

    if data is None:
        sys.exit(f'{label}: file is empty')

    if isinstance(data, list):
        tests = data
    elif isinstance(data, dict):
        # A lone traversal/v1 scenario needs no `tests:` wrapper.
        tests = data['tests'] if 'tests' in data else [data]
    else:
        sys.exit(f'{label}: expected a mapping or a list')

    if not isinstance(tests, list) or not tests:
        sys.exit(f'{label}: no scenarios found')

    body: dict = {'tests': tests}
    if sut_sdk:
        body['sut_sdk'] = sut_sdk
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenarios', required=True,
                        help="Scenario file, or '-' for stdin.")
    parser.add_argument(
        '--sut-sdk',
        help="SDK under test, e.g. 'python'. Resolves test_when and "
             'include_own_lines; ignored by legacy scenarios.',
    )
    parser.add_argument('--output', required=True,
                        help="Destination, or '-' for stdout.")
    args = parser.parse_args(argv)

    if args.scenarios == '-':
        text, label = sys.stdin.read(), '<stdin>'
    else:
        path = Path(args.scenarios)
        if not path.is_file():
            sys.exit(f'Scenario file not found: {path}')
        text, label = path.read_text(encoding='utf-8'), str(path)

    body = build(text, args.sut_sdk, label)
    payload = json.dumps(body)

    if args.output == '-':
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding='utf-8')
        # Progress goes to stderr so it can't corrupt a '-' payload.
        print(f'{args.output}: {len(body["tests"])} scenario declaration(s)'
              + (f", sut_sdk={body['sut_sdk']}" if 'sut_sdk' in body else ''),
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
