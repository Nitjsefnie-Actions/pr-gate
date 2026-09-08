"""Run every shipped suite, refusing empty discovery or empty execution."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'tests'))
import _util


def main():
    tests = []
    for path in sorted((ROOT / 'tests').glob('test_*.py')):
        module = _util.load(path, path.stem)
        cases = _util.collect(vars(module))
        if not cases:
            print(f'No tests collected from {path.name}', file=sys.stderr)
            return 1
        tests.extend(cases)
    return _util.runner(tests, tmp_prefix='pr_gate_tests_')


if __name__ == '__main__':
    raise SystemExit(main())
