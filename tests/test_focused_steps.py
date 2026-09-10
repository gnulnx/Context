"""Foundation acceptance: real files and child processes, no product green claims."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from click.testing import CliRunner

from bl_context.checks import (
    STEPS, CheckResult, CheckStatus, Step, checks_succeeded, is_ready, run_checks,
)
from bl_context.cli import main


class FileStep(Step):
    def install(self):
        Path(self.step_id).write_text('owned')

    def verify(self):
        exists = Path(self.step_id).is_file()
        return CheckResult(self.step_id, self.label,
                           CheckStatus.PASSED if exists else CheckStatus.FAILED,
                           'Present' if exists else 'Missing')


def test_dependency_execution_and_read_only_verification(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    steps = (FileStep('target', 'Target', prerequisites=('base',)), FileStep('base', 'Base'))
    assert run_checks(steps, selected_step='target')[0].status == CheckStatus.FAILED
    assert not list(tmp_path.iterdir())
    results = run_checks(steps, install=True, selected_step='target')
    assert [r.step_id for r in results] == ['base', 'target']
    assert [r.dependency for r in results] == [True, False]
    assert checks_succeeded(results)
    assert not is_ready(results)
    assert [r.step_id for r in run_checks(steps, install=True)] == ['target', 'base']
    Path('target').unlink()
    assert run_checks(steps, selected_step='target')[0].status == CheckStatus.FAILED
    assert not Path('target').exists()


def test_failed_prerequisite_blocks_install(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    steps = (Step('base', 'Base'), FileStep('target', 'Target', prerequisites=('base',)))
    results = run_checks(steps, install=True, selected_step='target')
    assert results[-1].summary == 'Prerequisites unavailable'
    assert results[-1].diagnostic == 'base'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('steps', [
    (Step('a', 'A', prerequisites=('missing',)),),
    (Step('a', 'A', prerequisites=('a',)),),
    (Step('a', 'A'), Step('a', 'Duplicate')),
])
def test_invalid_graph_is_rejected_before_mutation(steps):
    with pytest.raises(ValueError):
        run_checks(steps, install=True)


def test_structured_skip_and_full_readiness():
    results = [CheckResult(s.step_id, s.label, CheckStatus.PASSED, 'OK') for s in STEPS]
    assert is_ready(results)
    assert not is_ready(results, selected_step='data_directory')
    assert not is_ready(results[:-1])
    step = STEPS[-1]
    results[-1] = CheckResult(step.step_id, step.label, CheckStatus.SKIPPED,
                             'Skipped (--no-history)')
    assert not is_ready(results, no_history=True)
    results[-1] = CheckResult(step.step_id, step.label, CheckStatus.SKIPPED,
                             'Any human wording', skip_reason='no_history')
    assert is_ready(results, no_history=True)
    assert not is_ready(results)
    results[0] = CheckResult('data_directory', 'Data', CheckStatus.SKIPPED,
                            'Skipped (--no-history)', skip_reason='no_history')
    assert not is_ready(results, no_history=True)


@pytest.mark.parametrize('command', [['install', 'codex'], ['status'], ['doctor']])
def test_usage_and_output_options(command):
    runner = CliRunner()
    for args in [['--step', 'unknown'], ['--step', 'history_index', '--no-history']]:
        result = runner.invoke(main, command + args)
        assert result.exit_code == 2
    result = runner.invoke(main, ['--json', '--no-color'] + command + ['--step', 'data_directory'])
    data = json.loads(result.output)
    assert result.exit_code == data['exit_code'] == 1
    assert not data['ready'] and not data['success']
    assert data['selected_step'] == 'data_directory'


def test_selected_snapshot(monkeypatch):
    monkeypatch.setenv('COLUMNS', '100')
    result = CliRunner().invoke(main, ['install', 'codex', '--step', 'background_service', '--no-color'])
    assert result.exit_code == 1
    assert result.output == (Path(__file__).parent / 'snapshots/selected.txt').read_text()


# Only the test child installs this fixture registry. The shipped CLI has no
# environment switch or alternate entry point that can substitute fake checks.
FIXTURE_CHILD = '''
from pathlib import Path
from bl_context import checks, cli
class OwnedFile(checks.Step):
    def install(self):
        Path(self.step_id).write_text("owned")
    def verify(self):
        active = Path("owner").is_file() and Path(self.step_id).is_file()
        return checks.CheckResult(self.step_id, self.label,
            checks.CheckStatus.PASSED if active else checks.CheckStatus.FAILED,
            "Present" if active else "Missing")
class Remove(checks.Step):
    def verify(self):
        Path("owner").unlink(missing_ok=True)
        return checks.CheckResult(self.step_id, self.label, checks.CheckStatus.PASSED, "Disconnected")
checks.STEPS = (OwnedFile("owner", "Owner"),
                OwnedFile("fixture", "Fixture", prerequisites=("owner",)),
                checks.Step("future", "Future"))
cli.UNINSTALL_STEPS = (Remove("uninstall_codex", "Disconnect"),)
cli.main()
'''


def test_real_subprocess_lifecycle(tmp_path):
    sentinel = tmp_path / 'original-session.jsonl'
    sentinel.write_text('original history')
    env = {**os.environ, 'HOME': str(tmp_path), 'NO_COLOR': '1'}

    def invoke(args, expected, fixture=True):
        child = FIXTURE_CHILD if fixture else 'from bl_context.cli import main; main()'
        proc = subprocess.run([sys.executable, '-c', child, *args, '--json'],
                              cwd=tmp_path, env=env, text=True, capture_output=True)
        assert proc.returncode == expected, proc.stderr
        data = json.loads(proc.stdout)
        assert data['exit_code'] == expected
        assert not data['ready']
        assert sentinel.read_text() == 'original history'
        return data

    for command in ['status', 'doctor']:
        invoke([command, '--step', 'fixture'], 1)
    for _ in range(2):
        data = invoke(['install', 'codex', '--step', 'fixture'], 0)
        assert data['dependencies'] == ['owner']
        for command in ['status', 'doctor']:
            assert invoke([command, '--step', 'fixture'], 0)['checks'][0]['status'] == 'passed'
        assert invoke(['install', 'codex'], 1)['checks'][-1]['status'] == 'failed'
        invoke(['uninstall', 'codex'], 0)
        assert (tmp_path / 'fixture').read_text() == 'owned'
        for command in ['status', 'doctor']:
            invoke([command, '--step', 'fixture'], 1)
            assert not (tmp_path / 'owner').exists()
    for args in [['install', 'codex'], ['status'], ['doctor']]:
        invoke([*args, '--step', 'data_directory'], 1, fixture=False)
    invoke(['install', 'codex'], 1, fixture=False)
