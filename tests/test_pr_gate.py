#!/usr/bin/env python3
"""Pull-request gate state transitions and write ordering."""
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _util  # noqa: E402
from _prgate import (  # noqa: E402
    BOT, CLOSED_FIRST, CLOSED_MARKER, MARKER, OPEN_FIRST, REOPEN_FIRST,
    RESOLVED_FIRST, TEMPLATE,
    _api, _assert_ci_recovery_note, _assert_gate_message, _assert_no_writes,
    _assert_script_error,
    _assert_script_runs_through_gh_on_path,
    _capture, _closed_event, _comment_body, _comment_page_fields, _execute,
    _execute_without_runtime_escape, _gate_comment, _gate_module,
    _html_body, _inline_marker_comment, _issue, _markdown_code_spans,
    _PaginationApi, _pull, _recorded_writes, _Response, _run_script,
    _runtime_error, _script_fixtures, _text_html, _valid_body, _valid_html,
    _write_gh_stub, _write_sequence, FakeApi,
)
from _prfootnotes import (  # noqa: E402
    NESTED_HEADING_HTML, NESTED_HEADING_NOTE)
from _prgate_message import (  # noqa: E402
    ATTEMPT_FIRST, REFUSED_FIRST)
from _prgate_race import (  # noqa: E402
    _assert_closed_admissible_reclose_aborts_state,
    _assert_closed_inadmissible_reclose_aborts,
    _assert_closer_race_aborts,
    _assert_open_closable_human_close_aborts_state,
    _assert_open_closable_merge_aborts_comment,
    _assert_open_resolved_human_close_aborts_comment,
    _assert_state_races_abort, _assert_two_run_replay,
)
from _prgate import ROOT  # noqa: E402


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


