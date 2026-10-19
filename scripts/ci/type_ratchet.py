#!/usr/bin/env python3
"""Gate pyright's whole-tree type-error count against the committed baseline.

Reads a pyright ``--outputjson`` document (path argument or stdin), counts
diagnostics with severity ``error``, and compares the count against
``pyright-baseline.json`` at the repository root. Exit 1 only when the
measured count exceeds the recorded one; equal or fewer exits 0, so the
count may fall freely and rises only through a deliberate baseline edit.
Unparseable or malformed input is a broken gate, not an over-baseline
failure: it exits 2 with the reason on stderr.

The recorded number is a measured seed, never a guess. It is the error
count of a real run of the pinned checker over the tree it gates:

    pyright==1.1.414 (pip pin in requirements-lint.txt), basic mode,
    pyrightconfig.json, Python 3.13.14, Linux, at base commit bd6aa28,
    2026-10-02:

        pyright --outputjson > report.json
        python scripts/ci/type_ratchet.py report.json
        # type errors: 48 measured, 48 recorded: within baseline

With --tighten, a complete report whose error count is strictly lower
atomically lowers the baseline: exit 0 changed, 3 unchanged, 2 invalid.
The default gate behavior above is unchanged. Never raise the baseline
to admit a new error.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / 'pyright-baseline.json'


def count_errors(document):
    """Count error-severity diagnostics; an absent key counts as none."""
    if not isinstance(document, dict):
        raise ValueError('the report is not a JSON object')
    diagnostics = document.get('generalDiagnostics', [])
    if not isinstance(diagnostics, list):
        raise ValueError('"generalDiagnostics" is not a list')
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            raise ValueError(
                '"generalDiagnostics" carries a non-object entry')
    return sum(1 for diagnostic in diagnostics
               if diagnostic.get('severity') == 'error')


def load_baseline(path):
    """Return the recorded error count, refusing any malformed baseline."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'{path} is not valid JSON: {error}') from None
    if not isinstance(data, dict) or 'error_count' not in data:
        raise ValueError(f'{path} must be a JSON object with "error_count"')
    count = data['error_count']
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(
            f'{path}: "error_count" must be a non-negative integer')
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Gate the pyright type-error count against the '
                    'committed baseline.')
    parser.add_argument(
        'report', nargs='?', type=Path, default=None,
        help='pyright --outputjson document (default: stdin)')
    parser.add_argument(
        '--baseline', type=Path, default=BASELINE,
        help='baseline JSON holding {"error_count": N} '
             '(default: pyright-baseline.json at the repository root)')
    parser.add_argument('--tighten', action='store_true',
                        help='lower the baseline only when strictly fewer errors are measured')
    arguments = parser.parse_args(argv)
    try:
        text = (arguments.report.read_text(encoding='utf-8')
                if arguments.report is not None else sys.stdin.read())
        document = json.loads(text)
        measured = count_errors(document)
        recorded = load_baseline(arguments.baseline)
        if arguments.tighten:
            diagnostics = document.get('generalDiagnostics')
            summary = document.get('summary')
            if not isinstance(diagnostics, list) or not isinstance(summary, dict):
                raise ValueError('tightening requires diagnostics and a summary')
            reported = summary.get('errorCount')
            if type(reported) is not int or reported != measured:
                raise ValueError('summary errorCount must match the measured error count')
            if any(item.get('severity') not in ('error', 'warning', 'information')
                   for item in diagnostics):
                raise ValueError('tightening requires a valid severity on every diagnostic')
            if measured >= recorded:
                return 3
            # Replace only after the complete lower record has been written;
            # an interrupted write leaves the original baseline intact.
            with tempfile.TemporaryDirectory(dir=arguments.baseline.parent,
                                             prefix='.type-ratchet-') as directory:
                staged = Path(directory) / arguments.baseline.name
                staged.write_text(json.dumps({'error_count': measured}, indent=2) + '\n',
                                  encoding='utf-8')
                staged.chmod(arguments.baseline.stat().st_mode)
                staged.replace(arguments.baseline)
            return 0
    except (OSError, ValueError) as error:
        print(f'type_ratchet: {error}', file=sys.stderr)
        return 2
    verdict = ('over baseline' if measured > recorded
               else 'within baseline')
    print(f'type errors: {measured} measured, {recorded} recorded: '
          f'{verdict}')
    return int(measured > recorded)


if __name__ == '__main__':
    raise SystemExit(main())
