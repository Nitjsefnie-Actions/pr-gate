#!/usr/bin/env python3
"""Pull-request gate state transitions and write ordering."""
import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _util  # noqa: E402
from _prgate import (  # noqa: E402
    BOT, CLI_FOOTER_HTML, OPEN_FIRST, MARKER,
    RESOLVED_FIRST, TEMPLATE,
    _api, _assert_gate_message, _assert_no_writes,
    _assert_script_error,
    _assert_script_runs_through_gh_on_path,
    _capture, _comment_body, _comment_page_fields, _execute,
    _execute_without_runtime_escape, _gate_comment, _gate_module,
    _html_body, _inline_marker_comment, _issue, _issue_html, _layout_body,
    _markdown_code_spans,
    _PaginationApi, _pull, _recorded_writes, _Response, _run_script,
    _runtime_error, _script_fixtures, _text_html, _valid_body, _valid_html,
    _write_gh_stub, _write_sequence, FakeApi,
)
from _prfootnotes import (  # noqa: E402
    NESTED_HEADING_HTML, NESTED_HEADING_NOTE)
from _prgate_race import (  # noqa: E402
    _assert_open_inadmissible_human_close_aborts_comment,
    _assert_open_inadmissible_merge_aborts_comment,
    _assert_open_resolved_human_close_aborts_comment,
    _assert_two_run_replay,
)


# Each body paired with the rendering GitHub's /markdown returned for it
# in GFM mode with this repository as the context: the empty string for
# some, newline padding for others.
RENDERS_TO_NOTHING = (
    (None, ''),
    ('', ''),
    ('   \n\t\n', ''),
    ('<!-- draft -->', ''),
    ('<!-- a -->\n\n<!-- b -->', '\n'),
    ('<!-- a -->\n\n<!-- b -->\n\n<!-- c -->', '\n\n'),
)
MISSING_SECTIONS = [
    f'Required section "{name}" is missing.' for name in (
        'Summary', 'Related Issues and Pull Requests', 'Changes',
        'Testing')]


def test_admissible_open_without_prior_comment_does_not_write(tmp):
    del tmp
    code, writes, output, _error = _execute(_api(), _valid_body())
    assert code == 0
    _assert_no_writes(writes)
    assert '101' in output, output