def test_inline_marker_mention_is_not_a_closed_gate_comment(tmp):
    del tmp
    api = _api(
        state='closed', comments=[_inline_marker_comment()],
        timeline=[_closed_event()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    _assert_no_writes(writes)
    assert api.pull['state'] == 'closed'


def test_inline_marker_mention_does_not_replace_a_new_comment(tmp):
    del tmp
    api = _api(
        comments=[_inline_marker_comment()],
        issues={'101': _issue('bob')})
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    _assert_gate_message(
        writes[0], CLOSED_FIRST, ['Issue `#101` is not assigned to you.'],
        closed=True)


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


def test_bodies_github_renders_away_are_reported_and_closed(tmp):
    del tmp
    for body, rendered in RENDERS_TO_NOTHING:
        code, writes, _output, _error = _execute(
            _api(issues={}, rendered=rendered), body)
        assert code == 0, body
        assert _write_sequence(writes) == [
            ('POST', 'repos/owner/repo/issues/99/comments'),
            ('PATCH', 'repos/owner/repo/pulls/99')], body
        _assert_gate_message(
            writes[0], CLOSED_FIRST,
            [*MISSING_SECTIONS, 'No checked issue is assigned to you.'],
            closed=True)


def test_a_body_of_only_template_comments_is_reported_and_closed(tmp):
    """Instruction comments render as padding after all headings are removed."""
    del tmp
    body = '\n\n'.join(re.findall(r'<!--.*?-->', TEMPLATE, re.DOTALL))
    code, writes, _output, _error = _execute(
        _api(issues={}, rendered='\n' * 9), body)
    assert code == 0
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    _assert_gate_message(
        writes[0], CLOSED_FIRST,
        [*MISSING_SECTIONS, 'Remove the template instruction comments.',
         'No checked issue is assigned to you.'], closed=True)


def test_retained_instruction_comment_closes(tmp):
    del tmp
    instruction = re.search(r'<!--.*?-->', TEMPLATE, re.DOTALL).group(0)
    body = _valid_body().replace(
        '- One change', f'- One change\n{instruction}')
    code, writes, _output, _error = _execute(_api(), body)
    assert code == 0
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    _assert_gate_message(
        writes[0], CLOSED_FIRST,
        ['Remove the template instruction comments.'], closed=True)


def test_a_bullet_spelled_with_a_star_is_read_as_a_bullet(tmp):
    """The reason set is pinned against a marker class, not one spelling.

    A gate comment growing an item spelled `* ` carries a bullet no
    code path asked for, and GitHub renders it exactly as a `- ` item.
    Collecting bullets by the `- ` spelling alone leaves that mutation
    of the comment green at every call site here.
    """
    del tmp
    instruction = re.search(r'<!--.*?-->', TEMPLATE, re.DOTALL).group(0)
    body = _valid_body().replace(
        '- One change', f'- One change\n{instruction}')
    _code, writes, _output, _error = _execute(_api(), body)
    method, path, payload = writes[0]
    strayed = (
        method, path,
        {**payload, 'body': payload['body'] + '* Stray bullet.\n'})
    try:
        _assert_gate_message(
            strayed, CLOSED_FIRST,
            ['Remove the template instruction comments.'], closed=True)
    except AssertionError:
        pass
    else:
        raise AssertionError('a bullet spelled "* " went unseen')


def test_the_bullet_matcher_decides_indent_marker_and_separator(tmp):
    """Every decision the matcher makes, on its own axis.

    The marker and separator alphabets are finite, so those two axes
    are exhausted. The indent is unbounded repetition, so it runs to
    the width this comment can render: a stray bullet is still a list
    item at the numbered item's content column of three plus
    CommonMark's three-space allowance, so six is the domain and seven
    is the near miss the table carries past it. A bound of seven or
    wider survives here, outside what the comment can render rather
    than merely further out. The separator is the only axis answering
    no, so its rows keep the rest from passing vacuously.
    """
    del tmp
    indents = [''.join(pad) for width in range(8)
               for pad in itertools.product(' \t', repeat=width)]
    failures = []
    for indent, marker, separator in itertools.product(
            indents, '-*+', (' ', '\t', '')):
        stray = f'{indent}{marker}{separator}Stray.'
        write = ('POST', 'url', {
            'body': '\n'.join((OPEN_FIRST, MARKER, stray))})
        try:
            _assert_gate_message(write, OPEN_FIRST)
        except AssertionError:
            seen = True
        else:
            seen = False
        if seen is not bool(separator):
            failures.append((stray, seen))
    assert failures == [], failures


def test_gate_closed_admissible_pull_is_commented_then_reopened(tmp):
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(writes[0], ATTEMPT_FIRST, closed=True)
    _assert_gate_message(writes[2], REOPEN_FIRST)
    assert writes[1][2] == {'state': 'open'}


def test_reopen_notice_names_the_push_that_releases_ci(tmp):
    """The reopen strands the CI it triggers, so it carries the recovery.

    A bot-authored reopen makes GitHub create the runs in an
    approval-required state instead of starting them, and the gate
    cannot release them. The reopen notice is the only comment the
    author meets at the stranded head, so it must name the push that
    releases the checks, and must not send the author to approve them.
    """
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    for write, first in (
            (writes[0], ATTEMPT_FIRST), (writes[2], REOPEN_FIRST)):
        _assert_gate_message(write, first, closed=(write is writes[0]))
        _assert_ci_recovery_note(_comment_body(write))


def test_gate_closed_inadmissible_pull_updates_comment_and_stays_closed(tmp):
    del tmp
    body = _valid_body('none')
    rendered = _valid_html(references=_text_html('none'))
    api = _api(
        state='closed', issues={}, comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()], rendered=rendered)
    code, writes, _output, _error = _execute(api, body)
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(
        writes[0], CLOSED_FIRST,
        ['No checked issue is assigned to you.'], closed=True)


def test_closing_notice_names_the_push_needed_after_the_reopen(tmp):
    """Fixing the body is not the end of it, and the close says so.

    The closing comment is where the author learns that a fix will bring
    a reopen, so it is where the reopen's cost is stated: the reopen is
    bot-authored, its CI is held, one more push is what releases the
    checks, and the merge can still demand the author's own close and
    reopen before it — the same recovery the reopen notice carries, read
    here while the pull request is still closed.
    """
    del tmp
    body = _valid_body('none')
    api = _api(
        state='closed', issues={}, comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()],
        rendered=_valid_html(references=_text_html('none')))
    code, writes, _output, _error = _execute(api, body)
    assert code == 0
    _assert_gate_message(
        writes[0], CLOSED_FIRST,
        ['No checked issue is assigned to you.'], closed=True)
    _assert_ci_recovery_note(_comment_body(writes[0]))


