"""CI executes the shipped suite and audits the reusable action itself.

The audit.yml step tests execute the workflow's own run-block text under
``bash -e`` with fakes standing in only at the external boundary — the
pip-audit executable (PyPI and the advisory database over the network) as a
PATH stub, and sleep (time) as a BASH_ENV shell function, because a Git Bash
PATH stub for sleep never intercepts and the real sleep would run the
backoff. Fakes live only at that external boundary, per CONTRIBUTING;
everything between them is the script text parsed out of the workflow,
never retyped.
"""
import configparser
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml
from test_action import _action_bash

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    path = ROOT / '.github/workflows' / name
    assert path.is_file(), f'missing workflow: {name}'
    return yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)


def _shipped_action_pin(action):
    """Return the one revision every shipped workflow pins ``action`` at.

    The workflows are the source of truth for which release is in use, so a
    dependency upgrade edits YAML only; these tests check that the pins agree
    and are immutable, not which commit they name.
    """
    pins = set()
    for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
        for job in _workflow(path.name)['jobs'].values():
            for step in job.get('steps', []):
                if step.get('uses', '').startswith(action + '@'):
                    pins.add(step['uses'].split('@', 1)[1])
    assert len(pins) == 1, (
        f'{action} must be pinned at exactly one revision across the '
        f'workflows, not {sorted(pins)}')
    return pins.pop()


# YAML parsing strips comments, so pin lines are extracted by regex over the
# raw text of each file instead of through the parsed document. The comment
# group captures whatever trailing comment the line carries — spelling it is
# never asserted here (fleet-rules, "Merging and CI"); the two files are only
# held to carrying the SAME one.
PR_GATE_PIN_LINE = re.compile(
    r'^[ \t]*(?:-[ \t]+)?uses:[ \t]*Nitjsefnie-Actions/pr-gate@'
    r'([0-9a-f]{40})[ \t]*(#[^\n]*)?[ \t]*$',
    re.MULTILINE)


def _documented_pr_gate_pins(relative_path):
    """Return (pin, comment) per pr-gate uses: line, comments kept."""
    source = (ROOT / relative_path).read_text(encoding='utf-8')
    return PR_GATE_PIN_LINE.findall(source)


def _manifest_entries(path):
    """Decode a requirements file into one entry per logical requirement."""
    entries = []
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if line[0] in ' \t' and entries:
            entries[-1] = entries[-1] + ' ' + stripped
        else:
            entries.append(stripped)
    return entries


def _audit_steps():
    return _workflow('audit.yml')['jobs']['pip-audit']['steps']


def _named_run(steps, name):
    runs = [step['run'] for step in steps if step.get('name') == name]
    assert len(runs) == 1, f'expected exactly one step named {name!r}'
    return runs[0]


def test_ci_runs_supported_platforms_and_python_versions(tmp):
    del tmp
    workflow = _workflow('tests.yml')
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['on']['push']['branches'] == ['main']
    assert 'paths' not in workflow['on']['push']
    assert not (workflow['on'].get('pull_request') or {})
    job = workflow['jobs']['test']
    matrix = job['strategy']['matrix']
    assert matrix['os'] == ['ubuntu-latest', 'windows-latest', 'macos-latest']
    # The supported range is held as a shape, not as version literals.
    versions = matrix['python']
    assert len(versions) == 2, (
        f'the matrix carries exactly the supported range, not {versions!r}')
    assert versions == sorted(versions), 'the range is listed oldest first'
    assert all(re.fullmatch(r'3\.\d+', version) for version in versions), (
        f'every matrix entry is a version string, not {versions!r}')
    runs = [step['run'] for step in job['steps'] if 'run' in step]
    assert 'python -m ruff check --select E9,F63,F7,F82 .' in runs


def test_workflow_audit_includes_composite_metadata(tmp):
    del tmp
    workflow = _workflow('actionlint.yml')
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['on']['push'] == {'branches': ['main']}
    steps = workflow['jobs']['actionlint']['steps']
    # The ported checks follow setup-python directly, on the interpreter it
    # put on PATH; a stale, mis-scoped head never pays for the installs.
    setup = next(index for index, step in enumerate(steps)
                 if step.get('uses', '').startswith('actions/setup-python@'))
    checks = ['Check no tracked file carries a merge-conflict marker',
              "Require this head to carry main's gate-defining commits",
              'Refuse a commit whose scope names a workflow outside the ci type']
    ported = steps[setup + 1:setup + 4]
    assert [step.get('name') for step in ported] == checks, 'must follow setup-python directly'
    marker = ported[0]['run']
    assert "git grep -nI -E '^(<{7}( |$)|>{7}( |$)|={7}$)' -- ." in marker
    assert '${{' not in marker

    def _run_of(name):
        return next(step['run'] for step in steps if step.get('name') == name)

    actionlint_run = _run_of('actionlint')
    assert ('./actionlint -color .github/workflows/*.yml '
            '"$RUNNER_TEMP/pr-gate-action-workflow.yml"') in actionlint_run, (
        'the audit must read the generated wrapper, not only the workflows')
    assert "python - <<'PYTHON'" in actionlint_run, (
        'the wrapper is generated in the step, never a committed copy')
    assert 'zizmor --no-progress action.yml .github/workflows/' in _run_of('zizmor')


# The actionlint step derives its lint input from action.yml, never a
# hand-kept copy: the step's own generation code is executed against the
# real action.yml and the wrapper it writes is pinned here.
def test_the_generated_wrapper_mirrors_the_composite_action(tmp):
    step = _named_run(_workflow('actionlint.yml')['jobs']['actionlint']['steps'],
                      'actionlint')
    heredoc = step.split("python - <<'PYTHON'\n", 1)[1].split('\nPYTHON\n', 1)[0]
    staging = Path(tmp) / 'stage'
    staging.mkdir()
    (staging / 'action.yml').write_text(
        (ROOT / 'action.yml').read_text(encoding='utf-8'), encoding='utf-8')
    wrapper_dir = Path(tmp) / 'runner-temp'
    wrapper_dir.mkdir()
    environment = dict(os.environ)
    environment['RUNNER_TEMP'] = str(wrapper_dir)
    subprocess.run([sys.executable, '-c', heredoc], cwd=staging,
                   env=environment, check=True, capture_output=True, text=True)
    loader = yaml.BaseLoader
    action = yaml.load((staging / 'action.yml').read_text(encoding='utf-8'),
                       Loader=loader)
    wrapper = yaml.load(
        (wrapper_dir / 'pr-gate-action-workflow.yml').read_text(encoding='utf-8'),
        Loader=loader)
    inputs = {name: {'description': spec['description'], 'type': 'string'}
              for name, spec in action['inputs'].items()}
    assert wrapper['on'] == {'workflow_dispatch': {'inputs': inputs}}
    assert wrapper['permissions'] == {}
    assert wrapper['jobs']['action']['runs-on'] == 'ubuntu-latest'
    steps = wrapper['jobs']['action']['steps']
    assert steps[0] == {'uses': './'}
    assert steps[1:] == action['runs']['steps'], (
        'the wrapper carries the composite steps verbatim so actionlint '
        'reads the action, not a retyped copy')


