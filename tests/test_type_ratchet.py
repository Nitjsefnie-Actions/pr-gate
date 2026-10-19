"""The type-error ratchet is the gate on pyright's measured error count.

The fixtures are committed JSON documents shaped like pyright's
``--outputjson`` output; nothing here runs the real checker. The suite
drives the checked-in module IN-PROCESS — the coverage gate measures
this parent process (`coverage run --source=scripts/ci`), so driving
the script through subprocesses would count the gate's own statements
as missed. One subprocess test pins the ``__main__`` entry point
mapping the verdict to the process exit status.
"""
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import _util

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'ci' / 'type_ratchet.py'


def _module():
    """Return the checked-in ratchet module, loaded through the harness."""
    return _util.load(SCRIPT, 'type_ratchet_under_test')


def _document(errors=0, warnings=0, informations=0):
    """Return a pyright --outputjson-shaped document with fake findings."""
    diagnostics = []
    for severity, count in (('error', errors), ('warning', warnings),
                            ('information', informations)):
        diagnostics.extend({
            'file': f'/repo/file{index}.py',
            'severity': severity,
            'message': f'{severity} {index}',
            'rule': 'reportFakeFinding',
        } for index in range(count))
    return {'generalDiagnostics': diagnostics,
            'summary': {'filesAnalyzed': 1, 'errorCount': errors,
                        'warningCount': warnings,
                        'informationCount': informations}}


def _baseline(path, count):
    path.write_text(json.dumps({'error_count': count}), encoding='utf-8')


def _invoke(*arguments, document=None, stdin_text=None):
    """Call the ratchet's main() in-process; return (exit code, stdout)."""
    argv = [str(argument) for argument in arguments]
    if document is not None:
        stdin_text = json.dumps(document)
    stdout = io.StringIO()
    original_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text if stdin_text is not None else '')
    try:
        with contextlib.redirect_stdout(stdout):
            code = _module().main(argv)
    finally:
        sys.stdin = original_stdin
    return code, stdout.getvalue()


def test_document_at_baseline_passes_and_names_both_counts(tmp):
    _baseline(tmp / 'baseline.json', 3)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document=_document(errors=3))
    assert code == 0
    assert '3 measured' in out
    assert '3 recorded' in out


def test_zero_errors_pass_against_the_committed_baseline(tmp):
    del tmp
    code, _ = _invoke(document=_document(errors=0))
    assert code == 0, (
        'a clean document must pass against the committed baseline, which '
        'also proves the committed baseline file parses')


def test_error_count_over_baseline_fails(tmp):
    _baseline(tmp / 'baseline.json', 3)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document=_document(errors=4))
    assert code == 1
    assert '4 measured' in out
    assert '3 recorded' in out


def test_counts_fall_freely_below_the_baseline(tmp):
    _baseline(tmp / 'baseline.json', 3)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document=_document(errors=1))
    assert code == 0
    assert '1 measured' in out


def test_warnings_and_informations_never_count(tmp):
    _baseline(tmp / 'baseline.json', 0)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document=_document(warnings=2, informations=2))
    assert code == 0
    assert '0 measured' in out


def test_missing_general_diagnostics_key_counts_zero_errors(tmp):
    _baseline(tmp / 'baseline.json', 0)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document={'summary': {'filesAnalyzed': 1}})
    assert code == 0
    assert '0 measured' in out


def test_document_read_from_stdin_when_no_path_argument(tmp):
    _baseline(tmp / 'baseline.json', 0)
    code, _ = _invoke('--baseline', tmp / 'baseline.json',
                      document=_document(errors=0))
    assert code == 0, (
        'no path argument means the report arrives on stdin')


def test_malformed_document_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 0)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        stdin_text='{not json')
    assert code == 2, (
        'unparseable input is a broken gate, not an over-baseline failure; '
        'it must not borrow exit 1')
    assert out == '', 'a broken gate must not print a verdict'


def test_non_object_document_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 2)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        stdin_text='[1, 2, 3]')
    assert code == 2
    assert out == ''


def test_missing_baseline_file_fails_loudly(tmp):
    code, out = _invoke('--baseline', tmp / 'absent.json',
                        document=_document(errors=0))
    assert code == 2
    assert out == ''


def test_baseline_count_must_be_a_non_negative_integer(tmp):
    for bad in ('"three"', '-1', 'null', 'true', '1.5'):
        (tmp / 'baseline.json').write_text(
            json.dumps({'error_count': json.loads(bad)}), encoding='utf-8')
        code, out = _invoke('--baseline', tmp / 'baseline.json',
                            document=_document(errors=0))
        assert code == 2, f'error_count={bad} must be refused'
        assert out == ''


def test_non_dict_diagnostic_entry_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 0)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document={'generalDiagnostics': ['not a diagnostic'],
                                  'summary': {'filesAnalyzed': 1}})
    assert code == 2, (
        'an unreadable diagnostic entry is a broken gate: an undercount '
        'must never read as a clean one')
    assert out == ''