def test_resolved_notice_keeps_the_reopen_followup_conditionally(tmp):
    """The resolved notice replaces the reopen notice, so it re-states it.

    When the author edits the body of the now-open pull request, the
    gate patches its marker comment to the resolved notice, erasing the
    close-and-reopen guidance the reopen notice carried. The resolved
    notice therefore carries it too — conditionally, because the gate
    reaches this notice without knowing whether it closed and reopened
    this pull request earlier, and the phrasing must stay truthful when
    it never did.
    """
    del tmp
    api = _api(comments=[_gate_comment()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(writes[0], RESOLVED_FIRST)
    body = _comment_body(writes[0])
    text = ' '.join(body.split()).lower()
    for required in ('if the gate closed and reopened', 'close and reopen',
                     'before merging', 'unattributed'):
        assert required in text, (required, body)


def test_the_recovery_guard_rejects_contract_violating_bodies(tmp):
    """The guard's two arms are driven here, not assumed.

    The REQUIRED arm is the property arm: each term in it is load-bearing,
    and each is driven by being removed from the real reopen body in
    turn. The FORBIDDEN arm is a spelling set, not a property, and it is
    not exhaustive: its entries are driven by appending each phrasing the
    contract has been observed to break with to the real reopen body.
    A forbidden-spelling arm that no test exercises is not a control:
    six realistic violations of this contract passed the guard before
    this test existed, because the arm listed spellings instead of the
    behaviour. Each violation is appended to the real reopen body, and
    each required term is removed from it, so both arms are shown to
    fire; the correct body, and a correct body with an innocent
    sentence added, are shown to pass.
    """
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    body = _comment_body(writes[2])
    _assert_ci_recovery_note(body)
    _assert_ci_recovery_note(f'{body} The gate thanks you for reading this.')

    violations = (
        'You should approve the pending runs to release them.',
        'Go to the Actions tab and approve them.',
        'Ask a reviewer to approve the checks.',
        'The runs will start automatically in a moment.',
        'The checks will run by themselves.',
        'The runs kick off on their own.',
    )
    survived = []
    for violation in violations:
        try:
            _assert_ci_recovery_note(f'{body} {violation}')
        except AssertionError:
            continue
        survived.append(violation)
    assert survived == [], survived

    removals = (
        ('push', 'send it onward'),
        ('empty commit', 'a trivial change'),
        ('held', 'queued'),
        ('approval', 'sign-off'),
        ('github-actions[bot]', 'the automation account'),
        ('github_token', 'its own token'),
        ('close and reopen', 'reopen'),
        ('unattributed', 'unrecognized'),
        ('ruleset', 'policy'),
        ('green', 'passing'),
    )
    accepted = []
    rendered = ' '.join(body.split()).lower()
    for term, replacement in removals:
        mutated = rendered.replace(term, replacement)
        assert term in rendered and term not in mutated, (term, mutated)
        try:
            _assert_ci_recovery_note(mutated)
        except AssertionError:
            continue
        accepted.append(term)
    assert accepted == [], accepted


class _RefusedReopenApi(FakeApi):
    """The reopen PATCH answers with GitHub's validation refusal."""

    _DEFAULT_DATA = object()

    def __init__(self, status=422, data=_DEFAULT_DATA):
        super().__init__(
            pull=_pull('closed'), issues={'101': _issue('alice')},
            comments=[_gate_comment(closed=True)],
            timeline=[_closed_event()])
        self.refusal_status = status
        self.refusal_data = ({
            'message': 'Validation Failed',
            'errors': [{'resource': 'PullRequest', 'field': 'head',
                        'code': 'invalid'}]}
            if data is _RefusedReopenApi._DEFAULT_DATA else data)

    def request(self, method, endpoint, payload=None):
        if method == 'PATCH' and endpoint == 'repos/owner/repo/pulls/99':
            # Recorded like FakeApi records any write, but the refusal
            # applies none of the state change a 2xx PATCH would.
            self.calls.append((method, endpoint, payload))
            self.writes.append((method, endpoint, payload))
            return _Response(self.refusal_status, self.refusal_data)
        return super().request(method, endpoint, payload)


def test_write_failure_message_carries_the_response_body(tmp):
    """A refusal reason discarded from the error is a refusal unread.

    GitHub refuses a reopen with 422 and its reason travels only in the
    response body; the old message dropped it, so the stderr line and
    the refusal comment quoted the status alone. The body's `message`
    and `errors` are rendered into the error string (compact JSON,
    sorted keys, cut at a bound), and the status rides the error as
    data so the reopen path can split a 4xx refusal from a 5xx failure
    without parsing the message. A body with no content leaves the
    status-only message byte-identical.
    """
    del tmp
    gate = _gate_module()

    def raised(call):
        try:
            call()
        except gate._GateError as error:
            return error
        raise AssertionError('_GateError was not raised')

    error = raised(lambda: gate._write(
        _RefusedReopenApi(data={'message': 'y' * 500}),
        'PATCH', 'repos/owner/repo/pulls/99', {'state': 'open'}))
    prefix = 'GitHub returned 422 for repos/owner/repo/pulls/99: '
    assert str(error).startswith(prefix), error
    detail = str(error)[len(prefix):]
    assert detail.startswith('yyy'), detail
    assert len(detail) == gate._BODY_DETAIL_LIMIT, detail
    assert detail.endswith('...'), detail
    assert error.status == 422

    refused = raised(lambda: gate._write(
        _RefusedReopenApi(), 'PATCH', 'repos/owner/repo/pulls/99',
        {'state': 'open'}))
    assert str(refused) == (
        'GitHub returned 422 for repos/owner/repo/pulls/99: '
        'Validation Failed; '
        '{"code":"invalid","field":"head","resource":"PullRequest"}')
    assert refused.status == 422

    bare = raised(lambda: gate._write(
        _RefusedReopenApi(status=500, data=None),
        'PATCH', 'repos/owner/repo/pulls/99', {'state': 'open'}))
    assert str(bare) == (
        'GitHub returned 500 for repos/owner/repo/pulls/99')
    assert bare.status == 500


def test_reopen_refusal_reports_githubs_reason_and_recovery(tmp):
    """A refused reopen leaves an author-facing comment, not a status.

    GitHub refuses the reopen PATCH of a force-pushed, closed pull
    request with 422, and `gh pr reopen` by hand refuses the same way,
    so this comment is the only place the reason and the way out can
    reach the author. The gate writes the attempt comment first, still
    gate-owned, then rewrites the same comment with the refusal: it
    quotes GitHub's reason, names the force-push cause, gives both
    recovery paths, and keeps the marker pair, so a later edit still
    retries. The run still exits 1, and the refusal is prose — a
    bullet would read as a reasons list.
    """
    del tmp
    api = _RefusedReopenApi()
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == (
        'pr gate failed: GitHub returned 422 for '
        'repos/owner/repo/pulls/99: Validation Failed; '
        '{"code":"invalid","field":"head","resource":"PullRequest"}\n')
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(writes[0], ATTEMPT_FIRST, closed=True)
    _assert_ci_recovery_note(_comment_body(writes[0]))
    refusal = _assert_gate_message(writes[2], REFUSED_FIRST, closed=True)
    assert 'Validation Failed' in refusal, refusal
    assert 'force-pushed' in refusal, refusal
    assert 'maintainer' in refusal and 'reopen' in refusal, refusal
    assert 'fresh pull request' in refusal, refusal
    assert 'Editing the body again' in refusal, refusal
    assert api.pull['state'] == 'closed'


def test_reopen_500_failure_stays_commentless(tmp):
    """The refusal comment belongs to the 4xx arm only.

    A 5xx on the reopen PATCH is an interrupted retry, not a refusal:
    the gate exits 1 with the attempt comment still carrying the closed
    marker and posts nothing further. The recovery suite drives 500s
    but cannot tell a refusal write from a retry write, so this pins
    the write count the split must preserve.
    """
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()],
        fail={'PATCH repos/owner/repo/pulls/99'})
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == (
        'pr gate failed: GitHub returned 500 for '
        'repos/owner/repo/pulls/99\n')
    # The double records the failed PATCH itself, so the refusal control
    # is the comment writes: one attempt rewrite, and no refusal rewrite.
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    assert _write_sequence(
        [item for item in writes if item[1].endswith('/comments/7')]) == [
            ('PATCH', 'repos/owner/repo/issues/comments/7')]
    assert api.pull['state'] == 'closed'
    assert CLOSED_MARKER in api.comments[0]['body'].splitlines()


