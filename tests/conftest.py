"""Each test's private tmp directory; an empty test module or a skip fails."""
import contextlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

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
    except PermissionError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(lock)
    else:
        os.close(fd)


@pytest.fixture
def tmp():
    while True:
        fd, lock = tempfile.mkstemp(prefix='pr_gate_tests_', suffix='.lock')
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


def pytest_sessionstart():
    """Remove each tmp whose lock no live process holds."""
    for lock in list(Path(tempfile.gettempdir()).glob('pr_gate_tests_*.lock')):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue
        try:
            _lock(fd, wait=False)
            dead = os.fstat(fd).st_nlink > 0
            if dead and lock.with_suffix('').exists():
                shutil.rmtree(lock.with_suffix(''), onexc=_writable)
        except OSError:
            dead = False
        if dead:
            _release(fd, lock)
        else:
            os.close(fd)


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
