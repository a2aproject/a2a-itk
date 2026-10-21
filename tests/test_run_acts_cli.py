"""run_acts.py — argument handling for the two ways of naming a SUT.

The pipeline itself is covered by test_acts_deviations.py and
test_itk_service_v2.py, so this module only covers what the CLI adds: which
flag combinations it accepts, and what it hands to `acts_runner.run`.

Nothing here touches the network or spawns an agent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import acts_runner
import run_acts
from test_suite.acts import load_suite
from test_suite.acts.runner import Outcome, TestResult
from test_suite.acts.schema import Level


MANIFEST = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)

EXTERNAL = 'http://external.sut:8788'


@pytest.fixture
def captured(monkeypatch) -> dict[str, object]:
    """What `run_acts.main` passes to `acts_runner.run`, without running."""
    seen: dict[str, object] = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return acts_runner.ActsRun(
            results=[TestResult(
                id='CORE-SEND-001', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            )],
            suite=load_suite(MANIFEST),
            transport=kwargs['transport'],
            duration_ms=1,
        )

    monkeypatch.setattr(acts_runner, 'run', fake_run)
    monkeypatch.delenv('ITK_MOUNT_DIR', raising=False)
    monkeypatch.setattr(
        run_acts,
        '_generate_protos',
        lambda *a: (_ for _ in ()).throw(AssertionError('codegen must not run')),
    )
    return seen


def _refused(argv: list[str], capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        run_acts.main(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


class TestNamingTheSut:
    def test_one_of_mount_and_sut_url_is_required(self, capsys):
        _refused(['--sdk', 'x'], capsys)

    def test_mount_and_sut_url_are_mutually_exclusive(self, tmp_path, capsys):
        _refused(
            ['--mount', str(tmp_path), '--sut-url', EXTERNAL, '--sdk', 'x'], capsys,
        )

    def test_a_url_without_a_scheme_is_refused_before_any_work(self, capsys):
        err = _refused(['--sut-url', '127.0.0.1:8788', '--sdk', 'x'], capsys)
        assert 'http://' in err

    def test_sut_url_skips_codegen_and_the_mount(self, captured):
        assert run_acts.main(['--sut-url', EXTERNAL, '--sdk', 'x', '--json']) == 0
        assert 'ITK_MOUNT_DIR' not in os.environ
        assert captured['sut_url'] == EXTERNAL
        assert captured['deviation_urls'] == {}


class TestTheBehaviourContract:
    def test_behaviors_is_forwarded(self, captured, tmp_path):
        behaviors = tmp_path / 'sut-behaviors.yaml'
        rc = run_acts.main([
            '--sut-url', EXTERNAL, '--behaviors', str(behaviors),
            '--sdk', 'x', '--json',
        ])
        assert rc == 0
        assert captured['behaviors_path'] == behaviors
        assert captured['gate_on_behaviors'] is True

    def test_behaviors_and_no_gate_contradict(self, tmp_path, capsys):
        err = _refused([
            '--sut-url', EXTERNAL, '--behaviors', str(tmp_path / 'b.yaml'),
            '--no-gate', '--sdk', 'x',
        ], capsys)
        assert '--no-gate' in err


class TestDeviationInstances:
    def test_both_are_forwarded_keyed_by_their_variable(self, captured):
        rc = run_acts.main([
            '--sut-url', EXTERNAL,
            '--reduced-sut-url', 'http://127.0.0.1:2',
            '--auth-sut-url', 'http://127.0.0.1:3',
            '--sdk', 'x', '--json',
        ])
        assert rc == 0
        assert captured['deviation_urls'] == {
            acts_runner.REDUCED_CAPABILITIES_ENV: 'http://127.0.0.1:2',
            acts_runner.AUTH_ENFORCED_ENV: 'http://127.0.0.1:3',
        }

    def test_they_need_sut_url(self, tmp_path, capsys):
        err = _refused([
            '--mount', str(tmp_path), '--reduced-sut-url', 'http://127.0.0.1:2',
            '--sdk', 'x',
        ], capsys)
        assert '--sut-url' in err

    def test_their_urls_are_checked_too(self, capsys):
        _refused([
            '--sut-url', EXTERNAL, '--auth-sut-url', '127.0.0.1:3', '--sdk', 'x',
        ], capsys)
