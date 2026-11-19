"""Each test's private tmp directory; an empty test module or a skip fails."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory(prefix='pr_gate_tests_') as directory:
        yield Path(directory)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    report = yield
    if isinstance(collector, pytest.Module) and collector.path.name.startswith('test_'):
        if report.skipped:
            report.outcome = 'failed'
            report.longrepr = f'{collector.path.name} skipped at import: {report.longrepr}'
        elif report.passed and not report.result:
            report.outcome = 'failed'
            report.longrepr = f'No tests collected from {collector.path.name}'
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport():
    report = yield
    if report.skipped:
        report.outcome = 'failed'
        report.longrepr = f'skipped, and every collected test must run: {report.longrepr}'
    return report
