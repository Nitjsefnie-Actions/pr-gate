"""The secret scan: secret-scan/action.yml, its config module, and the workflow that runs it.

The scan's config and ignore list come from the commit that is not being
judged, so the action's steps that act on them are executed here against a
real repository, not only read; scripts/ci/secret_scan_config.py is driven
in-process.
"""
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from test_action import _action_bash
from test_workflows import _named_run, _shipped_action_pin, _workflow

ROOT = Path(__file__).resolve().parents[1]
CONFIG_STEP = "Take the scan's config and ignore list from the trusted commit"
ACTION = yaml.load((ROOT / 'secret-scan/action.yml').read_text(encoding='utf-8'),
                   Loader=yaml.BaseLoader)
# Shaped like gitleaks' default config: a global path list, a path-only rule allowlist,
# and a BitBake-style one that needs a path AND a line, which stays.
DEFAULT = r"""title = "default"
[allowlist]
description = "global"
paths = [
    '''go\.sum$''',
]
regexes = ['''^true$''']
stopwords = ["abcdefgh"]

[[rules]]
id = "github-pat"
[[rules.allowlists]]
paths = [
    '''README\.md$''',
]

[[rules]]
id = "generic-api-key"
[[rules.allowlists]]
condition = "AND"
paths = ['''\.bb$''']
regexes = ['''LICENSE''']
"""
STRIPPED = {'title': 'default', 'rules': [{'id': 'github-pat'}, {
    'id': 'generic-api-key', 'allowlists': [
        {'condition': 'AND', 'paths': [r'\.bb$'], 'regexes': ['LICENSE']}]}],
    'allowlist': {'description': 'global', 'regexes': ['^true$'], 'stopwords': ['abcdefgh']}}


SPEC = importlib.util.spec_from_file_location(
    'secret_scan_config', ROOT / 'scripts/ci/secret_scan_config.py')
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _git(repo, *arguments):
    return subprocess.run(['git', '-C', str(repo), *arguments], check=True,
                          capture_output=True, text=True).stdout


def _step_script(tmp, name):
    script = Path(tmp) / f'{name}.sh'
    script.write_text(_named_run(ACTION['runs']['steps'], name), encoding='utf-8', newline='\n')
    return script


def test_secrets_scans_full_history_from_a_trusted_config(tmp):
    del tmp
    workflow = _workflow('secrets.yml')
    events = workflow['on']
    assert set(events) == {'push', 'pull_request', 'schedule', 'workflow_dispatch'}
    assert events['push'] == {'branches': ['main']}, (
        'a scanner must see docs commits, so no paths filter may narrow the push')
    assert not (events['pull_request'] or events['workflow_dispatch'] or {}), (
        'the manual and pull request triggers stay unfiltered')
    assert events['schedule'] == [{'cron': '26 5 * * *'}], (
        "exactly one daily cron, off the hour, clear of codeql Wed '47 3 * * 3' "
        "and scorecard Sat '23 2 * * 6'")
    assert workflow['concurrency'] == {
        'group': 'secrets-${{ github.ref }}', 'cancel-in-progress': 'true'}
    steps = workflow['jobs']['gitleaks']['steps']
    assert steps == [{**steps[0], 'uses': (
        f"actions/checkout@{_shipped_action_pin('actions/checkout')}")}, {'uses': '$/secret-scan'}]
    assert ACTION['runs']['using'] == 'composite'
    steps = ACTION['runs']['steps']
    assert [(step['name'], step['shell']) for step in steps] == [
        ('Download gitleaks and verify its digest', 'bash'), (CONFIG_STEP, 'bash'),
        ('Scan the tree and the history', 'bash')]
    # One release, binary and default rules each frozen by its digest, fetched
    # through one retrying curl; which release is the action's call.
    download = re.sub(r'\s*\\\n\s*', ' ', steps[0]['run'])
    assert re.search(r'fetch\(\) \{\s+curl .*--retry 8 --retry-all-errors .*--output "\$1" "\$2"'
                     r'\s+\}', download), download
    release, = set(re.findall(r'/v(\d+\.\d+\.\d+)/', download))
    tarball = f'gitleaks_{release}_linux_x64.tar.gz'
    lines = [line.strip() for line in download.splitlines()][-6:]
    assert [re.sub('[0-9a-f]{64}', 'D', line) for line in lines] == [
        f'fetch {tarball} https://github.com/gitleaks/gitleaks/releases/download/v{release}/'
        f'{tarball}', f"echo 'D  {tarball}' | sha256sum -c -", f'tar xzf {tarball} gitleaks',
        './gitleaks version', 'fetch gitleaks-default.toml https://raw.githubusercontent.com/'
        f'gitleaks/gitleaks/v{release}/config/gitleaks.toml',
        "echo 'D  gitleaks-default.toml' | sha256sum -c -"], lines
    assert set(steps[1]['env']) == {'PR_GATE_ACTION_PATH', 'PR_GATE_EVENT_NAME', 'PR_GATE_BASE_SHA',
                                    'PR_GATE_BEFORE_SHA', 'PR_GATE_DEFAULT_BRANCH'}
    scan = steps[2]
    assert 'if' not in scan and 'continue-on-error' not in scan
    assert '||' not in scan['run'], 'no error-swallowing fallback may weaken the gate'
    config = tomllib.loads((ROOT / '.gitleaks.toml').read_text(encoding='utf-8'))
    assert config == {'extend': {'useDefault': True}}, (
        'the extend line IS the gate; a narrowing allowlist replacement must '
        'fail this suite')


