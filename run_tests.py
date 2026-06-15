import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys

import httpx
from testlib import (
    execute_itk_test,
    start_itk_cluster,
    start_notification_server,
    stop_itk_cluster,
)


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Hardcoded test case definitions
TEST_CASES = [
    {
        'name': 'resubscribe-jsonrpc',
        'sdks': ['python_v10', 'go_v03'],
        'protocols': ['jsonrpc'],
        'edges': None,
        'streaming': True,
        'behavior': 'resubscribe',
    },
     {
        'name': 'resubscribe-grpc',
        'sdks': ['python_v03', 'python_v10', 'go_v03'],
        'protocols': ['grpc'],
        'edges': None,
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'resubscribe-python-all-protocols',
        'sdks': ['python_v03', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'resubscribe-v10-all-protocols',
        'sdks': ['python_v10', 'go_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'resubscribe-java-v10-all-protocols',
        'sdks': ['java_v10', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'resubscribe-v03-grpc',
        'sdks': ['python_v03', 'go_v03'],
        'protocols': ['grpc'],
        'edges': None,
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'go-v03-v10-push-notification',
        'sdks': ['go_v03', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': None,
        'behavior': 'push_notification',
    },
    {
        'name': 'python-v10-and-v03-sdks-push-notifications',
        'sdks': ['python_v10', 'python_v03', 'go_v03', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': None,
        'behavior': 'push_notification',
    },
    {
        'name': 'python-v10-and-v03-sdks-push-notifications-grpc-http-json',
        'sdks': ['python_v10', 'python_v03'],
        'protocols': ['grpc', 'http_json'],
        'edges': None,
        'behavior': 'push_notification',
    },
    {
        'name': 'v03-core',
        'sdks': ['python_v03', 'go_v03'],
        'edges': None,
        'protocols': ['jsonrpc', 'grpc'],
        'behavior': 'send_message',
    },
    {
        'name': 'v03-core-streaming',
        'sdks': ['python_v03', 'go_v03'],
        'edges': None,
        'protocols': ['jsonrpc', 'grpc'],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'v10-core',
        'sdks': ['python_v10', 'go_v10'],
        'protocols': ['http_json', 'jsonrpc', 'grpc'],
        'edges': None,
        'behavior': 'send_message',
    },
    {
        'name': 'v10-core-streaming',
        'sdks': ['python_v10', 'go_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'java-v10-core',
        'sdks': ['java_v10', 'python_v10'],
        'protocols': ['http_json', 'jsonrpc', 'grpc'],
        'edges': None,
        'behavior': 'send_message',
    },
    {
        'name': 'java-v10-core-streaming',
        'sdks': ['java_v10', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'java-v10-go-v10-core',
        'sdks': ['java_v10', 'go_v10'],
        'protocols': ['http_json', 'jsonrpc', 'grpc'],
        'edges': None,
        'behavior': 'send_message',
    },
    {
        'name': 'java-v10-push-notification',
        'sdks': ['java_v10', 'python_v10'],
        'protocols': ['jsonrpc'],
        'edges': None,
        'behavior': 'push_notification',
    },
    {
        'name': 'python-v03-v10-all-transports',
        'sdks': ['python_v03', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'behavior': 'send_message',
    },
    {
        'name': 'python-v03-v10-all-transports-streaming',
        'sdks': ['python_v03', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': None,
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'python-v03-go-v03-python-v10-hub-all-common-transports',
        'sdks': ['python_v03', 'go_v03', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc'],
        'edges': ['2->0', '2->1', '0->2', '1->2'],
        'behavior': 'send_message',
        'build_subtests': True,
    },
    {
        'name': 'python-v03-go-v03-python-v10-hub-all-common-transports-streaming',
        'sdks': ['python_v03', 'go_v03', 'python_v10'],
        'protocols': ['jsonrpc', 'grpc'],
        'edges': ['2->0', '2->1', '0->2', '1->2'],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'full-backwards-compat-with-jsonrpc',
        'sdks': ['python_v03', 'go_v03', 'python_v10', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': [
            '3->0',
            '3->1',
            '2->0',
            '2->1',
            '0->2',
            '0->3',
            '1->2',
            '1->3',
        ],
        'behavior': 'send_message',
    },
    {
        'name': 'full-backwards-compat-with-jsonrpc-streaming',
        'sdks': ['python_v03', 'go_v03', 'python_v10', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': [
            '3->0',
            '3->1',
            '2->0',
            '2->1',
            '0->2',
            '0->3',
            '1->2',
            '1->3',
        ],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'disconnected-components',
        'sdks': ['python_v03', 'go_v03', 'python_v10', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['1->3', '3->1', '2->0', '0->2'],
        'behavior': 'send_message',
    },
    {
        'name': 'failing-go-v03-http-json',
        'sdks': ['python_v03', 'python_v10', 'go_v03'],
        'protocols': ['http_json'],
        'edges': None,
        'behavior': 'send_message',
    },
    {
        'name': 'failing-go-v10-grpc',
        'sdks': ['go_v03', 'go_v10'],
        'protocols': ['grpc'],
        'edges': None,
        'behavior': 'send_message',
    },
    # --- Rust v1.0 (current-mount) scenarios ---
    {
        'name': 'rust-v10-send-message-jsonrpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['0->1', '1->0'],
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-send-message-grpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['grpc'],
        'edges': ['0->1', '1->0'],
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-send-message-http-json',
        'sdks': ['current', 'python_v10'],
        'protocols': ['http_json'],
        'edges': ['0->1', '1->0'],
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-streaming-jsonrpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-streaming-grpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['grpc'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-streaming-http-json',
        'sdks': ['current', 'python_v10'],
        'protocols': ['http_json'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-push-notification-jsonrpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['0->1', '1->0'],
        'behavior': 'push_notification',
    },
    {
        'name': 'rust-v10-push-notification-grpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['grpc'],
        'edges': ['0->1', '1->0'],
        'behavior': 'push_notification',
    },
    {
        'name': 'rust-v10-push-notification-http-json',
        'sdks': ['current', 'python_v10'],
        'protocols': ['http_json'],
        'edges': ['0->1', '1->0'],
        'behavior': 'push_notification',
    },
    {
        'name': 'rust-v10-resubscribe-jsonrpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'rust-v10-resubscribe-http-json',
        'sdks': ['current', 'python_v10'],
        'protocols': ['http_json'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'rust-v10-resubscribe-grpc',
        'sdks': ['current', 'python_v10'],
        'protocols': ['grpc'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'rust-v10-go-v10-push-notification-jsonrpc',
        'sdks': ['current', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['0->1', '1->0'],
        'behavior': 'push_notification',
    },
    {
        'name': 'rust-v10-go-v10-push-notification-grpc',
        'sdks': ['current', 'go_v10'],
        'protocols': ['grpc'],
        'edges': ['0->1', '1->0'],
        'behavior': 'push_notification',
    },
    {
        'name': 'rust-v10-go-v10-push-notification-http-json',
        'sdks': ['current', 'go_v10'],
        'protocols': ['http_json'],
        'edges': ['0->1', '1->0'],
        'behavior': 'push_notification',
    },
    {
        'name': 'rust-v10-go-v10-resubscribe-jsonrpc',
        'sdks': ['current', 'go_v10'],
        'protocols': ['jsonrpc'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'rust-v10-go-v10-resubscribe-grpc',
        'sdks': ['current', 'go_v10'],
        'protocols': ['grpc'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'rust-v10-go-v10-resubscribe-http-json',
        'sdks': ['current', 'go_v10'],
        'protocols': ['http_json'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'resubscribe',
    },
    {
        'name': 'rust-v10-go-v10-all-transports',
        'sdks': ['current', 'go_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': ['0->1', '1->0'],
        'behavior': 'send_message',
    },
    {
        'name': 'rust-v10-go-v10-all-transports-streaming',
        'sdks': ['current', 'go_v10'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': ['0->1', '1->0'],
        'streaming': True,
        'behavior': 'send_message',
    },
    {
        'name': 'python-v10-go-v10-rust-v10-hub-all-transports',
        'sdks': ['python_v10', 'go_v10', 'current'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': ['2->0', '2->1', '0->2', '1->2'],
        'behavior': 'send_message',
        'build_subtests': True,
    },
    {
        'name': 'python-v10-go-v10-rust-v10-hub-all-transports-streaming',
        'sdks': ['python_v10', 'go_v10', 'current'],
        'protocols': ['jsonrpc', 'grpc', 'http_json'],
        'edges': ['2->0', '2->1', '0->2', '1->2'],
        'streaming': True,
        'behavior': 'send_message',
    },
]


def parse_args():
    """Parse command-line arguments for SDK filtering."""
    parser = argparse.ArgumentParser(
        description='Run ITK integration tests with optional SDK filtering.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Run all tests
  uv run run_tests.py

  # Run only tests involving Python v1.0, Go v1.0, and Rust v1.0
  uv run run_tests.py --sdks python_v10,go_v10,current

  # Run only Rust v1.0 and Python v1.0 tests
  uv run run_tests.py --sdks current,python_v10

  # Run only v0.3 agents
  uv run run_tests.py --sdks python_v03,go_v03

  # List available SDKs
  uv run run_tests.py --list-sdks
        '''
    )
    parser.add_argument(
        '--sdks',
        type=str,
        help='Comma-separated list of SDK version names to include '
             '(e.g., python_v10,go_v10,rust_v10). '
             'Only tests using these SDKs will run. If omitted, all tests run. '
             'Use --list-sdks to see available options.'
    )
    parser.add_argument(
        '--list-sdks',
        action='store_true',
        help='List all available SDK versions across all test cases.'
    )
    return parser.parse_args()


def get_available_sdks():
    """Extract all unique SDKs from TEST_CASES."""
    sdks = set()
    for case in TEST_CASES:
        sdks.update(case['sdks'])
    return sorted(sdks)


def filter_test_cases(selected_sdks=None):
    """Filter TEST_CASES to only include cases using selected SDKs.

    Args:
        selected_sdks: Set of SDK names to include, or None to include all.

    Returns:
        Filtered list of test cases.
    """
    if selected_sdks is None:
        return TEST_CASES

    filtered = []
    for case in TEST_CASES:
        # Include test if all its SDKs are in the selected set
        if all(sdk in selected_sdks for sdk in case['sdks']):
            filtered.append(case)
    return filtered


async def main_async() -> None:
    """Execute hardcoded integration test scenarios concurrently."""
    args = parse_args()

    # Handle --list-sdks flag
    if args.list_sdks:
        available = get_available_sdks()
        print('Available SDKs:')
        for sdk in available:
            print(f'  - {sdk}')
        sys.exit(0)

    # Parse and validate selected SDKs
    selected_sdks = None
    if args.sdks:
        selected_sdks = set(sdk.strip() for sdk in args.sdks.split(','))
        available = set(get_available_sdks())
        unknown = selected_sdks - available
        if unknown:
            logger.error(
                'Unknown SDK(s): %s. Available SDKs: %s',
                ', '.join(sorted(unknown)),
                ', '.join(sorted(available))
            )
            sys.exit(1)
        logger.info('Filtering tests to SDKs: %s', ', '.join(sorted(selected_sdks)))

    # Filter test cases based on selected SDKs
    test_cases = filter_test_cases(selected_sdks)

    num_original = len(TEST_CASES)
    num_filtered = len(test_cases)

    if num_filtered < num_original:
        logger.info(
            'Running %d/%d test cases (filtered by SDKs)',
            num_filtered,
            num_original
        )

    # 1. Identify all unique SDKs needed across filtered test cases
    all_required_sdks = set()
    for case in test_cases:
        all_required_sdks.update(case['sdks'])

    # Convert to sorted list for deterministic port assignment
    # (Though AGENT_DEFS currently have static ports anyway)
    sdk_list = sorted(all_required_sdks)

    # 2. Start the shared cluster
    procs = []
    ports = []
    procs, _uris, ports = await start_itk_cluster(sdk_list)

    try:
        # 3. Run all scenarios sequentially to prevent overwhelming the shared cluster
        logger.info('Starting sequential scenario execution...')
        results = []
        for case in test_cases:
            logger.info("Executing parent scenario '%s'...", case['name'])
            res_dict = await execute_itk_test(
                sdks=case['sdks'],
                behavior=case['behavior'],
                edges=case['edges'],
                scenario_name=case['name'],
                protocols=case.get('protocols'),
                streaming=case.get('streaming', False),
                build_subtests=case.get('build_subtests', False),
            )
            results.append(res_dict)

        # Merge the results dictionaries
        merged_results = {}
        for res_dict in results:
            merged_results.update(res_dict)

        # 5. Report results
        all_passed = True
        for idx, (name, details) in enumerate(merged_results.items()):
            passed = details['passed']
            status = 'PASSED' if passed else 'FAILED'
            logger.info(
                "Scenario %s/%s '%s': %s",
                idx + 1,
                len(merged_results),
                name,
                status,
            )
            if not passed:
                all_passed = False

        output_file = os.environ.get('ITK_OUTPUT_FILE')
        if output_file:
            raw = {'all_passed': all_passed, 'results': merged_results}
            with open(output_file, 'w') as f:
                json.dump(raw, f, indent=2)
            logger.info('Results written to %s', output_file)

        if not all_passed:
            logger.error('One or more test scenarios failed.')
        else:
            logger.info('All test scenarios passed.')

    except Exception:
        logger.exception('Concurrent test execution encountered an error.')
        sys.exit(1)
    finally:
        logger.info('Decommissioning shared agent cluster...')
        stop_itk_cluster(procs, ports)


def main() -> None:
    """Entry point for the integration test orchestrator."""
    asyncio.run(main_async())


if __name__ == '__main__':
    main()
