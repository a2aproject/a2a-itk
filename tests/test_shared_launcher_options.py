"""Exercise shared launcher options without starting a container."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "run_itk_shared.sh"


@pytest.mark.parametrize("nightly", [None, "false", "true"])
@pytest.mark.parametrize("label", ["0", "1"])
def test_launcher_defaults_and_mount_labels(tmp_path, nightly, label):
    sdk_dir = tmp_path / "sdk"
    itk_dir = sdk_dir / "itk"
    itk_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "mounts.txt"

    runtime = bin_dir / "fake-container"
    runtime.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = run ]; then printf "%s\\n" "$@" > "$TEST_CALLS"; fi\n'
        'if [ "$1" = exec ] && [ "${2:-}" = -i ]; then echo "{}"; fi\n'
        "exit 0\n"
    )
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        'while (($#)); do if [ "$1" = -o ]; then printf "{}" > "$2"; shift; fi; shift; done\n'
        "exit 0\n"
    )
    python = bin_dir / "python3"
    python.write_text("#!/bin/bash\nexit 0\n")
    for executable in (runtime, curl, python):
        executable.chmod(0o755)

    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    env = os.environ.copy()
    env.pop("ITK_NIGHTLY_RUN", None)
    env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{bin_dir}:{env['PATH']}",
        TEST_CALLS=str(calls),
        ITK_CONTAINER_RT=str(runtime),
        ITK_SDK_NAME="java",
        A2A_ITK_REVISION="test",
        ITK_COPY_PROTO="0",
        ITK_SKIP_BUILD="1",
        ITK_LOG_LEVEL="DEBUG",
        ITK_SELINUX_MOUNT_LABEL=label,
        SCENARIO_FILE=str(scenario),
    )
    if nightly is not None:
        env["ITK_NIGHTLY_RUN"] = nightly

    result = subprocess.run(
        ["bash", "-uc", 'source "$1"', "bash", str(DRIVER)],
        cwd=itk_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert ("Warning: SELinux :Z relabels launcher bind mounts" in result.stderr) == (label == "1")
    args = calls.read_text().splitlines()
    mounts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-v"]
    assert len(mounts) == 4
    assert all(mount.endswith(":Z") == (label == "1") for mount in mounts)


# ---------------------------------------------------------------------------
# ACTS at more than one A2A version
# ---------------------------------------------------------------------------

#: Answers `POST /run-acts` with a report for the version and binding the
#: request names, or with the service's error body at the version in
#: `$REFUSED_AT`, and logs each request as a line of JSON. Any other call
#: succeeds with an empty body.
FAKE_CURL = '''\
import json, os, sys
args = sys.argv[1:]
url = next((a for a in args if a.startswith('http')), '')
body = out = None
for flag, value in zip(args, args[1:]):
    if flag == '-d' and value.startswith('@'):
        body = value[1:]
    if flag == '-o':
        out = value
if url.endswith('/run-acts'):
    request = json.load(open(body))
    with open(os.environ['ACTS_REQUESTS'], 'a') as log:
        log.write(json.dumps(request) + '\\n')
    count = sum(1 for _ in open(os.environ['ACTS_REQUESTS']))
    report = {
        'generated_at': f'2026-11-19T10:00:{count:02d}.000Z',
        'sdk': {'name': request['sdk']},
        'transport': request['transport'],
        'spec_version': request.get('spec_version'),
    }
    if request.get('spec_version') == os.environ.get('REFUSED_AT'):
        report = {'detail': 'the agent card lists no interface for this version'}
    json.dump(report, open(out, 'w'))
elif out:
    open(out, 'w').write('{}')
'''

#: `python3` on the driver's PATH. A script read from standard input (the
#: driver's inline snippets) runs on the real interpreter; the two report
#: scripts are logged instead, and fail when an argument contains `$FAIL_ON`.
FAKE_PYTHON = '''\
import json, os, sys
args = sys.argv[1:]
if args and args[0] == '-':
    os.execv(sys.executable, [sys.executable, *args])
script = os.path.basename(args[0]) if args else ''
if script in ('acts_report.py', 'process_acts_results.py'):
    with open(os.environ['REPORT_CALLS'], 'a') as log:
        log.write(json.dumps({'script': script, 'args': args[1:]}) + '\\n')
    marker = os.environ.get('FAIL_ON')
    if marker and any(marker in arg for arg in args[1:]):
        sys.exit(1)
'''


def run_acts_driver(tmp_path, **settings):
    """Run the driver on its ACTS path. Returns the result and what it did."""
    sdk_dir = tmp_path / "sdk"
    itk_dir = sdk_dir / "itk"
    itk_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runtime_log = tmp_path / "runtime.txt"

    runtime = bin_dir / "fake-container"
    runtime.write_text(
        "#!/bin/bash\n"
        'echo "$1" >> "$RUNTIME_LOG"\n'
        'if [ "$1" = exec ] && [ "${2:-}" = -i ]; then echo "{}"; fi\n'
        "exit 0\n"
    )
    curl = bin_dir / "curl"
    curl.write_text(f"#!{sys.executable}\n{FAKE_CURL}")
    python = bin_dir / "python3"
    python.write_text(f"#!{sys.executable}\n{FAKE_PYTHON}")
    for executable in (runtime, curl, python):
        executable.chmod(0o755)

    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("ITK_", "ACTS_"))
    }
    env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{bin_dir}:{env['PATH']}",
        RUNTIME_LOG=str(runtime_log),
        ACTS_REQUESTS=str(tmp_path / "requests.jsonl"),
        REPORT_CALLS=str(tmp_path / "reports.jsonl"),
        ITK_CONTAINER_RT=str(runtime),
        ITK_SDK_NAME="java",
        A2A_ITK_REVISION="test",
        ITK_COPY_PROTO="0",
        ITK_SKIP_BUILD="1",
        ITK_ACTS_RUN="1",
        ITK_ACTS_TRANSPORTS="jsonrpc,grpc",
        SCENARIO_FILE=str(scenario),
    )
    env.update(settings)
    result = subprocess.run(
        ["bash", "-uc", 'source "$1"', "bash", str(DRIVER)],
        cwd=itk_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    def lines(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return SimpleNamespace(
        result=result,
        requests=lines(tmp_path / "requests.jsonl"),
        reports=[c for c in lines(tmp_path / "reports.jsonl") if c["script"] == "acts_report.py"],
        histories=[c for c in lines(tmp_path / "reports.jsonl") if c["script"] == "process_acts_results.py"],
        started=runtime_log.exists() and "run" in runtime_log.read_text().split(),
        kept=sorted(p.name for p in itk_dir.glob("acts-report-*.json")),
    )


def flag(call, name):
    """The value that follows ``name`` in a logged call."""
    return call["args"][call["args"].index(name) + 1]


def flags(call, name):
    return [value for key, value in zip(call["args"], call["args"][1:]) if key == name]


HISTORY_URL = "https://github.com/a2aproject/a2a-java/releases/download/nightly-metrics/"


class TestActsVersions:
    def test_by_default_it_runs_at_1_0_exactly_as_before(self, tmp_path):
        run = run_acts_driver(tmp_path, ITK_NIGHTLY_RUN="true")
        assert run.result.returncode == 0, run.result.stderr
        assert [(r["spec_version"], r["transport"]) for r in run.requests] == [
            ("1.0", "jsonrpc"), ("1.0", "grpc"),
        ]
        # The file names it has always used.
        assert [flag(c, "--response-file") for c in run.reports] == [
            "acts_results_jsonrpc.json", "acts_results_grpc.json",
        ]
        (history,) = run.histories
        assert flags(history, "--report-file") == [
            "acts_results_jsonrpc.json", "acts_results_grpc.json",
        ]
        assert flag(history, "--history_output_file") == "acts_java.json"
        assert flag(history, "--history_url") == HISTORY_URL + "acts_java.json"
        # A nightly records a failure; it does not gate on it.
        assert not any("--require-conformant" in c["args"] for c in run.reports)

    def test_an_empty_value_means_the_default(self, tmp_path):
        run = run_acts_driver(tmp_path, ITK_ACTS_SPEC_VERSIONS="")
        assert run.result.returncode == 0, run.result.stderr
        assert {r["spec_version"] for r in run.requests} == {"1.0"}

    def test_each_version_is_a_run_of_every_binding_with_a_history_of_its_own(self, tmp_path):
        run = run_acts_driver(
            tmp_path, ITK_NIGHTLY_RUN="true", ITK_ACTS_SPEC_VERSIONS="1.0,1.1"
        )
        assert run.result.returncode == 0, run.result.stderr
        assert [(r["spec_version"], r["transport"]) for r in run.requests] == [
            ("1.0", "jsonrpc"), ("1.0", "grpc"), ("1.1", "jsonrpc"), ("1.1", "grpc"),
        ]
        assert [flag(c, "--title") for c in run.reports] == [
            "ACTS CONFORMANCE (A2A 1.0, jsonrpc)", "ACTS CONFORMANCE (A2A 1.0, grpc)",
            "ACTS CONFORMANCE (A2A 1.1, jsonrpc)", "ACTS CONFORMANCE (A2A 1.1, grpc)",
        ]
        at_10, at_11 = run.histories
        assert flags(at_10, "--report-file") == [
            "acts_results_jsonrpc.json", "acts_results_grpc.json",
        ]
        assert flag(at_10, "--history_output_file") == "acts_java.json"
        assert flags(at_11, "--report-file") == [
            "acts_results_v11_jsonrpc.json", "acts_results_v11_grpc.json",
        ]
        assert flag(at_11, "--history_output_file") == "acts_java_v11.json"
        assert flag(at_11, "--history_url") == HISTORY_URL + "acts_java_v11.json"
        # Every report is kept for the workflow to upload.
        assert len(run.kept) == 4

    def test_a_version_alone_runs_without_the_1_0_one(self, tmp_path):
        run = run_acts_driver(tmp_path, ITK_NIGHTLY_RUN="true", ITK_ACTS_SPEC_VERSIONS="1.1")
        assert run.result.returncode == 0, run.result.stderr
        assert {r["spec_version"] for r in run.requests} == {"1.1"}
        assert [flag(c, "--history_output_file") for c in run.histories] == ["acts_java_v11.json"]

    def test_the_metrics_name_is_kept_in_the_file_of_every_version(self, tmp_path):
        run = run_acts_driver(
            tmp_path, ITK_NIGHTLY_RUN="true", ITK_ACTS_SPEC_VERSIONS="1.0,1.1",
            ITK_METRICS_NAME="js",
        )
        assert [flag(c, "--history_output_file") for c in run.histories] == [
            "acts_js.json", "acts_js_v11.json",
        ]

    def test_on_a_pull_request_every_version_is_gated_and_nothing_is_published(self, tmp_path):
        run = run_acts_driver(tmp_path, ITK_ACTS_SPEC_VERSIONS="1.0,1.1")
        assert run.result.returncode == 0, run.result.stderr
        assert all("--require-conformant" in c["args"] for c in run.reports)
        assert len(run.reports) == 4
        assert run.histories == []

    def test_a_version_that_is_not_conformant_fails_the_run_and_the_next_still_runs(self, tmp_path):
        run = run_acts_driver(
            tmp_path, ITK_ACTS_SPEC_VERSIONS="1.0,1.1", FAIL_ON="results_jsonrpc"
        )
        assert run.result.returncode == 1
        assert [(r["spec_version"], r["transport"]) for r in run.requests] == [
            ("1.0", "jsonrpc"), ("1.0", "grpc"), ("1.1", "jsonrpc"), ("1.1", "grpc"),
        ]

    def test_a_history_that_fails_to_update_fails_the_run_and_the_next_still_runs(self, tmp_path):
        run = run_acts_driver(
            tmp_path, ITK_NIGHTLY_RUN="true", ITK_ACTS_SPEC_VERSIONS="1.0,1.1",
            FAIL_ON="acts_java.json",
        )
        assert run.result.returncode == 1
        assert [flag(c, "--history_output_file") for c in run.histories] == [
            "acts_java.json", "acts_java_v11.json",
        ]

    def test_a_version_the_service_refuses_does_not_cost_the_other_its_results(self, tmp_path):
        run = run_acts_driver(
            tmp_path, ITK_NIGHTLY_RUN="true", ITK_ACTS_SPEC_VERSIONS="1.0,1.1",
            REFUSED_AT="1.1", FAIL_ON="results_v11",
        )
        assert run.result.returncode == 1
        assert "Traceback" not in run.result.stderr
        # The reports at 1.0 are kept and its history is updated.
        assert len(run.kept) == 2
        assert flag(run.histories[0], "--history_output_file") == "acts_java.json"

    @pytest.mark.parametrize(("versions", "said"), [
        ("1.1.0", "is '1.1.0', which is not a comma-separated list of Major.Minor versions"),
        ("latest", "is 'latest'"),
        ("01.0", "is '01.0'"),
        ("1.0,", "is '1.0,'"),
        ("1.0 1.1", "is '1.0 1.1'"),
        ("1.0,*", "is '1.0,*'"),
        ("1.0,1.0", "lists 1.0 twice"),
    ])
    def test_a_list_that_is_wrong_stops_the_run_before_anything_starts(self, tmp_path, versions, said):
        run = run_acts_driver(tmp_path, ITK_ACTS_SPEC_VERSIONS=versions)
        assert run.result.returncode == 1
        assert f"Error: ITK_ACTS_SPEC_VERSIONS {said}" in run.result.stderr
        assert not run.started
        assert run.requests == []

    def test_the_list_is_not_read_by_a_traversal_run(self, tmp_path):
        run = run_acts_driver(tmp_path, ITK_ACTS_RUN="0", ITK_ACTS_SPEC_VERSIONS="nonsense")
        assert run.result.returncode == 0, run.result.stderr
        assert run.started
        assert run.requests == []