def test_admissible_open_resolves_a_prior_gate_comment(tmp):
    del tmp
    api = _api(comments=[_gate_comment()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(writes[0], RESOLVED_FIRST)


def test_closed_pull_is_neither_analysed_nor_written(tmp):
    """A closed pull request cannot merge, so the gate leaves it alone,
    whoever closed it and whatever an earlier gate comment says.
    """
    del tmp
    for comments in ([], [_gate_comment()], [_inline_marker_comment()]):
        api = _api(state='closed', comments=comments,
                   fail={'comments', 'markdown'})
        code, writes, output, _error = _execute(api, _valid_body())
        assert (code, output) == (0, 'nothing to do\n'), comments
        _assert_no_writes(writes)
        assert api.calls == [('GET', 'repos/owner/repo/pulls/99', None)]


def test_inline_marker_mention_does_not_replace_a_new_comment(tmp):
    del tmp
    api = _api(
        comments=[_inline_marker_comment()],
        issues={'101': _issue('bob')})
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 2
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    _assert_gate_message(
        writes[0], OPEN_FIRST, ['Issue `#101` is not assigned to you.'])


def test_contributor_marker_comment_is_not_selected(tmp):
    del tmp
    comment = {
        'id': 55,
        'user': {'login': 'alice'},
        'body': f'contributor note\n{MARKER}',
    }
    code, writes, _output, _error = _execute(
        _api(comments=[comment]), _valid_body())
    assert code == 0
    _assert_no_writes(writes)


def test_earliest_bot_marker_comment_is_selected(tmp):
    del tmp
    later = {
        'id': 8,
        'user': {'login': BOT},
        'body': f'later gate message\n{MARKER}',
    }
    code, writes, _output, _error = _execute(
        _api(comments=[_gate_comment(), later]), _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7')]


def test_bodies_github_renders_away_are_reported(tmp):
    del tmp
    for body, rendered in RENDERS_TO_NOTHING:
        code, writes, _output, _error = _execute(
            _api(issues={}, rendered=rendered), body)
        assert code == 2, body
        assert _write_sequence(writes) == [
            ('POST', 'repos/owner/repo/issues/99/comments')], body
        _assert_gate_message(
            writes[0], OPEN_FIRST,
            [*MISSING_SECTIONS, 'No checked issue is assigned to you.'])


def test_a_body_of_only_template_comments_is_reported(tmp):
    """Instruction comments render as padding after all headings are removed."""
    del tmp
    body = '\n\n'.join(re.findall(r'<!--.*?-->', TEMPLATE, re.DOTALL))
    code, writes, _output, _error = _execute(
        _api(issues={}, rendered='\n' * 9), body)
    assert code == 2
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    _assert_gate_message(
        writes[0], OPEN_FIRST,
        [*MISSING_SECTIONS, 'Remove the template instruction comments.',
         'No checked issue is assigned to you.'])


def test_retained_instruction_comment_is_reported(tmp):
    del tmp
    instruction = re.search(r'<!--.*?-->', TEMPLATE, re.DOTALL).group(0)
    body = _valid_body().replace(
        '- One change', f'- One change\n{instruction}')
    code, writes, _output, _error = _execute(_api(), body)
    assert code == 2
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    _assert_gate_message(
        writes[0], OPEN_FIRST,
        ['Remove the template instruction comments.'])


def test_refusal_exits_two_and_names_the_reasons_on_stderr(tmp):
    """An inadmissible pull request fails the gate's own check.

    Exit 2 is what a ruleset requiring the check reads as a failure, and
    it stays distinct from the 1 an analysis or API failure returns. The
    stderr line states the observation, with the same defect reasons the
    gate comment carries, verbatim; the pull request stays open.
    """
    del tmp
    api = _api(issues={})
    code, writes, output, error = _execute(api, _valid_body())
    assert code == 2
    assert output == 'commented\n'
    assert error == (
        'pr gate: the pull request is inadmissible (exit 2): '
        'No checked issue is assigned to you.\n')
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    assert api.pull['state'] == 'open'


def test_refusal_comment_names_the_failing_check_and_no_close(tmp):
    del tmp
    code, writes, _output, _error = _execute(_api(issues={}), _valid_body())
    assert code == 2
    text = ' '.join(_comment_body(writes[0]).split())
    assert ("Until then this gate's check fails, and a ruleset that "
            'requires the check holds the merge. The gate re-checks every '
            'edit and push; there is no need to open a second pull '
            'request.') in text, text
    for phrase in ('reopen', 'closing this', 'stays closed'):
        assert phrase not in text.lower(), (phrase, text)


def test_refusal_joins_every_reason_on_one_stderr_line(tmp):
    del tmp
    rendered = NESTED_HEADING_HTML['footnote_section_in_heading']
    code, writes, output, error = _execute(
        _api(rendered=rendered), _valid_body())
    assert code == 2
    assert output == 'commented\n'
    expected = '; '.join([
        'Section "Testing" is empty.', NESTED_HEADING_NOTE,
        'No checked issue is assigned to you.'])
    assert error == (
        'pr gate: the pull request is inadmissible (exit 2): ' + expected + '\n')
    assert error.count('\n') == 1
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]


def test_refusal_carries_the_overflow_reason_verbatim(tmp):
    """A reason is long by design; the stderr line must not rewrap it.

    The joined line is the run-log copy of the gate comment's reasons
    block, so each reason travels whole: no truncation, no rewrap, and
    the overflow sentence rides beside a layout reason on one line.
    """
    del tmp
    numbers = list(range(101, 127))
    body = _layout_body(
        ('Related Issues and Pull Requests',
         ' '.join(f'Fixes #{number}' for number in numbers)),
        ('Changes', '- One change'),
        ('Testing', 'Ran the suite.'))
    rendered = _html_body(
        ('Related Issues and Pull Requests', ' '.join(
            f'Fixes {_issue_html(number)}' for number in numbers)),
        ('Changes', _text_html('One change')),
        ('Testing', _text_html('Ran the suite.')))
    issues = {str(number): _issue('bob') for number in numbers}
    code, writes, _output, error = _execute(
        _api(issues=issues, rendered=rendered), body)
    assert code == 2
    assert error == (
        'pr gate: the pull request is inadmissible (exit 2): '
        'Required section "Summary" is missing.; '
        'This body names more than 20 issue references, so only the '
        'first 20 were checked.\n')
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]


def test_resolved_notice_is_the_first_line_and_marker_only(tmp):
    """With no close there is no reopen follow-up for the resolved notice
    to carry: it says the pull request passes, and nothing more.
    """
    del tmp
    api = _api(comments=[_gate_comment()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    assert _comment_body(writes[0]) == f'{RESOLVED_FIRST}\n{MARKER}\n'


class _RefusedCommentApi(FakeApi):
    """The comment POST answers with the given refusal."""

    _DEFAULT_DATA = object()

    def __init__(self, status=422, data=_DEFAULT_DATA):
        super().__init__(pull=_pull(), issues={})
        self.refusal_status = status
        self.refusal_data = ({
            'message': 'Validation Failed',
            'errors': [{'resource': 'IssueComment', 'field': 'body',
                        'code': 'invalid'}]}
            if data is _RefusedCommentApi._DEFAULT_DATA else data)

    def request(self, method, endpoint, payload=None):
        if (method == 'POST'
                and endpoint == 'repos/owner/repo/issues/99/comments'):
            self.calls.append((method, endpoint, payload))
            self.writes.append((method, endpoint, payload))
            return _Response(self.refusal_status, self.refusal_data)
        return super().request(method, endpoint, payload)


def test_write_failure_message_carries_the_response_body(tmp):
    """A refusal reason discarded from the error is a refusal unread.

    GitHub's reason for refusing a write travels only in the response
    body, so the body's `message` and `errors` are rendered into the
    error string (compact JSON, sorted keys, cut at a bound). A body
    with no content leaves the status-only message byte-identical, and
    the run that hits the refusal exits 1 with that line on stderr.
    """
    del tmp
    gate = _gate_module()
    endpoint = 'repos/owner/repo/issues/99/comments'

    def raised(call):
        try:
            call()
        except gate._GateError as error:
            return error
        raise AssertionError('_GateError was not raised')

    error = raised(lambda: gate._write(
        _RefusedCommentApi(data={'message': 'y' * 500}),
        'POST', endpoint, {'body': 'x'}))
    prefix = f'GitHub returned 422 for {endpoint}: '
    assert str(error).startswith(prefix), error
    detail = str(error)[len(prefix):]
    assert detail.startswith('yyy'), detail
    assert len(detail) == gate._BODY_DETAIL_LIMIT, detail
    assert detail.endswith('...'), detail

    expected = (
        f'GitHub returned 422 for {endpoint}: Validation Failed; '
        '{"code":"invalid","field":"body","resource":"IssueComment"}')
    refused = raised(lambda: gate._write(
        _RefusedCommentApi(), 'POST', endpoint, {'body': 'x'}))
    assert str(refused) == expected

    bare = raised(lambda: gate._write(
        _RefusedCommentApi(status=500, data=None),
        'POST', endpoint, {'body': 'x'}))
    assert str(bare) == f'GitHub returned 500 for {endpoint}'

    code, _writes, _output, run_error = _execute(
        _RefusedCommentApi(), _valid_body())
    assert (code, run_error) == (1, f'pr gate failed: {expected}\n')


def test_open_admissible_human_close_aborts_resolved_comment_patch(_tmp):
    _assert_open_resolved_human_close_aborts_comment()


def test_open_inadmissible_human_close_aborts_the_comment(_tmp):
    _assert_open_inadmissible_human_close_aborts_comment()


def test_open_inadmissible_merge_aborts_before_comment(_tmp):
    _assert_open_inadmissible_merge_aborts_comment()


def test_merged_pull_returns_before_comments_or_render(tmp):
    del tmp
    api = _api(merged=True, fail={'comments', 'markdown'})
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    _assert_no_writes(writes)
    assert api.calls == [
        ('GET', 'repos/owner/repo/pulls/99', None)]


def test_unusable_render_fails_without_writes(tmp):
    del tmp
    cases = (
        (
            _api(fail={'markdown'}),
            'pr gate failed: GitHub returned 500 for markdown\n',
        ),
        (
            _api(rendered='<h2>Summary</h2><p'),
            'pr gate failed: could not analyze rendered body: '
            'rendered HTML is structurally incomplete\n',
        ),
    )
    for api, expected in cases:
        code, writes, _output, error = _execute(api, _valid_body())
        assert code == 1
        _assert_no_writes(writes)
        assert error == expected


def test_failed_comment_write_fails_the_run(tmp):
    del tmp
    body = _valid_body('none')
    rendered = _valid_html(references=_text_html('none'))
    api = _api(
        issues={}, rendered=rendered,
        fail={'POST repos/owner/repo/issues/99/comments'})
    code, writes, _output, error = _execute(api, body)
    assert code == 1
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    assert error == (
        'pr gate failed: GitHub returned 500 for '
        'repos/owner/repo/issues/99/comments\n')


def test_one_fixing_edit_resolves_the_refusal(tmp):
    del tmp
    _assert_two_run_replay()


def test_unknown_section_name_cannot_inject_a_live_reference(tmp):
    del tmp
    name = 'x`#1'
    body = _valid_body() + f'\n## {name}\nUnknown.\n'
    rendered = _valid_html() + _html_body((name, _text_html('Unknown.')))
    code, writes, _output, _error = _execute(
        _api(rendered=rendered), body)
    assert code == 2
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    comment = _assert_gate_message(
        writes[0], OPEN_FIRST,
        ['Section ``x`#1`` is not defined by the template.'])
    spans = _markdown_code_spans(comment)
    for match in re.finditer(r'#1', comment):
        assert any(start <= match.start() < end for start, end in spans), (
            match.start(), spans, comment)


def test_a_nested_heading_is_reported(tmp):
    del tmp
    rendered = NESTED_HEADING_HTML['footnote_section_in_heading']
    code, writes, _output, _error = _execute(
        _api(rendered=rendered), _valid_body())
    assert code == 2
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    _assert_gate_message(
        writes[0], OPEN_FIRST,
        ['Section "Testing" is empty.', NESTED_HEADING_NOTE,
         'No checked issue is assigned to you.'])


def test_a_nested_heading_alone_is_reported(tmp):
    del tmp
    rendered = NESTED_HEADING_HTML['empty_heading_in_div']
    code, writes, _output, _error = _execute(
        _api(rendered=rendered), _valid_body())
    assert code == 2
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    _assert_gate_message(
        writes[0], OPEN_FIRST,
        [NESTED_HEADING_NOTE, 'No checked issue is assigned to you.'])


def test_script_runs_through_gh_on_path(tmp):
    _assert_script_runs_through_gh_on_path(tmp)


def test_gh_absent_from_path_is_reported(tmp):
    empty = Path(tmp) / 'empty'
    empty.mkdir()
    gate = _gate_module()
    with mock.patch.dict(os.environ, {'PATH': str(empty)}):
        api = gate.GhApi()
    assert api.gh is None
    assert _runtime_error(
        lambda: api.request('GET', 'repos/owner/repo/pulls/99')) == (
            'gh was not found on PATH')


def test_gh_oserror_is_reported(tmp):
    target = Path(tmp) / 'not-executable'
    target.write_text('not executable', encoding='utf-8')
    target.chmod(0o644)
    api = _gate_module().GhApi()
    api.gh = str(target)
    error = _runtime_error(
        lambda: api.request('GET', 'repos/owner/repo/pulls/99'))
    assert error.startswith('could not run gh: '), error


def test_invalid_gh_status_line_is_reported(tmp):
    _assert_script_error(
        tmp, _script_fixtures(bad_status=True),
        'could not read gh response: no HTTP status in output')


def test_non_json_gh_body_is_reported(tmp):
    _assert_script_error(
        tmp, _script_fixtures(non_json=True),
        'could not parse gh response body')


def test_gh_response_requires_a_header_body_separator(tmp):
    _assert_script_error(
        tmp, _script_fixtures(no_separator=True),
        'could not read gh response headers')


def test_gh_response_rejects_an_unknown_media_type(tmp):
    _assert_script_error(
        tmp, _script_fixtures(unsupported_media=True),
        'unsupported gh response media type: application/octet-stream')


def test_gh_response_rejects_duplicate_content_type(tmp):
    _assert_script_error(
        tmp, _script_fixtures(duplicate_content_type=True),
        'duplicate gh response header: content-type')


def test_reasonless_status_line_is_accepted(tmp):
    result, calls = _run_script(tmp, _script_fixtures(no_reason=True))
    assert (result.returncode, _recorded_writes(calls)) == (0, [])


def test_paginated_page_must_be_a_list(tmp):
    del tmp
    gate = _gate_module()
    api = _PaginationApi([gate.Response(200, {'items': []})])
    error = _runtime_error(
        lambda: gate.GhApi.paginate(api, 'repos/x/issues/1/comments'))
    assert error == 'paginated gh response is not a list'


def test_pagination_short_page_uses_safe_fields(tmp):
    del tmp
    gate = _gate_module()
    api = _PaginationApi([gate.Response(200, [1, 2])])
    response = gate.GhApi.paginate(api, 'repos/x/issues/1/comments')
    assert response == gate.Response(200, [1, 2])
    assert api.calls == [
        ('GET', 'repos/x/issues/1/comments', None,
         ('per_page=100', 'page=1'))]


def test_executable_pagination_advances_page_fields(tmp):
    comments = [{} for _index in range(201)]
    result, calls = _run_script(
        tmp, _script_fixtures(comments=comments))
    assert result.returncode == 0, (result.stdout, result.stderr, calls)
    assert _comment_page_fields(calls) == ['page=1', 'page=2', 'page=3']


def test_non_200_second_page_fails_without_writes(tmp):
    _assert_script_error(
        tmp, _script_fixtures(comments=[{}] * 150, fail_page=2),
        'GitHub returned 500 for repos/owner/repo/issues/99/comments')


def test_pagination_stops_after_fifty_full_pages(tmp):
    del tmp
    gate = _gate_module()
    pages = [gate.Response(200, list(range(100))) for _ in range(51)]
    api = _PaginationApi(pages)
    error = _runtime_error(
        lambda: gate.GhApi.paginate(api, 'repos/x/issues/1/comments'))
    assert error == 'gh pagination exceeded 50 pages'
    assert len(api.calls) == 50
    assert api.calls[0][3][-1] == 'page=1'
    assert api.calls[-1][3][-1] == 'page=50'


def test_pagination_stops_after_three_full_commit_pages(tmp):
    del tmp
    gate = _gate_module()
    assert gate is not None, 'scripts/ci/pr_gate.py is not implemented'
    pages = [gate.Response(200, list(range(100))) for _ in range(3)]
    api = _PaginationApi(pages)
    error = _runtime_error(
        lambda: gate.GhApi.paginate(api, 'repos/x/pulls/1/commits'))
    assert error == 'gh pagination exceeded 3 commit pages'
    assert len(api.calls) == 3
    assert api.calls[0][3][-1] == 'page=1'
    assert api.calls[-1][3][-1] == 'page=3'


def test_paginate_runtime_error_is_reported_by_run(tmp):
    del tmp
    api = _api(paginate_error='boom')
    code, writes, _output, error = _execute_without_runtime_escape(
        api, _valid_body())
    assert code == 1
    _assert_no_writes(writes)
    assert error == 'pr gate failed: boom\n'


def test_runtime_error_is_reported_by_run(tmp):
    del tmp
    api = _api()

    def fail_request(_method, _endpoint, _payload=None):
        raise RuntimeError('transport unavailable')

    api.request = fail_request
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    _assert_no_writes(writes)
    assert error == 'pr gate failed: transport unavailable\n'


def test_pull_response_must_be_an_object(tmp):
    del tmp
    gate = _gate_module()
    api = _api()
    request = api.request

    def wrong_pull(method, endpoint, payload=None):
        if method == 'GET' and endpoint.endswith('/pulls/99'):
            return gate.Response(200, [])
        return request(method, endpoint, payload)

    api.request = wrong_pull
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    _assert_no_writes(writes)
    assert error == 'pr gate failed: pull request response is not an object\n'


def test_markdown_response_must_be_text(tmp):
    del tmp
    code, writes, _output, error = _execute(
        _api(rendered={'html': 'not text'}), _valid_body())
    assert code == 1
    _assert_no_writes(writes)
    assert error == 'pr gate failed: markdown response is not text\n'


def test_main_reports_missing_repo(tmp):
    del tmp
    gate = _gate_module()
    with mock.patch.dict(os.environ):
        os.environ.pop('REPO', None)
        os.environ.pop('PR_GATE_REPO', None)
        code, output, error = _capture(gate.main)
    assert code == 1
    assert output == ''
    assert error == "pr gate failed: 'REPO'\n"


def _run_main(tmp, environment, fixtures):
    gate = _gate_module()
    assert gate is not None
    directory, _command = _write_gh_stub(tmp)
    fixtures_path = tmp / 'fixtures.json'
    fixtures_path.write_text(json.dumps(fixtures), encoding='utf-8')
    calls_path = tmp / 'calls.jsonl'
    calls_path.write_text('', encoding='utf-8')
    with mock.patch.dict(os.environ):
        for name in ('REPO', 'PR', 'ACTOR', 'REQUIRE_COMMIT_ATTRIBUTION', 'TEMPLATE_PATH'):
            os.environ.pop(name, None)
            os.environ.pop(f'PR_GATE_{name}', None)
        os.environ.update({
            'PATH': f'{directory}{os.pathsep}{os.environ["PATH"]}',
            'GH_TOKEN': 'stub', 'STUB_FIXTURES': str(fixtures_path),
            'STUB_CALLS': str(calls_path), **environment})
        result = _capture(gate.main)
    calls = [json.loads(line) for line in calls_path.read_text(
        encoding='utf-8').splitlines()]
    return result, calls


def test_main_prefers_prefixed_environment(tmp):
    fixtures = _script_fixtures()
    fixtures.update(repository='owner/new', pull_number='42',
                    template_path='policy/new.md',
                    rendered=_valid_html(repo='owner/new') + CLI_FOOTER_HTML)
    result, calls = _run_main(tmp, {
        'REPO': 'owner/legacy', 'PR_GATE_REPO': 'owner/new',
        'PR': '98', 'PR_GATE_PR': '42',
        'ACTOR': 'bob', 'PR_GATE_ACTOR': 'alice',
        'REQUIRE_COMMIT_ATTRIBUTION': 'false',
        'PR_GATE_REQUIRE_COMMIT_ATTRIBUTION': ' TrUe ',
        'TEMPLATE_PATH': 'policy/legacy.md',
        'PR_GATE_TEMPLATE_PATH': 'policy/new.md',
    }, fixtures)
    assert result[0] == 0, (result, calls)
    endpoints = [call['argv'][4] for call in calls]
    assert endpoints[0] == 'repos/owner/new/pulls/42', endpoints
    assert any(endpoint.startswith('repos/owner/new/contents/policy/new.md?ref=')
               for endpoint in endpoints), endpoints
    assert 'repos/owner/new/pulls/42/commits' in endpoints, endpoints
    assert _recorded_writes(calls) == []


def test_main_falls_back_to_legacy_environment(tmp):
    fixtures = _script_fixtures()
    fixtures.update(repository='owner/legacy', pull_number='43',
                    template_path='policy/legacy.md',
                    rendered=_valid_html(repo='owner/legacy') + CLI_FOOTER_HTML)
    result, calls = _run_main(tmp, {
        'REPO': 'owner/legacy', 'PR': '43', 'ACTOR': 'alice',
        'REQUIRE_COMMIT_ATTRIBUTION': 'true',
        'TEMPLATE_PATH': 'policy/legacy.md',
    }, fixtures)
    assert result[0] == 0, (result, calls)
    endpoints = [call['argv'][4] for call in calls]
    assert endpoints[0] == 'repos/owner/legacy/pulls/43', endpoints
    assert any(endpoint.startswith('repos/owner/legacy/contents/policy/legacy.md?ref=')
               for endpoint in endpoints), endpoints
    assert 'repos/owner/legacy/pulls/43/commits' in endpoints, endpoints
    assert _recorded_writes(calls) == []


def test_main_optional_environment_defaults_and_empty_attribution(tmp):
    cases = ({}, {'REQUIRE_COMMIT_ATTRIBUTION': ''}, {
        'REQUIRE_COMMIT_ATTRIBUTION': 'true',
        'PR_GATE_REQUIRE_COMMIT_ATTRIBUTION': ''})
    for index, optional in enumerate(cases):
        directory = tmp / str(index)
        directory.mkdir()
        result, calls = _run_main(directory, {
            'PR_GATE_REPO': 'owner/repo', 'PR_GATE_PR': '99',
            'PR_GATE_ACTOR': 'alice', 'PR_GATE_CONVENTIONAL_COMMITS': 'false',
            **optional}, _script_fixtures())
        assert result[0] == 0, (result, calls)
        endpoints = [call['argv'][4] for call in calls]
        assert any(endpoint.startswith(
            'repos/owner/repo/contents/.github/PULL_REQUEST_TEMPLATE.md?ref=')
                   for endpoint in endpoints), endpoints
        assert 'repos/owner/repo/pulls/99/commits' not in endpoints, endpoints
        assert _recorded_writes(calls) == []


def test_main_empty_template_path_is_not_replaced_by_fallback_or_default(tmp):
    cases = ({'TEMPLATE_PATH': ''}, {
        'TEMPLATE_PATH': '.github/PULL_REQUEST_TEMPLATE.md',
        'PR_GATE_TEMPLATE_PATH': ''})
    for index, optional in enumerate(cases):
        directory = tmp / str(index)
        directory.mkdir()
        result, calls = _run_main(directory, {
            'PR_GATE_REPO': 'owner/repo', 'PR_GATE_PR': '99',
            'PR_GATE_ACTOR': 'alice', **optional,
        }, _script_fixtures())
        assert result == (1, '', 'pr gate failed: template path must be a '
                          'relative repository file path\n'), (result, calls)
        assert _recorded_writes(calls) == []


def test_unparsable_gh_failure_reports_one_stderr_line(tmp):
    fixtures = _script_fixtures(unparsable=True)
    result, _calls = _run_script(tmp, fixtures)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr.splitlines() == [
        'pr gate failed: could not read gh response: first gh failure line '
        'second gh failure line'], result.stderr


def main():
    return _util.runner(
        _util.collect(globals()), tmp_prefix='prgate_')


if __name__ == '__main__':
    raise SystemExit(main())
