"""The patch-coverage reporter measures a pull request's own added lines.

Every fixture here is synthetic: the Cobertura XML documents and unified
diffs are strings this suite writes to a temporary directory, so nothing
depends on coverage.py's installed version or on a real pull request. The
suite drives the checked-in module IN-PROCESS — the coverage gate measures
this parent process (``coverage run --source=scripts/ci``), so driving the
script through subprocesses would count the gate's own statements as
missed. One subprocess test pins the ``__main__`` entry point and the
stdout/exit contract a workflow step actually consumes.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

import _util

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'ci' / 'diff_coverage.py'


def _module():
    """Return the checked-in reporter module, loaded through the harness."""
    return _util.load(SCRIPT, 'diff_coverage_under_test')


def _xml(*classes):
    """Return a Cobertura document with one <class> per (filename, lines)."""
    body = ''.join(
        f'<class filename="{filename}"><lines>{lines}</lines></class>'
        for filename, lines in classes)
    return ('<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.0" version="7.16">\n'
            f'<packages><package><classes>{body}</classes></package>'
            '</packages></coverage>\n')


def _lines(*pairs):
    """Return <line> elements for (number, hits) pairs."""
    return ''.join(f'<line number="{number}" hits="{hits}"/>'
                   for number, hits in pairs)


def _write(path, text):
    path.write_text(text, encoding='utf-8', newline='')
    return path


def _main(*arguments):
    """Call the reporter's main() in-process; return (code, out, err)."""
    argv = [str(argument) for argument in arguments]
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        code = _module().main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _refuses(function, fragment):
    """Assert the call raises ValueError naming `fragment`, or fail."""
    try:
        function()
    except ValueError as refusal:
        assert fragment in str(refusal), str(refusal)
    else:
        raise AssertionError(
            f'expected a ValueError naming {fragment!r}, got none')


# --- executable_lines: the Cobertura XML half -------------------------------

def test_executable_lines_reads_paths_and_hit_counts(tmp):
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', _lines((1, 2), (2, 0), (40, 7)))))
    assert _module().executable_lines(report) == {
        'scripts/ci/pr_gate.py': {1: 2, 2: 0, 40: 7}}


def test_a_file_across_two_class_nodes_keeps_the_best_hit_count(tmp):
    # coverage.py can split one file across several <class> nodes; a line
    # reached through any of them counts as reached, so the merge is max,
    # never last-write-wins.
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', _lines((1, 0))),
        ('scripts/ci/pr_gate.py', _lines((1, 5), (3, 0)))))
    assert _module().executable_lines(report) == {
        'scripts/ci/pr_gate.py': {1: 5, 3: 0}}


def test_a_line_without_a_number_is_refused(tmp):
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', '<line hits="1"/>')))
    _refuses(lambda: _module().executable_lines(report), 'line number')


def test_a_line_without_hits_is_refused(tmp):
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', '<line number="4"/>')))
    _refuses(lambda: _module().executable_lines(report), 'hits')


def test_a_non_positive_line_number_is_refused(tmp):
    for number in ('0', '-3'):
        report = _write(tmp / 'coverage.xml', _xml(
            ('scripts/ci/pr_gate.py', _lines((number, 1)))))
        _refuses(lambda report=report: _module().executable_lines(report),
                 'must be positive')


def test_a_non_coverage_root_is_refused(tmp):
    report = _write(tmp / 'coverage.xml',
                    '<?xml version="1.0" ?>\n<not-coverage/>\n')
    _refuses(lambda: _module().executable_lines(report),
             'root is not <coverage>')


def test_a_report_with_no_usable_line_entries_is_refused(tmp):
    report = _write(tmp / 'coverage.xml', _xml())
    _refuses(lambda: _module().executable_lines(report),
             'no usable line entries')


def test_a_class_without_a_filename_contributes_nothing(tmp):
    # A <class> carrying no filename is skipped, so a report holding only
    # one has no usable line entries and is refused like any other empty.
    report = _write(tmp / 'coverage.xml',
                    '<?xml version="1.0" ?>\n<coverage><classes>'
                    '<class><lines><line number="1" hits="1"/></lines>'
                    '</class></classes></coverage>\n')
    _refuses(lambda: _module().executable_lines(report),
             'no usable line entries')


