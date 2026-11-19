"""tests/conftest.py's rules, most run as a nested pytest on a scratch copy of it."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / 'conftest.py'


def _pytest(project, files, workers='0'):
    shutil.copyfile(CONFTEST, project / 'conftest.py')
    for name, text in files.items():
        (project / name).write_text(text, encoding='utf-8')
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-n',
                           workers, str(project)], cwd=project, capture_output=True, text=True,
                          timeout=120, check=False)


def test_tmp_is_a_private_unresolved_directory(tmp):
    assert isinstance(tmp, Path)
    assert tmp.parent == Path(tempfile.gettempdir())
    assert tmp.name.startswith('pr_gate_tests_')
    assert tmp.is_dir() and not any(tmp.iterdir())


def test_tmp_is_removed_after_its_test(tmp):
    record = tmp / 'seen.txt'
    done = _pytest(tmp, {'test_seen.py': 'def test_seen(tmp):\n'
                         f'    open({str(record)!r}, "w", encoding="utf-8").write(str(tmp))\n'})
    assert done.returncode == 0, done.stdout + done.stderr
    seen = Path(record.read_text(encoding='utf-8'))
    assert seen.name.startswith('pr_gate_tests_') and not seen.exists()


def test_a_module_that_runs_no_test_fails_the_run(tmp):
    files = {'test_empty.py': 'VALUE = 1\n', 'test_full.py': 'def test_ok(tmp):\n    del tmp\n',
             'test_skip.py': 'import pytest\npytest.skip("no", allow_module_level=True)\n',
             'test_unit.py': 'import unittest\nraise unittest.SkipTest("no")\n'}
    for workers in ('0', '2'):
        done = _pytest(tmp, files, workers)
        assert done.returncode not in (0, 5), done.stdout + done.stderr
        for line in ('No tests collected from test_empty.py', 'test_skip.py skipped at import',
                     'test_unit.py skipped at import'):
            assert line in done.stdout, done.stdout


def test_a_skipped_test_fails_the_run(tmp):
    files = {'test_skip.py': (
        'import unittest\nimport pytest\n\n'
        'def test_pytest_skip(tmp):\n    pytest.skip("not run")\n\n'
        'def test_unittest_skip(tmp):\n    raise unittest.SkipTest("not run")\n\n'
        '@pytest.mark.skip\ndef test_marked_skip(tmp):\n    del tmp\n')}
    for workers in ('0', '2'):
        done = _pytest(tmp, files, workers)
        assert done.returncode == 1, done.stdout + done.stderr
        assert '2 failed, 1 error' in done.stdout, done.stdout
