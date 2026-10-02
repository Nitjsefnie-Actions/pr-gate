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
import _util
from test_action import _action_bash

ROOT = Path(__file__).resolve().parents[1]

# Dependency upgrades deliberately update this reviewed contract alongside YAML.
REVIEWED_ACTION_PINS = {
    'actions/checkout': '3d3c42e5aac5ba805825da76410c181273ba90b1',
    'actions/setup-python': '5fda3b95a4ea91299a34e894583c3862153e4b97',
    'Nitjsefnie-Actions/claim': '8abff4f2f27d59b984528cb736f64b9391952a25',
    'Nitjsefnie-Actions/pr-gate': 'bd6aa28fa8a82ef12c1afb61a46f41f23742f26a',
    'github/codeql-action/init': '2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2',
    'github/codeql-action/analyze': '2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2',
    'github/codeql-action/upload-sarif': '2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2',
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


# Boundary: exactly one matrix cell measures coverage, so a summary line and a
# gate failure name a single reproducible environment instead of three rows
# that disagree; unmeasured steps stay unconditional.
def test_coverage_measured_on_exactly_one_matrix_cell(tmp):
    del tmp
    steps = _workflow('tests.yml')['jobs']['suites']['steps']
    coverage_steps = [step for step in steps
                      if 'coverage' in step.get('run', '')]
    assert len(coverage_steps) == 3, (
        'exactly the measure, summary and gate steps may mention coverage')
    measured_if = ("matrix.os == 'ubuntu-latest' "
                   "&& matrix.python == '3.13'")
    for step in coverage_steps:
        assert step['if'] == measured_if, (
            'every coverage step must pin the one measured cell exactly')
    # An exact `if:` is fail-green on its own: a matrix edit dropping either
    # operand leaves the condition matching no cell, the gate silently stops
    # running, and nothing else names the cell the coverage steps depend on.
    # Tie both operands back into the pinned matrix lists.
    matrix = _workflow('tests.yml')['jobs']['suites']['strategy']['matrix']
    assert 'ubuntu-latest' in matrix['os'], (
        "the coverage steps' matrix.os operand 'ubuntu-latest' must stay in "
        'the pinned os matrix, or the gate runs on no cell')
    assert '3.13' in matrix['python'], (
        "the coverage steps' matrix.python operand '3.13' must stay in the "
        'pinned python matrix, or the gate runs on no cell')
    unmeasured_runs = ['python run_tests.py',
                       'python -m ruff check --select E9,F63,F7,F82 .',
                       'python -m pip install -r requirements-test.txt']
    unconditional_runs = {step.get('run', '') for step in steps
                          if 'if' not in step}
    for run in unmeasured_runs:
        assert run in unconditional_runs, (
            f'the plain run {run!r} must stay unmeasured and unconditional')
    for step in steps:
        if step in coverage_steps:
            continue
        assert 'if' not in step, (
            'checkout, setup-python, install, the plain suite and the lint '
            'must run unconditionally on every cell')


def test_coverage_gate_reads_floor_from_committed_thresholds(tmp):
    del tmp
    steps = [step for step in _workflow('tests.yml')['jobs']['suites']['steps']
             if 'coverage' in step.get('run', '')]
    assert len(steps) == 3
    measure = next(step for step in steps if 'coverage run' in step['run'])
    summary = next(step for step in steps
                   if 'GITHUB_STEP_SUMMARY' in step['run'])
    gate = next(step for step in steps if '--fail-under=' in step['run'])
    assert '--source=scripts/ci' in measure['run'], (
        'the measurement must scope coverage to the scripts/ci runtime surface')
    assert 'run_tests.py' in measure['run'], (
        'coverage must be measured by running the shipped suite itself')
    assert 'coverage report' in summary['run'], (
        'the summary must render the measured coverage table')
    assert '.github/ci-thresholds.json' in gate['run'], (
        'the gate must read its floor from the committed thresholds file')
    for step in steps:
        assert not re.search(r'fail-under=\d', step['run']), (
            'no coverage step may restate the floor as a literal number')


# Boundary: shape and cross-file agreement only; the measured value itself is
# refreshed by re-running the coverage cell, not by this suite.
def test_coverage_thresholds_floor_is_seeded_below_measurement(tmp):
    del tmp
    path = ROOT / '.github/ci-thresholds.json'
    assert path.is_file(), 'missing .github/ci-thresholds.json'
    text = path.read_text(encoding='utf-8')
    thresholds = json.loads(text)
    assert thresholds['schema_version'] == 1
    python = thresholds['coverage']['python']
    assert python['cell'] == 'ubuntu-latest / Python 3.13', (
        'the recorded cell must name the one matrix cell that measures')
    assert re.search(r'"floor":\s*\d+\.\d\b', text), (
        'floor must be a number with exactly one decimal place')
    assert re.search(r'"measured":\s*\d+\.\d\b', text), (
        'measured must be a number with exactly one decimal place')
    assert isinstance(python['floor'], float), (
        'coverage.python.floor must be a JSON float, '
        f"got {python['floor']!r}")
    assert isinstance(python['measured'], float), (
        'coverage.python.measured must be a JSON float, '
        f"got {python['measured']!r}")
    assert 0 <= python['floor'] <= python['measured'] <= 100, (
        'the floor seeds below the measured value and never above it')
    ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert '!.github/ci-thresholds.json' in ignore, (
        'the thresholds file must be whitelisted in .gitignore')
    requirements = (ROOT / 'requirements-test.txt').read_text(encoding='utf-8')
    assert re.search(r'^coverage==', requirements, re.MULTILINE), (
        'coverage must be a pinned test dependency')


def test_workflow_audit_includes_composite_metadata(tmp):
    del tmp
    workflow = _workflow('actionlint.yml')
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['on']['push'] == {'branches': ['main']}
    steps = workflow['jobs']['actionlint']['steps']
    names = [step.get('name') for step in steps]
    # The four ported checks run on the runner's system python3 and sit
    # before any download: cheap refusals precede setup-python and every
    # install step, so a stale, marker-carrying or mis-scoped head never
    # pays for the installs that follow.
    assert steps[0]['uses'].startswith('actions/checkout@')
    assert names[1] == 'Check no tracked file carries a merge-conflict marker'
    assert names[2] == "Require this head to carry main's gate-defining commits"
    assert names[3] == ('Refuse a commit whose scope names a workflow '
                        'outside the ci type')
    for step in steps[1:4]:
        assert set(step) == {'name', 'run'}, (
            'each ported check is a plain run step: no id, if, env or '
            'failure suppression')
    marker = steps[1]['run']
    assert "git grep -nI -E '^(<{7}( |$)|>{7}( |$)|={7}$)' -- ." in marker
    assert '${{' not in marker
    assert steps[2]['run'] == 'python3 scripts/ci/gate_base_freshness.py'
    assert steps[3]['run'] == 'python3 scripts/ci/commit_scopes.py'

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
    assert len(linters) == 2, (
        'exactly the two linters gate; nothing else runs the file list')
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
    assert 'continue-on-error' not in ratchet, (
        'no failure suppression may weaken the gate')
    assert '||' not in ratchet['run'], (
        'no error-swallowing fallback may weaken the gate')
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
    assert config['pythonVersion'] == '3.13'
    assert config['pythonPlatform'] == 'All'
    assert config['reportMissingModuleSource'] == 'none'
    for directory in ('scripts/ci', 'tests'):
        assert directory in config['include'], (
            f'pyright must analyse {directory} — the issue names both')
    assert config['include'] == ['run_tests.py', 'scripts/ci', 'tests'], (
        'the analysis scope is exactly the tracked Python')
    for excluded in ('**/__pycache__', '.venv'):
        assert excluded in config['exclude']
    pylintrc = configparser.ConfigParser()
    pylintrc.read_string((ROOT / '.pylintrc').read_text(encoding='utf-8'))
    messages = pylintrc['MESSAGES CONTROL']
    disabled = {entry.strip() for entry in messages['disable'].split(',')}
    assert {'C', 'R', 'I'} <= disabled, (
        'style/convention/refactor checks must stay off')
    assert 'enable' not in messages, (
        'a category enable re-enables every specific disable listed '
        'before it, so the E/W checks stay on by not being disabled')
    assert {'subprocess-run-check', 'broad-exception-caught',
            'protected-access'} <= disabled, (
        'each per-message disable must be one a real finding forced')
    assert len(disabled) == 6, (
        'no disable may join without a real finding and a reason comment')
    pycodestyle_cfg = configparser.ConfigParser()
    pycodestyle_cfg.read_string(
        (ROOT / 'setup.cfg').read_text(encoding='utf-8'))
    section = pycodestyle_cfg['pycodestyle']
    assert section['max-line-length'] == '100', (
        'the line ceiling must stay at the measured convention')
    ignore = section['ignore'].split(',')
    assert 'E402' in ignore, (
        'the sys.path-before-import harness pattern must stay admitted')
    for default_ignored in ('E121', 'E123', 'E126', 'E226', 'E24', 'E704',
                            'W503', 'W504'):
        assert default_ignored in ignore, (
            'ignore= replaces the pycodestyle defaults, so each default '
            'must stay spelled out beside E402')
    baseline = json.loads(
        (ROOT / 'pyright-baseline.json').read_text(encoding='utf-8'))
    assert isinstance(baseline.get('error_count'), int), (
        'the recorded count must be an integer')
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
        f'{action}@{REVIEWED_ACTION_PINS[action]}')
    shipped = _workflow('pr-gate.yml')
    assert documented['on'] == shipped['on'], (
        'README trigger block must equal the shipped workflow trigger block')
    documented_job = documented['jobs']['gate']
    shipped_job = shipped['jobs']['gate']
    assert ' '.join(documented_job['if'].split()) == (
        ' '.join(shipped_job['if'].split())), (
        'README gate condition must equal the shipped workflow gate condition')