def test_unparseable_xml_reaches_exit_1_through_main(tmp):
    report = _write(tmp / 'coverage.xml', '<coverage><unclosed>')
    diff = _write(tmp / 'patch.diff',
                  '--- a/scripts/ci/x.py\n+++ b/scripts/ci/x.py\n'
                  '@@ -1 +1 @@\n-x\n+y\n')
    code, out, err = _main('--coverage', report, '--diff', diff)
    assert code == 1
    assert out == '', 'a refused report must print nothing to stdout'
    assert err.startswith('diff_coverage:') and err.strip() != 'diff_coverage:'


# --- added_lines: the unified-diff half --------------------------------------

def test_a_single_hunk_yields_the_added_line_numbers(tmp):
    del tmp
    diff = (
        'diff --git a/scripts/ci/x.py b/scripts/ci/x.py\n'
        'index 1111111..2222222 100644\n'
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -1,3 +1,4 @@\n'
        ' keep\n'
        '-drop\n'
        '+new one\n'
        '+new two\n'
        ' keep too\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {2, 3}}


def test_lines_from_several_files_are_kept_apart(tmp):
    del tmp
    diff = (
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -1 +1,2 @@\n'
        '-old\n'
        '+first\n'
        '+second\n'
        'diff --git a/scripts/ci/y.py b/scripts/ci/y.py\n'
        '--- a/scripts/ci/y.py\n'
        '+++ b/scripts/ci/y.py\n'
        '@@ -10 +10 @@\n'
        '+tenth\n')
    assert _module().added_lines(diff) == {
        'scripts/ci/x.py': {1, 2},
        'scripts/ci/y.py': {10}}


def test_context_lines_advance_the_new_side_line_counter(tmp):
    del tmp
    diff = (
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -2,4 +2,5 @@\n'
        ' context\n'
        ' context\n'
        '+added after two context lines\n'
        ' context\n'
        '+added after one more\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {4, 6}}


def test_a_deletion_only_hunk_adds_nothing(tmp):
    del tmp
    diff = (
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -5,2 +5,0 @@\n'
        '-gone one\n'
        '-gone two\n')
    assert _module().added_lines(diff) == {}


def test_a_deleted_file_contributes_nothing_but_does_not_poison_the_rest(tmp):
    del tmp
    diff = (
        'diff --git a/scripts/ci/gone.py b/scripts/ci/gone.py\n'
        'extended description line\n'
        '--- a/scripts/ci/gone.py\n'
        '+++ /dev/null\n'
        '@@ -1,2 +0,0 @@\n'
        '-gone\n'
        '-also gone\n'
        'diff --git a/scripts/ci/x.py b/scripts/ci/x.py\n'
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -1 +1 @@\n'
        '-old\n'
        '+new\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {1}}


def test_a_binary_diff_record_is_refused(tmp):
    del tmp
    _refuses(lambda: _module().added_lines(
        'diff --git a/logo.png b/logo.png\n'
        'Binary files a/logo.png and b/logo.png differ\n'),
        'binary diff record')


def test_crlf_line_endings_parse_like_lf(tmp):
    del tmp
    diff = ('--- a/scripts/ci/x.py\r\n'
            '+++ b/scripts/ci/x.py\r\n'
            '@@ -1,2 +1,3 @@\r\n'
            ' keep\r\n'
            '+added\r\n'
            ' keep too\r\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {2}}


def test_a_git_quoted_octal_path_is_decoded(tmp):
    del tmp
    diff = (
        'diff --git "a/scripts/ci/caf\\303\\251.py" '
        '"b/scripts/ci/caf\\303\\251.py"\n'
        '--- "a/scripts/ci/caf\\303\\251.py"\n'
        '+++ "b/scripts/ci/caf\\303\\251.py"\n'
        '@@ -1 +1 @@\n'
        '+touched\n')
    assert _module().added_lines(diff) == {'scripts/ci/café.py': {1}}


def test_a_removed_line_beginning_with_two_dashes_is_not_a_header(tmp):
    del tmp
    # Git renders a REMOVED line whose content begins `-- ` as `--- ...`,
    # the exact spelling of a file header. Inside a hunk it is content:
    # taking it for a header would clear the path and silently drop every
    # later hunk of the file.
    diff = (
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -1,4 +1,2 @@\n'
        '-   -- comment looking like a header\n'
        '-second removed\n'
        '+kept\n'
        '@@ -20 +20,2 @@\n'
        '+later hunk first line\n'
        '+later hunk second line\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {1, 20, 21}}


def test_hunk_line_counts_default_to_one(tmp):
    del tmp
    diff = (
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -3 +3,2 @@\n'
        '+first added\n'
        '+second added\n'
        '@@ -9,2 +9 @@\n'
        '-dropped a\n'
        '-dropped b\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {3, 4}}


def test_hunk_state_survives_a_deletion_only_hunk(tmp):
    del tmp
    diff = (
        '--- a/scripts/ci/x.py\n'
        '+++ b/scripts/ci/x.py\n'
        '@@ -5,3 +5,0 @@\n'
        '-a\n'
        '-b\n'
        '-c\n'
        '@@ -20 +20,2 @@\n'
        '+later first\n'
        '+later second\n')
    assert _module().added_lines(diff) == {'scripts/ci/x.py': {20, 21}}


# --- measure, _ranges, unmeasured_sources, render ----------------------------

def test_measure_intersects_the_diff_with_the_report(tmp):
    del tmp
    measured = {'scripts/ci/x.py': {1: 2, 2: 0, 5: 0}}
    added = {'scripts/ci/x.py': {1, 2, 5}, 'scripts/other.py': {9}}
    rows, covered, total = _module().measure(measured, added)
    assert rows == [('scripts/ci/x.py', 1, 3, [2, 5])]
    assert (covered, total) == (1, 3)


def test_lines_coverage_does_not_consider_executable_leave_the_denominator(tmp):
    del tmp
    # Blank lines, comments and docstrings carry no XML record; measure()
    # scores the added lines the report DOES name, and their absence from
    # the report never counts as a miss.
    measured = {'scripts/ci/x.py': {1: 1, 3: 0}}
    added = {'scripts/ci/x.py': {1, 2, 3, 4}}  # 2 and 4: blank/comment
    rows, covered, total = _module().measure(measured, added)
    assert rows == [('scripts/ci/x.py', 1, 2, [3])]
    assert (covered, total) == (1, 2)


def test_a_scripts_ci_python_file_absent_from_the_report_is_not_measured(tmp):
    del tmp
    measured = {'scripts/ci/x.py': {1: 1}}
    added = {'scripts/ci/x.py': {1},
             'scripts/ci/new_module.py': {1, 2},
             'tests/test_new_thing.py': {1}}
    unmeasured = _module().unmeasured_sources(measured, added)
    assert unmeasured == {'scripts/ci/new_module.py'}


def test_changes_under_tests_are_never_not_measured(tmp):
    del tmp
    module = _module()
    assert module.unmeasured_sources(
        {}, {'tests/test_thing.py': {1}, 'tests/_util.py': {2}}) == set()


def test_ranges_collapse_into_spans(tmp):
    del tmp
    assert _module()._ranges([3, 5, 6, 7, 8, 9]) == '3, 5-9'
    assert _module()._ranges([2]) == '2'
    assert _module()._ranges([]) == ''


def test_render_reports_the_percent_and_the_per_file_table(tmp):
    del tmp
    rows = [('scripts/ci/x.py', 1, 3, [2, 5])]
    body = _module().render(rows, 1, 3)
    assert body.startswith('### Coverage of this change\n')
    assert '**33.3%** of added lines covered (1/3).' in body
    assert '| File | Covered | Added | Missed lines |' in body
    assert '| `scripts/ci/x.py` | 1 | 3 | 2, 5 |' in body


def test_render_caps_the_percent_at_99_9_while_anything_is_missed(tmp):
    del tmp
    body = _module().render([('scripts/ci/x.py', 9999, 10000, [10000])],
                            9999, 10000)
    assert '**99.9%** of added lines covered (9999/10000).' in body


def test_render_says_full_coverage_straight(tmp):
    del tmp
    body = _module().render([('scripts/ci/x.py', 2, 2, [])], 2, 2)
    assert '**100.0%** of added lines covered (2/2).' in body
    assert 'Every added line was reached.' in body


def test_render_lists_the_not_measured_files_beside_the_table(tmp):
    del tmp
    rows = [('scripts/ci/x.py', 2, 2, [])]
    body = _module().render(rows, 2, 2, unmeasured={'scripts/ci/new.py'})
    assert 'did not measure these changed' in body
    assert '- `scripts/ci/new.py`' in body


def test_render_says_when_no_measured_lines_were_added(tmp):
    del tmp
    body = _module().render([], 0, 0)
    assert 'No measured lines were added' in body
    assert '### Coverage of this change' in body
    assert body.endswith('\n')


def test_render_names_the_not_measured_files_when_nothing_was_measured(tmp):
    del tmp
    body = _module().render([], 0, 0,
                            unmeasured={'scripts/ci/orphan.py'})
    assert 'Unmeasured changed source files:' in body
    assert '- `scripts/ci/orphan.py`' in body


def test_render_closes_with_the_tree_wide_floor_sentence(tmp):
    del tmp
    body = _module().render([('scripts/ci/x.py', 0, 1, [1])], 0, 1)
    assert 'the tree-wide total still holds its floor' in body
    # The closing sentence belongs to every outcome, not only the table.
    assert 'the tree-wide total still holds its floor' in _module().render(
        [], 0, 0)
    assert 'the tree-wide total still holds its floor' in _module().render(
        [], 0, 0, unmeasured={'scripts/ci/orphan.py'})


# --- main(): the contract the workflow step consumes -------------------------

def test_main_prints_the_report_and_exits_zero(tmp):
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', _lines((1, 1), (2, 0)))))
    diff = _write(tmp / 'patch.diff',
                  '--- a/scripts/ci/pr_gate.py\n'
                  '+++ b/scripts/ci/pr_gate.py\n'
                  '@@ -1,2 +1,2 @@\n'
                  ' first\n'
                  '-second\n'
                  '+second changed\n')
    code, out, err = _main('--coverage', report, '--diff', diff)
    assert code == 0, err
    assert out.startswith('### Coverage of this change\n')
    assert '| `scripts/ci/pr_gate.py` | 0 | 1 | 2 |' in out
    assert err == ''


def test_main_refuses_a_binary_diff_with_exit_one(tmp):
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', _lines((1, 1)))))
    diff = _write(tmp / 'patch.diff',
                  'diff --git a/logo.png b/logo.png\n'
                  'Binary files a/logo.png and b/logo.png differ\n')
    code, out, err = _main('--coverage', report, '--diff', diff)
    assert code == 1
    assert out == ''
    assert 'binary diff record' in err


def test_a_missing_coverage_file_exits_one_with_a_reason(tmp):
    code, out, err = _main('--coverage', tmp / 'absent.xml',
                           '--diff', tmp / 'absent.diff')
    assert code == 1
    assert out == ''
    assert err.startswith('diff_coverage:')
    # The reason names the path that could not be read.
    assert 'absent' in err


def test_the_main_entry_point_writes_stdout_and_maps_the_exit_code(tmp):
    report = _write(tmp / 'coverage.xml', _xml(
        ('scripts/ci/pr_gate.py', _lines((1, 1)))))
    diff = _write(tmp / 'patch.diff',
                  '--- a/scripts/ci/pr_gate.py\n'
                  '+++ b/scripts/ci/pr_gate.py\n'
                  '@@ -1 +1 @@\n'
                  '+only\n')
    done = subprocess.run(
        [sys.executable, str(SCRIPT), '--coverage', str(report),
         '--diff', str(diff)],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith('### Coverage of this change\n')
    assert '**100.0%** of added lines covered (1/1).' in done.stdout

    bad = _write(tmp / 'bad.xml', '<unclosed>')
    done = subprocess.run(
        [sys.executable, str(SCRIPT), '--coverage', str(bad),
         '--diff', str(diff)],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 1
    assert done.stdout == ''
    assert done.stderr.startswith('diff_coverage:')


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()),
                                  tmp_prefix='prdiffcoverage_'))