def test_the_scan_step_clears_the_judged_exemptions(tmp):
    repo = Path(tmp) / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True,
                   capture_output=True)
    attributes = repo / '.git/info/attributes'
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text('some/file.txt -diff\n', encoding='utf-8')
    (repo / '.gitleaksignore').write_text('judged exemption\n', encoding='utf-8')
    runner = Path(tmp) / 'runner-temp'
    copy_dir = runner / 'gitleaks-trusted'
    copy_dir.mkdir(parents=True)
    record = Path(tmp) / 'record'
    record.mkdir()
    # The stub takes the binary's place where the download step left it and
    # records what the step did around it.
    stub = runner / 'gitleaks'
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$@" > "$PR_GATE_STUB_ARGV"\n'
        'cat .git/info/attributes > "$PR_GATE_STUB_ATTRIBUTES"\n'
        'if [ -e .gitleaksignore ]; then echo present > "$PR_GATE_STUB_IGNORE"; '
        'else echo absent > "$PR_GATE_STUB_IGNORE"; fi\nexit 3\n',
        encoding='utf-8', newline='\n')
    stub.chmod(0o755)
    result = subprocess.run(
        [_action_bash(), '-e', str(_step_script(tmp, 'Scan the tree and the history'))],
        cwd=repo, capture_output=True, text=True, timeout=120,
        env={**os.environ, 'RUNNER_TEMP': str(runner),
             'PR_GATE_STUB_ARGV': str(record / 'argv'),
             'PR_GATE_STUB_ATTRIBUTES': str(record / 'attributes'),
             'PR_GATE_STUB_IGNORE': str(record / 'ignore')})
    assert result.returncode == 3, ('the scan status is the step status', result.stderr)
    assert (record / 'attributes').read_text(encoding='utf-8') == '* diff\n', (
        'a plain diff attribute keeps a NUL-bearing blob in the scan; -diff '
        'and an unspecified attribute both hide it from git log -p')
    assert (record / 'ignore').read_text(encoding='utf-8').strip() == 'absent', (
        'the judged tree no longer exempts itself through .gitleaksignore')
    argv = (record / 'argv').read_text(encoding='utf-8').splitlines()
    assert argv[:4] == ['detect', '--verbose', '--redact', '--ignore-gitleaks-allow'], argv
    assert argv[4] == '--config' and Path(argv[5]) == copy_dir / '.gitleaks.toml', argv
    assert argv[6] == '--gitleaks-ignore-path' and Path(argv[7]) == copy_dir, argv
    log_opts = '--full-history --all --diff-filter=uxdb --diff-merges=first-parent'
    assert argv[8:] == [f'--log-opts={log_opts}'], 'the walk keeps type changes and merges'