def test_zizmor_pin_is_hash_pinned_and_dependabot_visible(tmp):
    del tmp
    workflow = _workflow('actionlint.yml')
    job = workflow['jobs']['actionlint']
    runs = [step['run'] for step in job['steps'] if 'run' in step]
    assert ('python -m pip install --require-hashes --only-binary=:all: '
            '--retries 8 --timeout 60 -r requirements-zizmor.txt') in runs, (
        'zizmor must install from the hash-pinned root manifest, wheels only, '
        'with pip retrying transport failures')
    assert not any('--upgrade pip' in run for run in runs), (
        'the audit job must not carry the unreviewed pip upgrade')
    manifest = ROOT / 'requirements-zizmor.txt'
    assert manifest.is_file(), 'missing requirements-zizmor.txt'
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert '!requirements-zizmor.txt' in ignore, (
        'requirements-zizmor.txt must be whitelisted in .gitignore')
    entries = _manifest_entries(manifest)
    assert entries, 'requirements-zizmor.txt must carry at least one requirement'
    for entry in entries:
        assert '==' in entry, f'not version-pinned: {entry}'
        assert '--hash=sha256:' in entry, f'not hash-pinned: {entry}'


def test_audit_gate_answers_on_every_trigger_an_advisory_can_arrive_under(tmp):
    del tmp
    workflow = _workflow('audit.yml')
    events = workflow['on']
    assert set(events) == {'push', 'pull_request', 'schedule', 'workflow_dispatch'}
    # Both event triggers are filtered to the root manifests the audit glob
    # reads: a commit that changes no pin cannot change the answer, and the
    # daily cron re-answers for every commit under the moving advisory data.
    assert events['push'] == {'branches': ['main'], 'paths': ['requirements*.txt']}
    assert events['pull_request'] == {'paths': ['requirements*.txt']}
    assert events['schedule'] == [{'cron': '12 4 * * *'}]
    assert workflow['concurrency'] == {
        'group': "audit-${{ github.event_name == 'push' && github.sha || github.ref }}",
        'cancel-in-progress': 'true'}


def test_audit_installs_pip_audit_from_a_visible_unhashed_manifest(tmp):
    del tmp
    steps = _audit_steps()
    installs = [step for step in steps if step.get('name') == 'Install pip-audit']
    audits = [step for step in steps if step.get('name') == 'Audit dependencies']
    assert len(installs) == 1 and len(audits) == 1
    assert installs[0]['run'] == (
        'python -m pip install --retries 8 --timeout 60 -r requirements-pip-audit.txt'), (
        'pip-audit must install from the manifest Dependabot watches, not an '
        'inline pin, with pip retrying transport failures')
    assert [step.get('name') for step in steps].index('Install pip-audit') < (
        [step.get('name') for step in steps].index('Audit dependencies')), (
        'the audit step must resolve pip-audit from the PATH the install step populates')
    assert not any('--upgrade pip' in step['run'] for step in steps if 'run' in step), (
        'the unreviewed pip upgrade is an unaudited component')
    manifest = ROOT / 'requirements-pip-audit.txt'
    assert manifest.is_file(), 'missing requirements-pip-audit.txt'
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert '!requirements-pip-audit.txt' in ignore, (
        'requirements-pip-audit.txt must be whitelisted in .gitignore')
    entries = _manifest_entries(manifest)
    assert entries, 'requirements-pip-audit.txt must carry at least one requirement'
    for entry in entries:
        assert '==' in entry, f'not version-pinned: {entry}'
        assert '--hash' not in entry, (
            'pip-audit is deliberately NOT hash-pinned: unlike the zizmor install, '
            'its transitive tree is the subject this gate audits, and a hash pin '
            f'would freeze that tree: {entry}')


