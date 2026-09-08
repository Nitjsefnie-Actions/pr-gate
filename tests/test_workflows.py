"""CI executes the shipped suite and audits the reusable action itself."""
from pathlib import Path
import re
import yaml
import _util

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    path = ROOT / '.github/workflows' / name
    assert path.is_file(), f'missing workflow: {name}'
    return yaml.load(path.read_text(encoding='utf-8'),
                     Loader=yaml.BaseLoader)


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


def test_documented_consumer_uses_the_canonical_sha_pinned_action(tmp):
    del tmp
    source = (ROOT / 'README.md').read_text(encoding='utf-8')
    example = re.search(r'```yaml\n(.*?)\n```', source, re.DOTALL)
    assert example is not None
    workflow = yaml.load(example[1], Loader=yaml.BaseLoader)
    assert workflow['jobs']['gate']['steps'][0]['uses'] == (
        'Nitjsefnie-Actions/pr-gate@0000000000000000000000000000000000000000')


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
        'claim.yml': ({'issues': 'write'}, 'claim', None, '5'),
        'codeql.yml': ({}, 'analyze', {
            'contents': 'read', 'actions': 'read', 'security-events': 'write'}, '30'),
        'scorecard.yml': ({'contents': 'read'}, 'analysis', {
            'contents': 'read', 'security-events': 'write', 'id-token': 'write'}, '15'),
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
        if name not in ('claim.yml', 'scorecard.yml'):
            assert 'if' not in job, name


def test_claim_only_processes_serialized_open_issue_commands(tmp):
    del tmp
    workflow = _workflow('claim.yml')
    assert workflow['on'] == {'issue_comment': {'types': ['created']}}
    assert workflow.get('concurrency') == {
        'group': 'claim-${{ github.event.issue.number }}',
        'cancel-in-progress': 'false'}
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
                family = '/'.join(action.split('/')[:2])
                families.setdefault(family, set()).add(pin)
                if action == 'actions/checkout':
                    assert step.get('with', {}).get('persist-credentials') == 'false', path
    assert families
    for family, pins in families.items():
        assert len(pins) == 1, f'{family} must use one revision across all action paths'


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


def test_security_policy_and_workflow_inventory_are_shipped(tmp):
    del tmp
    required = [
        'SECURITY.md', '.github/dependabot.yml', '.github/workflows/claim.yml',
        '.github/workflows/codeql.yml', '.github/workflows/scorecard.yml']
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert ignore[0] == '*'
    for name in required:
        assert (ROOT / name).is_file(), f'missing repository policy: {name}'
        assert f'!{name}' in ignore, f'{name} must be shipped'
    security = (ROOT / 'SECURITY.md').read_text(encoding='utf-8')
    assert 'https://github.com/Nitjsefnie-Actions/pr-gate/security/advisories/new' in security
    assert '[SECURITY.md](SECURITY.md)' in (ROOT / 'README.md').read_text(encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prworkflows_'))