def test_the_trusted_config_step_reads_the_trusted_event_commit(tmp):
    repo = Path(tmp) / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.name', 'fixture')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    tip_config = '[extend]\nuseDefault = true\n[allowlist]\ndescription = "tip"\n'

    def commit(config, message, ignore=None):
        (repo / '.gitleaks.toml').write_text(config, encoding='utf-8', newline='\n')
        if ignore is None:
            (repo / '.gitleaksignore').unlink(missing_ok=True)
        else:
            (repo / '.gitleaksignore').write_text(ignore, encoding='utf-8', newline='\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', message)
        return _git(repo, 'rev-parse', 'HEAD').strip()

    tip = commit(tip_config, 'tip')
    commit('[allowlist]\ndescription = "the head exempts itself"\n', 'head',
           ignore='the head exempts itself\n')
    script = _step_script(tmp, CONFIG_STEP)
    # The secrets job runs on ubuntu, where python3 exists; this suite also
    # runs on Windows, so the step's python3 is the interpreter running it.
    bash_env = Path(tmp) / 'python3.bash'
    bash_env.write_text(f'python3() {{ {shlex.quote(sys.executable)} "$@"; }}\n',
                        encoding='utf-8', newline='\n')
    runs = iter(range(100))

    def run(before='', base='', event='push', default_branch=''):
        runner = Path(tmp) / f'runner-temp-{next(runs)}'
        runner.mkdir()
        (runner / 'gitleaks-default.toml').write_text(DEFAULT, encoding='utf-8')
        result = subprocess.run(
            [_action_bash(), '-e', str(script)], cwd=repo, capture_output=True,
            text=True, timeout=120, env={
                **os.environ, 'RUNNER_TEMP': str(runner), 'BASH_ENV': str(bash_env),
                'PR_GATE_ACTION_PATH': str(ROOT / 'secret-scan'), 'PR_GATE_EVENT_NAME': event,
                'PR_GATE_BASE_SHA': base, 'PR_GATE_BEFORE_SHA': before,
                'PR_GATE_DEFAULT_BRANCH': default_branch})
        copy_dir = runner / 'gitleaks-trusted'
        stripped = runner / 'gitleaks-default-stripped.toml'
        if result.returncode == 0:
            assert tomllib.loads(stripped.read_text(encoding='utf-8')) == STRIPPED
        return (result, (copy_dir / '.gitleaks.toml').read_text(encoding='utf-8'),
                (copy_dir / '.gitleaksignore').read_text(encoding='utf-8'), str(stripped))

    result, config, ignored, stripped = run(before=tip)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert tomllib.loads(config) == {'extend': {'path': stripped},
                                     'allowlist': {'description': 'tip'}}, (
        'a push must read the previous tip, its useDefault now the stripped rules')
    assert 'exempts itself' not in ignored, (
        'the judged head\'s .gitleaksignore must never reach the trusted copy')
    result, config, _, _ = run(base=tip, event='pull_request')
    assert result.returncode == 0 and 'description = "tip"' in config, (
        'a pull request must read its base commit')
    _git(repo, 'update-ref', 'refs/remotes/origin/main', tip)
    result, config, _, _ = run(event='schedule')
    assert result.returncode == 0 and 'description = "tip"' in config, (
        'a scheduled scan must read origin/main, not the judged tree')
    _git(repo, 'update-ref', 'refs/remotes/origin/trunk', 'HEAD')
    result, config, _, _ = run(event='workflow_dispatch', default_branch='trunk')
    assert result.returncode == 0 and 'the head exempts itself' in config, (
        'a dispatched scan reads the default branch the event names')
    for named in ({'before': 'not-a-sha'}, {'event': 'pull_request'}):
        result, config, _, stripped = run(**named)
        assert result.returncode == 0 and tomllib.loads(config) == {'extend': {'path': stripped}}, (
            f'{named}: a name that is no commit falls back to the default rules')
    for config_text in ('[extend]\npath = "elsewhere.toml"\n',
                        '[extend]\nPath = "elsewhere.toml"\n',
                        '[extend]\nuseDefault = true\nPath = "elsewhere.toml"\n'):
        result, config, _, _ = run(before=commit(config_text, 'tip redirects'))
        assert result.returncode != 0 and 'sets [extend] path' in result.stderr, (
            f'trusted config {config_text!r} must refuse when it redirects')
        assert config == config_text, 'the refusal reads the trusted file before any rewrite'


