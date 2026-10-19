"""Stage a Python coverage raise: exit 0 raised, 3 unchanged, 2 invalid.

Percentages use coverage report --precision=1 semantics: Python's round()
followed by one decimal place, with nonzero/non-100 endpoints clamped to
0.1/99.9. The workflow supplies coverage report --format=total's string.
The recorded ``measured`` value rises only when a new measurement beats it
by more than the raise hysteresis, and a raise sets ``floor`` one hysteresis
below the new value, so a later dip cannot turn the coverage gate red; a
measurement within the hysteresis justifies no raise and writes nothing.
"""
import argparse
import json
import math
import sys
from pathlib import Path

RAISE_HYSTERESIS = 1.5


def _percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('coverage percentages must be numbers')
    if not 0 <= value <= 100 or not math.isfinite(value):
        raise ValueError('coverage percentages must be finite and between 0 and 100')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thresholds', type=Path, default=Path('.github/ci-thresholds.json'))
    parser.add_argument('--measured', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        measured = _percentage(float(args.measured))
        if 0 < measured < 0.1:
            measured = 0.1
        elif 99.9 < measured < 100:
            measured = 99.9
        else:
            measured = round(measured, 1)
        document = json.loads(args.thresholds.read_text(encoding='utf-8'))
        record = document['coverage']['python']
        floor = _percentage(record['floor'])
        previous = _percentage(record['measured'])
        if floor > previous:
            raise ValueError('recorded measured coverage is below its floor')
        if not isinstance(record['cell'], str) or not record['cell'].strip():
            raise ValueError('coverage cell must be a nonempty string')
        if measured - previous <= RAISE_HYSTERESIS:
            return 3
        record.update(floor=measured - RAISE_HYSTERESIS, measured=measured)
        args.output.write_text(json.dumps(document, indent=2, allow_nan=False) + '\n',
                               encoding='utf-8')
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f'coverage ratchet: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
