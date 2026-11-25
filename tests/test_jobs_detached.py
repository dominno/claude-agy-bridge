"""Exercise job discovery across real worker processes with a fake AGY executable."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'agy_bridge'
CONVERSATIONS = {
    'alpha': '1f6682c8-3ea1-4019-9d32-e7feeb746681',
    'bravo': '1f6682c8-3ea1-4019-9d32-e7feeb746682',
}
TASK = 'Change the number in the staged file. This prompt is private.'


@pytest.fixture
def detached(tmp_path):
    """Use real CLI launch/handoff, with only the AGY sandbox adapter replaced in a temp copy."""
    tmp_path = tmp_path.resolve()
    bridge = tmp_path / 'bridge'
    pkg = bridge / 'src' / 'agy_bridge'
    shutil.copytree(SOURCE, pkg, ignore=shutil.ignore_patterns('__pycache__', '_test_mode'))
    (pkg / '_test_mode').touch()

    control = tmp_path / 'control'
    control.mkdir()
    fake_bin = tmp_path / 'fake-bin'
    fake_bin.mkdir()
    agy = fake_bin / 'agy'
    agy.write_text(textwrap.dedent(f'''\
        import json
        import os
        from pathlib import Path
        import signal
        import sys
        import time

        control = Path({str(control)!r})
        target = Path('app.py')
        label, value = target.read_text().splitlines()
        label = label.removeprefix('# ')
        conversation = {CONVERSATIONS!r}[label]
        print(json.dumps(dict(event='init', conversation_id=conversation, init={{}})), flush=True)
        if '--conversation' in sys.argv:
            assert sys.argv[sys.argv.index('--conversation') + 1] == conversation
            assert value == 'value = 2'
            target.write_text(f'# {{label}}\\nvalue = 3\\n')
        else:
            assert value == 'value = 1'
            worker = os.getppid()
            target.write_text(f'# {{label}}\\nvalue = 2\\n')
            (control / (label + '-entered')).touch()
            deadline = time.monotonic() + 25
            while not (control / (label + '-release')).exists():
                if (control / (label + '-interrupt')).exists():
                    # Terminate only the parent test worker that launched this fake.
                    assert os.getppid() == worker and worker > 1
                    os.kill(worker, signal.SIGKILL)
                    sys.exit(0)
                if time.monotonic() >= deadline:
                    sys.exit(1)
                time.sleep(.02)
        print(json.dumps(dict(event='result', result=dict(conversation_id=conversation,
                          status='SUCCESS'))), flush=True)
        '''), encoding='utf-8')
    agy.chmod(0o700)

    # Both supported checkout/installed entry paths run the same isolated test fixture.
    entry_text = textwrap.dedent(f'''\
        import sys

        sys.path.insert(0, {str(pkg.parent)!r})
        from agy_bridge import edit_sandbox
        from agy_bridge.cli import main

        def preflight(*, exe, **_):
            assert exe == {str(agy)!r}
            return '1.5.16'

        def agent_command(argv, **_):
            assert argv[0] == {str(agy)!r}
            return [sys.executable, *argv]

        edit_sandbox.preflight = preflight
        edit_sandbox.agent_command = agent_command
        sys.exit(main())
        ''')
    (pkg / '__main__.py').write_text(entry_text, encoding='utf-8')
    entry = bridge / 'bin' / 'agy-bridge'
    entry.parent.mkdir()
    entry.write_text(entry_text, encoding='utf-8')

    state_dir = tmp_path / 'state'
    env = {'PATH': str(fake_bin) + os.pathsep + os.defpath,
           'AGY_BRIDGE_HOME': str(state_dir), 'AGY_BRIDGE_TEST_MODE': '1'}

    def invoke(*argv):
        return subprocess.run([sys.executable, '-I', str(entry), *map(str, argv)],
                              capture_output=True, text=True, env=env, timeout=10)

    def inventory(*argv):
        result = invoke('jobs', 'list', '--json', *argv)
        assert result.returncode == 0, result.stderr
        assert TASK not in result.stdout
        data = json.loads(result.stdout)
        assert data['schema_version'] == 1
        return data

    def submit(label):
        repo = tmp_path / label
        (repo / '.git').mkdir(parents=True)
        (repo / 'app.py').write_text(f'# {label}\nvalue = 1\n', encoding='utf-8')
        profile = tmp_path / (label + '-profile')
        profile.mkdir(mode=0o700)
        result = invoke('implement', '--repo', repo, '--profile', profile, '--file', 'app.py',
                        '--turn-timeout', '30s', '--', TASK)
        assert result.returncode == 0, result.stderr
        job_id = json.loads(result.stdout)['job_id']
        return repo.resolve(), job_id

    try:
        yield invoke, inventory, submit, control, state_dir
    finally:
        for label in CONVERSATIONS:
            (control / (label + '-release')).touch()


def _await(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.025)
    pytest.fail('timed out waiting for detached job state')


def test_cross_repo_jobs_survive_submitter_exit_cancel_and_resume(detached):
    invoke, inventory, submit, control, state_dir = detached
    alpha, first = submit('alpha')
    bravo, second = submit('bravo')

    def rows(*argv):
        return {row['job_id']: row for row in inventory(*argv)['jobs']}

    def running_jobs():
        if not all((control / (label + '-entered')).exists() for label in CONVERSATIONS):
            return None
        current = rows('--active')
        if set(current) == {first, second} and all(row['phase'] == 'running' for row in current.values()):
            return current
        return None

    active = _await(running_jobs)
    assert active[first]['repository'] == str(alpha)
    assert active[second]['repository'] == str(bravo)
    assert all(row['kind'] == 'implement' and row['active'] for row in active.values())
    assert set(rows('--active', '--repo', alpha)) == {first}
    for row in active.values():
        assert {'task', 'profile', 'check', 'conversation_id', 'manifest'}.isdisjoint(row)

    result = invoke('implement-cancel', first)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['phase'] == 'cancel_requested'
    _await(lambda: rows()[first]['phase'] == 'canceled')
    assert set(rows('--active')) == {second}
    assert rows('--active')[second]['phase'] == 'running'
    partial = invoke('implement-patch', first)
    assert partial.returncode == 0, partial.stderr
    assert '-value = 1\n+value = 2\n' in partial.stdout
    assert (alpha / 'app.py').read_text(encoding='utf-8') == '# alpha\nvalue = 1\n'

    # Wait for the real transport to persist the conversation before the forced interruption.
    def conversation_saved():
        result = invoke('implement-status', second)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout).get('conversation_id') == CONVERSATIONS['bravo']

    _await(conversation_saved)
    (control / 'bravo-interrupt').touch()
    _await(lambda: rows()[second]['phase'] == 'interrupted')
    assert not rows('--active')
    job = state_dir / 'implement' / second
    assert (job / 'tree' / 'app.py').read_text(encoding='utf-8') == '# bravo\nvalue = 2\n'

    resumed = invoke('implement-resume', second)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)['job_id'] == second
    _await(lambda: rows()[second]['phase'] == 'ready')
    patch = invoke('implement-patch', second)
    assert patch.returncode == 0, patch.stderr
    assert '-value = 1\n+value = 3\n' in patch.stdout
    assert (bravo / 'app.py').read_text(encoding='utf-8') == '# bravo\nvalue = 1\n'


def test_inventory_skips_bad_states_without_creating_locks(detached):
    invoke, inventory, _, _, state_dir = detached
    assert inventory() == {'schema_version': 1, 'jobs': [], 'warnings': [
        'No implementation jobs. Delegation queues are listed per repository: agy-bridge jobs list -C <repo>']}
    assert not state_dir.exists()

    stale_id, bad_id = 'a' * 32, 'b' * 32
    stale = state_dir / 'implement' / stale_id
    broken = state_dir / 'implement' / bad_id
    for directory in (state_dir, stale.parent, stale, broken):
        directory.mkdir(mode=0o700)
    raw = json.dumps({'job_id': stale_id, 'phase': 'running', 'run_id': 'c' * 32,
                      'repository': str(state_dir.parent / 'project'), 'created_at': 1,
                      'updated_at': 2, 'attempt': 1})
    (stale / 'state.json').write_text(raw, encoding='utf-8')
    (broken / 'state.json').write_text('[]', encoding='utf-8')

    result = inventory()
    assert result['warnings']
    assert [(row['job_id'], row['phase']) for row in result['jobs']] == [(stale_id, 'interrupted')]
    assert invoke('implement-cancel', stale_id).returncode == 1
    assert (stale / 'state.json').read_text(encoding='utf-8') == raw
    assert sorted(path.name for path in stale.iterdir()) == ['state.json']
    assert sorted(path.name for path in broken.iterdir()) == ['state.json']
