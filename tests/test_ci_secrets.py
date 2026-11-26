"""The secret scan's workflow: a frozen binary and a trusted config source.

The scan's config and ignore list come from the commit that is not being
judged, so the step that reads them is executed here against a real
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
        None, 'Download gitleaks and verify its digest',
        "Take the scan's config and ignore list from the trusted commit",
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
    # The scan's config and ignore list come from the trusted commit: read
    # from the judged tree, a change could exempt its own finding. The names
    # it reads are the data flow, so they are pinned as the contract.
    assert set(steps[2]['env']) == {'EVENT_NAME', 'BASE_SHA', 'BEFORE_SHA'}
    trusted = steps[2]['run']
    assert 'pull_request) trusted="$BASE_SHA"' in trusted
    assert 'push) trusted="$BEFORE_SHA"' in trusted
    assert "case \"$trusted\" in '' | *[!0-9a-f]*)" in trusted, (
        'a non-hex name must be refused before git reads a blob')
    scan = steps[3]
    assert scan['run'].splitlines()[-3:] == [
        './gitleaks detect --verbose --redact --ignore-gitleaks-allow \\',
        '  --config "$RUNNER_TEMP/gitleaks-trusted/.gitleaks.toml" \\',
        '  --gitleaks-ignore-path "$RUNNER_TEMP/gitleaks-trusted"'], (
        'the scan takes --ignore-gitleaks-allow and reads the trusted copy')
    assert 'if' not in scan and 'continue-on-error' not in scan
    assert '||' not in scan['run'], 'no error-swallowing fallback may weaken the gate'
    config = tomllib.loads((ROOT / '.gitleaks.toml').read_text(encoding='utf-8'))
    assert config == {'extend': {'useDefault': True}}, (
        'the extend line IS the gate; a narrowing allowlist replacement must '
        'fail this suite')


# The trusted-config step decides where the scan's exemptions come from, so
# it is executed rather than read: the run of python -I matches CI's.
def test_the_trusted_config_step_reads_the_previous_tip(tmp):
    steps = _workflow('secrets.yml')['jobs']['gitleaks']['steps']
    run_text = _named_run(steps, "Take the scan's config and ignore list from the trusted commit")
    repo = Path(tmp) / 'repo'
    repo.mkdir()

    def git(*arguments):
        return subprocess.run(['git', '-C', str(repo), *arguments], check=True,
                              capture_output=True, text=True).stdout

    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'fixture')
    git('config', 'user.email', 'fixture@example.com')

    def commit(config, message):
        (repo / '.gitleaks.toml').write_text(config, encoding='utf-8', newline='\n')
        git('add', '.gitleaks.toml')
        git('commit', '-qm', message)
        return git('rev-parse', 'HEAD').strip()

    tip = commit('[extend]\nuseDefault = true\n[allowlist]\ndescription = "tip"\n', 'tip')
    commit('[allowlist]\ndescription = "the head exempts itself"\n', 'head')
    script = Path(tmp) / 'trusted.sh'
    script.write_text(run_text, encoding='utf-8', newline='\n')
    # The secrets job runs on ubuntu, where python3 exists; this suite also
    # runs on Windows, so the step's python3 is the interpreter running it.
    bash_env = Path(tmp) / 'python3.bash'
    bash_env.write_text(f'python3() {{ {shlex.quote(sys.executable)} "$@"; }}\n',
                        encoding='utf-8', newline='\n')
    runs = iter(range(100))

    def run(before, event='push'):
        runner = Path(tmp) / f'runner-temp-{next(runs)}'
        runner.mkdir()
        result = subprocess.run(
            [_action_bash(), '-e', str(script)], cwd=repo, capture_output=True,
            text=True, timeout=120, env={**os.environ, 'RUNNER_TEMP': str(runner),
                                         'BASH_ENV': str(bash_env), 'EVENT_NAME': event,
                                         'BASE_SHA': '', 'BEFORE_SHA': before})
        return result, (runner / 'gitleaks-trusted/.gitleaks.toml').read_text(encoding='utf-8')

    result, config = run(tip)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'description = "tip"' in config, 'the scan must read the previous tip, never the head'
    result, config = run('not-a-sha')
    assert result.returncode == 0 and config == '[extend]\nuseDefault = true\n', (
        'a name that is no commit falls back to the default rules, not the judged tree')
    result, config = run(commit('[extend]\npath = "elsewhere.toml"\n', 'tip redirects'))
    assert result.returncode != 0 and 'refusing to scan' in result.stderr, (
        'a trusted config that redirects into the judged checkout must refuse')
