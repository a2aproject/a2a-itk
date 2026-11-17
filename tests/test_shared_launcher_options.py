"""Exercise shared launcher options without starting a container."""

import os
from pathlib import Path
import subprocess

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
