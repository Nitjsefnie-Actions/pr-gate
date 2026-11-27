"""The freshness check refuses when it is given no required check."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_required_check_is_a_refusal(monkeypatch):
    monkeypatch.setenv('PR_GATE_REQUIRED_CHECKS', ' \n')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'pull_request')
    done = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/gate_base_freshness.py')],
                          capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert 'names no required check' in done.stdout + done.stderr
