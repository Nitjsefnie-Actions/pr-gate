#!/usr/bin/env python3
"""How much of what THIS change added is covered, and what it missed.

The gate next door reports one number for the whole tree, and that number
barely moves: a pull request can add uncovered lines to the measured surface
and the tree-wide total falls by a fraction of a point, inside the buffer the
floor already allows. So the tree-level figure cannot tell a reviewer whether
the code in front of them was tested — only whether the repository as a whole
still is. This reports the other number: of the lines this change ADDED that
coverage considers executable, how many did the suites reach. It is
deliberately NOT a gate. A refactor that only moves code, a change that only
deletes, and a fix whose test lives in tests/ all produce a low patch figure
for reasons a reviewer should judge rather than a threshold should block on.

Lines the diff added that coverage does not consider executable are excluded:
blank lines, comments and docstrings carry no record in the report, and
counting them would make the percentage depend on formatting. The report is
the only oracle here — a changed line it does not name simply leaves the
denominator, and a changed scripts/ci module the report names nowhere is
listed as not measured rather than silently scored zero.

Two inputs feed the number: the Cobertura XML from ``coverage xml``
(--coverage) and the unified diff of the pull request's own merge
(--diff, written as ``git diff HEAD^1..HEAD`` by the workflow step that calls
this). Invalid input — an unparseable report, a root that is not
``<coverage>``, a line without a number or hits, a non-positive line number,
no usable line entries at all, or a binary diff record — is a loud exit 1
with the reason on stderr, never a flattering number.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# `+++ b/path`, with git's optional quoting and the /dev/null of a deletion.
_TARGET = re.compile(r'^\+\+\+ (.*)$')
# `@@ -old,count +new,count @@`; the counts are optional and mean 1.
_HUNK = re.compile(r'^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')

# The only Python this repository's coverage run measures, and therefore the
# only changed files whose absence from the report means something.
_MEASURED_ROOT = 'scripts/ci/'


def _decode_git_path(value):
    """Decode a quoted Git path and remove its diff-side prefix."""
    if not value.startswith('"'):
        value = value.split('\t', 1)[0]
        return value[2:] if value.startswith('b/') else value
    escaped = value[1:-1] if value.endswith('"') else value[1:]
    escapes = {
        '\\': b'\\', '"': b'"', 'a': b'\a', 'b': b'\b',
        'f': b'\f', 'n': b'\n', 'r': b'\r', 't': b'\t',
        'v': b'\v',
    }
    decoded = bytearray()
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char != '\\':
            decoded.extend(char.encode('utf-8'))
            index += 1
            continue
        octal = escaped[index + 1:index + 4]
        if len(octal) == 3 and all(item in '01234567' for item in octal):
            decoded.append(int(octal, 8))
            index += 4
            continue
        if index + 1 == len(escaped):
            decoded.extend(b'\\')
            index += 1
            continue
        escaped_char = escaped[index + 1]
        if escaped_char in escapes:
            decoded.extend(escapes[escaped_char])
        else:
            decoded.extend(escaped_char.encode('utf-8'))
        index += 2
    value = decoded.decode('utf-8')
    return value[2:] if value.startswith('b/') else value


def executable_lines(coverage_xml):
    """Return {path: {line number: times hit}} from a Cobertura report."""
    root = ET.parse(coverage_xml).getroot()
    if root.tag != 'coverage':
        raise ValueError('root is not <coverage>')
    measured = {}
    usable = False
    for class_node in root.iter('class'):
        filename = class_node.get('filename')
        if not filename:
            continue
        lines = measured.setdefault(filename, {})
        for line_node in class_node.iter('line'):
            number = line_node.get('number')
            hits = line_node.get('hits')
            if number is None:
                raise ValueError(f'missing line number for {filename}')
            try:
                number_value = int(number)
            except ValueError as error:
                raise ValueError(
                    f'invalid line number for {filename}: {number!r}'
                ) from error
            if number_value <= 0:
                raise ValueError(
                    f'invalid line number for {filename}: {number!r} '
                    '(must be positive)')
            if hits is None:
                raise ValueError(
                    f'missing hits for {filename}:{number_value}')
            try:
                hits_value = int(hits)
            except ValueError as error:
                raise ValueError(
                    f'invalid hits for {filename}:{number_value}: '
                    f'{hits!r}') from error
            # A file can appear as more than one <class>; take the best hit
            # count so a line reached by any of them counts as covered.
            lines[number_value] = max(lines.get(number_value, 0), hits_value)
            usable = True
    if not usable:
        raise ValueError('no usable line entries')
    return measured


def added_lines(diff_text):
    """Return {path: {line numbers this diff adds}} from a unified diff."""
    added = {}
    path = None
    line_number = 0
    in_hunk = False
    old_remaining = new_remaining = 0
    for line in diff_text.split('\n'):
        header = line.removesuffix('\r')
        if line.startswith('Binary files '):
            raise ValueError(
                f'binary diff record is not measurable: {line}')
        # `--- ` counts as a file header only outside a hunk. Git renders a
        # REMOVED line whose content begins `-- ` as `--- ...`, and taking
        # that for a header clears the path and silently drops every later
        # hunk of the file. The `+++` match below is guarded the same way.
        if header.startswith('diff --git ') or (
                not in_hunk and header.startswith('--- ')):
            path = None
            in_hunk = False
            continue
        target = _TARGET.match(header) if not in_hunk else None
        if target is not None:
            name = _decode_git_path(target.group(1))
            path = None if name == '/dev/null' else name
            continue
        hunk = _HUNK.match(header)
        if hunk is not None:
            old_remaining = int(hunk.group(1) or 1)
            line_number = int(hunk.group(2))
            new_remaining = int(hunk.group(3) or 1)
            in_hunk = bool(old_remaining or new_remaining)
            continue
        if path is None or not in_hunk:
            continue
        # Classify on the \r-stripped form: a blank CONTEXT line arrives as
        # a bare carriage return, and testing the raw line would leave it
        # unmatched — nothing advances and every later line number in the
        # hunk shifts. The structural matches above already read `header`.
        if header.startswith('+'):
            added.setdefault(path, set()).add(line_number)
            line_number += 1
            new_remaining -= 1
        elif header.startswith('-'):
            old_remaining -= 1
        elif header.startswith(' ') or header == '':
            line_number += 1
            old_remaining -= 1
            new_remaining -= 1
        # A `-` line exists only in the old file and moves nothing.
        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False
    return added


def _ranges(numbers):
    """Collapse sorted line numbers into `3`, `5-9` spans for readability."""
    spans = []
    for number in sorted(numbers):
        if spans and number == spans[-1][1] + 1:
            spans[-1][1] = number
        else:
            spans.append([number, number])
    return ', '.join(str(low) if low == high else f'{low}-{high}'
                     for low, high in spans)


def measure(measured, added):
    """Return (per-file rows, covered, total) over the added lines."""
    rows = []
    covered = total = 0
    for path in sorted(added):
        lines = measured.get(path)
        if not lines:
            # Not a file this report measures — a test, a workflow, a
            # Markdown file. Absent is not the same as uncovered.
            continue
        touched = sorted(number for number in added[path] if number in lines)
        if not touched:
            continue
        missed = [number for number in touched if lines[number] == 0]
        rows.append((path, len(touched) - len(missed), len(touched), missed))
        covered += len(touched) - len(missed)
        total += len(touched)
    return rows, covered, total


def unmeasured_sources(measured, added):
    """Return changed scripts/ci modules the coverage report never named.

    Coverage measures scripts/ci alone in this repository, so a changed .py
    file under it that the report does not name is a measurement gap worth
    naming in the comment. Files anywhere else — a test, a workflow, a
    document — are outside the measured surface, and their absence says
    nothing.
    """
    return {path for path in added
            if path.startswith(_MEASURED_ROOT)
            and path.endswith('.py')
            and path not in measured}


def render(rows, covered, total, unmeasured=()):
    """Render the markdown comment body for one run."""
    unmeasured = set(unmeasured)
    out = ['### Coverage of this change', '']
    if total == 0 and unmeasured:
        out.append('This change touched source files under scripts/ci that '
                   'the coverage report does not name. Nothing here was '
                   'measured, which is not the same as nothing needing '
                   'to be:')
        out.append('')
        out.append('Unmeasured changed source files:')
        out.extend(f'- `{path}`' for path in sorted(unmeasured))
    elif total == 0:
        # Said in full rather than as "0 lines": the common case here is a
        # change confined to tests or to the workflow YAML, and a bare
        # "nothing added" reads like the tool failed to find the diff.
        out.append('No measured lines were added: coverage measures '
                   '`scripts/ci` only, so a change confined to tests, to the '
                   'workflows, or to documentation has no patch coverage to '
                   'report — that is not the same as none of it running.')
    else:
        percent = 100.0 * covered / total
        if covered < total:
            # 100.0 must mean every added line was reached; 99.9 is the
            # honest ceiling for anything less.
            percent = min(percent, 99.9)
        out.append(f'**{percent:.1f}%** of added lines covered '
                   f'({covered}/{total}).')
        out.append('')
        out.append('| File | Covered | Added | Missed lines |')
        out.append('| --- | ---: | ---: | --- |')
        for path, file_covered, file_total, missed in rows:
            detail = _ranges(missed) if missed else '—'
            out.append(
                f'| `{path}` | {file_covered} | {file_total} | {detail} |')
        if unmeasured:
            out.append('')
            out.append('The coverage report did not measure these changed '
                       'scripts/ci files:')
            out.extend(f'- `{path}`' for path in sorted(unmeasured))
        if covered == total:
            out.append('')
            out.append('Every added line was reached.')
    out.append('')
    # The floor the gate enforces is the tree-wide one; this number is the
    # reviewer's lens on the diff alone, and the two can disagree in both
    # directions without either being wrong.
    out.append('This patch number can be low while the tree-wide total '
               'still holds its floor: the gate measures the whole tree, '
               'and a reviewer judges the lines this change put in front '
               'of them.')
    return '\n'.join(out) + '\n'


def main(argv=None):
    """Read the coverage report and a diff; print the markdown body."""
    parser = argparse.ArgumentParser(
        description='Report the coverage of the lines a pull request adds.')
    parser.add_argument('--coverage', required=True,
                        help='Cobertura XML written by `coverage xml`')
    parser.add_argument('--diff', required=True,
                        help='unified diff of the pull request merge')
    arguments = parser.parse_args(argv)
    try:
        diff_text = Path(arguments.diff).read_text(encoding='utf-8')
        measured = executable_lines(arguments.coverage)
        added = added_lines(diff_text)
    except (OSError, ET.ParseError, ValueError, UnicodeDecodeError) as error:
        print(f'diff_coverage: {error}', file=sys.stderr)
        return 1
    rows, covered, total = measure(measured, added)
    sys.stdout.write(render(rows, covered, total,
                            unmeasured_sources(measured, added)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
