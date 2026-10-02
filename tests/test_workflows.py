"""CI executes the shipped suite and audits the reusable action itself.

The audit.yml step tests execute the workflow's own run-block text under
``bash -e`` with stubs standing in only for the external boundary — the
pip-audit executable (PyPI and the advisory database over the network) and
sleep (time). Fakes live only at that external boundary, per CONTRIBUTING;
everything between the stubs is the script text parsed out of the workflow,
never retyped.
"""
import fnmatch
import os
import re
import subprocess
from pathlib import Path

import yaml
import _util
from test_action import _action_bash

ROOT = Path(__file__).resolve().parents[1]

# Dependency upgrades deliberately update this reviewed contract alongside YAML.
REVIEWED_ACTION_PINS = {
    'actions/checkout': '3d3c42e5aac5ba805825da76410c181273ba90b1',
    'actions/setup-python': '5fda3b95a4ea91299a34e894583c3862153e4b97',
    'Nitjsefnie-Actions/claim': '6ae0d102c79d2feec867520795b318b7ca33904a',
    'Nitjsefnie-Actions/pr-gate': 'b641821e74822ca7983d487f8c75865dcdebfaa8',
    'github/codeql-action/init': 'cdf488f595d80d6e07e03d4674febd5ab45fa938',
    'github/codeql-action/analyze': 'cdf488f595d80d6e07e03d4674febd5ab45fa938',
    'github/codeql-action/upload-sarif': 'cdf488f595d80d6e07e03d4674febd5ab45fa938',
    'ossf/scorecard-action': '2d1146689b8cda280b9bc96326124645441f03bc',
    'actions/upload-artifact': '043fb46d1a93c77aae656e7c1c64a875d1fc6a0a',
}


def _workflow(name):
    path = ROOT / '.github/workflows' / name
    assert path.is_file(), f'missing workflow: {name}'
    return yaml.load(path.read_text(encoding='utf-8'),
                     Loader=yaml.BaseLoader)


# YAML parsing strips comments, so pin lines are extracted by regex over the
# raw text of each file instead of through the parsed document.
PR_GATE_PIN_LINE = re.compile(
    r'^[ \t]*(?:-[ \t]+)?uses:[ \t]*Nitjsefnie-Actions/pr-gate@'
    r'([0-9a-f]{40})[ \t]*(#[ \t]+v\d+\.\d+\.\d+)?[ \t]*$',
    re.MULTILINE)


def _documented_pr_gate_pins(relative_path):
    """Return (pin, version comment) per pr-gate uses: line, comments kept."""
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
    job = workflow['jobs']['suites']
    assert job['strategy']['matrix'] == {
        'os': ['ubuntu-latest', 'windows-latest', 'macos-latest'],
        'python': ['3.11', '3.12', '3.13', '3.14']}
    runs = [step['run'] for step in job['steps'] if 'run' in step]
    assert 'python run_tests.py' in runs
    assert 'python -m ruff check --select E9,F63,F7,F82 .' in runs
    for step in job['steps']:
        if 'uses' in step:
            assert re.search(r'@[0-9a-f]{40}$', step['uses'])
        if step.get('uses', '').startswith('actions/checkout@'):
            assert step['with']['persist-credentials'] == 'false'


def test_workflow_audit_includes_composite_metadata(tmp):
    del tmp
    workflow = _workflow('actionlint.yml')
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'paths' not in workflow['on']['push']
    runs = [step['run'] for step in workflow['jobs']['actionlint']['steps'] if 'run' in step]
    assert './actionlint -color .github/workflows/*.yml' in runs
    assert 'zizmor --no-progress action.yml .github/workflows/' in runs