def test_stripping_drops_only_the_allowlists_that_name_a_path_alone(tmp):
    del tmp
    strip = MODULE.strip_path_allowlists
    assert tomllib.loads(strip(DEFAULT)) == STRIPPED
    for broken in (DEFAULT.replace('[allowlist]', '[other]'),
                   DEFAULT + 'x = """\n[allowlist]\n"""\n',
                   DEFAULT.replace("paths = [\n    '''go", "pathz = [\n    '''go"),
                   DEFAULT + "[[rules]]\nid = 'x'\nallowlists = [{paths = ['y']}]\n"):
        with pytest.raises(MODULE.ConfigError):
            strip(broken)


@pytest.mark.parametrize('text, expected', [
    ('[extend]\nuseDefault = true\n', {'extend': {'path': 'C:\\run "x"\\s.toml'}}),
    ('[Extend]\nUseDefault = true # kept\n[allowlist]\nregexes = ["x"]\n',
     {'Extend': {'path': 'C:\\run "x"\\s.toml'}, 'allowlist': {'regexes': ['x']}}),
    ('[extend]\nuseDefault = false\n', None), ('[allowlist]\nregexes = ["x"]\n', None)])
def test_use_default_alone_is_pointed_at_the_stripped_rules(text, expected):
    rewritten = MODULE.point_at_stripped(text, 'C:\\run "x"\\s.toml')
    assert (tomllib.loads(rewritten) if expected else rewritten) == (expected or text)


@pytest.mark.parametrize('text', [
    '[extend]\nuseDefault = true\nUseDefault = true\n', '[extend]\nuseDefault = true\n[Extend]\n',
    '[extend]\nuseDefault = "true"\n', '[extend]\nuseDefault = true\nPath = ""\n',
    'extend = {useDefault = true}\n', 'extend.useDefault = true\n',
    '[extend]\n"useDefault" = true\n[other]\nuseDefault = true\n',
    'a = """\nuseDefault = true\n"""\n[extend]\nuseDefault = true\n',
    'a = """\nuseDefault = true\n"""\n[extend]\n"useDefault" = true\n'])
def test_a_use_default_rewrite_it_cannot_prove_refuses(text):
    with pytest.raises((MODULE.ConfigError, tomllib.TOMLDecodeError)):
        MODULE.point_at_stripped(text, '/stripped.toml')


def test_the_module_writes_both_configs_or_exits_naming_the_refusal(tmp):
    default, stripped, trusted = (Path(tmp) / name for name in ('d.toml', 's.toml', 't.toml'))
    default.write_text(DEFAULT, encoding='utf-8')
    trusted.write_text('[extend]\nuseDefault = true\n', encoding='utf-8')
    MODULE.main([str(default), str(stripped), str(trusted)])
    assert tomllib.loads(stripped.read_text(encoding='utf-8')) == STRIPPED
    assert tomllib.loads(trusted.read_text(encoding='utf-8')) == {
        'extend': {'path': os.path.abspath(stripped)}}
    default.write_text('[other]\n', encoding='utf-8')  # refused too, but after the trusted file
    trusted.write_text('[extend]\nuseDefault = true\nPath = "x"\n', encoding='utf-8')
    with pytest.raises(SystemExit, match=r'sets \[extend\] path'):
        MODULE.main([str(default), str(stripped), str(trusted)])
    assert trusted.read_text(encoding='utf-8') == '[extend]\nuseDefault = true\nPath = "x"\n'
