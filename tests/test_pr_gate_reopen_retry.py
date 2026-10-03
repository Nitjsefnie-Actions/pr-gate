#!/usr/bin/env python3
"""GitHub's stale-object reopen refusal takes exactly one bounded retry."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _util  # noqa: E402
from _prgate import (  # noqa: E402
    FakeApi, _Response, _assert_gate_message, _closed_event, _execute,
    _gate_comment, _gate_module, _issue, _pull, _valid_body, _write_sequence,
)
from _prgate_message import (  # noqa: E402
    REFUSED_FIRST, REOPEN_FIRST,
)


# Captured 2026-10-03 from live `PATCH /repos/<owner>/<repo>/pulls/<n>`
# responses in a throwaway private repository, quoted verbatim: a
# modelled error string is a claim about GitHub's behaviour, so these
# are the captured ones. Note the body's own `status` member is the
# string `"422"`, exactly as GitHub sends it.
STALE_422 = (
    '{"message":"Validation Failed","errors":[{"resource":"PullRequest",'
    '"code":"custom","field":"state","message":"state cannot be changed. '
    'The probe-branch branch was force-pushed or recreated."}],'
    '"documentation_url":"https://docs.github.com/rest/pulls/pulls'
    '#update-a-pull-request","status":"422"}')
CONTRAST_422 = (
    '{"message":"Validation Failed","errors":[{"message":"Proposed base '
    'branch \'does-not-exist\' was not found","resource":"PullRequest",'
    '"field":"base","code":"invalid"}],"documentation_url":"https://'
    'docs.github.com/rest/pulls/pulls#update-a-pull-request",'
    '"status":"422"}')

_LOST = object()


def _stale_body():
    return json.loads(STALE_422)


def _contrast_body():
    return json.loads(CONTRAST_422)


class _RefusingApi(FakeApi):
    """The reopen PATCH is refused by GitHub with a captured body.

    `first` is the (status, body) pair the first PATCH to the pull
    endpoint answers with, or None to let every PATCH succeed; `_LOST`
    raises the transport RuntimeError a dead pipe produces, before
    GitHub sees anything. `retry` is what the NEXT PATCH answers with —
    None lets it succeed, a pair refuses it, `_LOST` loses it — and is
    never consumed by a first PATCH. `race` mutates the repository in
    the window the first refusal opens, so the retry's ownership
    revalidation observes a merged or re-closed pull request. Every
    request is recorded in `sequence`, so a test can pin the retry
    PATCH landing after a fresh GET of the pull endpoint.
    """

    def __init__(self, first=None, retry=None, race=None):
        super().__init__(
            pull=_pull('closed'), issues={'101': _issue('alice')},
            comments=[_gate_comment(closed=True)],
            timeline=[_closed_event()])
        self.first = first
        self.retry = retry
        self.race = race
        self.sequence = []

    def _race_now(self):
        if self.race == 'merged':
            self.pull['merged'] = True
        elif self.race == 'reclosed':
            self.timeline.append(_closed_event('maintainer'))

    def request(self, method, endpoint, payload=None):
        self.sequence.append((method, endpoint))
        pull_patch = (
            method == 'PATCH' and endpoint == 'repos/owner/repo/pulls/99')
        if pull_patch and self.first is _LOST:
            raise RuntimeError('transport unavailable')
        if pull_patch:
            if self.first is not None:
                # Recorded like FakeApi records any write, but a
                # refusal applies none of the state change a 2xx
                # PATCH would; a landed PATCH falls through to
                # FakeApi, which records it once itself.
                self.calls.append((method, endpoint, payload))
                self.writes.append((method, endpoint, payload))
                refusal, self.first = self.first, None
                self._race_now()
                return _Response(*refusal)
            if self.retry is _LOST:
                raise RuntimeError('transport unavailable')
            if self.retry is not None:
                self.calls.append((method, endpoint, payload))
                self.writes.append((method, endpoint, payload))
                refusal, self.retry = self.retry, None
                return _Response(*refusal)
        return super().request(method, endpoint, payload)


def _pull_patch_positions(api):
    return [index for index, (method, endpoint) in enumerate(api.sequence)
            if method == 'PATCH' and endpoint == 'repos/owner/repo/pulls/99']


def _narrowed_gate():
    gate = _gate_module()
    assert gate is not None, 'scripts/ci/pr_gate.py is not implemented'
    return gate


def test_stale_refusal_is_retried_once_and_succeeds(tmp):
    """The stale 422 takes one retry, and a landed retry wins.

    The captured stale body names a force-pushed branch, so the gate
    re-reads the pull request (fresh head SHA and object version) and
    PATCHes the reopen once more. The retry succeeds, and the run
    continues down the first-try success path exactly as it stands:
    the comment is rewritten to the reopen text, `reopened` is printed,
    and the process exits 0. No refusal text appears anywhere in the
    comment, and the retry PATCH was issued after a fresh GET.
    """
    del tmp
    api = _RefusingApi(first=(422, _stale_body()))
    code, writes, output, _error = _execute(api, _valid_body())
    assert code == 0
    assert output == 'reopened\n'
    patches = _pull_patch_positions(api)
    assert len(patches) == 2, api.sequence
    between = api.sequence[patches[0] + 1:patches[1]]
    assert ('GET', 'repos/owner/repo/pulls/99') in between, api.sequence
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    reopen = _assert_gate_message(writes[3], REOPEN_FIRST)
    assert 'refused' not in reopen, reopen
    assert api.pull['state'] == 'open'


def test_stale_refusal_retried_once_still_refused_writes_the_refusal(tmp):
    """A retry that is refused again lands in the refusal comment.

    The wedge has no recoverable window on today's GitHub (measured
    2026-10-03: every reopen PATCH answers the stale 422 after a
    force-push while closed), so the retry is best effort and its
    refusal is the final state: exactly two PATCHes, the comment
    rewritten with the refusal and the way out, exit 1. The comment
    quotes GitHub's reason and never claims the reopen landed.
    """
    del tmp
    api = _RefusingApi(first=(422, _stale_body()),
                       retry=(422, _stale_body()))
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == (
        'pr gate failed: GitHub returned 422 for '
        'repos/owner/repo/pulls/99: Validation Failed; '
        '{"code":"custom","field":"state","message":"state cannot be '
        'changed. The probe-branch branch was force-pushed or '
        'recreated.","resource":"PullRequest"}\n')
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    refusal = _assert_gate_message(writes[3], REFUSED_FIRST, closed=True)
    assert 'GitHub\'s reason:' in refusal, refusal
    assert 'has been reopened' not in refusal, refusal
    assert api.pull['state'] == 'closed'


def test_stale_refusal_retry_with_a_different_refusal_takes_refusal_path(
        tmp):
    """Any refusal on the retry takes the refusal path, whatever shape.

    The retry is issued once; its failure is not re-classified for a
    second attempt. Here the retry is refused with the contrast 422
    (an invalid base), and the run ends in the refusal comment quoting
    that reason, with exactly two PATCHes and exit 1.
    """
    del tmp
    api = _RefusingApi(first=(422, _stale_body()),
                       retry=(422, _contrast_body()))
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert len(_pull_patch_positions(api)) == 2
    assert error == (
        'pr gate failed: GitHub returned 422 for '
        'repos/owner/repo/pulls/99: Validation Failed; '
        '{"code":"invalid","field":"base","message":"Proposed base '
        "branch 'does-not-exist' was not found\","
        '"resource":"PullRequest"}\n')
    refusal = _assert_gate_message(writes[3], REFUSED_FIRST, closed=True)
    assert "Proposed base branch 'does-not-exist' was not found" in refusal
    assert api.pull['state'] == 'closed'


def test_the_contrast_422_takes_no_retry(tmp):
    """A 422 that is not the stale shape is refused once, unretried.

    The contrast body — GitHub's 422 for a proposed base branch that
    does not exist — must not take the retry: classification reads the
    parsed body, and this body names field `base` with code `invalid`,
    not the stale `state` shape. Exactly one PATCH, the refusal comment
    quoting GitHub's raw reason, exit 1.
    """
    del tmp
    api = _RefusingApi(first=(422, _contrast_body()))
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert len(_pull_patch_positions(api)) == 1
    assert error == (
        'pr gate failed: GitHub returned 422 for '
        'repos/owner/repo/pulls/99: Validation Failed; '
        '{"code":"invalid","field":"base","message":"Proposed base '
        "branch 'does-not-exist' was not found\","
        '"resource":"PullRequest"}\n')
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    refusal = _assert_gate_message(writes[2], REFUSED_FIRST, closed=True)
    assert "Proposed base branch 'does-not-exist' was not found" in refusal
    assert api.pull['state'] == 'closed'


def test_a_403_refusal_takes_no_retry(tmp):
    """A non-422 refusal below 500 is not stale and is not retried.

    A 403 carries no stale shape in its body, so the retry must not
    fire: exactly one PATCH, the refusal comment quoting the reason,
    exit 1.
    """
    del tmp
    api = _RefusingApi(
        first=(403, {'message': 'Resource not accessible by integration'}))
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert len(_pull_patch_positions(api)) == 1
    assert error == (
        'pr gate failed: GitHub returned 403 for '
        'repos/owner/repo/pulls/99: Resource not accessible by '
        'integration\n')
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(writes[2], REFUSED_FIRST, closed=True)
    assert api.pull['state'] == 'closed'


def test_a_500_on_the_reopen_patch_stays_commentless(tmp):
    """A 5xx is an interrupted retry, not a refusal: raise, no comment.

    The status split is unchanged by the bounded retry: status >= 500
    re-raises before any classification, and nothing is written after
    the attempt comment.
    """
    del tmp
    api = _RefusingApi(first=(500, None))
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == (
        'pr gate failed: GitHub returned 500 for '
        'repos/owner/repo/pulls/99\n')
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    assert api.pull['state'] == 'closed'


def test_a_transport_failure_on_the_reopen_patch_stays_commentless(tmp):
    """A transport failure raises with `status` None and no comment."""
    del tmp
    api = _RefusingApi(first=_LOST)
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == 'pr gate failed: transport unavailable\n'
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    assert api.pull['state'] == 'closed'


def test_a_healthy_reopen_patch_is_never_retried(tmp):
    """The retry machinery cannot fire on the clean path.

    With the first PATCH succeeding there is exactly one PATCH to the
    pull endpoint, the comment lands on the reopen text, and the run
    exits 0 — the new acceptance class must not widen this.
    """
    del tmp
    api = _RefusingApi()
    code, writes, output, _error = _execute(api, _valid_body())
    assert code == 0
    assert output == 'reopened\n'
    assert len(_pull_patch_positions(api)) == 1
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99'),
        ('PATCH', 'repos/owner/repo/issues/comments/7')]
    _assert_gate_message(writes[2], REOPEN_FIRST)
    assert api.pull['state'] == 'open'


def test_a_merge_in_the_retry_window_aborts_before_the_retry_patch(tmp):
    """Ownership revalidation gates the retry: a merge aborts it.

    The stale 422 arrives, and the pull request is merged before the
    retry runs. The retry's ownership revalidation aborts with the
    usual `_GateError` before any retry PATCH — one PATCH total, no
    refusal comment after the attempt, exit 1.
    """
    del tmp
    api = _RefusingApi(first=(422, _stale_body()), race='merged')
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == (
        'pr gate failed: pull request was merged during analysis\n')
    # The double records the refused PATCH itself, so the two entries
    # are the attempt comment and that PATCH — and no comment rewrite
    # after it: the abort is an ownership failure, not a refusal.
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    assert len(_pull_patch_positions(api)) == 1
    assert api.pull['state'] == 'closed'


def test_a_reclose_in_the_retry_window_aborts_before_the_retry_patch(tmp):
    """A maintainer close in the window changes the closer: abort."""
    del tmp
    api = _RefusingApi(first=(422, _stale_body()), race='reclosed')
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert error == (
        'pr gate failed: pull request closer changed during analysis\n')
    assert _write_sequence(writes) == [
        ('PATCH', 'repos/owner/repo/issues/comments/7'),
        ('PATCH', 'repos/owner/repo/pulls/99')]
    assert len(_pull_patch_positions(api)) == 1
    assert api.pull['state'] == 'closed'


def test_a_transport_failure_on_the_retry_takes_the_refusal_path(tmp):
    """Any `_GateError` on the retry lands in the refusal comment.

    The contract bounds the retry, not its failure shapes: a retry
    whose transport dies is one `_GateError`, so the run revalidates
    ownership and writes the refusal comment quoting it. Exactly two
    PATCHes, exit 1, and the pull request is still closed — a lost
    response that had landed would have aborted at the revalidation
    in front of the comment instead.
    """
    del tmp
    api = _RefusingApi(first=(422, _stale_body()), retry=_LOST)
    code, writes, _output, error = _execute(api, _valid_body())
    assert code == 1
    assert len(_pull_patch_positions(api)) == 2
    assert error == 'pr gate failed: transport unavailable\n'
    # The lost retry never reaches the double's write list, so the
    # refusal rewrite is the third write, not the fourth.
    refusal = _assert_gate_message(writes[2], REFUSED_FIRST, closed=True)
    assert 'GitHub\'s reason:' in refusal, refusal
    assert api.pull['state'] == 'closed'


def test_the_stale_shape_predicate_drives_on_the_captured_bodies(tmp):
    """The classifier accepts the captured stale body alone.

    The captured stale shape is accepted; the captured contrast shape
    (field `base`, code `invalid`) and a non-422 refusal body are not.
    """
    del tmp
    gate = _narrowed_gate()
    assert gate._stale_state_refusal(_stale_body()) is True
    assert gate._stale_state_refusal(_contrast_body()) is False
    assert gate._stale_state_refusal(
        {'message': 'Resource not accessible by integration'}) is False


def test_the_predicate_accepts_the_second_captured_stale_variant(tmp):
    """The other captured stale body — a different branch name — matches.

    race1.txt and race2.txt captured the same shape naming
    `probe-branch-2`, so the phrase, not the branch name, is the
    discriminator.
    """
    del tmp
    gate = _narrowed_gate()
    body = _stale_body()
    body['errors'][0]['message'] = (
        'state cannot be changed. The probe-branch-2 branch was '
        'force-pushed or recreated.')
    assert gate._stale_state_refusal(body) is True


def test_the_stale_shape_predicate_is_exact_on_every_field(tmp):
    """Every member of the stale shape carries weight; perturb each one.

    The predicate requires `message == "Validation Failed"` and an
    `errors` entry with resource `PullRequest`, code `custom`, field
    `state` and a message containing the captured force-push phrase.
    Each pin kills the mutant that relaxes its member, and the
    matching-entry-among-several case pins that the shape is found in
    an errors list, not only at index 0.
    """
    del tmp
    gate = _narrowed_gate()

    def body_variant(**changes):
        data = _stale_body()
        data.update(changes)
        return data

    def error_variant(**changes):
        data = _stale_body()
        data['errors'][0].update(changes)
        return data

    negatives = [
        body_variant(message='Validation failed'),
        body_variant(message=None),
        body_variant(errors={}),
        body_variant(errors=[]),
        body_variant(errors=['no objects here']),
        error_variant(resource='Pull'),
        error_variant(resource='pullRequest'),
        error_variant(code='invalid'),
        error_variant(field='base'),
        error_variant(field='head'),
        error_variant(message='state cannot be changed.'),
        error_variant(message=None),
        error_variant(message=422),
    ]
    for body in negatives:
        assert gate._stale_state_refusal(body) is False, body

    moved = _stale_body()
    moved['errors'] = [_contrast_body()['errors'][0], moved['errors'][0]]
    assert gate._stale_state_refusal(moved) is True


def test_the_stale_shape_predicate_refuses_non_object_bodies(tmp):
    """Bodies that are not objects at all are never stale."""
    del tmp
    gate = _narrowed_gate()
    for body in (None, 'Validation Failed', [], 422, True):
        assert gate._stale_state_refusal(body) is False, body


if __name__ == '__main__':
    raise SystemExit(_util.runner(
        _util.collect(globals()), tmp_prefix='prreopenretry_'))