def test_zizmor_pin_is_hash_pinned_and_dependabot_visible(tmp):
    del tmp
    workflow = _workflow('actionlint.yml')
    job = workflow['jobs']['actionlint']
    runs = [step['run'] for step in job['steps'] if 'run' in step]
    assert 'python -m pip install --require-hashes -r requirements-zizmor.txt' in runs, (
        'zizmor must install from the hash-pinned root manifest')
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
    assert events['push'] == {
        'branches': ['main'],
        'paths-ignore': ['README.md', 'CONTRIBUTING.md',
                         'CODE_OF_CONDUCT.md', 'LICENSE']}
    assert not (events['pull_request'] or {})
    assert events['schedule'] == [{'cron': '12 4 * * *'}]
    assert workflow['concurrency'] == {
        'group': 'audit-${{ github.event.pull_request.number || github.ref }}',
        'cancel-in-progress': 'true'}


def test_audit_installs_pip_audit_from_a_visible_unhashed_manifest(tmp):
    del tmp
    steps = _audit_steps()
    installs = [step for step in steps if step.get('name') == 'Install pip-audit']
    audits = [step for step in steps if step.get('name') == 'Audit dependencies']
    assert len(installs) == 1 and len(audits) == 1
    assert installs[0]['run'] == 'python -m pip install -r requirements-pip-audit.txt', (
        'pip-audit must install from the manifest Dependabot watches, not an inline pin')
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


def test_documented_consumer_uses_the_canonical_sha_pinned_action(tmp):
    del tmp
    source = (ROOT / 'README.md').read_text(encoding='utf-8')
    example = re.search(r'```yaml\n(.*?)\n```', source, re.DOTALL)
    assert example is not None
    documented = yaml.load(example[1], Loader=yaml.BaseLoader)
    action = 'Nitjsefnie-Actions/pr-gate'
    assert documented['jobs']['gate']['steps'][0]['uses'] == (
        f'{action}@{REVIEWED_ACTION_PINS[action]}')
    shipped = _workflow('pr-gate.yml')
    assert documented['on'] == shipped['on'], (
        'README trigger block must equal the shipped workflow trigger block')
    documented_job = documented['jobs']['gate']
    shipped_job = shipped['jobs']['gate']
    assert ' '.join(documented_job['if'].split()) == (
        ' '.join(shipped_job['if'].split())), (
        'README gate condition must equal the shipped workflow gate condition')


def test_required_pr_checks_are_unfiltered_and_keep_main_push_filters(tmp):
    del tmp
    for name in ('tests.yml', 'actionlint.yml'):
        events = _workflow(name)['on']
        assert set(events) == {'push', 'pull_request', 'workflow_dispatch'}, name
        assert not (events['pull_request'] or {}), f'{name}: filtered PR trigger'
        assert events['push'] == {
            'branches': ['main'],
            'paths-ignore': ['README.md', 'CONTRIBUTING.md',
                             'CODE_OF_CONDUCT.md', 'LICENSE']}, name


def test_workflow_jobs_keep_exact_permissions_and_timeouts(tmp):
    del tmp
    contracts = {
        'tests.yml': ({'contents': 'read'}, 'suites', None, '20'),
        'actionlint.yml': ({'contents': 'read'}, 'actionlint', None, '15'),
        'audit.yml': ({'contents': 'read'}, 'pip-audit', None, '15'),
        'claim.yml': (None, 'claim', {'issues': 'write'}, '5'),
        'pr-gate.yml': ({'contents': 'read', 'issues': 'read',
                         'pull-requests': 'write'}, 'gate', None, '5'),
        'codeql.yml': ({}, 'analyze', {
            'contents': 'read', 'actions': 'read', 'security-events': 'write'}, '30'),
        'scorecard.yml': ({'contents': 'read'}, 'analysis', {
            'contents': 'read', 'security-events': 'write', 'id-token': 'write'}, '15'),
        'secrets.yml': ({'contents': 'read'}, 'gitleaks', None, '10'),
    }
    assert {path.name for path in (ROOT / '.github/workflows').glob('*.yml')} == set(contracts)
    for name, (permissions, job_name, job_permissions, timeout) in contracts.items():
        workflow = _workflow(name)
        assert workflow.get('permissions') == permissions, name
        assert set(workflow['jobs']) == {job_name}, name
        job = workflow['jobs'][job_name]
        if job_permissions is None:
            assert 'permissions' not in job, name
        else:
            assert job.get('permissions') == job_permissions, name
        assert job.get('timeout-minutes') == timeout, name
        assert 'concurrency' not in job, name
        assert job['runs-on'] == ('${{ matrix.os }}' if name == 'tests.yml'
                                  else 'ubuntu-latest'), name
        if name not in ('claim.yml', 'scorecard.yml', 'pr-gate.yml'):
            assert 'if' not in job, name


