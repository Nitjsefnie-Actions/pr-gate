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


if __name__ == '__main__':
    from _util import collect, runner
    raise SystemExit(runner(collect(globals()), tmp_prefix='typeratchet_'))
