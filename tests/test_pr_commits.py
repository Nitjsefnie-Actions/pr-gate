"""The Conventional Commits subject check and the shared commit-list fetch."""
import contextlib
import io
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _util
from _prgate import (
    FakeApi, TEMPLATE, _gate_module, _issue, _pull, _recorded_writes,
    _run_script, _script_fixtures, _valid_body,
)

COMMITS = 'repos/owner/repo/pulls/99/commits'
FORMAT = ('A commit subject follows Conventional Commits as '
          '`type(scope)!: description`, where the scope and `!` are optional '
          'and the type is one of build, chore, ci, docs, feat, fix, perf, '
          'refactor, revert, style or test; reword each commit named above.')


def _commit(message, *, sha='0123456789abcdef0123456789abcdef01234567',
            parents=1, login='alice'):
    return {
        'sha': sha,
        'parents': [{'sha': f'{index:040x}'} for index in range(parents)],
        'author': {'login': login, 'id': 10},
        'committer': {'login': login, 'id': 10},
        'commit': {
            'author': {'name': login, 'email': f'{login}@example.com'},
            'committer': {'name': login, 'email': f'{login}@example.com'},
            'message': message,
        },
    }


def _gate(commits, *, count=None, attribution=False, conventional=True):
    api = FakeApi(pull={**_pull(), 'body': _valid_body(),
                        'commits': len(commits) if count is None else count},
                  issues={'101': _issue('alice')}, commits=commits)
    error = io.StringIO()
    gate = _gate_module()
    assert gate is not None
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
        code = gate.run(
            api, 'owner/repo', '99', 'alice', TEMPLATE,
            require_commit_attribution=attribution,
            conventional_commits=conventional)
    fetches = [call for call in api.calls if call == ('GET', COMMITS, None)]
    body = api.comments[-1]['body'] if api.comments else ''
    return code, body, len(fetches), error.getvalue(), api


def _verdict(message):
    code, body, _fetches, _error, _api = _gate([_commit(message)])
    return code, body


def test_conforming_subjects_pass(tmp):
    del tmp
    for message in ('feat: add the check', 'fix(gate): x', 'FIX(Gate)!: x',
                    'Docs(a.b/c-d_é): x', 'chore!: x', 'revert: "feat: x"',
                    'refactor:  description after a second space',
                    'test: x\n\nUpdate stuff', 'style: x\r\n\r\nbody',
                    'build: x', 'ci: x', 'perf: x', 'ci(pr gate): x', 'ci(size baseline)!: x'):
        assert _verdict(message) == (0, ''), message


def test_nonconforming_subjects_fail(tmp):
    del tmp
    for message in ('Update stuff', 'feature: x', 'feat:x', 'feat : x',
                    'feat(): x', 'feat( ): x', 'feat( a): x', 'feat(a ): x', 'feat(a(b)): x',
                    'feat(a)b: x', 'feat!(scope): x', 'feat(scope)!!: x',
                    'feat: ', 'feat: \t ', 'feat:\tx', ' feat: x',
                    'ſtyle: x', 'feat\n\nfeat: x', 'Update\nfeat: x', ''):
        code, body = _verdict(message)
        subject = message.split('\n', 1)[0]
        assert code == 2, message
        assert f'- Commit 0123456 has the subject `{subject}`' in body or (
            not subject.strip()), (message, body)
        assert FORMAT in body, (message, body)


def test_every_failing_commit_is_listed_once_with_the_format_last(tmp):
    del tmp
    commits = [_commit('Update stuff', sha='a' * 40),
               _commit('feat: fine', sha='b' * 40),
               _commit('Merge branch main', sha='c' * 40, parents=2),
               _commit('Fix `it`\n\nbody', sha='d' * 40)]
    code, body, fetches, error, api = _gate(commits)
    assert (code, fetches, api.pull['state']) == (2, 1, 'open')
    reasons = [line for line in body.splitlines() if line.startswith('- ')]
    assert reasons == [
        '- Commit aaaaaaa has the subject `Update stuff`, which does not '
        'follow Conventional Commits.',
        '- Commit ddddddd has the subject `` Fix `it` ``, which does not '
        'follow Conventional Commits.',
        f'- {FORMAT}'], body
    assert error.startswith('pr gate: the pull request is inadmissible (exit 2): '
                            'Commit aaaaaaa has the subject'), error