def test_audit_glob_covers_every_tracked_requirements_manifest(tmp):
    del tmp
    match = re.search(r'^manifests=\(([^)]*)\)$',
                      _named_run(_audit_steps(), 'Audit dependencies'), re.MULTILINE)
    assert match, 'the audit step must build its manifest list from a glob assignment'
    patterns = match[1].split()
    assert patterns
    universe = subprocess.run(
        ['git', 'ls-files', '--', '*requirements*.txt'],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
    assert universe, 'the repository tracks no requirements*.txt manifest to audit'
    for path in universe:
        assert any(fnmatch.fnmatch(path, pattern) for pattern in patterns), (
            f'{path} is a tracked manifest the audit glob would miss')


def test_lint_manifest_is_hash_pinned_and_dependabot_visible(tmp):
    del tmp
    workflow = _workflow('lint.yml')
    job = workflow['jobs']['lint']
    runs = [step['run'] for step in job['steps'] if 'run' in step]
    assert 'python -m pip install --require-hashes -r requirements-lint.txt' in runs, (
        'the lint gates must install from the hash-pinned root manifest')
    manifest = ROOT / 'requirements-lint.txt'
    assert manifest.is_file(), 'missing requirements-lint.txt'
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert '!requirements-lint.txt' in ignore, (
        'requirements-lint.txt must be whitelisted in .gitignore')
    entries = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if line[0] in ' \t' and entries:
            entries[-1] = entries[-1] + ' ' + stripped
        else:
            entries.append(stripped)
    assert entries, 'requirements-lint.txt must carry at least one requirement'
    for entry in entries:
        assert '==' in entry, f'not version-pinned: {entry}'
        assert '--hash=sha256:' in entry, f'not hash-pinned: {entry}'
    named = {entry.split('==')[0] for entry in entries}
    assert {'pylint', 'pycodestyle', 'pyright'} <= named, (
        'the manifest must pin the three tools the lint and types gates run')


def test_lint_workflow_runs_both_defect_linters_from_the_pinned_manifest(tmp):
    del tmp
    workflow = _workflow('lint.yml')
    job = workflow['jobs']['lint']
    steps = job['steps']
    runs = [step['run'] for step in steps if 'run' in step]
    assert 'git ls-files "*.py" | xargs python -m pycodestyle' in runs, (
        'pycodestyle must gate every tracked Python file on its exit code')
    assert 'git ls-files "*.py" | xargs pylint --rcfile=.pylintrc' in runs, (
        'pylint must gate every tracked Python file against the checked-in '
        'defect-only configuration')
    install = next((step for step in steps
                    if 'requirements-lint.txt' in step.get('run', '')), None)
    assert install is not None, 'the lint toolchain must be installed first'
    install_index = steps.index(install)
    test_install = next((step for step in steps
                         if 'requirements-test.txt' in step.get('run', '')),
                        None)
    assert test_install is not None, (
        'pylint resolves `import yaml` in the suites, so the test '
        'dependencies must be installed before the linters run')
    test_install_index = steps.index(test_install)
    linters = [step for step in steps
               if step.get('run', '').startswith('git ls-files')]
    assert len(linters) == 2, 'exactly the two linters gate; nothing else runs the file list'
    assert steps.index(linters[0]) > install_index, (
        'both linters must run after the toolchain install')
    assert steps.index(linters[1]) > install_index, (
        'both linters must run after the toolchain install')
    assert steps.index(linters[0]) > test_install_index, (
        'both linters must run after the test dependencies install')
    assert steps.index(linters[1]) > test_install_index, (
        'both linters must run after the test dependencies install')
    # One push reports both gates: pylint still runs when pycodestyle is
    # red, never when the install itself failed (actionlint.yml's pattern).
    assert '!cancelled()' in linters[1].get('if', ''), (
        'pylint must not be skipped because pycodestyle found a defect')


def test_types_workflow_gates_on_the_ratchet_after_the_toolchain(tmp):
    del tmp
    workflow = _workflow('types.yml')
    job = workflow['jobs']['types']
    steps = job['steps']
    runs = [step['run'] for step in steps if 'run' in step]
    assert 'python -m pip install --require-hashes -r requirements-lint.txt' in runs
    assert any('requirements-test.txt' in run for run in runs), (
        'pyright resolves imports from tests/, so PyYAML must be installed')
    assert any('--outputjson' in run for run in runs), (
        'the gate consumes the machine-readable report, so pyright must '
        'produce one')
    pyright_step = next((step for step in steps
                         if '--outputjson' in step.get('run', '')), None)
    assert pyright_step is not None
    ratchet = next((step for step in steps
                    if step.get('run', '').startswith(
                        'python scripts/ci/type_ratchet.py')), None)
    assert ratchet is not None, (
        'a new CI step implementing a checked-in module needs a pin that '
        'the step exists')
    assert 'if' not in ratchet, 'no step condition may weaken the gate'
    assert 'continue-on-error' not in ratchet, 'no failure suppression may weaken the gate'
    assert '||' not in ratchet['run'], 'no error-swallowing fallback may weaken the gate'
    assert pyright_step.get('continue-on-error') == 'true', (
        'pyright exits 1 while the seeded backlog stands, so its own exit '
        'must not pre-empt the ratchet verdict; only the ratchet gates')
    install = next((step for step in steps
                    if 'requirements-lint.txt' in step.get('run', '')), None)
    assert install is not None
    assert steps.index(pyright_step) > steps.index(install)
    assert steps.index(ratchet) > steps.index(pyright_step), (
        'the gate consumes the report the measurement step wrote')


def test_gate_configurations_are_shipped_and_defect_oriented(tmp):
    del tmp
    config = json.loads(
        (ROOT / 'pyrightconfig.json').read_text(encoding='utf-8'))
    assert config['typeCheckingMode'] == 'basic'
    matrix = _workflow('tests.yml')['jobs']['test']['strategy']['matrix']
    assert config['pythonVersion'] == matrix['python'][-1], 'type-check the newest matrix entry'
    assert config['pythonPlatform'] == 'All'
    assert config['reportMissingModuleSource'] == 'none'
    for directory in ('scripts/ci', 'tests'):
        assert directory in config['include'], (
            f'pyright must analyse {directory} — the issue names both')
    assert config['include'] == ['scripts/ci', 'tests'], (
        'the analysis scope is exactly the tracked Python')
    for excluded in ('**/__pycache__', '.venv'):
        assert excluded in config['exclude']
    pylintrc = configparser.ConfigParser()
    pylintrc.read_string((ROOT / '.pylintrc').read_text(encoding='utf-8'))
    messages = pylintrc['MESSAGES CONTROL']
    disabled = {entry.strip() for entry in messages['disable'].split(',')}
    assert {'C', 'R', 'I'} <= disabled, 'style/convention/refactor checks must stay off'
    assert 'enable' not in messages, (
        'a category enable re-enables every specific disable listed '
        'before it, so the E/W checks stay on by not being disabled')
    assert {'subprocess-run-check', 'protected-access'} <= disabled, (
        'each per-message disable must be one a real finding forced')
    assert len(disabled) == 5, 'no disable may join without a real finding and a reason comment'
    pycodestyle_cfg = configparser.ConfigParser()
    pycodestyle_cfg.read_string(
        (ROOT / 'setup.cfg').read_text(encoding='utf-8'))
    section = pycodestyle_cfg['pycodestyle']
    assert section['max-line-length'] == '100', (
        'the line ceiling must stay at the measured convention')
    ignore = section['ignore'].split(',')
    assert 'E402' in ignore, 'the sys.path-before-import harness pattern must stay admitted'
    for default_ignored in ('E121', 'E123', 'E126', 'E226', 'E24', 'E704',
                            'W503', 'W504'):
        assert default_ignored in ignore, (
            'ignore= replaces the pycodestyle defaults, so each default '
            'must stay spelled out beside E402')
    baseline = json.loads(
        (ROOT / 'pyright-baseline.json').read_text(encoding='utf-8'))
    assert isinstance(baseline.get('error_count'), int), 'the recorded count must be an integer'
    assert not isinstance(baseline.get('error_count'), bool)
    assert baseline['error_count'] >= 0


def test_documented_consumer_uses_the_canonical_sha_pinned_action(tmp):
    del tmp
    source = (ROOT / 'README.md').read_text(encoding='utf-8')
    example = re.search(r'```yaml\n(.*?)\n```', source, re.DOTALL)
    assert example is not None
    documented = yaml.load(example[1], Loader=yaml.BaseLoader)
    action = 'Nitjsefnie-Actions/pr-gate'
    assert documented['jobs']['gate']['steps'][0]['uses'] == (
        f'{action}@{_shipped_action_pin(action)}')
    shipped = _workflow('pr-gate.yml')
    assert documented['on'] == shipped['on'], (
        'README trigger block must equal the shipped workflow trigger block')
    documented_job = documented['jobs']['gate']
    shipped_job = shipped['jobs']['gate']
    assert ' '.join(documented_job['if'].split()) == (
        ' '.join(shipped_job['if'].split())), (
        'README gate condition must equal the shipped workflow gate condition')


def test_required_checks_run_unfiltered_on_every_pull_request_and_main_push(tmp):
    del tmp
    for name in ('tests.yml', 'lint.yml', 'types.yml'):
        events = _workflow(name)['on']
        assert set(events) == {'push', 'pull_request', 'workflow_dispatch'}, name
        assert not (events['pull_request'] or {}), f'{name}: filtered PR trigger'
        # Unfiltered, like claim, comment-ratchet and size-ratchet: a
        # ratchet-only commit re-measures the same values, so neither ratchet
        # stages anything and ratchet-push commits nothing — no loop; and
        # every required context runs on every main push tip.
        assert events['push'] == {'branches': ['main']}, name
    # Every push-on-main gate shares its group per SHA: a newer push must not
    # cancel an older commit's still-running required run. A PR keeps one.
    for name in ('tests', 'lint', 'types', 'actionlint'):
        assert _workflow(f'{name}.yml')['concurrency'] == {
            'group': name + "-${{ github.event_name == 'push' && github.sha || github.ref }}",
            'cancel-in-progress': 'true'}, name
    # Only the two unrequired workflows may path-filter a push trigger.
    filtered = {path.name for path in (ROOT / '.github/workflows').glob('*.yml')
                if {'paths', 'paths-ignore'} & set(_workflow(path.name)['on'].get('push') or {})}
    assert filtered == {'audit.yml', 'codeql.yml'}, filtered
    # actionlint is unfiltered too, so the merge-marker check it hosts is
    # never path-gated.
    events = _workflow('actionlint.yml')['on']
    assert set(events) == {'push', 'pull_request', 'workflow_dispatch'}
    assert not (events['pull_request'] or {}), 'filtered PR trigger'
    assert events['push'] == {'branches': ['main']}, (
        'a documentation-only push to main must still run the '
        'merge-marker check')


def test_workflow_jobs_keep_exact_permissions_and_timeouts(tmp):
    del tmp
    # name: (workflow permissions, {job: (job permissions, timeout)}).
    # A job with a None permissions entry inherits the workflow grant, so
    # spelling one out where the workflow is already least-privilege would
    # be a second place to drift; the jobs that hold LESS than the
    # workflow — or anything when the workflow holds none — say so here.
    #
    # claim.yml, pin-guard.yml and pr-gate.yml are the repository's
    # no-checkout conversation writers — their tokens answer /claim commands
    # and post the pin screen's comments and the gate's refusal comment — so
    # the pull-requests: write each holds is the class, not a one-off
    # exception. The one other holder is test-ratchet's `report` (tests.yml's
    # job and test-ratchet-comment.yml's): it checks out the tree for the
    # floors at the base commit, with persist-credentials: false, and runs
    # only the pinned action, executing nothing from the checkout. The grants
    # sit where this table records them: claim.yml carries issues: write and
    # pull-requests: write at JOB level under a workflow that declares none;
    # pin-guard.yml (with contents: read) and pr-gate.yml (with contents: read
    # and issues: read) carry pull-requests: write at WORKFLOW level, each
    # one's job inheriting.
    report = {'contents': 'read', 'pull-requests': 'write', 'actions': 'read'}
    contracts = {
        'release-dispatch.yml': ({}, {
            'verify': ({'contents': 'read', 'actions': 'read', 'checks': 'read'}, '50'),
            'publish': ({'contents': 'write'}, '10')}),
        'comment-share.yml': ({'contents': 'read'}, {'comment-share': (None, '10')}),
        'size-baseline.yml': ({'contents': 'read'}, {'size-baseline': (None, '10')}),
        'tests.yml': ({'contents': 'read'}, {
            'plan': (None, '10'), 'test': (None, '10'),
            'report': (report, '10')}),
        'ratchet-push.yml': ({}, {'push': ({'actions': 'read'}, '15')}),
        'actionlint.yml': ({'contents': 'read'}, {
            'actionlint': (None, '15')}),
        'audit.yml': ({'contents': 'read'}, {'pip-audit': (None, '15')}),
        'lint.yml': ({'contents': 'read'}, {'lint': (None, '10')}),
        'types.yml': ({'contents': 'read'}, {'types': (None, '10')}),
        'pin-guard.yml': ({'contents': 'read', 'pull-requests': 'write'}, {
            'pin-guard': (None, '5')}),
        'claim.yml': (None, {'claim': ({'issues': 'write',
                                        'pull-requests': 'write'}, '5')}),
        'pr-gate.yml': ({'contents': 'read', 'issues': 'read',
                         'pull-requests': 'write'}, {'gate': (None, '5')}),
        'codeql.yml': ({}, {'analyze': ({
            'actions': 'read', 'contents': 'read',
            'security-events': 'write'}, '15')}),
        'scorecard.yml': ({'contents': 'read'}, {'analysis': ({
            'contents': 'read', 'security-events': 'write',
            'id-token': 'write'}, '15')}),
        'secrets.yml': ({'contents': 'read'}, {'gitleaks': (None, '10')}),
        'test-ratchet-comment.yml': ({}, {'comment': (report, '10')}),
    }
    assert {path.name for path in (ROOT / '.github/workflows').glob('*.yml')} == set(contracts)
    # The jobs allowed a job-level `if:`, with the guard each one carries.
    guarded_jobs = {
        ('claim.yml', 'claim'),
        ('pr-gate.yml', 'gate'),
        ('scorecard.yml', 'analysis'),
        ('tests.yml', 'report'),
        ('ratchet-push.yml', 'push'),
        ('test-ratchet-comment.yml', 'comment'),
    }
    for name, (permissions, jobs) in contracts.items():
        workflow = _workflow(name)
        assert workflow.get('permissions') == permissions, name
        assert set(workflow['jobs']) == set(jobs), name
        for job_name, (job_permissions, timeout) in jobs.items():
            job = workflow['jobs'][job_name]
            if job_permissions is None:
                assert 'permissions' not in job, name
            else:
                assert job.get('permissions') == job_permissions, name
            assert job.get('timeout-minutes') == timeout, name
            assert 'concurrency' not in job, name
            expected_runs = ('${{ matrix.os }}'
                             if (name, job_name) == ('tests.yml', 'test')
                             else 'ubuntu-latest')
            assert job['runs-on'] == expected_runs, name
            if (name, job_name) not in guarded_jobs:
                assert 'if' not in job, name


def test_pr_gate_consumes_reviewed_action_without_checkout(tmp):
    del tmp
    workflow = _workflow('pr-gate.yml')
    assert set(workflow) == {'name', 'on', 'permissions', 'concurrency', 'jobs'}, (
        'pr gate must not add workflow execution overrides')
    assert workflow['on'] == {
        'pull_request_target': {'types': [
            'opened', 'edited', 'reopened', 'synchronize', 'ready_for_review']}}, (
        'pr gate must run on synchronize so its check attaches to every new '
        'head, and on ready_for_review so a draft gates when marked ready')
    assert workflow.get('concurrency') == {
        'group': 'pr-gate-${{ github.event.pull_request.number }}',
        'cancel-in-progress': 'false'}, 'pr gate must serialize each PR without cancellation'
    job = workflow['jobs']['gate']
    assert set(job) == {'if', 'runs-on', 'timeout-minutes', 'steps'}, (
        'pr gate must not add job overrides or suppress failures')
    assert ' '.join(job['if'].split()) == (
        "github.event.pull_request.user.type != 'Bot' "
        '&& github.event.pull_request.draft == false'), (
        'pr gate must skip exactly Bot authors and draft pull requests')
    assert len(job['steps']) == 1, 'pr gate must execute only its pinned action'
    step = job['steps'][0]
    assert set(step) == {'uses', 'with'}, (
        'pr gate needs no checkout, shell, step condition or failure suppression')


# Boundary: shape and cross-file agreement only; whether a version
# comment names the release carrying that SHA is a reviewer-side oracle
# (tag API).
def test_readme_and_pr_gate_workflow_pin_one_reviewed_release_with_matching_version_comments(tmp):
    del tmp
    documented = {}
    for name in ('README.md', '.github/workflows/pr-gate.yml'):
        found = _documented_pr_gate_pins(name)
        assert len(found) == 1, (
            f'{name} must carry exactly one pr-gate uses: pin line, '
            f'not {len(found)}')
        pin, comment = found[0]
        documented[name] = (pin, comment)
    assert documented['README.md'] == documented['.github/workflows/pr-gate.yml'], (
        'README and pr-gate.yml must pin the same SHA under the same version comment')


# The pin-sync step ports Nitjsefnie-Actions/claim's "Check the README pin
# matches claim.yml" step: Dependabot's github-actions ecosystem reads
# workflow files and action metadata, never markdown, so it bumps
# .github/workflows/pr-gate.yml alone and nothing moves README.md's pin.
# The step is the enforcement the prose only promises. Its observable
# contract is an exit status plus output naming the pins it compared, and
# the script text is the workflow's own, parsed out of actionlint.yml,
# never retyped.
_PIN_SYNC_STEP_NAME = 'Check the README pin matches pr-gate.yml'


def _pin_sync_step_run():
    return _named_run(_workflow('actionlint.yml')['jobs']['actionlint']['steps'],
                      _PIN_SYNC_STEP_NAME)


def _documented_pin_line(comment):
    reviewed = _shipped_action_pin('Nitjsefnie-Actions/pr-gate')
    return f'- uses: Nitjsefnie-Actions/pr-gate@{reviewed} {comment}'


def _run_pin_sync_step(tmp, *, readme, workflow):
    staging = Path(tmp) / 'repo'
    staging.mkdir()
    (staging / 'README.md').write_text(readme, encoding='utf-8', newline='\n')
    (staging / '.github/workflows').mkdir(parents=True)
    (staging / '.github/workflows/pr-gate.yml').write_text(
        workflow, encoding='utf-8', newline='\n')
    script = Path(tmp) / 'pin-sync-step.sh'
    script.write_text(_pin_sync_step_run(), encoding='utf-8', newline='\n')
    return subprocess.run(
        [_action_bash(), '-e', str(script)], cwd=staging,
        capture_output=True, text=True, timeout=120)


def test_actionlint_carries_one_pin_sync_step_before_install_actionlint(tmp):
    del tmp
    steps = _workflow('actionlint.yml')['jobs']['actionlint']['steps']
    names = [step.get('name') for step in steps]
    assert names.count(_PIN_SYNC_STEP_NAME) == 1, names
    index = names.index(_PIN_SYNC_STEP_NAME)
    assert set(steps[index]) == {'name', 'run'}, (
        'the pin-sync step must be a plain run step: no id, if, env, '
        'continue-on-error or failure suppression')
    assert '${{' not in steps[index]['run'], (
        'the pin-sync step must not interpolate expressions: zizmor flags '
        'script injection through ${{ }} in run blocks')
    assert names[index + 1] == 'Install actionlint', (
        'the pin-sync step must precede Install actionlint')


def test_pin_sync_step_passes_when_both_files_pin_the_same_release(tmp):
    line = _documented_pin_line('# v1.0.1')
    result = _run_pin_sync_step(
        tmp,
        readme=f'# example\n\n{line}\n',
        workflow=f'jobs:\n  gate:\n    steps:\n      {line}\n')
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_pin_sync_step_fails_when_the_workflow_bump_leaves_the_readme_sha_behind(tmp):
    divergent = 'a' * 40
    reviewed = _shipped_action_pin('Nitjsefnie-Actions/pr-gate')
    result = _run_pin_sync_step(
        tmp,
        readme=f'# example\n\n{_documented_pin_line("# v1.0.1")}\n',
        workflow=f'jobs:\n  gate:\n    steps:\n'
                 f'      - uses: Nitjsefnie-Actions/pr-gate@{divergent} # v1.0.1\n')
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert f'{reviewed} # v1.0.1' in result.stdout, result.stdout
    assert f'{divergent} # v1.0.1' in result.stdout, result.stdout


def test_pin_sync_step_fails_when_only_the_version_comment_diverges(tmp):
    reviewed = _shipped_action_pin('Nitjsefnie-Actions/pr-gate')
    result = _run_pin_sync_step(
        tmp,
        readme=f'# example\n\n{_documented_pin_line("# v1.0.1")}\n',
        workflow=f'jobs:\n  gate:\n    steps:\n'
                 f'      {_documented_pin_line("# v1.1.0")}\n')
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert f'{reviewed} # v1.0.1' in result.stdout, result.stdout
    assert f'{reviewed} # v1.1.0' in result.stdout, result.stdout


def test_pin_sync_step_fails_when_a_file_carries_no_pin_line(tmp):
    line = _documented_pin_line('# v1.0.1')
    result = _run_pin_sync_step(
        tmp,
        readme='# example\n\nNo pin here.\n',
        workflow=f'jobs:\n  gate:\n    steps:\n      {line}\n')
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert ("Expected exactly one 'uses: Nitjsefnie-Actions/pr-gate@' line "
            'in README.md') in result.stdout, result.stdout
    assert 'found 0' in result.stdout, result.stdout
    assert 'removed, renamed, or re-spelled' in result.stdout, result.stdout
    assert 'trailing prose' not in result.stdout, (
        'the count-0 cause must not blame comment trailing prose: the '
        "count-guard's grep pattern ends at '@', so nothing on the far side "
        'of the @ can move the count')


def test_pin_sync_step_fails_when_a_file_carries_two_pin_lines(tmp):
    line = _documented_pin_line('# v1.0.1')
    result = _run_pin_sync_step(
        tmp,
        readme=f'# example\n\n{line}\n',
        workflow=f'jobs:\n  gate:\n    steps:\n'
                 f'      {line}\n      {line}\n')
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert ("Expected exactly one 'uses: Nitjsefnie-Actions/pr-gate@' line "
            'in .github/workflows/pr-gate.yml') in result.stdout, result.stdout
    assert 'found 2' in result.stdout, result.stdout


# The job prefilters only bots and command words; the action itself declines
# a /claim on a pull request or a closed issue with a reply, so an issue-kind
# or issue-state limb here would leave the commander with no answer.
def test_claim_prefilters_only_bots_and_command_words(tmp):
    del tmp
    workflow = _workflow('claim.yml')
    assert workflow['on'] == {'issue_comment': {'types': ['created']}}
    assert workflow.get('concurrency') == {
        'group': 'claim-${{ github.event.issue.number }}',
        'cancel-in-progress': 'false',
        'queue': 'max'}
    job = workflow['jobs']['claim']
    condition = job.get('if', '')
    assert ' '.join(condition.split()) == (
        "github.event.comment.user.type != 'Bot' "
        "&& (contains(github.event.comment.body, '/claim') "
        "|| contains(github.event.comment.body, '/unclaim') "
        "|| contains(github.event.comment.body, '/release'))"
    ), 'claim must guard exactly the commenter and all three commands'
    assert len(job['steps']) == 1, 'claim must execute only its pinned action'
    step = job['steps'][0]
    assert set(step) == {'uses', 'with'}, 'claim needs no checkout, shell or step condition'


def test_codeql_analyzes_python_and_actions_at_the_event_revision(tmp):
    del tmp
    workflow = _workflow('codeql.yml')
    events = workflow['on']
    assert set(events) == {'push', 'schedule', 'workflow_dispatch'}, (
        'codeql analyses via push runs on every branch, never a pull_request event')
    assert events['push'] == {
        'paths-ignore': ['**/*.md', 'LICENSE', '.gitignore']}, (
        'every branch push is analysed; only doc-only pushes skip it')
    assert events['schedule'] == [{'cron': '47 3 * * 3'}]
    assert workflow['concurrency'] == {
        'group': "codeql-${{ github.event_name == 'push' && "
                 "format('{0}-{1}', github.ref, github.sha) || github.ref }}",
        'cancel-in-progress': 'true'}
    job = workflow['jobs']['analyze']
    assert job['strategy'] == {
        'fail-fast': 'false', 'matrix': {'include': [
            {'language': 'python', 'build-mode': 'none'},
            {'language': 'actions', 'build-mode': 'none'}]}}
    steps = job['steps']
    assert 'ref' not in steps[0].get('with', {})
    assert all('if' not in step for step in steps)


def test_scorecard_only_publishes_scheduled_or_manual_default_branch_analysis(tmp):
    del tmp
    workflow = _workflow('scorecard.yml')
    assert set(workflow['on']) == {'schedule', 'workflow_dispatch'}
    assert workflow['on']['schedule'] == [{'cron': '23 2 * * 6'}]
    assert workflow['concurrency'] == {
        'group': 'scorecard-${{ github.ref }}', 'cancel-in-progress': 'true'}
    job = workflow['jobs']['analysis']
    assert ' '.join(job.get('if', '').split()) == (
        "${{ !github.event.repository.fork && github.ref == format('refs/heads/{0}', "
        'github.event.repository.default_branch) }}')
    steps = job['steps']
    assert 'ref' not in steps[0].get('with', {})
    assert all('if' not in step for step in steps)


def test_all_action_references_are_immutable_and_share_family_pins(tmp):
    del tmp
    steps_by_path = []
    for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
        for job in _workflow(path.name)['jobs'].values():
            steps_by_path.append((path, job['steps']))
    action_paths = subprocess.run(
        ['git', 'ls-files', '--', 'action.yml', '*/action.yml'],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
    for name in action_paths:
        path = ROOT / name
        action = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        steps_by_path.append((path, action['runs']['steps']))
    families = {}
    checkouts = 0
    for path, steps in steps_by_path:
        for step in steps:
            if 'uses' not in step:
                continue
            reference = step['uses']
            action, pin = reference.split('@')
            family = '/'.join(action.split('/')[:2])
            families.setdefault(family, set()).add(pin)
            if action == 'actions/checkout':
                checkouts += 1
                assert step.get('with', {}).get('persist-credentials') == 'false', path
    assert checkouts, 'no checkouts found: the persist-credentials assertion would be vacuous'
    assert families
    for family, pins in families.items():
        assert len(pins) == 1, f'{family} must use one revision across all action paths'


def test_security_analysis_propagates_job_and_step_failures(tmp):
    del tmp
    for name in ('codeql.yml', 'scorecard.yml'):
        for job in _workflow(name)['jobs'].values():
            for node in [job, *job['steps']]:
                assert node.get('continue-on-error', 'false') == 'false', (
                    f'{name}: analysis job and step failures must propagate')


def test_dependabot_updates_actions_and_pip_weekly_in_utc_and_groups_whole_families(tmp):
    del tmp
    path = ROOT / '.github/dependabot.yml'
    assert path.is_file(), 'missing Dependabot configuration'
    config = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert config['version'] == '2'
    updates = config['updates']
    assert len(updates) == 2
    assert {entry['package-ecosystem'] for entry in updates} == {'github-actions', 'pip'}
    actions = next(entry for entry in updates
                   if entry['package-ecosystem'] == 'github-actions')
    pip = next(entry for entry in updates if entry['package-ecosystem'] == 'pip')
    for entry in (actions, pip):
        assert entry['directory'] == '/'
        assert entry['schedule']['interval'] == 'weekly'
        assert entry['schedule']['timezone'] == 'Etc/UTC', (
            'the weekly run must land at a stated UTC time')
    assert actions['commit-message'] == {'prefix': 'ci', 'include': 'scope'}, (
        '`include: scope` is what turns the bare `ci` prefix into `ci(deps): ...`')
    assert pip['commit-message'] == {'prefix': 'build', 'include': 'scope'}, (
        '`include: scope` is what turns the bare `build` prefix into '
        '`build(deps): ...`')
    assert actions['groups']['github-actions']['patterns'] == ['*'], (
        'every action bump rides in one version-update pull request, '
        'codeql-action included: `init` and `analyze` must stay on one revision')
    assert actions['groups']['github-actions-security'] == {
        'applies-to': 'security-updates', 'patterns': ['*']}, (
        'security updates are enabled on this repository, so a security bump '
        'would otherwise arrive outside the version group')
    # The version group must keep relying on Dependabot's `applies-to`
    # default: naming `security-updates` there would leave version updates
    # ungrouped, and naming the default explicitly is dead configuration
    # this shape pin refuses.
    assert list(actions['groups']['github-actions']) == ['patterns']


def test_security_policy_and_workflow_inventory_are_shipped(tmp):
    del tmp
    required = [
        'SECURITY.md', '.github/dependabot.yml', '.github/workflows/claim.yml',
        '.github/workflows/codeql.yml', '.github/workflows/scorecard.yml',
        '.github/workflows/pr-gate.yml', '.github/workflows/secrets.yml',
        '.github/workflows/lint.yml', '.github/workflows/types.yml',
        '.gitleaks.toml', 'pyrightconfig.json', 'pyright-baseline.json',
        '.pylintrc', 'requirements-lint.txt', 'setup.cfg',
        'requirements-actionlint.txt',
        'scripts/ci/type_ratchet.py', 'tests/test_type_ratchet.py']
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert ignore[0] == '*'
    for name in required:
        assert (ROOT / name).is_file(), f'missing repository policy: {name}'
        assert f'!{name}' in ignore, f'{name} must be shipped'
    security = (ROOT / 'SECURITY.md').read_text(encoding='utf-8')
    assert 'https://github.com/Nitjsefnie-Actions/pr-gate/security/advisories/new' in security
    assert '[SECURITY.md](SECURITY.md)' in (ROOT / 'README.md').read_text(encoding='utf-8')


# Test doubles for the audit step's external boundary only. pip-audit stands
# in for the PyPI/advisory-network side of the step (its whole observable
# contract here is an argv line and an exit status plus output); the sleep
# function stands in for time, so the attempt-scaled backoff is pinned
# without waiting.
_PIP_AUDIT_STUB = r"""#!/usr/bin/env bash
# Test double for the pip-audit executable: appends one line per call ("$*")
# to $PIP_AUDIT_LOG and replays the call-th "exit|payload" line of
# $PIP_AUDIT_PLAN. A payload prefixed "err " is emitted on stderr (the way
# pip-audit reports a failed attempt's diagnostic); anything else on stdout.
set -e
printf '%s\n' "$*" >> "$PIP_AUDIT_LOG"
index=0
if [ -f "$PIP_AUDIT_COUNT" ]; then
  index=$(cat "$PIP_AUDIT_COUNT")
fi
index=$((index + 1))
printf '%s\n' "$index" > "$PIP_AUDIT_COUNT"
line=$(sed -n "${index}p" "$PIP_AUDIT_PLAN")
if [ -z "$line" ]; then
  echo "pip-audit stub: the plan has no call ${index}" >&2
  exit 99
fi
payload=${line#*|}
case $payload in
  'err '*)
    printf '%s\n' "${payload#err }" >&2
    ;;
  *)
    printf '%s\n' "$payload"
    ;;
esac
exit "${line%%|*}"
"""

_SLEEP_FUNCTION = r"""sleep() {
  printf '%s\n' "$@" >> "$SLEEP_LOG"
}
"""


def _run_audit_step(tmp, plan, manifests=(), tool='pip-audit', step=None):
    staging = Path(tmp) / 'repo'
    staging.mkdir()
    for name in manifests:
        (staging / name).write_text(f'# stand-in for {name}\n',
                                    encoding='utf-8', newline='\n')
    stubs = Path(tmp) / 'stubs'
    stubs.mkdir()
    stub = stubs / tool
    stub.write_text(_PIP_AUDIT_STUB, encoding='utf-8', newline='\n')
    stub.chmod(0o755)
    bash_env = Path(tmp) / 'bash-env.bash'
    bash_env.write_text(_SLEEP_FUNCTION, encoding='utf-8', newline='\n')
    plan_path = Path(tmp) / 'plan.txt'
    plan_path.write_text(''.join(f'{line}\n' for line in plan),
                         encoding='utf-8', newline='\n')
    script = Path(tmp) / 'audit-step.sh'
    script.write_text(step or _named_run(_audit_steps(), 'Audit dependencies'),
                      encoding='utf-8', newline='\n')
    environment = dict(os.environ)
    environment['PATH'] = os.pathsep.join((str(stubs), environment['PATH']))
    environment['BASH_ENV'] = str(bash_env)
    environment.update(
        PIP_AUDIT_LOG=str(Path(tmp) / 'calls.log'),
        PIP_AUDIT_COUNT=str(Path(tmp) / 'calls.count'),
        PIP_AUDIT_PLAN=str(plan_path),
        SLEEP_LOG=str(Path(tmp) / 'sleeps.log'))
    result = subprocess.run(
        [_action_bash(), '-e', str(script)], cwd=staging, env=environment,
        capture_output=True, text=True, timeout=120)

    def lines(path):
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    return result, lines(Path(tmp) / 'calls.log'), lines(Path(tmp) / 'sleeps.log')


# zizmor's audit error (exit 1, such as a failed GitHub API call) is re-run
# with backoff; a finding (11-14) or a third error is final.
def test_zizmor_reruns_only_its_audit_error(tmp):
    step = _named_run(_workflow('actionlint.yml')['jobs']['actionlint']['steps'], 'zizmor')
    for case, plan in enumerate((['1|err e', '0|ok'], ['13|x'], ['1|e', '14|x'], ['1|e'] * 3)):
        (Path(tmp) / str(case)).mkdir()
        result, calls, sleeps = _run_audit_step(Path(tmp) / str(case), plan, (), 'zizmor', step)
        assert (result.returncode, len(calls), sleeps) == (
            int(plan[-1].split('|')[0]), len(plan), ['15', '30'][:len(plan) - 1]), result.stderr


def _audited_manifests(calls):
    audited = []
    for call in calls:
        argv = call.split()
        requirements = [argv[index + 1] for index, token in enumerate(argv)
                        if token == '--requirement']
        assert len(requirements) == 1, calls
        audited.append(requirements[0])
    return audited


def test_audit_step_runs_one_invocation_per_staged_manifest(tmp):
    manifests = ('requirements-pip-audit.txt', 'requirements-test.txt',
                 'requirements-zizmor.txt')
    result, calls, sleeps = _run_audit_step(
        tmp, ['0|No vulnerabilities found'] * len(manifests), manifests)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert len(calls) == len(manifests), calls
    for call in calls:
        argv = call.split()
        assert argv[argv.index('--progress-spinner') + 1] == 'off'
    assert sorted(_audited_manifests(calls)) == sorted(manifests), calls
    assert 'No vulnerabilities found' in result.stdout
    assert sleeps == []


def test_audit_step_reports_findings_immediately_without_a_retry(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|Found 2 vulnerabilities'],
        ('requirements-a-findings.txt', 'requirements-b-clean.txt'))
    assert result.returncode == 1
    assert 'Found 2 vulnerabilities' in result.stderr
    # Glob order puts requirements-a-findings.txt first; a finding there must
    # end the step before any later manifest is audited.
    assert len(calls) == 1, calls
    assert '--requirement requirements-a-findings.txt' in calls[0], calls
    assert sleeps == []


def test_audit_step_retries_a_reset_connection_then_succeeds(tmp):
    failing = 'requirements-a-retry.txt'
    clean = 'requirements-b-clean.txt'
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|err Connection reset by peer', '0|No vulnerabilities found',
              '0|No vulnerabilities found'],
        (failing, clean))
    assert result.returncode == 0
    # The failed attempt's diagnostic reaches this stdout only if the step
    # captured stderr into audit.out (`2>&1`) AND the retry arm printed the
    # captured file back (`tail -n 5 audit.out`); removing either loses it.
    assert 'Connection reset by peer' in result.stdout, result.stdout
    # The retry re-runs ONLY the failing manifest, in place: the clean
    # manifest is audited once, after the failing manifest resolves.
    assert _audited_manifests(calls) == [failing, failing, clean], calls
    assert sleeps == ['15'], sleeps


