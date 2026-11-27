"""What the freshness check derives past a required job's own steps, and how it reads them.

scripts/ci/gate_base_freshness.py's set follows `needs`, local reusable workflows,
display-name contexts and the scripts a step runs (scripts/ci/freshness_reach.py),
through the workflow reader in scripts/ci/freshness_workflows.py. Every fixture is a
throwaway git repository under the test's own tmp, never this repository.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JOB = '  {job}:\n    runs-on: ubuntu-latest\n{body}'
# A workflow whose `aggregate` job folds `unit`, which folds `build`; `docs` is folded by nothing.
AGGREGATE = ('name: ci\non: push\njobs:\n'
             + JOB.format(job='aggregate', body='    needs: [unit]\n    steps:\n'
                          '      - run: echo folded\n')
             + JOB.format(job='unit', body='    needs: build\n    steps:\n'
                          '      - run: python unit_gate.py\n')
             + JOB.format(job='build', body='    steps:\n      - run: cat build.cfg\n')
             + JOB.format(job='docs', body='    steps:\n      - run: cat docs.cfg\n'))


def _load():
    path = ROOT / 'scripts/ci/gate_base_freshness.py'
    spec = importlib.util.spec_from_file_location('gate_base_freshness', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _derive(monkeypatch, tmp, files, *contexts):
    """The derived set over a one-commit repository holding `files`, for `contexts`."""
    repo = Path(tempfile.mkdtemp(dir=tmp))
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text, encoding='utf-8', newline='\n')
    for arguments in (('init', '-q', '-b', 'main'), ('config', 'user.name', 'fixture'),
                      ('config', 'user.email', 'fixture@example.com'),
                      ('config', 'commit.gpgsign', 'false'), ('add', '-A'),
                      ('commit', '-q', '-m', 'base: seed')):
        subprocess.run(['git', '-C', str(repo), *arguments], check=True, capture_output=True)
    monkeypatch.setenv('PR_GATE_REQUIRED_CHECKS', '\n'.join(contexts))
    return _load().gate_paths(repo)


def _refusal(monkeypatch, tmp, files, *contexts):
    module = _load()
    with pytest.raises(module.GateError) as caught:
        _derive(monkeypatch, tmp, files, *contexts)
    return str(caught.value)


def _jobs(text):
    return _load().workflow_jobs(text, 'w.yml')


def _workflow(steps, keys=''):
    return f'name: w\njobs:\n  test:\n{keys}    steps:\n{steps}'


# --- the jobs a required check runs ---------------------------------------------


def test_needs_are_followed_transitively_and_nothing_else(monkeypatch, tmp):
    files = {'.github/workflows/ci.yml': AGGREGATE, 'unit_gate.py': 'print(1)\n',
             'build.cfg': 'b\n', 'docs.cfg': 'd\n'}
    assert _derive(monkeypatch, tmp, files, 'aggregate') == [
        '.github/workflows/ci.yml', 'build.cfg', 'unit_gate.py']


@pytest.mark.parametrize('spelling', ['./', '$/'])
def test_a_local_reusable_workflow_is_descended_into(monkeypatch, tmp, spelling):
    caller = ('name: gate\non: push\njobs:\n  aggregate:\n    needs: lint\n'
              '    steps:\n      - run: echo folded\n'
              f'  lint:\n    uses: {spelling}.github/workflows/lint.yml  # zizmor: ignore\n'
              '    with:\n      rules: input.cfg\n')
    callee = ('name: lint\non: workflow_call\njobs:\n  pylint:\n    steps:\n'
              '      - run: pylint lint_rules.py\n')
    files = {'.github/workflows/gate.yml': caller, '.github/workflows/lint.yml': callee,
             'lint_rules.py': 'x = 1\n', 'input.cfg': 'i\n', 'other.cfg': 'o\n'}
    assert _derive(monkeypatch, tmp, files, 'aggregate') == [
        '.github/workflows/gate.yml', '.github/workflows/lint.yml', 'input.cfg',
        'lint_rules.py']


def test_a_call_or_need_naming_nothing_refuses(monkeypatch, tmp):
    calls = ('name: gate\njobs:\n  aggregate:\n    uses: ./.github/workflows/gone.yml\n')
    assert 'not a tracked workflow' in _refusal(
        monkeypatch, tmp, {'.github/workflows/gate.yml': calls}, 'aggregate')
    needs = ('name: gate\njobs:\n  aggregate:\n    needs: gone\n    steps:\n'
             '      - run: cat x.cfg\n')
    assert 'which it does not define' in _refusal(
        monkeypatch, tmp, {'.github/workflows/gate.yml': needs, 'x.cfg': 'x\n'}, 'aggregate')


def test_a_context_resolves_through_a_display_name(monkeypatch, tmp):
    named = ('name: ci\njobs:\n  aggregate:\n    name: Aggregate workflow checks\n'
             '    steps:\n      - run: cat agg.cfg\n'
             '  codeql:\n    name: scan (${{ matrix.language }})\n'
             '    steps:\n      - run: cat scan.cfg\n')
    files = {'.github/workflows/ci.yml': named, 'agg.cfg': 'a\n', 'scan.cfg': 's\n'}
    assert _derive(monkeypatch, tmp, files, 'Aggregate workflow checks') == [
        '.github/workflows/ci.yml', 'agg.cfg']
    assert _derive(monkeypatch, tmp, files, 'scan (python)') == [
        '.github/workflows/ci.yml', 'scan.cfg']
    assert 'defines the required job `scan`' in _refusal(monkeypatch, tmp, files, 'scan')


def test_every_level_of_env_and_with_is_step_text(monkeypatch, tmp):
    workflow = ('name: ci\nenv:\n  TOP: top.cfg\njobs:\n  test:\n'
                '    env: {JOB: job.cfg}\n    steps:\n'
                '      - uses: o/a@1\n        env:\n          STEP: step.cfg\n')
    files = {'.github/workflows/ci.yml': workflow, 'top.cfg': 't\n', 'job.cfg': 'j\n',
             'step.cfg': 's\n'}
    assert _derive(monkeypatch, tmp, files, 'test') == [
        '.github/workflows/ci.yml', 'job.cfg', 'step.cfg', 'top.cfg']


def test_a_self_reference_step_takes_its_action_directory(monkeypatch, tmp):
    workflow = _workflow('      - uses: $/.github/actions/setup\n')
    files = {'.github/workflows/ci.yml': workflow, '.github/actions/setup/action.yml': 'x\n',
             'elsewhere.cfg': 'e\n'}
    assert _derive(monkeypatch, tmp, files, 'test') == [
        '.github/actions/setup/action.yml', '.github/workflows/ci.yml']


# --- the files a reached script names --------------------------------------------


def test_python_imports_and_literals_are_followed(monkeypatch, tmp):
    gate = ('import os\nfrom importlib import import_module\nimport helper\n'
            'from pkg import mod\nimport_module("dyn")\nROOT = None\n'
            'open("data/x.json")\nPATH = ROOT / "conf" / "y.toml"\n'
            'os.path.join(ROOT, "conf", "z.ini")\nDIRECTORY = "data"\n')
    files = {'.github/workflows/ci.yml': _workflow('      - run: python gate.py\n'),
             'gate.py': gate, 'lib/helper.py': 'OWN = "helper.json"\n',
             'lib/helper.json': '{}\n', 'pkg/__init__.py': '', 'pkg/mod.py': 'from . import sib\n',
             'pkg/sib.py': '', 'dyn.py': '', 'data/x.json': '{}\n', 'data/other.json': '{}\n',
             'conf/y.toml': '', 'conf/z.ini': '', 'unrelated.py': ''}
    assert _derive(monkeypatch, tmp, files, 'test') == [
        '.github/workflows/ci.yml', 'conf/y.toml', 'conf/z.ini', 'data/x.json', 'dyn.py',
        'gate.py', 'lib/helper.json', 'lib/helper.py', 'pkg/__init__.py', 'pkg/mod.py',
        'pkg/sib.py']


def test_a_shell_script_names_files_from_its_own_directory(monkeypatch, tmp):
    script = '#!/bin/bash\n"$(dirname "$0")/retry.sh" cat conf/sh.cfg\n'
    files = {'.github/workflows/ci.yml': _workflow('      - run: bash ci/check.sh\n'),
             'ci/check.sh': script, 'ci/retry.sh': 'exec "$@"\n', 'conf/sh.cfg': 'c\n'}
    assert _derive(monkeypatch, tmp, files, 'test') == [
        '.github/workflows/ci.yml', 'ci/check.sh', 'ci/retry.sh', 'conf/sh.cfg']


def test_an_unparseable_gate_script_refuses(monkeypatch, tmp):
    files = {'.github/workflows/ci.yml': _workflow('      - run: python gate.py\n'),
             'gate.py': 'def (:\n'}
    assert 'cannot read the imports of gate.py' in _refusal(monkeypatch, tmp, files, 'test')


# --- the reader's widened shapes ------------------------------------------------


def test_a_block_scalar_header_may_carry_a_comment():
    job = _jobs(_workflow('      - run: | # zizmor: ignore[github-env]\n'
                          '          tarball="gitleaks_${V}_linux_x64.tar.gz"\n'
                          '          cat scan.cfg\n'))['test']
    assert job.steps[0]['run'] == 'tarball="gitleaks_${V}_linux_x64.tar.gz"\ncat scan.cfg'


def test_a_plain_scalar_continues_onto_deeper_lines():
    job = _jobs(_workflow('      - if: ${{ !cancelled()\n'
                          '                && github.ref == \'refs/heads/main\' }}\n'
                          '        run: cat after.cfg\n'
                          '      - run: cat one.cfg\n            two.cfg\n'))['test']
    assert [step['run'] for step in job.steps] == ['cat after.cfg', 'cat one.cfg two.cfg']


def test_needs_and_names_in_every_modelled_spelling():
    jobs = _jobs('name: w\njobs:\n'
                 '  flow:\n    needs: [a, "b",\n      c]  # wrapped\n    name: "say \\"hi\\""\n'
                 '  block:\n    needs:\n      - a\n      - \'b\'  # quoted\n    name: \'it\'\'s\'\n'
                 '  plain:\n    needs: a  # one\n    name: plain words\n')
    assert [(job.needs, job.name) for job in jobs.values()] == [
        (('a', 'b', 'c'), 'say "hi"'), (('a', 'b'), "it's"), (('a',), 'plain words')]


@pytest.mark.parametrize('keys', [
    '    needs: ${{ fromJSON(x) }}\n', '    needs: [a, [b]]\n', '    needs: []\n',
    '    needs:\n      a: b\n', '    needs: a\n      b: c\n', '    name: [x]\n',
    '    name: "a\\x"\n', '    uses: &anchor ./w.yml\n', '    name:\n'])
def test_a_needs_or_scalar_shape_it_cannot_read_refuses(keys):
    module = _load()
    with pytest.raises(module.WorkflowError):
        _jobs(_workflow('      - run: echo\n', keys))


@pytest.mark.parametrize('steps', [
    '      - run: &a cat x.cfg\n', '      - uses: o/a@1\n        with:\n          p: *a\n',
    '      - env:\n          <<: *base\n'])
def test_an_anchor_alias_or_merge_in_a_step_refuses(steps):
    module = _load()
    with pytest.raises(module.WorkflowError, match='anchor, alias or tag'):
        _jobs(_workflow(steps))


# The exact derived set on this repository's HEAD: a required job naming a new file forces this
# pin to change in the same commit, so the set can neither shrink nor grow in silence. The test
# job's `tests` input names `tests/`, which resolves to every tracked file under it; the files
# the suite's own literals name, such as `LICENSE`, are files a required check reads.
# Run under an ASCII locale, as on a Windows runner: the scripts it reads are UTF-8 all the same.
def test_gate_paths_on_this_repository():
    script = str(ROOT / 'scripts/ci/gate_base_freshness.py')
    done = subprocess.run([sys.executable, '-X', 'utf8=0', script, '--print-paths'], cwd=ROOT,
                          capture_output=True, text=True, check=False,
                          env={**os.environ, 'LC_ALL': 'POSIX', 'PYTHONCOERCECLOCALE': '0'})
    assert done.returncode == 0, done.stderr
    tests = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'HEAD', 'tests/'], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.splitlines()
    assert done.stdout.splitlines() == sorted(PINNED + tests), done.stdout


PINNED = [
    '.github/PULL_REQUEST_TEMPLATE.md', '.github/ci-thresholds.json', '.github/dependabot.yml',
    '.github/test-times.json', '.github/workflows/actionlint.yml', '.github/workflows/audit.yml',
    '.github/workflows/claim.yml', '.github/workflows/codeql.yml',
    '.github/workflows/comment-share.yml', '.github/workflows/lint.yml',
    '.github/workflows/pin-guard.yml', '.github/workflows/pr-gate.yml',
    '.github/workflows/ratchet-push.yml', '.github/workflows/release-dispatch.yml',
    '.github/workflows/scorecard.yml', '.github/workflows/secrets.yml',
    '.github/workflows/size-baseline.yml', '.github/workflows/test-ratchet-comment.yml',
    '.github/workflows/tests.yml', '.github/workflows/types.yml', '.gitignore', '.gitleaks.toml',
    '.pylintrc', 'LICENSE', 'README.md', 'SECURITY.md', 'action.yml', 'pyright-baseline.json',
    'pyrightconfig.json', 'requirements-actionlint.txt', 'requirements-lint.txt',
    'requirements-pip-audit.txt', 'requirements-test.txt', 'requirements-zizmor.txt',
    'scripts/ci/commit_scopes.py', 'scripts/ci/freshness_reach.py',
    'scripts/ci/freshness_workflows.py', 'scripts/ci/gate_base_freshness.py',
    'scripts/ci/pr_attribution.py', 'scripts/ci/pr_body.py', 'scripts/ci/pr_body_closing.py',
    'scripts/ci/pr_commits.py', 'scripts/ci/pr_content.py', 'scripts/ci/pr_gate.py',
    'scripts/ci/pr_policy.py', 'scripts/ci/release_dispatch.py', 'scripts/ci/type_ratchet.py',
    'setup.cfg',
]