def test_the_stderr_line_carries_no_control_character(tmp):
    del tmp
    subject = 'Update\r::error::forged\x1b[31m\u2028x'
    code, body, _fetches, error, _api = _gate([_commit(subject)])
    assert code == 2
    assert f'`{subject}`' in body, body
    line = error.removesuffix('\n')
    assert line.isprintable(), repr(error)
    assert 'Commit 0123456 has the subject `Update\\r::error::forged\\x1b[31m\\u2028x`' in (
        line), line


def test_only_a_multi_parent_commit_is_skipped(tmp):
    del tmp
    for parents, expected in ((0, 2), (1, 2), (2, 0), (3, 0)):
        code, _body, _fetches, _error, _api = _gate(
            [_commit('Merge pull request #1', parents=parents)])
        assert code == expected, parents


def test_both_checks_share_one_commit_list_fetch(tmp):
    del tmp
    commits = [_commit('Update stuff', login='mallory')]
    code, body, fetches, _error, _api = _gate(commits, attribution=True)
    assert (code, fetches) == (2, 1)
    assert 'is authored by mallory, who is not you' in body, body
    assert '- Commit 0123456 has the subject `Update stuff`' in body, body
    # Each check off must stay off: a subject or an author only it would refuse.
    for attribution, conventional, login, message in (
            (True, False, 'alice', 'Update stuff'),
            (False, True, 'mallory', 'feat: x'),
            (False, False, 'mallory', 'Update stuff')):
        code, body, fetches, _error, api = _gate(
            [_commit(message, login=login)], attribution=attribution,
            conventional=conventional)
        assert (code, body, fetches) == (
            0, '', int(attribution or conventional)), (attribution, conventional)
        assert not any(call[1].startswith((
            'repos/owner/repo/collaborators/', 'users/', 'search/commits'))
            for call in api.calls), api.calls


def test_more_than_250_commits_name_every_check_that_cannot_verify(tmp):
    del tmp
    prefix = 'This pull request carries 251 commits; '
    for attribution, conventional, checks in (
            (False, True, 'the Conventional Commits check can verify at most '
                          '250, so it'),
            (True, False, 'the commit-attribution check can verify at most '
                          '250, so it'),
            (True, True, 'the commit-attribution and Conventional Commits '
                         'checks can verify at most 250, so they')):
        code, body, fetches, _error, _api = _gate(
            [], count=251, attribution=attribution, conventional=conventional)
        assert (code, fetches) == (2, 0)
        assert (f'- {prefix}{checks} cannot verify this pull request.'
                in body.splitlines()), body
    code, body, fetches, _error, _api = _gate([_commit('feat: x')], count=250)
    assert (code, body, fetches) == (0, '', 1)


def test_a_malformed_commit_item_fails_closed(tmp):
    del tmp
    code, _body, _fetches, error, _api = _gate(['feat: x'])
    assert (code, error) == (
        1, 'pr gate failed: commit response item is not an object\n')
    code, body, _fetches, _error, _api = _gate([{'sha': 'a' * 40}])
    assert code == 2 and '- Commit aaaaaaa has the subject `  `' in body, body


def test_the_environment_turns_the_check_off_only_with_false(tmp):
    fixtures = _script_fixtures(commits=[_commit('Update stuff')])
    fixtures['pull']['commits'] = 1
    for index, value in enumerate((None, '', 'true', 'no', '0', ' FaLsE ', 'false')):
        directory = tmp / str(index)
        directory.mkdir()
        with mock.patch.dict(os.environ):
            os.environ.pop('PR_GATE_CONVENTIONAL_COMMITS', None)
            os.environ['CONVENTIONAL_COMMITS'] = 'false'
            if value is not None:
                os.environ['PR_GATE_CONVENTIONAL_COMMITS'] = value
            result, calls = _run_script(directory, fixtures)
        off = value is not None and value.strip().casefold() == 'false'
        assert result.returncode == (0 if off else 2), (value, result.stderr)
        fetched = any(call['argv'][4] == COMMITS for call in calls)
        assert fetched is not off, value
        assert len(_recorded_writes(calls)) == (0 if off else 1), value


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prcommits_'))
