"""Coverage raises are monotonic, precision-bound, and preserve other records."""
import contextlib
import copy
import io
import json
import subprocess
import sys
from pathlib import Path

from coverage.results import display_covered
import _util

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/ci/coverage_ratchet.py'


def _record() -> dict:
    return {'schema_version': 1, 'coverage': {'python': {
        'floor': 91.9, 'measured': 93.6, 'cell': 'ubuntu-latest / Python 3.13'}},
        'unrelated': {'keep': [1, 2]}}


def _invoke(tmp, measured, document=None):
    source = tmp / 'thresholds.json'
    output = tmp / 'raised.json'
    if document is None:
        document = _record()
    source.write_text(json.dumps(document), encoding='utf-8')
    before = source.read_bytes()
    module = _util.load(SCRIPT, 'coverage_ratchet_under_test')
    with contextlib.redirect_stderr(io.StringIO()) as errors:
        code = module.main(['--thresholds', str(source), '--measured', measured,
                            '--output', str(output)])
    assert source.read_bytes() == before
    return code, output, errors.getvalue()


def test_raise_updates_only_floor_and_measured(tmp):
    code, output, _ = _invoke(tmp, '95.2')
    assert code == 0
    expected = _record()
    expected['coverage']['python'].update(floor=95.2 - 1.5, measured=95.2)
    assert json.loads(output.read_text(encoding='utf-8')) == expected


def test_raise_needs_more_than_the_hysteresis_and_keeps_a_gap(tmp):
    # Issue #95: a measurement within the hysteresis justifies no raise, and
    # a raise keeps a fixed 1.5-point gap below the new measured value, so
    # the record never reads floor == measured again.
    code, output, _ = _invoke(tmp, '95.1')
    assert code == 3 and not output.exists(), 'a 1.5-point tie never raises'
    code, output, _ = _invoke(tmp, '94.2')
    assert code == 3 and not output.exists(), (
        'a dip to just above the floor justifies no raise')
    code, output, _ = _invoke(tmp, '95.2')
    assert code == 0
    written = json.loads(output.read_text(encoding='utf-8'))['coverage']['python']
    assert written['floor'] == 95.2 - 1.5, 'a raise keeps the gap below measured'
    assert written['measured'] == 95.2


def test_equal_and_lower_write_nothing_even_over_existing_output(tmp):
    code, output, _ = _invoke(tmp, '95.2')
    assert code == 0
    before = output.read_bytes()
    for measured in ('91.9', '91.8', '0.0'):
        code, _, _ = _invoke(tmp, measured)
        assert code == 3, measured
        assert output.read_bytes() == before
    output.unlink()
    code, output, _ = _invoke(tmp, '91.9')
    assert code == 3 and not output.exists()


def test_precision_matches_coverage_report_total(tmp):
    for raw, expected in [('91.94', 91.9), ('91.96', 92.0), ('92.25', 92.2),
                          ('92.35', 92.3), ('0.001', 0.1), ('99.999', 99.9),
                          ('0', 0.0), ('100', 100.0)]:
        document = _record()
        document['coverage']['python'].update(floor=0, measured=0)
        code, output, _ = _invoke(tmp, raw, document)
        assert float(display_covered(float(raw), 1)) == expected
        if expected > 1.5:
            assert code == 0
            record = json.loads(output.read_text())['coverage']['python']
            assert record['measured'] == expected
            assert record['floor'] == expected - 1.5
        else:
            assert code == 3, 'a measurement within the hysteresis never raises'
    code, _, _ = _invoke(tmp, '91.94')
    assert code == 3, 'sub-precision improvement cannot raise the record'


def test_malformed_measurements_fail_loud_without_writing(tmp):
    code, output, _ = _invoke(tmp, '95.2')
    assert code == 0
    before = output.read_bytes()
    for raw in ('', 'garbage', '94.2%', 'NaN', 'Infinity', '-0.1', '100.1'):
        code, _, error = _invoke(tmp, raw)
        assert code == 2 and error, raw
        assert output.read_bytes() == before


def test_invalid_thresholds_fail_loud_without_writing(tmp):
    code, output, _ = _invoke(tmp, '95.2')
    assert code == 0
    before = output.read_bytes()
    documents = [False, [], {}, {'coverage': []}, {'coverage': {'python': []}}]
    for key, values in {'floor': [True, '91.9', -1, 101, None, float('nan'), 10 ** 400],
                        'measured': [True, '93.6', 90, 101, float('inf')],
                        'cell': ['', None, 3]}.items():
        for value in values:
            doc = copy.deepcopy(_record())
            doc['coverage']['python'][key] = value
            documents.append(doc)
    for document in documents:
        code, _, error = _invoke(tmp, '95.0', document)
        assert code == 2 and error, document
        assert output.read_bytes() == before


def test_cli_malformed_json_missing_file_and_write_error_fail(tmp):
    module = _util.load(SCRIPT, 'coverage_ratchet_under_test')
    source = tmp / 'thresholds.json'
    output = tmp / 'output.json'
    args = ['--thresholds', str(source), '--measured', '95.2', '--output', str(output)]
    with contextlib.redirect_stderr(io.StringIO()):
        assert module.main(args) == 2
        source.write_text('{', encoding='utf-8')
        assert module.main(args) == 2
        source.write_text(json.dumps(_record()), encoding='utf-8')
        output.mkdir()
        assert module.main(args) == 2
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 2 and result.stderr


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='coverage_ratchet_'))