def test_attempt_and_success_reopen_texts_split_the_claim(tmp):
    """Only the post-PATCH text may say the reopen has landed.

    While the patch is still untried the comment must not claim it,
    because GitHub can refuse it and the last comment on the PR would
    then promise a reopen that never happened. The attempt text and
    the success text are driven through the real flow here: both carry
    the CI recovery contract, the success text states the reopen as
    accomplished, and the attempt text never carries that wording —
    the positive half is what makes that absence assertable.
    """
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    attempt, success = (_comment_body(writes[0]), _comment_body(writes[2]))
    for body in (attempt, success):
        _assert_ci_recovery_note(body)
    rendered_attempt = ' '.join(attempt.split())
    rendered_success = ' '.join(success.split())
    assert 'has been reopened' in rendered_success, rendered_success
    assert 'has been reopened' not in rendered_attempt, rendered_attempt

    refused = _RefusedReopenApi()
    code, writes, _output, _error = _execute(refused, _valid_body())
    assert code == 1
    refused_attempt = ' '.join(_comment_body(writes[0]).split())
    _assert_ci_recovery_note(_comment_body(writes[0]))
    assert 'has been reopened' not in refused_attempt, refused_attempt


def test_human_closed_pull_is_not_written(tmp):
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event('alice')])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    _assert_no_writes(writes)


