"""The type-error ratchet is the gate on pyright's measured error count.

The fixtures are committed JSON documents shaped like pyright's
``--outputjson`` output; nothing here runs the real checker. Driving the
real pyright is the types workflow's job (with the ratchet as its gate
step); this suite pins the verdict logic itself.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'ci' / 'type_ratchet.py'


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


def _run(*arguments, document=None, stdin_text=None):
    stdin_payload = stdin_text
    if document is not None:
        stdin_payload = json.dumps(document)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        input=stdin_payload, capture_output=True, text=True)


def test_document_at_baseline_passes_and_names_both_counts(tmp):
    _baseline(tmp / 'baseline.json', 3)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document=_document(errors=3))
    assert result.returncode == 0
    assert '3 measured' in result.stdout
    assert '3 recorded' in result.stdout


def test_zero_errors_pass_against_the_committed_baseline(tmp):
    del tmp
    result = _run(document=_document(errors=0))
    assert result.returncode == 0, (
        'a clean document must pass against the committed baseline, which '
        'also proves the committed baseline file parses')


def test_error_count_over_baseline_fails(tmp):
    _baseline(tmp / 'baseline.json', 3)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document=_document(errors=4))
    assert result.returncode == 1
    assert '4 measured' in result.stdout
    assert '3 recorded' in result.stdout


def test_counts_fall_freely_below_the_baseline(tmp):
    _baseline(tmp / 'baseline.json', 3)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document=_document(errors=1))
    assert result.returncode == 0
    assert '1 measured' in result.stdout


def test_warnings_and_informations_never_count(tmp):
    _baseline(tmp / 'baseline.json', 0)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document=_document(warnings=2, informations=2))
    assert result.returncode == 0
    assert '0 measured' in result.stdout


def test_missing_general_diagnostics_key_counts_zero_errors(tmp):
    _baseline(tmp / 'baseline.json', 0)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document={'summary': {'filesAnalyzed': 1}})
    assert result.returncode == 0
    assert '0 measured' in result.stdout


def test_non_dict_diagnostic_entry_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 0)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document={'generalDiagnostics': ['not a diagnostic'],
                            'summary': {'filesAnalyzed': 1}})
    assert result.returncode == 2, (
        'an unreadable diagnostic entry is a broken gate: an undercount '
        'must never read as a clean one')
    assert result.stderr


def test_document_read_from_stdin_when_no_path_argument(tmp):
    _baseline(tmp / 'baseline.json', 0)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  document=_document(errors=0))
    assert result.returncode == 0


def test_malformed_document_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 0)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  stdin_text='{not json')
    assert result.returncode == 2, (
        'unparseable input is a broken gate, not an over-baseline failure; '
        'it must not borrow exit 1')
    assert result.stderr, 'the failure must say what was wrong'


def test_non_object_document_fails_loudly(tmp):
    _baseline(tmp / 'baseline.json', 0)
    result = _run('--baseline', str(tmp / 'baseline.json'),
                  stdin_text='[1, 2, 3]')
    assert result.returncode == 2
    assert result.stderr


def test_missing_baseline_file_fails_loudly(tmp):
    result = _run('--baseline', str(tmp / 'absent.json'),
                  document=_document(errors=0))
    assert result.returncode == 2
    assert result.stderr


def test_baseline_count_must_be_a_non_negative_integer(tmp):
    for bad in ('"three"', '-1', 'null', 'true', '1.5'):
        (tmp / 'baseline.json').write_text(
            json.dumps({'error_count': json.loads(bad)}), encoding='utf-8')
        result = _run('--baseline', str(tmp / 'baseline.json'),
                      document=_document(errors=0))
        assert result.returncode == 2, f'error_count={bad} must be refused'
        assert result.stderr


if __name__ == '__main__':
    from _util import collect, runner
    raise SystemExit(runner(collect(globals()), tmp_prefix='typeratchet_'))