def test_audit_step_retries_a_503_service_unavailable(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|503 Service Unavailable', '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert _audited_manifests(calls) == ['requirements-test.txt'] * 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_retries_a_429_too_many_requests(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|429 Too Many Requests', '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert _audited_manifests(calls) == ['requirements-test.txt'] * 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_retries_a_pip_audit_service_error(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|pip_audit._service.interface.ServiceError: '
              'the vulnerability service is unavailable',
              '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert _audited_manifests(calls) == ['requirements-test.txt'] * 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_exits_immediately_on_a_non_retryable_4xx(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|404 Client Error: Not Found for url: '
              'https://pypi.org/simple/pip-audit/'],
        ('requirements-test.txt',))
    assert result.returncode == 1
    assert 'non-retryable 4xx' in result.stderr
    assert _audited_manifests(calls) == ['requirements-test.txt'], calls
    assert sleeps == []


def test_audit_step_exhausts_three_attempts_on_a_transport_failure(tmp):
    manifest = 'requirements-test.txt'
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|Connection reset by peer'] * 3, (manifest,))
    assert result.returncode == 1
    assert 'exhausted 3 attempts' in result.stderr
    assert manifest in result.stderr, result.stderr
    assert _audited_manifests(calls) == [manifest] * 3, calls
    assert sleeps == ['15', '30', '45'], sleeps


def test_audit_step_fails_loud_when_the_glob_matches_no_manifest(tmp):
    result, calls, sleeps = _run_audit_step(tmp, [])
    assert result.returncode != 0
    assert 'requirements' in result.stderr and 'nothing to audit' in result.stderr
    assert calls == [] and sleeps == []


# The corpus's rehearsal rule, run-workflow-step-scripts-dont-read-them: the
# marker step is executable code, and a text pin cannot tell a working exit
# decode from a neutered one. The step's own run block is executed under
# bash -e, red over a planted marker tree and green over a clean one.
def test_marker_step_executes_as_shipped(tmp):
    step = _named_run(_workflow('actionlint.yml')['jobs']['actionlint']['steps'],
                      'Check no tracked file carries a merge-conflict marker')
    script = Path(tmp) / 'marker-step.sh'
    script.write_text(step, encoding='utf-8', newline='\n')

    def _run_step(repo):
        return subprocess.run([_action_bash(), '-e', str(script)], cwd=repo,
                              capture_output=True, text=True, timeout=120)

    def _git(*arguments):
        return subprocess.run(['git', '-C', str(repo), *arguments], check=True,
                              capture_output=True, text=True).stdout

    repo = Path(tmp) / 'repo'
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True,
                   capture_output=True)
    _git('config', 'user.name', 'fixture')
    _git('config', 'user.email', 'fixture@example.com')
    _git('config', 'commit.gpgsign', 'false')
    (repo / 'clean.md').write_text('# clean\n', encoding='utf-8', newline='\n')
    _git('add', '--', 'clean.md')
    _git('commit', '-m', 'clean: no marker')
    result = _run_step(repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    (repo / 'clean.md').write_text(
        '# conflict\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n',
        encoding='utf-8', newline='\n')
    _git('add', '--', 'clean.md')
    _git('commit', '-m', 'conflict: a planted marker')
    result = _run_step(repo)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert 'A merge-conflict marker is committed' in result.stdout
    assert re.search(r'^clean\.md:\d+:', result.stdout, re.MULTILINE), (
        'the step must name the file and line it reddened')


# Dependabot updates each hash-pinned manifest in its own pull request, so
# two manifests carrying PyYAML can drift apart silently; the actionlint
# job's generator and the suites must keep importing the same PyYAML.
def test_pyyaml_pin_agrees_across_the_hash_pinned_manifests(tmp):
    del tmp
    pins = {}
    for name in ('requirements-test.txt', 'requirements-actionlint.txt'):
        matches = [entry for entry in _manifest_entries(ROOT / name)
                   if entry.startswith('PyYAML==')]
        assert len(matches) == 1, f'{name}: expected exactly one PyYAML pin'
        # requirements-test.txt is deliberately unhashed, so the pin that
        # must agree is the name==version token, not the whole entry.
        pins[name] = matches[0].split()[0]
    assert pins['requirements-test.txt'] == pins['requirements-actionlint.txt'], (
        'the two manifests must pin the same PyYAML release: the actionlint '
        'job and the suites import the same module')
