"""Run the shipped ratchet shell with real local git and fake network transport."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from coverage import CoverageData
import _util
from test_action import _action_bash
from test_workflows import _workflow, _shipped_action_pin

ROOT = Path(__file__).resolve().parents[1]


def _step(workflow, name):
    jobs = _workflow(workflow)['jobs']
    steps = [step for job in jobs.values() for step in job['steps']
             if step.get('name') == name]
    assert len(steps) == 1, name
    return steps[0]


def test_privileged_job_accepts_only_successful_main_pushes(tmp):
    del tmp
    workflow = _workflow('ratchet-push.yml')
    assert workflow['on'] == {'workflow_run': {'workflows': ['tests'], 'types': ['completed']}}
    assert workflow['permissions'] == {}
    assert workflow['concurrency'] == {'group': 'ratchet-push', 'cancel-in-progress': 'false'}
    assert set(workflow['jobs']) == {'push'}
    job = workflow['jobs']['push']
    assert ' '.join(job['if'].split()) == (
        "github.event.workflow_run.event == 'push' && "
        "github.event.workflow_run.head_branch == 'main' && "
        "github.event.workflow_run.conclusion == 'success'")
    assert job['environment'] == 'main-push'
    assert job['permissions'] == {'actions': 'read'}
    assert job['runs-on'] == 'ubuntu-latest' and int(job['timeout-minutes']) > 0
    assert all('uses' not in step or step['uses'].startswith('actions/download-artifact@')
               for step in job['steps'])
    download = _step('ratchet-push.yml', 'Download the ratchet data')
    assert download['uses'] == ('actions/download-artifact@'
                                + _shipped_action_pin('actions/download-artifact'))
    assert download['if'] == "steps.list.outputs.ratchet == 'true'"
    assert download['with'] == {
        'name': 'ratchet-push', 'path': '${{ runner.temp }}/ratchet-data',
        'run-id': '${{ github.event.workflow_run.id }}',
        'github-token': '${{ secrets.GITHUB_TOKEN }}'}
    push = _step('ratchet-push.yml', 'Push the ratchet commit to main')
    assert push['if'] == "steps.list.outputs.ratchet == 'true'"
    assert push['env'] == {'MASTER_PUSH_DEPLOY_KEY': '${{ secrets.MASTER_PUSH_DEPLOY_KEY }}',
                           'REPO': '${{ github.repository }}'}
    for step in job['steps']:
        assert 'continue-on-error' not in step
        assert '${{' not in step.get('run', '')


def test_staging_is_push_only_on_the_measured_cell_after_a_green_gate(tmp):
    del tmp
    workflow = _workflow('tests.yml')
    assert '.github/ci-thresholds.json' in workflow['on']['push']['paths-ignore']
    assert workflow['on']['push']['branches'] == ['main']
    assert workflow['on']['pull_request'] == ''
    steps = workflow['jobs']['suites']['steps']
    stage = _step('tests.yml', 'Stage the ratchet data')
    upload = _step('tests.yml', 'Upload the ratchet data')
    assert stage['id'] == 'ratchet'
    assert ' '.join(stage['if'].split()) == (
        "matrix.os == 'ubuntu-latest' && matrix.python == '3.13' "
        "&& github.event_name == 'push'")
    assert stage['env'] == {'MEASURED_HEAD': '${{ github.sha }}'}
    assert stage['shell'] == 'bash'
    assert upload['if'] == (
        "github.event_name == 'push' && steps.ratchet.outputs.changed == 'true'")
    assert upload['uses'] == ('actions/upload-artifact@'
                              + _shipped_action_pin('actions/upload-artifact'))
    assert upload['with'] == {'name': 'ratchet-push',
                              'path': '${{ runner.temp }}/ratchet-data',
                              'retention-days': '1', 'if-no-files-found': 'error'}
    assert steps.index(_step('tests.yml', 'Coverage gate')) < steps.index(stage)
    assert steps.index(stage) < steps.index(upload)
    for step in (stage, upload, _step('tests.yml', 'Coverage gate')):
        assert 'continue-on-error' not in step
        # With no status function GitHub implicitly requires success().
        assert not any(f'{status}(' in step['if']
                       for status in ('always', 'cancelled', 'failure'))


def _run(script, tmp, environment, cwd):
    path = tmp / 'step.sh'
    path.write_text(script, encoding='utf-8', newline='\n')
    return subprocess.run([_action_bash(), '-e', '-o', 'pipefail', str(path)],
                          cwd=cwd, env={**os.environ, **environment},
                          capture_output=True, text=True, timeout=60)


def test_staging_executes_real_measurement_and_helper_decisions(tmp):
    script = _step('tests.yml', 'Stage the ratchet data')['run']
    repo = tmp / 'repo'
    (repo / '.github').mkdir(parents=True)
    (repo / 'scripts/ci').mkdir(parents=True)
    shutil.copyfile(ROOT / 'scripts/ci/coverage_ratchet.py',
                    repo / 'scripts/ci/coverage_ratchet.py')
    thresholds = {'coverage': {'python': {'floor': 91.9, 'measured': 93.6, 'cell': 'keep'}}}
    (repo / '.github/ci-thresholds.json').write_text(json.dumps(thresholds), encoding='utf-8')
    # Real coverage output: 920/1000 is 92.0, 919/1000 is 91.9, 918/1000 is 91.8.
    source = repo / 'measured.py'
    source.write_text('x = 1\n' * 1000, encoding='utf-8')
    for count, changed in [(920, True), (919, False), (918, False)]:
        run = tmp / str(count)
        run.mkdir()
        data = CoverageData(basename=str(run / '.coverage'))
        data.add_lines({str(source): set(range(1, count + 1))})
        data.write()
        outputs = run / 'outputs'
        outputs.touch()
        base = 'a' * 40
        result = _run(script, run, {
            'RUNNER_TEMP': str(run), 'GITHUB_OUTPUT': str(outputs),
            'MEASURED_HEAD': base, 'COVERAGE_FILE': str(run / '.coverage'),
            'PATH': os.pathsep.join((str(Path(sys.executable).parent), os.environ['PATH']))}, repo)
        assert result.returncode == 0, (result.stdout, result.stderr)
        artifact = run / 'ratchet-data'
        assert outputs.read_text().strip() == f'changed={str(changed).lower()}'
        if changed:
            assert (artifact / 'base.txt').read_text() == base + '\n'
            record = json.loads((artifact / 'ci-thresholds.json').read_text())
            assert record['coverage']['python'] == {'floor': 92.0, 'measured': 92.0, 'cell': 'keep'}
        else:
            assert not artifact.exists(), 'equal/lower coverage must stage no artifact'
    bad = tmp / 'bad'
    bad.mkdir()
    environment = {'RUNNER_TEMP': str(bad), 'GITHUB_OUTPUT': str(bad / 'out'),
                   'MEASURED_HEAD': base, 'COVERAGE_FILE': str(bad / 'missing')}
    result = _run(script, bad, environment, repo)
    assert result.returncode != 0
    assert not (bad / 'ratchet-data/base.txt').exists()
    # Measurement succeeds, but the helper refuses a malformed record. This
    # distinguishes its error status from its no-raise status in the caller.
    thresholds['coverage']['python']['floor'] = 'invalid'
    (repo / '.github/ci-thresholds.json').write_text(json.dumps(thresholds), encoding='utf-8')
    environment['COVERAGE_FILE'] = str(tmp / '920/.coverage')
    result = _run(script, bad, environment, repo)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not (bad / 'ratchet-data').exists()


# All local git operations are real. Only fetch/push (the remote boundary)
# and ssh-agent/ssh-add (credential boundary) are replaced. Unknown network
# operations are refused, and every invocation is retained for argv assertions.
_GIT_BOUNDARY = r'''
import json
import os
import subprocess
import sys
args = sys.argv[1:]
with open(os.environ['GIT_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\n')
command = args[2:] if args[:1] == ['-C'] else args
prefix = args[:2] if args[:1] == ['-C'] else []
if command[:1] == ['fetch']:
    assert command == ['fetch', '--quiet', 'origin', 'main'], command
    args = prefix + ['fetch', '--quiet', os.environ['LOCAL_REMOTE'], 'main']
elif command[:1] == ['push']:
    assert command == ['push', 'origin', 'HEAD:main'], command
    assert 'StrictHostKeyChecking=yes' in os.environ['GIT_SSH_COMMAND']
    if os.environ.get('MOVE_ON_PUSH') == 'true':
        subprocess.run([os.environ['REAL_GIT'], '-C', os.environ['LOCAL_REMOTE'],
                        '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'moved'],
                       check=True, capture_output=True)
    raise SystemExit(int(os.environ.get('PUSH_STATUS', '0')))
raise SystemExit(subprocess.run([os.environ['REAL_GIT'], *args]).returncode)
'''

_SSH_BOUNDARY = r'''
git() { "$BOUNDARY_PYTHON" "$GIT_BOUNDARY" "$@"; }
ssh-agent() { printf '%s\n' 'SSH_AGENT_PID=999999; export SSH_AGENT_PID;'; }
ssh-add() { cat "$1" > "$KEY_INPUT"; }
kill() { printf '%s\n' "$*" >> "$AGENT_CLEANUP"; }
'''


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _push_run(tmp, *, empty=False, stale=False, unchanged=False, push_status=0,
              move_on_push=False):
    step = _step('ratchet-push.yml', 'Push the ratchet commit to main')
    repo = tmp / 'remote'
    repo.mkdir()
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.name', 'fixture')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    (repo / '.github').mkdir()
    thresholds = repo / '.github/ci-thresholds.json'
    thresholds.write_text(json.dumps({'coverage': {'python': {
        'floor': 91.9, 'measured': 93.6, 'cell': 'keep'}}}) + '\n', encoding='utf-8')
    _git(repo, 'add', '.github/ci-thresholds.json')
    _git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')
    base = _git(repo, 'rev-parse', 'HEAD')
    artifact = tmp / 'ratchet-data'
    artifact.mkdir()
    (artifact / 'base.txt').write_text(('0' * 40 if stale else base) + '\n', encoding='utf-8')
    if unchanged:
        shutil.copyfile(thresholds, artifact / 'ci-thresholds.json')
    else:
        module = _util.load(ROOT / 'scripts/ci/coverage_ratchet.py',
                            'coverage_ratchet_under_test')
        assert module.main(['--thresholds', str(thresholds), '--measured', '94.2',
                            '--output', str(artifact / 'ci-thresholds.json')]) == 0
    boundary = tmp / 'boundary.py'
    boundary.write_text(_GIT_BOUNDARY, encoding='utf-8')
    bash_env = tmp / 'bash-env'
    bash_env.write_text(_SSH_BOUNDARY, encoding='utf-8', newline='\n')
    log = tmp / 'git.log'
    summary = tmp / 'summary'
    summary.touch()
    result = _run(step['run'], tmp, {
        'BASH_ENV': str(bash_env), 'BOUNDARY_PYTHON': sys.executable,
        'GIT_BOUNDARY': str(boundary), 'REAL_GIT': shutil.which('git') or 'git',
        'LOCAL_REMOTE': str(repo), 'GIT_LOG': str(log), 'KEY_INPUT': str(tmp / 'key'),
        'AGENT_CLEANUP': str(tmp / 'cleanup'), 'RUNNER_TEMP': str(tmp),
        'GITHUB_STEP_SUMMARY': str(summary), 'REPO': 'owner/repo',
        'MASTER_PUSH_DEPLOY_KEY': '' if empty else ''.join(chr(c) for c in (102, 105, 120)),
        'PUSH_STATUS': str(push_status), 'MOVE_ON_PUSH': str(move_on_push).lower()}, tmp)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    commands = [call[2:] if call[:1] == ['-C'] else call for call in calls]
    return result, commands, summary.read_text()


def test_empty_key_fails_closed_before_any_git_operation(tmp):
    result, commands, _ = _push_run(tmp, empty=True)
    assert result.returncode != 0 and 'MASTER_PUSH_DEPLOY_KEY' in result.stderr
    assert commands == []


def test_raise_commits_exact_staged_data_and_pushes_only_main(tmp):
    result, commands, _ = _push_run(tmp)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert ['fetch', '--quiet', 'origin', 'main'] in commands
    assert ['remote', 'add', 'origin', 'git@github.com:owner/repo.git'] in commands
    assert ['add', '.github/ci-thresholds.json'] in commands
    assert ['config', 'user.name', 'github-actions[bot]'] in commands
    assert ['config', 'user.email',
            '41898282+github-actions[bot]@users.noreply.github.com'] in commands
    assert ['commit', '-m', 'Ratchet ci-thresholds: raise coverage floor (automated)'] in commands
    assert commands[-1] == ['push', 'origin', 'HEAD:main']
    repo = tmp / 'ratchet-repo'
    record = json.loads(_git(repo, 'show', 'HEAD:.github/ci-thresholds.json'))
    assert record['coverage']['python'] == {'floor': 94.2, 'measured': 94.2, 'cell': 'keep'}
    assert _git(repo, 'show', '--format=%an|%ae', '--no-patch') == (
        'github-actions[bot]|41898282+github-actions[bot]@users.noreply.github.com')
    assert _git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD') == (
        '.github/ci-thresholds.json')
    assert (tmp / 'cleanup').read_text().strip() == '999999'
    assert (tmp / 'key').read_text().strip() == 'fix'


def test_main_movement_drops_raise_before_commit_and_push(tmp):
    result, commands, summary = _push_run(tmp, stale=True)
    assert result.returncode == 0, result.stderr
    assert '::notice::' in result.stdout and 'main moved' in summary.lower()
    assert not any(command[0] in ('commit', 'push', 'add') for command in commands)
    assert (tmp / 'ratchet-repo/.github/ci-thresholds.json').read_bytes() == (
        tmp / 'remote/.github/ci-thresholds.json').read_bytes()


def test_no_diff_does_not_commit_or_push(tmp):
    result, commands, _ = _push_run(tmp, unchanged=True)
    assert result.returncode == 0, result.stderr
    assert not any(command[0] in ('commit', 'push') for command in commands)


def test_rejected_push_fails_loud(tmp):
    result, commands, _ = _push_run(tmp, push_status=1)
    assert result.returncode != 0
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1
    assert commands.count(['fetch', '--quiet', 'origin', 'main']) == 2


def test_main_movement_during_push_drops_raise_without_retrying(tmp):
    result, commands, summary = _push_run(tmp, push_status=1, move_on_push=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert '::notice::' in result.stdout and 'main moved' in summary.lower()
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1
    assert commands.count(['fetch', '--quiet', 'origin', 'main']) == 2


def test_artifact_listing_uses_exact_names_and_propagates_api_failure(tmp):
    step = _step('ratchet-push.yml', "List the triggering run's artifacts")
    assert step['id'] == 'list'
    assert step['env'] == {'GH_TOKEN': '${{ github.token }}',
                           'REPO': '${{ github.repository }}',
                           'RUN_ID': '${{ github.event.workflow_run.id }}'}
    boundary = tmp / 'bash-env'
    boundary.write_text(
        'gh() { printf "%s\\n" "$*" >> "$CALLS"; '
        'cat "$NAMES"; return "$API_STATUS"; }\n', encoding='utf-8')
    for index, (names, status, expected) in enumerate([
            ('', '0', 'false'), ('ratchet-push-extra', '0', 'false'),
            ('other\nratchet-push\ncoverage-xml', '0', 'true'),
            ('ratchet.push\nxratchet-push', '0', 'false'),
            ('ratchet-push', '1', None)]):
        run = tmp / str(index)
        run.mkdir()
        (run / 'names').write_text(names, encoding='utf-8')
        result = _run(step['run'], run, {
            'BASH_ENV': str(boundary), 'CALLS': str(run / 'calls'),
            'NAMES': str(run / 'names'), 'API_STATUS': status,
            'GITHUB_OUTPUT': str(run / 'outputs'), 'REPO': 'owner/repo', 'RUN_ID': '42'}, tmp)
        assert (run / 'calls').read_text().strip() == (
            "api -H Cache-Control: no-cache --paginate "
            "repos/owner/repo/actions/runs/42/artifacts "
            "--jq .artifacts[] | select(.expired != true) | .name")
        if expected is None:
            assert result.returncode != 0
            assert not (run / 'outputs').exists()
        else:
            assert result.returncode == 0, result.stderr
            assert (run / 'outputs').read_text().strip() == f'ratchet={expected}'


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='ratchet_push_'))
