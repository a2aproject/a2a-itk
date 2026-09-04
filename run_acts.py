#!/usr/bin/env python3
"""Run the ACTS conformance corpus against a local SDK checkout.

The dev counterpart of ``POST /run-acts``: same pipeline, no container.

    ./run_acts.py --mount ../a2a-python/itk --sdk a2a-python --language python
    ./run_acts.py --mount ../a2a-python/itk --sdk a2a-python --transport all
    ./run_acts.py --mount ../a2a-python/itk --sdk a2a-python -t CORE-SEND-001

Each binding gets its own fresh SUT, matching what the nightly does: one
`POST /run-acts` per transport, so no run inherits another's task store.

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


#: Every binding, in the order a run reports them.
ALL_BINDINGS = ('jsonrpc', 'grpc', 'rest')


def _bindings(requested: list[str] | None) -> list[str]:
    """Which bindings to run, de-duplicated and in a stable order."""
    if not requested:
        return ['jsonrpc']
    if 'all' in requested:
        return list(ALL_BINDINGS)
    return [b for b in ALL_BINDINGS if b in set(requested)]


def _render_combined(reports: list, report_writer) -> None:
    """One line per binding, then the verdict across all of them.

    Conformance is the conjunction: passing over JSON-RPC and failing over
    gRPC is not conformant, and that is the number worth seeing after a
    multi-binding run rather than three separate verdicts.
    """
    print('=' * 56)
    print(f"ACROSS {len(reports)} BINDING(S)")
    print('=' * 56)
    for report in reports:
        summary = report['summary']
        must = summary['by_level']['must']
        graded = must['total'] - must['skipped']
        verdict = 'ok' if report_writer.is_conformant(report) else 'NOT CONFORMANT'
        print(
            f"  {report['transport']:8} {summary['passed']:3}/{summary['total']} passed"
            f"   must {must['passed']}/{graded}"
            f"   {summary['failed']} failed, {summary['errors']} error(s)   {verdict}"
        )
    conformant = all(report_writer.is_conformant(r) for r in reports)
    print(f"  => {'CONFORMANT' if conformant else 'NOT CONFORMANT'} overall")


def _generate_protos(mount: Path) -> None:
    """Generate the mounted agent's protobuf stubs, as `run_itk.sh` would.

    The launcher builds and runs codegen for a ``CHECKOUT`` spec but not for a
    ``MOUNT`` one: in CI the SDK's own ``run_itk.sh`` does it before the
    service starts. Nothing plays that role here, so a fresh checkout fails
    readiness with a bare ``ModuleNotFoundError: pyproto`` 35 seconds later,
    which says nothing about the cause. Doing it here makes ``--mount`` a
    complete driver rather than half of one.

    Idempotent per language — `prepare_python` returns immediately when the
    stubs already exist.
    """
    from test_suite.launcher import builders

    try:
        language = builders.detect_language(mount)
    except RuntimeError as exc:
        logging.getLogger(__name__).warning(
            'Cannot tell what language %s is (%s); skipping codegen.', mount, exc
        )
        return

    prepare = builders._CODEGEN_PREPARERS.get(language)  # noqa: SLF001
    if prepare is None:
        return
    logging.getLogger(__name__).info(
        'Generating %s protobuf stubs in %s', language.value, mount
    )
    prepare(mount)


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
        '--transport', action='append', dest='transports', metavar='BINDING',
        choices=['jsonrpc', 'grpc', 'rest', 'all'],
        help='Binding to run against. Repeatable, or `all` for every one. '
             'Each gets its own fresh SUT, as the nightly does. '
             'Default: jsonrpc.',
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
    parser.add_argument(
        '--no-codegen', action='store_true',
        help="Skip generating the agent's protobuf stubs before starting it.",
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

    if not args.no_codegen:
        _generate_protos(mount)

    reports = []
    for binding in _bindings(args.transports):
        try:
            result = asyncio.run(
                acts_runner.run(
                    transport=TransportBinding(binding),
                    suite_path=args.suite,
                    test_ids=args.tests,
                    # The corpus names these and no document defines them;
                    # locally they only need to be values the SUT will reject.
                    variables={
                        'insufficientAuthToken': 'itk-insufficient-token',
                        'otherUserTaskId': '00000000-0000-0000-0000-0000000000ff',
                    },
                    gate_on_behaviors=not args.no_gate,
                )
            )
        except acts_runner.ActsRunError as exc:
            print(f'error ({binding}): {exc}', file=sys.stderr)
            return 2

        reports.append(
            acts_runner.to_report(
                result,
                sdk_name=args.sdk,
                sdk_version=args.sdk_version,
                language=args.language,
                repository=args.repository,
            )
        )

    if args.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=2))
    else:
        for report in reports:
            report_writer.render(report)
            for test_id, message in report_writer.failures(report):
                print(f'    {test_id}: {message}')
            print()
        if len(reports) > 1:
            _render_combined(reports, report_writer)

    if args.out:
        for report in reports:
            print(f'report: {report_writer.write(report, args.out)}')

    return 0 if all(report_writer.is_conformant(r) for r in reports) else 1


if __name__ == '__main__':
    sys.exit(main())
