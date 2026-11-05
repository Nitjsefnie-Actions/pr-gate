#!/usr/bin/env python3
"""Race assertions for pull-request gate write boundaries."""
from _prgate import (
    OPEN_FIRST, RESOLVED_FIRST, _RaceApi, _api, _assert_gate_message,
    _assert_no_writes, _execute, _gate_comment, _issue, _pull, _text_html,
    _valid_body, _valid_html, _write_sequence,
)


STATE_ERROR = (
    'pr gate failed: pull request state changed during analysis\n')
MERGED_ERROR = (
    'pr gate failed: pull request was merged during analysis\n')


def _assert_two_run_replay():
    api = _api(
        issues={},
        rendered=_valid_html(references=_text_html('none')))
    code, writes, output, error = _execute(api, _valid_body('none'))
    assert (code, output, error) == (
        2, 'commented\n',
        'pr gate: the pull request is inadmissible (exit 2): '
        'No checked issue is assigned to you.\n')
    assert _write_sequence(writes) == [
        ('POST', 'repos/owner/repo/issues/99/comments')]
    _assert_gate_message(
        writes[0], OPEN_FIRST, ['No checked issue is assigned to you.'])
    assert api.pull['state'] == 'open'

    api.issues = {'101': _issue('alice')}
    api.rendered = _valid_html()
    code, writes, output, error = _execute(api, _valid_body())
    assert (code, output, error) == (0, 'covered by claimed issue 101\n', '')
    assert _write_sequence(writes[1:]) == [
        ('PATCH', 'repos/owner/repo/issues/comments/100')]
    _assert_gate_message(writes[1], RESOLVED_FIRST)
    assert api.pull['state'] == 'open'
    assert len(api.comments) == 1


def _assert_open_resolved_human_close_aborts_comment():
    api = _RaceApi(
        transition='closed', number=2,
        pull=_pull(), issues={'101': _issue('alice')},
        comments=[_gate_comment()])
    code, writes, output, error = _execute(api, _valid_body())
    assert (code, output, error) == (1, '', STATE_ERROR)
    _assert_no_writes(writes)


def _assert_open_inadmissible_human_close_aborts_comment():
    api = _RaceApi(
        transition='closed', number=2,
        pull=_pull(), issues={},
        rendered=_valid_html(references=_text_html('none')))
    code, writes, output, error = _execute(api, _valid_body('none'))
    assert (code, output, error) == (1, '', STATE_ERROR)
    _assert_no_writes(writes)


def _assert_open_inadmissible_merge_aborts_comment():
    api = _RaceApi(
        transition='merged', number=2,
        pull=_pull(), issues={},
        rendered=_valid_html(references=_text_html('none')))
    code, writes, output, error = _execute(api, _valid_body('none'))
    assert (code, output, error) == (1, '', MERGED_ERROR)
    _assert_no_writes(writes)
