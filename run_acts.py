#!/usr/bin/env python3
"""Run the ACTS conformance corpus against a local SDK checkout.

The dev counterpart of ``POST /run-acts``: same pipeline, no container.

    ./run_acts.py --mount ../a2a-python/itk --sdk a2a-python --language python
    ./run_acts.py --mount ../a2a-python/itk --sdk a2a-python -t CORE-SEND-001

``--mount`` points the SUT at a checkout's ``itk/`` directory, exactly as the
container's bind mount does, by setting ``$ITK_MOUNT_DIR`` before the launcher
reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--mount', type=Path, required=True,
        help="The SDK's itk/ directory to run as the SUT.",
    )
    parser.add_argument('--sdk', required=True, help='SDK name for the report.')
    parser.add_argument('--sdk-version', default='unknown')
    parser.add_argument('--language', default='unknown')
    parser.add_argument('--repository', default=None)
    parser.add_argument(
        '--transport', default='jsonrpc', choices=['jsonrpc', 'grpc', 'rest'],
    )
    parser.add_argument(
        '-t', '--test', action='append', dest='tests', metavar='ID',
        help='Run only this test id. Repeatable.',
    )
    parser.add_argument(
        '--suite', type=Path, default=None,
        help='An alternative *.acts.yaml manifest.',
    )
    parser.add_argument(
        '--out', type=Path, default=None,
        help='Directory to write the §13 report into.',
    )
    parser.add_argument(
        '--no-gate', action='store_true',
        help="Ignore the SUT's acts/sut-behaviors.yaml contract.",
    )
    parser.add_argument('--json', action='store_true', help='Print the report.')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(levelname)s %(name)s: %(message)s',
    )

    mount = args.mount.resolve()
    if not mount.is_dir():
        parser.error(f'--mount {mount} is not a directory')
    # Read by test_suite.launcher.config.mount_dir(); must be set before the
    # launcher is imported anywhere that caches it.
    os.environ['ITK_MOUNT_DIR'] = str(mount)

    import acts_runner
    from test_suite.acts import report as report_writer
    from test_suite.acts.schema import TransportBinding

    try:
        result = asyncio.run(
            acts_runner.run(
                transport=TransportBinding(args.transport),
                suite_path=args.suite,
                test_ids=args.tests,
                # The corpus names these and no document defines them; story
                # 4.6 owns supplying them, and locally they only need to be
                # values the SUT will reject.
                variables={
                    'insufficientAuthToken': 'itk-insufficient-token',
                    'otherUserTaskId': '00000000-0000-0000-0000-0000000000ff',
                },
                gate_on_behaviors=not args.no_gate,
            )
        )
    except acts_runner.ActsRunError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    report = acts_runner.to_report(
        result,
        sdk_name=args.sdk,
        sdk_version=args.sdk_version,
        language=args.language,
        repository=args.repository,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        report_writer.render(report)
        for test_id, message in report_writer.failures(report):
            print(f'    {test_id}: {message}')

    if args.out:
        path = report_writer.write(report, args.out)
        print(f'\nreport: {path}')

    return 0 if report_writer.is_conformant(report) else 1


if __name__ == '__main__':
    sys.exit(main())