def test_non_list_general_diagnostics_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 0)
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document={'generalDiagnostics': 'everything',
                                  'summary': {'filesAnalyzed': 1}})
    assert code == 2, (
        'a diagnostics member no list can hold is a broken gate, not a '
        'clean count of zero')
    assert out == ''


def test_malformed_baseline_json_fails_loudly(tmp):
    (tmp / 'baseline.json').write_text('{not json', encoding='utf-8')
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document=_document(errors=0))
    assert code == 2
    assert out == ''


def test_baseline_without_error_count_fails_loudly(tmp):
    (tmp / 'baseline.json').write_text('{"other": 1}', encoding='utf-8')
    code, out = _invoke('--baseline', tmp / 'baseline.json',
                        document=_document(errors=0))
    assert code == 2, (
        'a baseline without a recorded count cannot gate anything')
    assert out == ''


def test_main_entry_point_maps_the_verdict_to_exit_status(tmp):
    report = tmp / 'report.json'
    report.write_text(json.dumps(_document(errors=0)), encoding='utf-8')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(report)],
        capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert '0 measured' in result.stdout


def test_tighten_cli_writes_strictly_lower_count(tmp):
    baseline = tmp / 'baseline.json'
    _baseline(baseline, 3)
    report = tmp / 'report.json'
    report.write_text(json.dumps(_document(errors=1)), encoding='utf-8')
    result = subprocess.run([sys.executable, str(SCRIPT), '--tighten',
                             '--baseline', str(baseline), str(report)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(baseline.read_text()) == {'error_count': 1}


def test_tighten_equal_higher_and_zero_preserve_the_successful_write(tmp):
    baseline = tmp / 'baseline.json'
    _baseline(baseline, 4)
    code, _ = _invoke('--tighten', '--baseline', baseline, document=_document(errors=2))
    assert code == 0
    assert json.loads(baseline.read_text()) == {'error_count': 2}
    before = baseline.read_bytes()
    for count in (2, 3, 5):
        code, _ = _invoke('--tighten', '--baseline', baseline, document=_document(errors=count))
        assert code == 3, count
        assert baseline.read_bytes() == before
    code, _ = _invoke('--tighten', '--baseline', baseline,
                      document=_document(warnings=2, informations=1))
    assert code == 0
    assert json.loads(baseline.read_text()) == {'error_count': 0}
    before = baseline.read_bytes()
    code, _ = _invoke('--tighten', '--baseline', baseline, document=_document())
    assert code == 3 and baseline.read_bytes() == before


def test_tighten_refuses_malformed_or_incomplete_reports_without_writes(tmp):
    baseline = tmp / 'baseline.json'
    _baseline(baseline, 4)
    code, _ = _invoke('--tighten', '--baseline', baseline, document=_document(errors=2))
    assert code == 0
    before = baseline.read_bytes()
    documents = [[], {}, {'summary': {'errorCount': 0}},
                 {'generalDiagnostics': []},
                 {'generalDiagnostics': [], 'summary': []},
                 {'generalDiagnostics': [False], 'summary': {'errorCount': 0}},
                 {'generalDiagnostics': 'bad', 'summary': {'errorCount': 0}}]
    for bad_count in (True, -1, '0', None, 1):
        documents.append({'generalDiagnostics': [], 'summary': {'errorCount': bad_count}})
    for severity in (None, 'fatal'):
        documents.append({'generalDiagnostics': [{'severity': severity}],
                          'summary': {'errorCount': 0}})
    for document in documents:
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code, _ = _invoke('--tighten', '--baseline', baseline, document=document)
        assert code == 2 and error.getvalue(), document
        assert baseline.read_bytes() == before
    with contextlib.redirect_stderr(io.StringIO()) as error:
        code, _ = _invoke('--tighten', '--baseline', baseline, stdin_text='{bad')
    assert code == 2 and error.getvalue()
    assert baseline.read_bytes() == before


def test_tighten_refuses_malformed_baseline_without_writing(tmp):
    baseline = tmp / 'baseline.json'
    for content in ('{bad', '[]', '{}', '{"error_count":true}',
                    '{"error_count":-1}', '{"error_count":"2"}'):
        baseline.write_text(content, encoding='utf-8')
        before = baseline.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code, _ = _invoke('--tighten', '--baseline', baseline, document=_document())
        assert code == 2 and error.getvalue()
        assert baseline.read_bytes() == before
    absent = tmp / 'absent.json'
    with contextlib.redirect_stderr(io.StringIO()) as error:
        code, _ = _invoke('--tighten', '--baseline', absent, document=_document())
    assert code == 2 and error.getvalue() and not absent.exists()


if __name__ == '__main__':
    from _util import collect, runner
    raise SystemExit(runner(collect(globals()), tmp_prefix='typeratchet_'))
