"""Run the shipped ratchet shell with real local git and fake network transport."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from coverage import CoverageData
import _util
from test_action import _action_bash
from test_workflows import _CELL_OPERAND, _workflow, _shipped_action_pin
from test_type_ratchet import _document

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
    assert workflow['on'] == {'workflow_run': {
        'workflows': ['tests', 'types', 'comment share', 'size baseline'],
        'types': ['completed']}}
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
    assert all(step['uses'].startswith(('actions/download-artifact@',
                                        'actions/setup-python@'))
               for step in job['steps'] if 'uses' in step)
    download = _step('ratchet-push.yml', 'Download the ratchet data')
    assert download['uses'] == ('actions/download-artifact@'
                                + _shipped_action_pin('actions/download-artifact'))
    assert download['if'] == "steps.list.outputs.ratchet == 'true'"
    push = _step('ratchet-push.yml', 'Push the ratchet commit to main')
    assert push['if'] == "steps.list.outputs.ratchet == 'true'"
    assert push['env'] == {
        'PR_GATE_MASTER_PUSH_DEPLOY_KEY': '${{ secrets.MASTER_PUSH_DEPLOY_KEY }}',
        'PR_GATE_REPO': '${{ github.repository }}'}
    for step in job['steps']:
        assert 'continue-on-error' not in step
        assert '${{' not in step.get('run', '')


def test_staging_is_push_only_on_the_measured_cell_after_a_green_gate(tmp):
    del tmp
    workflow = _workflow('tests.yml')
    assert workflow['on']['push']['branches'] == ['main']
    assert workflow['on']['pull_request'] == ''
    steps = workflow['jobs']['suites']['steps']
    stage = _step('tests.yml', 'Stage the ratchet data')
    upload = _step('tests.yml', 'Upload the ratchet data')
    assert stage['id'] == 'ratchet'
    # The staging version is read from the step's own `if:` text — never
    # held as a test literal — and tied back into the matrix the same
    # workflow declares: an operand the matrix does not carry names no
    # cell, so staging would silently stop running.
    match = _CELL_OPERAND.search(stage['if'])
    assert match, 'the staging step must pin one measured python cell'
    operand = match[1]
    matrix = _workflow('tests.yml')['jobs']['suites']['strategy']['matrix']
    assert operand in matrix['python'], (
        "the staging step's python operand must stay in the matrix the "
        'same workflow declares, or staging runs on no cell')
    assert ' '.join(stage['if'].split()) == (
        f"matrix.os == 'ubuntu-latest' && matrix.python == '{operand}' "
        "&& github.event_name == 'push'")
    assert stage['env'] == {'PR_GATE_MEASURED_HEAD': '${{ github.sha }}'}
    assert stage['shell'] == 'bash'
    assert upload['if'] == (
        "github.event_name == 'push' && steps.ratchet.outputs.changed == 'true'")
    assert upload['uses'] == ('actions/upload-artifact@'
                              + _shipped_action_pin('actions/upload-artifact'))
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
    thresholds = {'coverage': {'python': {'floor': 88.9, 'measured': 90.4, 'cell': 'keep'}}}
    (repo / '.github/ci-thresholds.json').write_text(json.dumps(thresholds), encoding='utf-8')
    # Real coverage output: 920/1000 is 92.0 (a 1.6-point raise), 919/1000 is
    # 91.9 (exactly the 1.5-point tie, no raise), 918/1000 is 91.8 (no raise).
    source = repo / 'measured.py'
    source.write_text('x = 1\n' * 1000, encoding='utf-8')
    for count, changed in [(920, True), (919, False), (918, False)]:
        run = tmp / str(count)
        run.mkdir()
        data = CoverageData(basename=str(run / '.coverage'))
        # The reporter resolves filenames, including macOS /var aliases.
        # Synthetic data must use the same physical source path.
        data.add_lines({str(source.resolve()): set(range(1, count + 1))})
        data.write()
        outputs = run / 'outputs'
        outputs.touch()
        base = 'a' * 40
        result = _run("PS4='+ '\nset -x\n" + script, run, {
            'RUNNER_TEMP': str(run), 'GITHUB_OUTPUT': str(outputs),
            'PR_GATE_MEASURED_HEAD': base, 'COVERAGE_FILE': str(run / '.coverage'),
            'PATH': os.pathsep.join((str(Path(sys.executable).parent), os.environ['PATH']))}, repo)
        actual = outputs.read_text()
        captured = re.search(r'^\+ measured=(.*)$', result.stderr, re.MULTILINE)
        measured = captured[1] if captured else '<not captured>'
        diagnostic = (f'outputs={actual!r}; measured={measured!r}; '
                      f'stdout={result.stdout!r}; stderr={result.stderr!r}')
        assert result.returncode == 0, diagnostic
        artifact = run / 'ratchet-data'
        assert actual.strip() == f'changed={str(changed).lower()}', diagnostic
        if changed:
            assert (artifact / 'base.txt').read_text() == base + '\n'
            record = json.loads((artifact / '.github/ci-thresholds.json').read_text())
            assert record['coverage']['python'] == {'floor': 90.5, 'measured': 92.0, 'cell': 'keep'}
        else:
            assert not artifact.exists(), 'equal/lower coverage must stage no artifact'
    bad = tmp / 'bad'
    bad.mkdir()
    environment = {'RUNNER_TEMP': str(bad), 'GITHUB_OUTPUT': str(bad / 'out'),
                   'PR_GATE_MEASURED_HEAD': base, 'COVERAGE_FILE': str(bad / 'missing')}
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


def test_staging_measurements_survive_a_temporary_directory_alias(tmp):
    physical = tmp / 'physical'
    physical.mkdir()
    alias = tmp / 'alias'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(physical)],
                       check=True, capture_output=True, text=True)
    else:
        alias.symlink_to(physical, target_is_directory=True)
    test_staging_executes_real_measurement_and_helper_decisions(alias)


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


def _blob(repo, path):
    return subprocess.run(['git', '-C', str(repo), 'show', f'HEAD:{path}'],
                          check=True, capture_output=True).stdout


def _push_run(tmp, *, empty=False, stale=False, unchanged=False, push_status=0,
              move_on_push=False, records=('coverage',), extra_records=None, escape=False,
              move='none'):
    step = _step('ratchet-push.yml', 'Push the ratchet commit to main')
    repo = tmp / 'remote'
    repo.mkdir()
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.name', 'fixture')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    (repo / '.github').mkdir()
    thresholds = repo / '.github/ci-thresholds.json'
    thresholds.write_text(json.dumps({'coverage': {'python': {
        'floor': 91.9, 'measured': 93.6, 'cell': 'keep'}}}) + '\n', encoding='utf-8',
        newline='\n')
    baseline = repo / 'pyright-baseline.json'
    baseline.write_text(
        '{\n  "error_count": 3,\n  "schema_version": 1,\n  "checked_at": "base"\n}\n'
        if move in ('split', 'broken-json')
        else '{"error_count":3}\n', encoding='utf-8', newline='\n')
    if move == 'size-record':
        (repo / '.github/size-baseline.json').write_text(
            '{\n  "baseline": {\n    "big.py": 700\n  }\n}\n',
            encoding='utf-8', newline='\n')
        _git(repo, 'add', '.github/size-baseline.json')
    if move == 'cs-record':
        (repo / '.github/comment-share.json').write_text(
            '{\n  "source.py": [\n    20,\n    100\n  ]\n}\n',
            encoding='utf-8', newline='\n')
        _git(repo, 'add', '.github/comment-share.json')
    _git(repo, 'add', '.github/ci-thresholds.json', 'pyright-baseline.json')
    _git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')
    base = _git(repo, 'rev-parse', 'HEAD')
    if move != 'none':
        # The commit lands on the remote after the run measured `base`.
        if move == 'records':
            baseline.write_text('{"error_count":2}\n', encoding='utf-8')
            _git(repo, 'add', 'pyright-baseline.json')
        elif move == 'other':
            (repo / 'note.txt').write_text('not a record\n', encoding='utf-8', newline='\n')
            _git(repo, 'add', 'note.txt')
        elif move == 'conflict':
            thresholds.write_text(json.dumps({'coverage': {'python': {
                'floor': 90.1, 'measured': 91.0, 'cell': 'keep'}}}) + '\n', encoding='utf-8',
                newline='\n')
            _git(repo, 'add', '.github/ci-thresholds.json')
        elif move == 'split':
            baseline.write_text(
                '{\n  "error_count": 3,\n  "schema_version": 1,\n'
                '  "checked_at": "moved"\n}\n', encoding='utf-8', newline='\n')
            _git(repo, 'add', 'pyright-baseline.json')
        elif move == 'size-record':
            (repo / '.github/size-baseline.json').write_text(
                '{\n  "baseline": {\n    "big.py": 650\n  }\n}\n',
                encoding='utf-8', newline='\n')
            _git(repo, 'add', '.github/size-baseline.json')
        elif move == 'cs-record':
            (repo / '.github/comment-share.json').write_text(
                '{\n  "source.py": [\n    19,\n    100\n  ]\n}\n',
                encoding='utf-8', newline='\n')
            _git(repo, 'add', '.github/comment-share.json')
        elif move == 'broken-json':
            # The in-between edit merges line-clean with the staged one and
            # leaves the merge result syntactically invalid.
            baseline.write_text(
                '{\n  "error_count": 3,\n  "schema_version": 1,\n'
                '  "checked_at": ,\n}\n', encoding='utf-8', newline='\n')
            _git(repo, 'add', 'pyright-baseline.json')
        _git(repo, '-c', 'commit.gpgsign=false', 'commit', '-m', 'moved after the base')
    artifact = tmp / 'ratchet-data'
    artifact.mkdir()
    (artifact / 'base.txt').write_text(('0' * 40 if stale else base) + '\n',
                                       encoding='utf-8', newline='\n')
    if 'coverage' in records:
        (artifact / '.github').mkdir()
        if unchanged:
            shutil.copyfile(thresholds, artifact / '.github/ci-thresholds.json')
        else:
            module = _util.load(ROOT / 'scripts/ci/coverage_ratchet.py',
                                'coverage_ratchet_under_test')
            assert module.main(['--thresholds', str(thresholds), '--measured', '95.2',
                                '--output', str(artifact / '.github/ci-thresholds.json')]) == 0
    if 'types' in records:
        shutil.copyfile(baseline, artifact / 'pyright-baseline.json')
        if not unchanged:
            report = tmp / 'report.json'
            report.write_text(json.dumps(_document(errors=1)), encoding='utf-8')
            module = _util.load(ROOT / 'scripts/ci/type_ratchet.py', 'type_ratchet_under_test')
            assert module.main(['--tighten', '--baseline',
                                str(artifact / 'pyright-baseline.json'), str(report)]) == 0
    for relative, content in (extra_records or {}).items():
        path = artifact / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')
    if escape:
        outside = tmp / 'outside'
        outside.mkdir()
        installed_repo = tmp / 'ratchet-repo'
        installed_repo.mkdir()
        link = installed_repo / 'escape'
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                           check=True, capture_output=True, text=True)
        else:
            link.symlink_to('../outside', target_is_directory=True)
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
        'GITHUB_STEP_SUMMARY': str(summary), 'PR_GATE_REPO': 'owner/repo',
        'PR_GATE_MASTER_PUSH_DEPLOY_KEY': '' if empty else ''.join(chr(c) for c in (102, 105, 120)),
        'PUSH_STATUS': str(push_status), 'MOVE_ON_PUSH': str(move_on_push).lower()}, tmp)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    commands = [call[2:] if call[:1] == ['-C'] else call for call in calls]
    return result, commands, summary.read_text()


def test_empty_key_fails_closed_before_any_git_operation(tmp):
    result, commands, _ = _push_run(tmp, empty=True)
    assert result.returncode != 0 and 'PR_GATE_MASTER_PUSH_DEPLOY_KEY' in result.stderr
    assert commands == []


def test_raise_commits_exact_staged_data_and_pushes_only_main(tmp):
    result, commands, _ = _push_run(tmp)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert ['fetch', '--quiet', 'origin', 'main'] in commands
    assert ['remote', 'add', 'origin', 'git@github.com:owner/repo.git'] in commands
    assert ['add', '--', '.github/ci-thresholds.json'] in commands
    assert ['config', 'user.name', 'github-actions[bot]'] in commands
    assert ['config', 'user.email',
            '41898282+github-actions[bot]@users.noreply.github.com'] in commands
    subject = 'Ratchet CI records (automated)'
    assert ['commit', '-m', subject] in commands
    assert commands[-1] == ['push', 'origin', 'HEAD:main']
    repo = tmp / 'ratchet-repo'
    record = json.loads(_git(repo, 'show', 'HEAD:.github/ci-thresholds.json'))
    assert record['coverage']['python'] == {'floor': 93.7, 'measured': 95.2, 'cell': 'keep'}
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
    assert 'could not be read' in summary
    assert not any(command[0] in ('commit', 'push', 'add') for command in commands)
    assert (tmp / 'ratchet-repo/.github/ci-thresholds.json').read_bytes() == (
        tmp / 'remote/.github/ci-thresholds.json').read_bytes()


def test_non_records_main_movement_drops_the_raise(tmp):
    result, commands, summary = _push_run(tmp, move='other')
    assert result.returncode == 0, result.stderr
    assert '::notice::' in result.stdout and 'main moved' in summary.lower()
    assert 'non-records changes' in summary.lower()
    assert not any(command[0] in ('commit', 'push', 'add') for command in commands)
    assert (tmp / 'ratchet-repo/.github/ci-thresholds.json').read_bytes() == (
        tmp / 'remote/.github/ci-thresholds.json').read_bytes()


def test_records_only_main_movement_lands_with_the_in_between_edit_preserved(tmp):
    result, commands, summary = _push_run(tmp, move='records')
    assert result.returncode == 0, (result.stdout, result.stderr)
    repo = tmp / 'ratchet-repo'
    record = json.loads(_git(repo, 'show', 'HEAD:.github/ci-thresholds.json'))
    assert record['coverage']['python']['floor'] == 93.7
    assert json.loads(_git(repo, 'show', 'HEAD:pyright-baseline.json')) == {'error_count': 2}
    changed = set(_git(repo, 'diff-tree', '--no-commit-id', '--name-only',
                       '-r', 'HEAD').splitlines())
    assert changed == {'.github/ci-thresholds.json'}
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1
    assert 'main moved' not in summary.lower()


def test_conflicting_records_edits_drop_the_raise(tmp):
    result, commands, summary = _push_run(tmp, move='conflict')
    assert result.returncode == 0, result.stderr
    assert '::notice::' in result.stdout and 'main moved' in summary.lower()
    assert 'conflict' in summary.lower()
    assert not any(command[0] in ('commit', 'push', 'add') for command in commands)
    assert json.loads(_git(tmp / 'remote', 'show', 'HEAD:.github/ci-thresholds.json'))[
        'coverage']['python']['floor'] == 90.1


def test_a_split_records_edit_on_a_moved_main_merges_clean(tmp):
    result, commands, _ = _push_run(
        tmp, records=('coverage',),
        extra_records={'pyright-baseline.json':
                       '{\n  "error_count": 1,\n  "schema_version": 1,\n'
                       '  "checked_at": "base"\n}\n'},
        move='split')
    assert result.returncode == 0, (result.stdout, result.stderr)
    repo = tmp / 'ratchet-repo'
    assert _blob(repo, 'pyright-baseline.json') == (
        '{\n  "error_count": 1,\n  "schema_version": 1,\n'
        '  "checked_at": "moved"\n}\n'.encode())
    assert json.loads(_git(repo, 'show', 'HEAD:.github/ci-thresholds.json'))[
        'coverage']['python']['floor'] == 93.7
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1


def test_a_size_record_edit_on_moved_main_lands_the_raise(tmp):
    result, commands, summary = _push_run(tmp, move='size-record')
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(_git(tmp / 'ratchet-repo', 'show',
                           'HEAD:.github/ci-thresholds.json'))[
        'coverage']['python']['floor'] == 93.7
    assert json.loads(_git(tmp / 'remote', 'show',
                           'HEAD:.github/size-baseline.json'))['baseline']['big.py'] == 650
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1
    assert 'main moved' not in summary.lower()


def test_a_comment_share_edit_on_moved_main_lands_the_raise(tmp):
    result, commands, summary = _push_run(tmp, move='cs-record')
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(_git(tmp / 'ratchet-repo', 'show',
                           'HEAD:.github/ci-thresholds.json'))[
        'coverage']['python']['floor'] == 93.7
    assert json.loads(_git(tmp / 'remote', 'show',
                           'HEAD:.github/comment-share.json'))['source.py'] == [19, 100]
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1
    assert 'main moved' not in summary.lower()


def test_invalid_merged_records_drop_the_raise(tmp):
    result, commands, summary = _push_run(
        tmp, records=('coverage',),
        extra_records={'pyright-baseline.json':
                       '{\n  "error_count": 1,\n  "schema_version": 1,\n'
                       '  "checked_at": "base"\n}\n'},
        move='broken-json')
    assert result.returncode == 0, result.stderr
    assert '::notice::' in result.stdout and 'main moved' in summary.lower()
    assert 'not valid json' in summary.lower()
    assert not any(command[0] in ('commit', 'push') for command in commands)
    assert _blob(tmp / 'remote', 'pyright-baseline.json').endswith(
        b'  "checked_at": ,\n}\n')


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
                           'PR_GATE_REPO': '${{ github.repository }}',
                           'PR_GATE_RUN_ID': '${{ github.event.workflow_run.id }}'}
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
            'GITHUB_OUTPUT': str(run / 'outputs'), 'PR_GATE_REPO': 'owner/repo',
            'PR_GATE_RUN_ID': '42'}, tmp)
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


def test_the_type_carrier_ignores_the_three_records_on_push_only(tmp):
    del tmp
    events = _workflow('types.yml')['on']
    assert events['push']['branches'] == ['main']
    assert events['push']['paths-ignore'] == [
        'README.md', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'LICENSE',
        '.github/ci-thresholds.json', 'pyright-baseline.json',
        '.github/comment-share.json']
    assert events['pull_request'] == ''
    # The coverage carrier runs unfiltered: a record-only push re-measures
    # the same values and stages nothing, so its silence on a record-only
    # push is the ratchet's, not a path filter (issue 131).
    assert 'paths-ignore' not in _workflow('tests.yml')['on']['push']


def test_type_staging_runs_after_gate_on_push_and_uses_shared_artifact(tmp):
    del tmp
    job = _workflow('types.yml')['jobs']['types']
    assert 'if' not in job
    stage = _step('types.yml', 'Stage the ratchet data')
    upload = _step('types.yml', 'Upload the ratchet data')
    assert stage['if'] == "github.event_name == 'push'"
    assert stage['id'] == 'ratchet'
    assert stage['env'] == {'PR_GATE_MEASURED_HEAD': '${{ github.sha }}'}
    assert upload['if'] == (
        "github.event_name == 'push' && steps.ratchet.outputs.changed == 'true'")
    assert upload['uses'] == ('actions/upload-artifact@'
                              + _shipped_action_pin('actions/upload-artifact'))
    steps = job['steps']
    gate = _step('types.yml', 'Type-error ratchet')
    assert steps.index(gate) < steps.index(stage) < steps.index(upload)
    for step in (gate, stage, upload):
        assert 'continue-on-error' not in step
        assert not any(status + '(' in step.get('if', '')
                       for status in ('always', 'failure', 'cancelled'))
    assert not any('DEPLOY_KEY' in str(step.get('env', {})) for step in steps)


def test_type_staging_executes_helper_for_lower_equal_higher_and_error(tmp):
    script = _step('types.yml', 'Stage the ratchet data')['run']
    repo = tmp / 'repo'
    (repo / 'scripts/ci').mkdir(parents=True)
    shutil.copyfile(ROOT / 'scripts/ci/type_ratchet.py', repo / 'scripts/ci/type_ratchet.py')
    baseline = repo / 'pyright-baseline.json'
    base = 'b' * 40
    for index, (document, changed, status) in enumerate([
            (_document(errors=2), True, 0), (_document(errors=3), False, 0),
            (_document(errors=4), False, 0), ({}, False, 2)]):
        run = tmp / str(index)
        run.mkdir()
        baseline.write_text('{"error_count":3}\n', encoding='utf-8')
        before = baseline.read_bytes()
        (run / 'pyright-report.json').write_text(json.dumps(document), encoding='utf-8')
        outputs = run / 'outputs'
        outputs.touch()
        result = _run(script, run, {'RUNNER_TEMP': str(run), 'PR_GATE_MEASURED_HEAD': base,
                                    'GITHUB_OUTPUT': str(outputs)}, repo)
        assert result.returncode == status, (result.stdout, result.stderr)
        artifact = run / 'ratchet-data'
        if changed:
            assert (artifact / 'base.txt').read_text() == base + '\n'
            lowered = json.loads((artifact / 'pyright-baseline.json').read_text())
            assert lowered == {'error_count': 2}
            assert set(p.name for p in artifact.iterdir()) == {'base.txt', 'pyright-baseline.json'}
        else:
            assert not artifact.exists()
            assert baseline.read_bytes() == before
        assert outputs.read_text().strip() == (
            f'changed={str(changed).lower()}' if status == 0 else '')


def test_push_applies_type_only_and_combined_records_once(tmp):
    for records in (('types',), ('coverage', 'types')):
        run = tmp / '-'.join(records)
        run.mkdir()
        result, commands, _ = _push_run(run, records=records)
        assert result.returncode == 0, (result.stdout, result.stderr)
        repo = run / 'ratchet-repo'
        baseline = json.loads(_git(repo, 'show', 'HEAD:pyright-baseline.json'))
        assert baseline == {'error_count': 1}
        changed = set(_git(repo, 'diff-tree', '--no-commit-id', '--name-only',
                           '-r', 'HEAD').splitlines())
        expected = {'pyright-baseline.json'}
        if 'coverage' in records:
            expected.add('.github/ci-thresholds.json')
            record = json.loads(_git(repo, 'show', 'HEAD:.github/ci-thresholds.json'))
            assert record['coverage']['python']['floor'] == 93.7
        assert changed == expected
        assert ['add', '--', 'pyright-baseline.json'] in commands
        assert sum(command[0] == 'commit' for command in commands) == 1
        assert commands.count(['push', 'origin', 'HEAD:main']) == 1


def test_stale_and_unchanged_type_records_never_push(tmp):
    for records in (('types',), ('coverage', 'types')):
        for stale in (True, False):
            run = tmp / ('-'.join(records) + str(stale))
            run.mkdir()
            result, commands, summary = _push_run(run, records=records, stale=stale,
                                                  unchanged=not stale)
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert not any(command[0] in ('commit', 'push') for command in commands)
            assert (run / 'ratchet-repo/pyright-baseline.json').read_bytes() == (
                run / 'remote/pyright-baseline.json').read_bytes()
            if stale:
                assert '::notice::' in result.stdout and 'main moved' in summary.lower()


def test_push_installs_three_records_at_their_staged_paths_once(tmp):
    third = '.github/extra records/item[1].json'
    decoy = tmp / 'ratchet-repo/.github/extra records/item1.json'
    decoy.parent.mkdir(parents=True)
    decoy.write_text('unrelated worktree file\n', encoding='utf-8')
    content = '{"example_count":1}\n'
    result, commands, _ = _push_run(tmp, records=('coverage', 'types'),
                                    extra_records={third: content})
    assert result.returncode == 0, (result.stdout, result.stderr)
    repo = tmp / 'ratchet-repo'
    expected = {'.github/ci-thresholds.json', 'pyright-baseline.json', third}
    changed = set(_git(repo, 'diff-tree', '--no-commit-id', '--name-only',
                       '-r', 'HEAD').splitlines())
    assert changed == expected
    assert _git(repo, 'show', f'HEAD:{third}') == content.strip()
    assert {tuple(command) for command in commands if command[0] == 'add'} == {
        ('add', '--', path) for path in expected}
    status = [command for command in commands if command[:2] == ['status', '--porcelain']]
    assert len(status) == 1
    assert status[0][2] == '--' and set(status[0][3:]) == expected
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1
    assert sum(command[0] == 'commit' for command in commands) == 1
    assert not (repo / 'base.txt').exists()


def test_push_installs_an_unlisted_record_without_known_records(tmp):
    third = 'records/another.json'
    result, commands, _ = _push_run(tmp, records=(), extra_records={third: '{}\n'})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _git(tmp / 'ratchet-repo', 'diff-tree', '--no-commit-id', '--name-only',
                '-r', 'HEAD') == third
    assert ['add', '--', third] in commands
    assert commands.count(['push', 'origin', 'HEAD:main']) == 1


def test_push_refuses_a_staged_destination_that_traverses_outside_the_repo(tmp):
    result, commands, _ = _push_run(tmp, records=('coverage', 'types'), escape=True,
                                    extra_records={'escape/record.json': '{}\n'})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'unsafe staged path' in result.stderr
    assert not any(command[0] in ('add', 'commit', 'push') for command in commands)
    assert not (tmp / 'outside/record.json').exists()


def test_push_with_only_base_metadata_does_not_commit_or_push(tmp):
    result, commands, _ = _push_run(tmp, records=())
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not any(command[0] in ('add', 'commit', 'push') for command in commands)
    assert not (tmp / 'ratchet-repo/base.txt').exists()


def test_comment_share_name_matches_the_ratchet_push_subscription(tmp):
    del tmp
    assert _workflow('comment-share.yml')['name'] == 'comment share'
    assert _workflow('size-baseline.yml')['name'] == 'size baseline'
    assert _workflow('ratchet-push.yml')['on']['workflow_run']['workflows'] == [
        'tests', 'types', 'comment share', 'size baseline']


def test_comment_share_stages_after_the_gate_and_uploads_the_shared_artifact(tmp):
    del tmp
    steps = _workflow('comment-share.yml')['jobs']['comment-share']['steps']
    assert len(steps) == 4
    stage = _step('comment-share.yml', 'Stage changed records for the ratchet push')
    upload = _step('comment-share.yml', 'Upload the changed records')
    assert steps[2:] == [stage, upload]
    assert stage['id'] == 'stage'
    assert stage['if'] == "github.event_name == 'push'"
    assert stage['env'] == {'PR_GATE_BASE_SHA': '${{ github.sha }}'}
    assert upload['if'] == "success() && steps.stage.outputs.changed == 'true'"
    download = _step('ratchet-push.yml', 'Download the ratchet data')
    assert upload['with']['name'] == download['with']['name']


def _comment_share_stage_fixture(tmp):
    repo = tmp / 'repo'
    (repo / '.github').mkdir(parents=True)
    records = repo / '.github/comment-share.json'
    records.write_text('{"source.py": [20, 100]}\n', encoding='utf-8')
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.name', 'fixture')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    _git(repo, 'add', '.github/comment-share.json')
    _git(repo, 'commit', '-m', 'fixture')
    base = _git(repo, 'rev-parse', 'HEAD')
    return repo, records, base


def _run_comment_share_stage(tmp, repo, base):
    script = _step('comment-share.yml', 'Stage changed records for the ratchet push')['run']
    return _run(script, tmp, {
        'RUNNER_TEMP': str(tmp), 'GITHUB_OUTPUT': str(tmp / 'outputs'),
        'PR_GATE_BASE_SHA': base}, repo)


def test_comment_share_staging_unchanged_records_stages_nothing(tmp):
    repo, _, base = _comment_share_stage_fixture(tmp)
    result = _run_comment_share_stage(tmp, repo, base)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp / 'outputs').read_text().strip() == 'changed=false'
    assert not (tmp / 'ratchet-data').exists()


def test_comment_share_staging_preserves_the_records_path_and_measured_head(tmp):
    repo, records, base = _comment_share_stage_fixture(tmp)
    records.write_text('{"source.py": [19, 100]}\n', encoding='utf-8')
    result = _run_comment_share_stage(tmp, repo, base)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp / 'outputs').read_text().strip() == 'changed=true'
    artifact = tmp / 'ratchet-data'
    assert sorted(p.relative_to(artifact).as_posix() for p in artifact.rglob('*')
                  if p.is_file()) == ['.github/comment-share.json', 'base.txt']
    assert (artifact / '.github/comment-share.json').read_bytes() == records.read_bytes()
    assert (artifact / 'base.txt').read_text() == base + '\n'


def test_comment_share_staging_refuses_a_different_checkout_head(tmp):
    repo, records, base = _comment_share_stage_fixture(tmp)
    _git(repo, 'commit', '--allow-empty', '-m', 'different head')
    records.write_text('{"source.py": [19, 100]}\n', encoding='utf-8')
    result = _run_comment_share_stage(tmp, repo, base)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'Checkout does not match the measured run head' in result.stderr
    assert not (tmp / 'ratchet-data').exists()
    assert not (tmp / 'outputs').exists()


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='ratchet_push_'))