def test_pr_gate_consumes_reviewed_action_without_checkout(tmp):
    del tmp
    workflow = _workflow('pr-gate.yml')
    assert set(workflow) == {'name', 'on', 'permissions', 'concurrency', 'jobs'}, (
        'pr gate must not add workflow execution overrides')
    assert workflow['on'] == {
        'pull_request_target': {'types': [
            'opened', 'edited', 'reopened', 'ready_for_review']}}, (
        'pr gate must handle opened, edited, reopened and ready-for-review '
        'PR targets so a draft gates when marked ready for review')
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
    reviewed = REVIEWED_ACTION_PINS['Nitjsefnie-Actions/pr-gate']
    assert step['uses'] == f'Nitjsefnie-Actions/pr-gate@{reviewed}', (
        'pr gate must execute its reviewed action revision')
    assert step['with'] == {
        'github-token': '${{ github.token }}',
        'repository': '${{ github.repository }}',
        'pull-request-number': '${{ github.event.pull_request.number }}',
        'pull-request-author': '${{ github.event.pull_request.user.login }}'}, (
        'pr gate must pass exactly the four documented PR inputs')


# Boundary: shape and cross-file agreement only; whether a version comment names the release carrying that SHA is a reviewer-side oracle (tag API).
def test_readme_and_pr_gate_workflow_pin_one_reviewed_release_with_matching_version_comments(tmp):
    del tmp
    reviewed = REVIEWED_ACTION_PINS['Nitjsefnie-Actions/pr-gate']
    documented = {}
    for name in ('README.md', '.github/workflows/pr-gate.yml'):
        found = _documented_pr_gate_pins(name)
        assert len(found) == 1, (
            f'{name} must carry exactly one pr-gate uses: pin line, not {len(found)}; '
            'when the count is 0 despite a visible pin line, its version comment '
            'likely carries trailing prose such as "# v1.0.0 (notes)", which '
            'the deliberately tight pin-line pattern does not match')
        pin, comment = found[0]
        assert re.fullmatch(r'#[ \t]+v\d+\.\d+\.\d+', comment), (
            f'{name}: the pinned action reference must carry a "# vX.Y.Z" '
            'version comment naming the reviewed release carrying that SHA')
        documented[name] = (pin, comment)
    assert documented['README.md'] == documented['.github/workflows/pr-gate.yml'], (
        'README and pr-gate.yml must pin the same SHA under the same version comment')
    assert documented['README.md'][0] == reviewed, (
        'both files must pin the reviewed pr-gate revision from '
        'REVIEWED_ACTION_PINS')


def test_claim_only_processes_serialized_open_issue_commands(tmp):
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
        'github.event.issue.pull_request == null '
        "&& github.event.issue.state == 'open' "
        "&& github.event.comment.user.type != 'Bot' "
        "&& (contains(github.event.comment.body, '/claim') "
        "|| contains(github.event.comment.body, '/unclaim') "
        "|| contains(github.event.comment.body, '/release'))"
    ), 'claim must guard issue kind, state, commenter and all three commands'
    assert len(job['steps']) == 1, 'claim must execute only its pinned action'
    step = job['steps'][0]
    assert set(step) == {'uses'}, 'claim needs no checkout, shell, inputs or step condition'
    assert re.fullmatch(r'Nitjsefnie-Actions/claim@[0-9a-f]{40}', step['uses'])


