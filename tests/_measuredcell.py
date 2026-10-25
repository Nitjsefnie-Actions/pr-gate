"""The measured matrix cell, parsed from the workflow that runs on it."""
import re

_CELL_OPERAND = re.compile(r"matrix\.python == '([^']+)'")


def measured_python(matrix, steps):
    operands = set()
    for step in steps:
        match = _CELL_OPERAND.search(step.get('if', ''))
        if match:
            operands.add(match[1])
    assert operands, 'no step names a matrix.python cell'
    assert len(operands) == 1, (
        f'the measured steps must name one python cell, not {sorted(operands)}')
    operand = operands.pop()
    assert operand in matrix['python'], (
        f"the measured python operand {operand!r} must stay in the pinned "
        'python matrix, or the gate runs on no cell')
    return operand


def measured_if(operand):
    return f"matrix.os == 'ubuntu-latest' && matrix.python == '{operand}'"
