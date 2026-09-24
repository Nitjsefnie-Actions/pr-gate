#!/usr/bin/env python3
"""The gate comment's fixed strings and the assertions over one it posts."""
import re


BOT = 'github-actions[bot]'
MARKER = '<!-- pr-gate -->'
CLOSED_MARKER = '<!-- pr-gate: closed -->'
OPEN_FIRST = (
    '@alice — this pull request needs changes before it can be reviewed.')
CLOSED_FIRST = (
    '@alice — closing this automatically; it is recoverable, read on.')
RESOLVED_FIRST = (
    '@alice — every condition now passes; nothing further is needed '
    'from you.')
REOPEN_FIRST = (
    '@alice — the body now names a claimed issue and matches the pull '
    'request')
REASONS_END = (
    'Fix every item above, including these two repository requirements:')


def _comment_body(write):
    return write[2]['body']


def _assert_ci_recovery_note(body):
    """The reopen is bot-authored, so its CI runs are held; say so, and
    name the push that releases them.

    Two arms, and only the first is a property. The REQUIRED arm is
    load-bearing: whatever else the notice says, it must tell the author
    to push, must say an empty commit is acceptable, and must name the
    bot, its token and the held-for-approval reason — dropping any one
    of those from the message fails here, which
    test_the_recovery_guard_rejects_contract_violating_bodies drives
    term by term.

    The FORBIDDEN arm is a spelling set, not a property, and it is NOT
    exhaustive. It lists the phrasings this contract has been observed to
    break with — instructions to approve the held runs, and promises
    that the runs start on their own — and a violation phrased outside
    this set passes. Read it as a cheap net whose coverage is the
    evidenced set, never as the control that proves the text honest.

    The comparison is over rendered text, so single newlines count as
    the spaces GitHub renders them as; only an unrendered break, a hard
    paragraph edge, would change a word.
    """
    text = ' '.join(body.split()).lower()
    for required in ('push', 'empty commit', 'held', 'approval',
                     'github-actions[bot]', 'github_token'):
        assert required in text, (required, body)
    # Not exhaustive by construction: a new spelling is a new entry.
    for forbidden in ('approve the run', 'approve the workflow',
                      'approve these runs', 'approve it', 'please approve',
                      'click approve', 'you should approve', 'actions tab',
                      'ask a maintainer to approve',
                      'ask a reviewer to approve', 'runs automatically',
                      'start automatically', 'will run automatically',
                      'by themselves', 'on their own',
                      'will run on its own', 'runs on its own'):
        assert forbidden not in text, (forbidden, body)


def _gate_reasons(body):
    """The reasons a gate comment lists, or None when it lists none.

    Bounding the block by the marker paragraph and the fixed instructions
    keeps the numbered list out of it however it is spelled, and a comment
    carrying those instructions without a well-formed block fails here
    rather than reading as no reasons at all.
    """
    lines = body.splitlines()
    if REASONS_END not in lines:
        return None
    end = lines.index(REASONS_END)
    markers = [index for index, line in enumerate(lines)
               if line in (MARKER, CLOSED_MARKER)]
    assert markers, body
    start = markers[-1] + 1
    assert lines[start] == '' and lines[end - 1] == '', body
    block = lines[start + 1:end - 1]
    assert block and all(line.startswith('- ') for line in block), body
    return [line[2:] for line in block]


def _assert_gate_message(write, first, reasons=(), closed=False):
    body = _comment_body(write)
    lines = body.splitlines()
    assert lines[0] == first, body
    assert MARKER in lines, body
    assert (CLOSED_MARKER in lines) is closed, body
    found = _gate_reasons(body)
    # GitHub renders '-', '*' and '+' as the same list marker, so a
    # bullet the reasons do not name is caught however it is spelled.
    bullets = [line[2:] for line in lines
               if re.match(r'[ \t]*[-*+][ \t]', line)]
    if reasons:
        assert found is not None, (reasons, body)
        assert found == list(reasons), (found, reasons, body)
    else:
        assert found is None, (found, body)
    assert bullets == list(reasons), (bullets, reasons, body)
    return body


def _assert_no_writes(writes):
    assert writes == [], writes