def test_required_pr_checks_are_unfiltered_and_docs_only_pushes_still_run_actionlint(tmp):
    del tmp
    for name in ('tests.yml', 'lint.yml', 'types.yml'):
        events = _workflow(name)['on']
        assert set(events) == {'push', 'pull_request', 'workflow_dispatch'}, name
        assert not (events['pull_request'] or {}), f'{name}: filtered PR trigger'
        assert events['push'] == {
            'branches': ['main'],
            'paths-ignore': ['README.md', 'CONTRIBUTING.md',
                             'CODE_OF_CONDUCT.md', 'LICENSE']}, name
    # actionlint is the one required workflow whose push trigger carries no
    # paths-ignore: a documentation-only push to main runs no other
    # workflow at all, so the merge-marker check it hosts would never see
    # a marker committed to README.md — the gap issue 41 closes.
    events = _workflow('actionlint.yml')['on']
    assert set(events) == {'push', 'pull_request', 'workflow_dispatch'}
    assert not (events['pull_request'] or {}), 'filtered PR trigger'
    assert events['push'] == {'branches': ['main']}, (
        'a documentation-only push to main must still run the '
        'merge-marker check')


def test_workflow_jobs_keep_exact_permissions_and_timeouts(tmp):
    del tmp
    contracts = {
        'tests.yml': ({'contents': 'read'}, 'suites', None, '20'),
        'actionlint.yml': ({'contents': 'read'}, 'actionlint', None, '15'),
        'audit.yml': ({'contents': 'read'}, 'pip-audit', None, '15'),
        'lint.yml': ({'contents': 'read'}, 'lint', None, '15'),
        'types.yml': ({'contents': 'read'}, 'types', None, '15'),

        'claim.yml': (None, 'claim', {'issues': 'write'}, '5'),
        'pr-gate.yml': ({'contents': 'read', 'issues': 'read',
                         'pull-requests': 'write'}, 'gate', None, '5'),
        'codeql.yml': ({}, 'analyze', {
            'contents': 'read', 'security-events': 'write'}, '15'),
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


# Boundary: shape and cross-file agreement only; whether a version
# comment names the release carrying that SHA is a reviewer-side oracle
# (tag API).
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
    reviewed = REVIEWED_ACTION_PINS['Nitjsefnie-Actions/pr-gate']
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
    assert steps[index - 1].get('uses', '').startswith('actions/setup-python@'), (
        'the pin-sync step must follow the setup-python step')


def test_pin_sync_step_passes_when_both_files_pin_the_same_release(tmp):
    line = _documented_pin_line('# v1.0.1')
    result = _run_pin_sync_step(
        tmp,
        readme=f'# example\n\n{line}\n',
        workflow=f'jobs:\n  gate:\n    steps:\n      {line}\n')
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_pin_sync_step_fails_when_the_workflow_bump_leaves_the_readme_sha_behind(tmp):
    divergent = 'a' * 40
    reviewed = REVIEWED_ACTION_PINS['Nitjsefnie-Actions/pr-gate']
    result = _run_pin_sync_step(
        tmp,
        readme=f'# example\n\n{_documented_pin_line("# v1.0.1")}\n',
        workflow=f'jobs:\n  gate:\n    steps:\n'
                 f'      - uses: Nitjsefnie-Actions/pr-gate@{divergent} # v1.0.1\n')
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert f'{reviewed} # v1.0.1' in result.stdout, result.stdout
    assert f'{divergent} # v1.0.1' in result.stdout, result.stdout


def test_pin_sync_step_fails_when_only_the_version_comment_diverges(tmp):
    reviewed = REVIEWED_ACTION_PINS['Nitjsefnie-Actions/pr-gate']
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
    assert set(step) == {'uses', 'with'}, (
        'claim needs no checkout, shell or step condition; the two inputs '
        'are the step\'s whole configuration')
    assert step['with'] == {
        'max-claims': 'read=2, triage=4, write=6, maintain=10, admin=-1',
        'expire': '7'}, 'claim must pass exactly the two documented inputs'
    assert re.fullmatch(r'Nitjsefnie-Actions/claim@[0-9a-f]{40}', step['uses'])


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
        'scripts/ci/type_ratchet.py', 'tests/test_type_ratchet.py',
        'scripts/ci/gate_base_freshness.py', 'tests/test_gate_base_freshness.py',
        'scripts/ci/commit_scopes.py', 'tests/test_commit_scopes.py']
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


def _run_audit_step(tmp, plan, manifests=()):
    staging = Path(tmp) / 'repo'
    staging.mkdir()
    for name in manifests:
        (staging / name).write_text(f'# stand-in for {name}\n',
                                    encoding='utf-8', newline='\n')
    stubs = Path(tmp) / 'stubs'
    stubs.mkdir()
    stub = stubs / 'pip-audit'
    stub.write_text(_PIP_AUDIT_STUB, encoding='utf-8', newline='\n')
    stub.chmod(0o755)
    bash_env = Path(tmp) / 'bash-env.bash'
    bash_env.write_text(_SLEEP_FUNCTION, encoding='utf-8', newline='\n')
    plan_path = Path(tmp) / 'plan.txt'
    plan_path.write_text(''.join(f'{line}\n' for line in plan),
                         encoding='utf-8', newline='\n')
    script = Path(tmp) / 'audit-step.sh'
    script.write_text(_named_run(_audit_steps(), 'Audit dependencies'),
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


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prworkflows_'))