def test_bot_timeline_without_closed_marker_is_not_gate_owned(tmp):
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment()],
        timeline=[_closed_event()])
    code, writes, _output, _error = _execute(api, _valid_body())
    assert code == 0
    _assert_no_writes(writes)


def test_unreadable_closed_timeline_fails_without_writes(tmp):
    del tmp
    api = _api(
        state='closed', comments=[_gate_comment(closed=True)],
        timeline=[_closed_event()], fail={'timeline'})
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    _assert_no_writes(writes)
    assert error == (
        'pr gate failed: GitHub returned 500 for '
        'repos/owner/repo/issues/99/timeline\n')


def test_state_changes_during_analysis_abort_writes(tmp):
    del tmp
    _assert_state_races_abort()


def test_latest_closer_change_during_analysis_aborts_reopen(tmp):
    del tmp
    _assert_closer_race_aborts()


def test_closed_inadmissible_human_reclose_aborts_comment_patch(_tmp):
    _assert_closed_inadmissible_reclose_aborts()


def test_closed_admissible_reclose_after_comment_aborts_reopen(_tmp):
    _assert_closed_admissible_reclose_aborts_state()


def test_open_admissible_human_close_aborts_resolved_comment_patch(_tmp):
    _assert_open_resolved_human_close_aborts_comment()


def test_open_closable_human_close_after_comment_landed_aborts_state(_tmp):
    _assert_open_closable_human_close_aborts_state()


def test_open_closable_merge_aborts_before_comment(_tmp):
    _assert_open_closable_merge_aborts_comment()


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


def test_failed_comment_write_prevents_state_change(tmp):
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
    assert api.pull['state'] == 'open'
    assert error == (
        'pr gate failed: GitHub returned 500 for '
        'repos/owner/repo/issues/99/comments\n')


def test_one_edit_self_heals_gate_closed_state(tmp):
    del tmp
    _assert_two_run_replay()


def test_unknown_section_name_cannot_inject_a_live_reference(tmp):
    del tmp
    name = 'x`#1'
    body = _valid_body() + f'\n## {name}\nUnknown.\n'
    rendered = _valid_html() + _html_body((name, _text_html('Unknown.')))
    code, writes, _output, _error = _execute(
        _api(rendered=rendered), body)
    assert code == 0
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    comment = _assert_gate_message(
        writes[0], CLOSED_FIRST,
        ['Section ``x`#1`` is not defined by the template.'], closed=True)
    spans = _markdown_code_spans(comment)
    for match in re.finditer(r'#1', comment):
        assert any(start <= match.start() < end for start, end in spans), (
            match.start(), spans, comment)


def test_a_nested_heading_comments_and_closes(tmp):
    del tmp
    rendered = NESTED_HEADING_HTML['footnote_section_in_heading']
    code, writes, _output, _error = _execute(
        _api(rendered=rendered), _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    _assert_gate_message(
        writes[0], CLOSED_FIRST,
        ['Section "Testing" is empty.', NESTED_HEADING_NOTE,
         'No checked issue is assigned to you.'], closed=True)


def test_a_nested_heading_alone_closes(tmp):
    del tmp
    rendered = NESTED_HEADING_HTML['empty_heading_in_div']
    code, writes, _output, _error = _execute(
        _api(rendered=rendered), _valid_body())
    assert code == 0
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    _assert_gate_message(
        writes[0], CLOSED_FIRST,
        [NESTED_HEADING_NOTE, 'No checked issue is assigned to you.'],
        closed=True)




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
        code, output, error = _capture(gate.main)
    assert code == 1
    assert output == ''
    assert error == "pr gate failed: 'REPO'\n"


def test_gh_stub_refuses_unmodelled_calls(tmp):
    directory, command = _write_gh_stub(tmp)
    fixtures = Path(tmp) / 'fixtures.json'
    fixtures.write_text('{}', encoding='utf-8')
    calls = Path(tmp) / 'calls.jsonl'
    calls.write_text('', encoding='utf-8')
    environment = {
        **os.environ,
        'PATH': f'{directory}{os.pathsep}{os.environ["PATH"]}',
        'STUB_FIXTURES': str(fixtures),
        'STUB_CALLS': str(calls),
    }
    result = subprocess.run(
        [str(command), 'api', 'repos/owner/repo/labels'],
        env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert 'unsupported' in result.stderr, result.stderr

    stub = directory / ('gh.py' if os.name == 'nt' else 'gh')
    result = subprocess.run(
        [sys.executable, str(stub), 'api', '--include', '-X', 'GET',
         'repos/owner/repo/issues/99/comments?per_page=100&page=1'],
        env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert 'unsupported' in result.stderr, result.stderr


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