def test_codeql_analyzes_python_and_actions_at_the_event_revision(tmp):
    del tmp
    workflow = _workflow('codeql.yml')
    events = workflow['on']
    assert set(events) == {'push', 'pull_request', 'schedule', 'workflow_dispatch'}
    assert events['push'] == {'branches': ['main']}
    assert not (events['pull_request'] or {})
    assert events['schedule'] == [{'cron': '47 3 * * 3'}]
    assert workflow['concurrency'] == {
        'group': 'codeql-${{ github.ref }}', 'cancel-in-progress': 'true'}
    job = workflow['jobs']['analyze']
    assert job['strategy'] == {
        'fail-fast': 'false', 'matrix': {'include': [
            {'language': 'python', 'build-mode': 'none'},
            {'language': 'actions', 'build-mode': 'none'}]}}
    steps = job['steps']
    assert [step['uses'].split('@')[0] for step in steps] == [
        'actions/checkout', 'github/codeql-action/init', 'github/codeql-action/analyze']
    assert 'ref' not in steps[0].get('with', {})
    assert steps[1]['with'] == {
        'languages': '${{ matrix.language }}', 'build-mode': '${{ matrix.build-mode }}',
        'queries': 'security-extended'}
    assert steps[2]['with'] == {'category': '/language:${{ matrix.language }}'}
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
    assert [step['uses'].split('@')[0] for step in steps] == [
        'actions/checkout', 'ossf/scorecard-action', 'actions/upload-artifact',
        'github/codeql-action/upload-sarif']
    assert 'ref' not in steps[0].get('with', {})
    assert steps[1]['with'] == {
        'results_file': 'results.sarif', 'results_format': 'sarif', 'publish_results': 'true'}
    assert steps[2]['with'] == {
        'name': 'scorecard-results', 'path': 'results.sarif', 'retention-days': '5'}
    assert steps[3]['with'] == {'sarif_file': 'results.sarif'}
    assert all('if' not in step for step in steps)


def test_secrets_scans_full_history_with_a_frozen_binary(tmp):
    del tmp
    workflow = _workflow('secrets.yml')
    events = workflow['on']
    assert set(events) == {'push', 'pull_request', 'schedule', 'workflow_dispatch'}
    assert events['push'] == {'branches': ['main']}, (
        'a scanner must see docs commits, so no paths filter may narrow the push')
    assert not (events['pull_request'] or {}), 'the pull_request trigger stays unfiltered'
    assert events['schedule'] == [{'cron': '26 5 * * *'}], (
        "exactly one daily cron, off the hour, clear of codeql Wed '47 3 * * 3' "
        "and scorecard Sat '23 2 * * 6'")
    assert not (events['workflow_dispatch'] or {}), 'the manual trigger stays unfiltered'
    assert workflow.get('permissions') == {'contents': 'read'}
    assert workflow['concurrency'] == {
        'group': 'secrets-${{ github.ref }}', 'cancel-in-progress': 'true'}
    assert set(workflow['jobs']) == {'gitleaks'}
    job = workflow['jobs']['gitleaks']
    assert job['runs-on'] == 'ubuntu-latest'
    assert job['timeout-minutes'] == '10'
    assert 'permissions' not in job, 'the workflow-level read must be the only grant'
    steps = job['steps']
    assert len(steps) == 3
    assert steps[0]['uses'] == (
        'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1')
    assert steps[0]['with'] == {'fetch-depth': '0', 'persist-credentials': 'false'}
    download = steps[1]
    assert download['run'].splitlines() == [
        'curl --connect-timeout 5 --max-time 120 -fsSLO '
        'https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/'
        'gitleaks_8.30.1_linux_x64.tar.gz',
        'echo \'551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'
        '  gitleaks_8.30.1_linux_x64.tar.gz\' | sha256sum -c -',
        'tar xzf gitleaks_8.30.1_linux_x64.tar.gz gitleaks',
        './gitleaks version']
    assert '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb' in (
        download['run'])
    scan = steps[2]
    assert scan['run'] == './gitleaks detect --verbose --redact --config .gitleaks.toml', (
        'the scan command is the gate itself and ships verbatim')
    assert 'if' not in scan, 'no step condition may weaken the gate'
    assert 'continue-on-error' not in scan, 'no failure suppression may weaken the gate'
    assert '||' not in scan['run'], 'no error-swallowing fallback may weaken the gate'
    assert (ROOT / '.gitleaks.toml').read_text(encoding='utf-8').splitlines() == [
        "# The [extend] line is load-bearing: it keeps gitleaks's default rules as",
        '# the gate. An allowlist file that replaces the default rules would',
        '# silently narrow the gate to nothing, so nothing here replaces them.',
        '#',
        "# pr-gate's full history scans clean under the default rules today",
        '# (gitleaks 8.30.1, 2026-10-02), so no allowlist entry exists.',
        '[extend]',
        'useDefault = true'], (
        'the extend line IS the gate; a narrowing allowlist replacement must '
        'fail this suite')


