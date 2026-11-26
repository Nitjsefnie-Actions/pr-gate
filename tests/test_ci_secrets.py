"""The secret scan's workflow: a frozen binary and a trusted config source.

The scan's config and ignore list come from the commit that is not being
judged, so both steps that act on them are executed here against a real
repository, not only read.
"""
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from test_action import _action_bash
from test_workflows import _named_run, _shipped_action_pin, _workflow

ROOT = Path(__file__).resolve().parents[1]
CONFIG_STEP = "Take the scan's config and ignore list from the trusted commit"


def _git(repo, *arguments):
    return subprocess.run(['git', '-C', str(repo), *arguments], check=True,
                          capture_output=True, text=True).stdout


def _step_script(tmp, name):
    steps = _workflow('secrets.yml')['jobs']['gitleaks']['steps']
    script = Path(tmp) / f'{name}.sh'
    script.write_text(_named_run(steps, name), encoding='utf-8', newline='\n')
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
    assert [step.get('name') for step in steps] == [
        None, 'Download gitleaks and verify its digest', CONFIG_STEP,
        'Scan the tree and the history'], [step.get('name') for step in steps]
    assert steps[0]['uses'] == (
        f"actions/checkout@{_shipped_action_pin('actions/checkout')}")
    # One release, frozen by its digest, retried because a reset connection
    # exits nonzero too; which release is the workflow's call.
    lines = steps[1]['run'].splitlines()
    download = ' '.join(lines)
    release = re.search(
        r'--retry 8 --retry-all-errors.*releases/download/v(\d+\.\d+\.\d+)/'
        r'gitleaks_\1_linux_x64\.tar\.gz', download)
    assert release, lines
    tarball = f'gitleaks_{release[1]}_linux_x64.tar.gz'
    assert re.fullmatch(
        r"echo '[0-9a-f]{64}  " + re.escape(tarball) + r"' \| sha256sum -c -",
        lines[-3]), 'the download must be verified against a sha256 digest'
    assert lines[-2:] == [f'tar xzf {tarball} gitleaks', './gitleaks version']
    assert set(steps[2]['env']) == {'EVENT_NAME', 'BASE_SHA', 'BEFORE_SHA'}
    scan = steps[3]
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
    # The step runs ./gitleaks, so the stub takes the binary's place in the
    # repository root and records what the step did around it.
    stub = repo / 'gitleaks'
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$@" > "$STUB_ARGV"\n'
        'cat .git/info/attributes > "$STUB_ATTRIBUTES"\n'
        'if [ -e .gitleaksignore ]; then echo present > "$STUB_IGNORE"; '
        'else echo absent > "$STUB_IGNORE"; fi\n',
        encoding='utf-8', newline='\n')
    stub.chmod(0o755)
    result = subprocess.run(
        [_action_bash(), '-e', str(_step_script(tmp, 'Scan the tree and the history'))],
        cwd=repo, capture_output=True, text=True, timeout=120,
        env={**os.environ, 'RUNNER_TEMP': str(runner),
             'STUB_ARGV': str(record / 'argv'),
             'STUB_ATTRIBUTES': str(record / 'attributes'),
             'STUB_IGNORE': str(record / 'ignore')})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (record / 'attributes').read_text(encoding='utf-8') == '* diff\n', (
        'a plain diff attribute keeps a NUL-bearing blob in the scan; -diff '
        'and an unspecified attribute both hide it from git log -p')
    assert (record / 'ignore').read_text(encoding='utf-8').strip() == 'absent', (
        'the judged tree no longer exempts itself through .gitleaksignore')
    argv = (record / 'argv').read_text(encoding='utf-8').splitlines()
    assert argv[:4] == ['detect', '--verbose', '--redact', '--ignore-gitleaks-allow'], argv
    assert argv[4] == '--config' and Path(argv[5]) == copy_dir / '.gitleaks.toml', argv
    assert argv[6] == '--gitleaks-ignore-path' and Path(argv[7]) == copy_dir, argv


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

    def run(before='', base='', event='push'):
        runner = Path(tmp) / f'runner-temp-{next(runs)}'
        runner.mkdir()
        result = subprocess.run(
            [_action_bash(), '-e', str(script)], cwd=repo, capture_output=True,
            text=True, timeout=120, env={**os.environ, 'RUNNER_TEMP': str(runner),
                                         'BASH_ENV': str(bash_env), 'EVENT_NAME': event,
                                         'BASE_SHA': base, 'BEFORE_SHA': before})
        copy_dir = runner / 'gitleaks-trusted'
        return (result, (copy_dir / '.gitleaks.toml').read_text(encoding='utf-8'),
                (copy_dir / '.gitleaksignore').read_text(encoding='utf-8'))

    result, config, ignored = run(before=tip)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'description = "tip"' in config, 'a push must read the previous tip'
    assert 'exempts itself' not in ignored, (
        'the judged head\'s .gitleaksignore must never reach the trusted copy')
    result, config, _ = run(base=tip, event='pull_request')
    assert result.returncode == 0 and 'description = "tip"' in config, (
        'a pull request must read its base commit')
    _git(repo, 'update-ref', 'refs/remotes/origin/main', tip)
    result, config, _ = run(event='schedule')
    assert result.returncode == 0 and 'description = "tip"' in config, (
        'a scheduled scan must read origin/main, not the judged tree')
    result, config, _ = run(before='not-a-sha')
    assert result.returncode == 0 and config == '[extend]\nuseDefault = true\n', (
        'a name that is no commit falls back to the default rules')
    for config_text in ('[extend]\npath = "elsewhere.toml"\n',
                        '[extend]\nPath = "elsewhere.toml"\n'):
        result, _, _ = run(before=commit(config_text, 'tip redirects'))
        assert result.returncode != 0 and 'refusing to scan' in result.stderr, (
            f'trusted config {config_text!r} must refuse when it redirects')
