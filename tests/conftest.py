"""Each test's private tmp, inside its run's own; an empty test module or a skip fails."""
import contextlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

if sys.platform == 'win32':
    import msvcrt  # pylint: disable=import-error

    def _lock(fd, wait):
        msvcrt.locking(fd, msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
else:
    import fcntl

    def _lock(fd, wait):
        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _writable(function, path, _):
    os.chmod(path, stat.S_IWRITE)  # Windows unlinks no read-only git object
    function(path)


def _release(fd, lock):
    """Unlink lock while held; Windows unlinks no open file, so only once closed."""
    try:
        os.unlink(lock)
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(lock)
    else:
        os.close(fd)


def _private(directory=None):
    while True:
        fd, lock = tempfile.mkstemp(prefix='pr_gate_tests_', suffix='.lock', dir=directory)
        _lock(fd, wait=True)
        if os.fstat(fd).st_nlink:  # else a sweep unlinked it first
            break
        os.close(fd)
    directory = Path(lock).with_suffix('')
    try:
        directory.mkdir(mode=0o700)
        yield directory
        shutil.rmtree(directory, onexc=_writable)
    except BaseException:
        os.close(fd)
        raise
    _release(fd, lock)


tmp = pytest.fixture(_private, name='tmp')


@pytest.fixture(autouse=True)
def _required_checks(monkeypatch):
    """The freshness check's required checks without the network: matrix legs name one job."""
    monkeypatch.setenv('PR_GATE_REQUIRED_CHECKS',
                       'actionlint\ntest (ubuntu-latest, 3.15, 1/2)\ntest (macos-latest)\nreport\n')


def pytest_sessionstart(session):
    """Sweep dead runs' tmps; root this run's files in one beside, never in, its parent run's."""
    system = os.environ.get('PR_GATE_TESTS_TEMP', tempfile.gettempdir())
    for lock in list(Path(system).glob('pr_gate_tests_*.lock')):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue
        try:
            _lock(fd, wait=False)
            dead = os.fstat(fd).st_nlink > 0
            if dead and lock.with_suffix('').is_dir(follow_symlinks=False):
                shutil.rmtree(lock.with_suffix(''), onexc=_writable)
        except OSError:
            dead = False
        if dead:
            _release(fd, lock)
        else:
            os.close(fd)
    session.config.add_cleanup((run := contextlib.ExitStack()).close)
    root = str(run.enter_context(contextlib.contextmanager(_private)(system)))
    run.enter_context(mock.patch.dict(os.environ, TMPDIR=root, PR_GATE_TESTS_TEMP=system))
    run.enter_context(mock.patch.object(tempfile, 'tempdir', root))


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    report = yield
    if isinstance(collector, pytest.Module) and collector.path.name.startswith('test_'):
        if report.skipped:
            report.outcome = 'failed'
            report.longrepr = f'{collector.path.name} skipped at import: {report.longrepr}'
        elif report.passed and not report.result and not collector.config.getoption('lf', False):
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