def test_all_action_references_are_immutable_and_share_family_pins(tmp):
    del tmp
    families = {}
    for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
        for job in _workflow(path.name)['jobs'].values():
            for step in job['steps']:
                if 'uses' not in step:
                    continue
                reference = step['uses']
                assert re.fullmatch(r'[\w.-]+/[\w./-]+@[0-9a-f]{40}', reference), reference
                action, pin = reference.split('@')
                assert pin == REVIEWED_ACTION_PINS.get(action), (
                    f'{path.name}: {action} must use its reviewed revision')
                family = '/'.join(action.split('/')[:2])
                families.setdefault(family, set()).add(pin)
                if action == 'actions/checkout':
                    assert step.get('with', {}).get('persist-credentials') == 'false', path
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


def test_dependabot_updates_actions_and_pip_and_groups_codeql(tmp):
    del tmp
    path = ROOT / '.github/dependabot.yml'
    assert path.is_file(), 'missing Dependabot configuration'
    config = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert config['version'] == '2'
    updates = config['updates']
    assert len(updates) == 2
    assert {entry['package-ecosystem'] for entry in updates} == {'github-actions', 'pip'}
    for entry in updates:
        assert entry['directory'] == '/'
        assert entry['schedule']['interval'] == 'weekly'
    actions = next(entry for entry in updates if entry['package-ecosystem'] == 'github-actions')
    assert actions['groups']['codeql-action']['patterns'] == ['github/codeql-action*']
    security = actions['groups']['codeql-action-security']
    assert security['applies-to'] == 'security-updates'
    assert security['patterns'] == ['github/codeql-action*']
    # The version group must keep relying on Dependabot's `applies-to` default;
    # naming a kind here would stop the other kind from being grouped.
    assert list(actions['groups']['codeql-action']) == ['patterns']


def test_security_policy_and_workflow_inventory_are_shipped(tmp):
    del tmp
    required = [
        'SECURITY.md', '.github/dependabot.yml', '.github/workflows/claim.yml',
        '.github/workflows/codeql.yml', '.github/workflows/scorecard.yml',
        '.github/workflows/pr-gate.yml', '.github/workflows/secrets.yml',
        '.gitleaks.toml']
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
# contract here is an argv line and an exit status plus output); sleep stands
# in for time, so the attempt-scaled backoff is pinned without waiting.
_PIP_AUDIT_STUB = r"""#!/usr/bin/env bash
# Test double for the pip-audit executable: appends one line per call ("$*")
# to $PIP_AUDIT_LOG and replays the call-th "exit|output" line of
# $PIP_AUDIT_PLAN.
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
printf '%s\n' "${line#*|}"
exit "${line%%|*}"
"""

