"""The secret scan's workflow triggers and the config it scans under."""
import tomllib
from pathlib import Path

from test_workflows import _workflow

ROOT = Path(__file__).resolve().parents[1]


def test_secrets_scans_on_every_event_under_the_default_rules(tmp):
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
    config = tomllib.loads((ROOT / '.gitleaks.toml').read_text(encoding='utf-8'))
    assert config == {'extend': {'useDefault': True}}, (
        'the extend line IS the gate; a narrowing allowlist replacement must '
        'fail this suite')
