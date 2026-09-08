"""CI executes the shipped suite and audits the reusable action itself."""
from pathlib import Path
import re
import yaml
import _util

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'),
                     Loader=yaml.BaseLoader)


def test_ci_runs_supported_platforms_and_python_versions(tmp):
    del tmp
    workflow = _workflow('tests.yml')
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['on']['push']['branches'] == ['main']
    assert 'paths' not in workflow['on']['push']
    assert 'paths' not in workflow['on']['pull_request']
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


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prworkflows_'))