_SLEEP_STUB = r"""#!/usr/bin/env bash
# Test double for sleep: records its argument so the backoff formula is
# pinned without waiting in real time.
printf '%s\n' "$*" >> "$SLEEP_LOG"
"""


def _run_audit_step(tmp, plan, manifests=()):
    staging = Path(tmp) / 'repo'
    staging.mkdir()
    for name in manifests:
        (staging / name).write_text(f'# stand-in for {name}\n',
                                    encoding='utf-8', newline='\n')
    stubs = Path(tmp) / 'stubs'
    stubs.mkdir()
    for name, source in (('pip-audit', _PIP_AUDIT_STUB), ('sleep', _SLEEP_STUB)):
        stub = stubs / name
        stub.write_text(source, encoding='utf-8', newline='\n')
        stub.chmod(0o755)
    plan_path = Path(tmp) / 'plan.txt'
    plan_path.write_text(''.join(f'{line}\n' for line in plan),
                         encoding='utf-8', newline='\n')
    script = Path(tmp) / 'audit-step.sh'
    script.write_text(_named_run(_audit_steps(), 'Audit dependencies'),
                      encoding='utf-8', newline='\n')
    environment = dict(os.environ)
    environment['PATH'] = os.pathsep.join((str(stubs), environment['PATH']))
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


def test_audit_step_runs_one_invocation_over_every_staged_manifest(tmp):
    manifests = ('requirements-pip-audit.txt', 'requirements-test.txt',
                 'requirements-zizmor.txt')
    result, calls, sleeps = _run_audit_step(
        tmp, ['0|No vulnerabilities found'], manifests)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert len(calls) == 1, calls
    argv = calls[0].split()
    assert argv[argv.index('--progress-spinner') + 1] == 'off'
    requirements = [argv[index + 1] for index, token in enumerate(argv)
                    if token == '--requirement']
    assert sorted(requirements) == sorted(manifests), calls
    assert 'No vulnerabilities found' in result.stdout
    assert sleeps == []


def test_audit_step_reports_findings_immediately_without_a_retry(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|Found 2 vulnerabilities'], ('requirements-test.txt',))
    assert result.returncode == 1
    assert 'Found 2 vulnerabilities' in result.stderr
    assert len(calls) == 1, calls
    assert sleeps == []


def test_audit_step_retries_a_reset_connection_then_succeeds(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|Connection reset by peer', '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert len(calls) == 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_retries_a_503_service_unavailable(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|503 Service Unavailable', '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert len(calls) == 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_retries_a_429_too_many_requests(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|429 Too Many Requests', '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert len(calls) == 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_retries_a_pip_audit_service_error(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|pip_audit._service.interface.ServiceError: '
              'the vulnerability service is unavailable',
              '0|No vulnerabilities found'],
        ('requirements-test.txt',))
    assert result.returncode == 0
    assert len(calls) == 2, calls
    assert sleeps == ['15'], sleeps


def test_audit_step_exits_immediately_on_a_non_retryable_4xx(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|404 Client Error: Not Found for url: '
              'https://pypi.org/simple/pip-audit/'],
        ('requirements-test.txt',))
    assert result.returncode == 1
    assert 'non-retryable 4xx' in result.stderr
    assert len(calls) == 1, calls
    assert sleeps == []


def test_audit_step_exhausts_three_attempts_on_a_transport_failure(tmp):
    result, calls, sleeps = _run_audit_step(
        tmp, ['1|Connection reset by peer'] * 3, ('requirements-test.txt',))
    assert result.returncode == 1
    assert 'exhausted 3 attempts' in result.stderr
    assert len(calls) == 3, calls
    assert sleeps == ['15', '30', '45'], sleeps


def test_audit_step_fails_loud_when_the_glob_matches_no_manifest(tmp):
    result, calls, sleeps = _run_audit_step(tmp, [])
    assert result.returncode != 0
    assert 'requirements' in result.stderr and 'nothing to audit' in result.stderr
    assert calls == [] and sleeps == []


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prworkflows_'))
