"""Commit authorship checks at the GitHub API boundary."""
import sys
import re
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _util
from _prgate import (
    FakeApi, _Response, _issue, _pull, _run_script, _script_fixtures,
    ROOT, _valid_body, run_gate,
)
sys.path.insert(0, str(ROOT))
from scripts.ci import pr_attribution


def _commit(*, sha='0123456789abcdef0123456789abcdef01234567',
            author_login: str | None = 'Pleng',
            committer_login: str | None = 'alice',
            author_email='pleng@example.com', committer_email='alice@example.com',
            message='Change the implementation'):
    return {
        'sha': sha,
        'author': {'login': author_login, 'id': 10} if author_login else None,
        'committer': ({'login': committer_login, 'id': 11}
                      if committer_login else None),
        'commit': {
            'author': {'name': author_login or 'Unlinked author',
                       'email': author_email},
            'committer': {'name': committer_login or 'Unlinked committer',
                          'email': committer_email},
            'message': message,
        },
    }


def _attribution_api(commits, *, count=None, **api_kwargs):
    if count is None:
        count = len(commits)
    api = FakeApi(
        pull={**_pull(), 'commits': count},
        issues={'101': _issue('alice')},
        commits=commits, **api_kwargs)
    api.pull['body'] = _valid_body()
    return api


class _ScriptedApi:
    """Serve only explicitly scripted requests; everything else fails."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def request(self, method, endpoint, payload=None):
        call = (method, endpoint, payload)
        self.calls.append(call)
        key = (method, endpoint, payload)
        if key not in self.responses:
            raise AssertionError(f'unmodelled request: {method} {endpoint}')
        return self.responses[key]


def _check(commit, responses=None, *, commits=None):
    api = _ScriptedApi(responses or {})
    if commits is None:
        commits = [commit]
    return pr_attribution.check_commits(api, 'owner/repo', 'alice', commits), api


def test_unrelated_author_without_exemption_is_refused_and_closed(tmp):
    del tmp
    commit = _commit()
    api = _attribution_api([commit])
    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    assert writes[-1] == ('PATCH', 'repos/owner/repo/pulls/99',
                          {'state': 'closed'})
    reason = ('Commit 0123456 is authored by Pleng, who is not you (@alice), '
              'has no Co-Authored-By trailer naming you, and has no push '
              'access to this repository.')
    assert reason in api.comments[-1]['body']
    assert ('GET', 'repos/owner/repo/pulls/99/commits', None) in api.calls


def test_attribution_flag_off_preserves_the_existing_verdict_and_request_sequence(tmp):
    del tmp
    api = _attribution_api([_commit()])
    code, writes = run_gate(api, _valid_body())

    assert code == 0
    assert api.pull['state'] == 'open'
    assert writes == []
    assert api.calls == [
        ('GET', 'repos/owner/repo/pulls/99', None),
        ('GET', 'repos/owner/repo/issues/99/comments', None),
        ('POST', 'markdown', {
            'text': _valid_body(), 'mode': 'gfm', 'context': 'owner/repo'}),
        ('GET', 'repos/owner/repo/issues/101', None),
    ]


def test_commits_by_the_pr_author_pass_when_attribution_is_enabled(tmp):
    del tmp
    commit = _commit(author_login='alice', committer_login='alice',
                     author_email='alice@example.com',
                     committer_email='alice@example.com')
    api = _attribution_api([commit])

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 0
    assert api.pull['state'] == 'open'
    assert writes == []
    assert ('GET', 'repos/owner/repo/pulls/99/commits', None) in api.calls


def test_unlinked_author_and_committer_are_refused_independently(tmp):
    del tmp
    commit = _commit(
        author_login=None, committer_login=None,
        author_email='pleng@users.noreply.github.com',
        committer_email='pleng@users.noreply.github.com')
    api = _attribution_api([commit])

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    body = api.comments[-1]['body']
    assert ('Commit 0123456 is authored by Unlinked author '
            '<pleng@users.noreply.github.com>, which does not resolve to a '
            'GitHub account.') in body
    assert ('Commit 0123456 is committed by Unlinked committer '
            '<pleng@users.noreply.github.com>, which does not resolve to a '
            'GitHub account.') in body


def test_web_flow_is_exempt_but_a_similar_committer_login_is_not(tmp):
    del tmp
    allowed = _commit(author_login='alice', committer_login='web-flow')
    allowed_api = _attribution_api([allowed])
    allowed_code, allowed_writes = run_gate(
        allowed_api, _valid_body(), require_commit_attribution=True)
    assert allowed_code == 0 and allowed_writes == []
    assert not any('/collaborators/web-flow/' in call[1]
                   for call in allowed_api.calls)

    refused = _commit(author_login='alice', committer_login='web-flow-bot')
    refused_api = _attribution_api([refused])
    refused_code, refused_writes = run_gate(
        refused_api, _valid_body(), require_commit_attribution=True)
    assert refused_code == 2
    assert refused_api.pull['state'] == 'closed'
    assert len(refused_writes) == 2
    assert 'Commit 0123456 is committed by web-flow-bot' in (
        refused_api.comments[-1]['body'])


def test_old_style_noreply_coauthor_resolves_via_users_endpoint(tmp=None):
    del tmp
    commit = _commit(
        message='Change\n\nCo-Authored-By: Alice '
        '<alice@users.noreply.github.com>')
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(
            200, {'login': 'alice', 'id': 112233}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == []
    assert api.calls == [('GET', 'users/alice', None)]


def test_new_style_noreply_coauthor_requires_matching_numeric_id(tmp=None):
    del tmp
    commit = _commit(
        message='Change\n\nCo-Authored-By: Alice '
        '<112233+alice@users.noreply.github.com>')
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(
            200, {'login': 'alice', 'id': 112233}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == []
    assert api.calls == [('GET', 'users/alice', None)]
    assert all('/users/112233' not in call[1] for call in api.calls)


def test_commit_author_email_resolves_a_trailer_before_exempting_committer(tmp=None):
    del tmp
    commit = _commit(
        author_login='alice', committer_login='Pleng',
        author_email='z@x.com', committer_email='pleng@example.com',
        message='Change\n\nCo-Authored-By: Alice <z@x.com>')

    reasons, api = _check(commit)

    assert reasons == []
    assert api.calls == []


def test_coauthor_exemption_requires_the_pr_author_account(tmp=None):
    del tmp
    commit = _commit(
        message='Change\n\nCo-Authored-By: Bob '
        '<bob@users.noreply.github.com>')
    api = _ScriptedApi({
        ('GET', 'users/bob', None): _Response(
            200, {'login': 'bob', 'id': 223344}),
        ('GET', 'repos/owner/repo/collaborators/Pleng/permission', None):
            _Response(403, {'message': 'no push access'}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == [
        'Commit 0123456 is authored by Pleng, who is not you (@alice), has '
        'no Co-Authored-By trailer naming you, and has no push access to this '
        'repository.']
    assert ('GET', 'users/bob', None) in api.calls


def test_write_maintain_and_admin_permission_values_grant_push_access(tmp):
    del tmp
    permission_endpoint = 'repos/owner/repo/collaborators/Pleng/permission'
    for permission in ('admin', 'maintain', 'write'):
        api = _ScriptedApi({
            ('GET', permission_endpoint, None): _Response(
                200, {'permission': permission}),
        })
        commit = _commit()

        reasons = pr_attribution.check_commits(
            api, 'owner/repo', 'alice', [commit])

        assert reasons == [], permission
        assert api.calls == [('GET', permission_endpoint, None)], permission


def test_read_permission_does_not_use_the_nested_push_boolean(tmp=None):
    del tmp
    permission_endpoint = 'repos/owner/repo/collaborators/Pleng/permission'
    api = _ScriptedApi({
        ('GET', permission_endpoint, None): _Response(200, {
            'permission': 'read',
            'user': {'permissions': {'push': True}},
        }),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [_commit()])

    assert len(reasons) == 1
    assert 'is authored by Pleng' in reasons[0]
    assert 'has no push access to this repository.' in reasons[0]


def test_permission_403_and_404_both_mean_no_push_access(tmp=None):
    del tmp
    permission_endpoint = 'repos/owner/repo/collaborators/Pleng/permission'
    for status in (403, 404):
        api = _ScriptedApi({
            ('GET', permission_endpoint, None): _Response(status, {}),
        })

        reasons = pr_attribution.check_commits(
            api, 'owner/repo', 'alice', [_commit()])

        assert reasons and 'is authored by Pleng' in reasons[0]


def test_unresolvable_human_coauthor_trailer_is_refused(tmp):
    del tmp
    commit = _commit(
        author_login='alice', committer_login='alice',
        message='Change\n\nCo-Authored-By: Peter <peter@example.com>')
    api = _attribution_api([commit])

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    assert ('Commit 0123456 carries the Co-Authored-By trailer Peter '
            '<peter@example.com>, which does not resolve to a GitHub account.') in (
                api.comments[-1]['body'])


def test_unresolvable_no_space_coauthor_trailer_is_refused(tmp):
    del tmp
    email = 'unlinked@example.test'
    message = (
        'Change implementation\n\n'
        f'Co-Authored-By:Ghost <{email}>\n')
    git_output = subprocess.run(
        ['git', 'interpret-trailers', '--parse'], input=message,
        capture_output=True, text=True, check=True).stdout
    assert git_output == f'Co-Authored-By: Ghost <{email}>\n'

    commit = _commit(
        author_login='alice', committer_login='alice', message=message)
    api = _attribution_api([commit])

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    assert ('carries the Co-Authored-By trailer Ghost '
            f'<{email}>, which does not resolve to a GitHub account.') in (
                api.comments[-1]['body'])


def test_unresolvable_folded_coauthor_trailer_is_refused(tmp):
    del tmp
    email = 'unlinked@example.test'
    message = (
        'Change implementation\n\n'
        'Reviewed-by: Reviewer\n'
        ' additional review context\n'
        'Co-Authored-By: Ghost\n'
        f' <{email}>\n')
    git_output = subprocess.run(
        ['git', 'interpret-trailers', '--parse'], input=message,
        capture_output=True, text=True, check=True).stdout
    assert git_output == (
        'Reviewed-by: Reviewer additional review context\n'
        f'Co-Authored-By: Ghost <{email}>\n')

    commit = _commit(
        author_login='alice', committer_login='alice', message=message)
    api = _attribution_api([commit])

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    assert ('carries the Co-Authored-By trailer Ghost '
            f'<{email}>, which does not resolve to a GitHub account.') in (
                api.comments[-1]['body'])


def test_model_noreply_trailers_are_ignored_and_case_insensitive(tmp=None):
    del tmp
    for email in (
            'noreply@anthropic.com', 'noreply@z.ai', 'noreply@openai.com',
            'noreply@vendor.example', 'Noreply@anthropic.com'):
        commit = _commit(
            author_login='alice', committer_login='alice',
            message=f'Change\n\nCo-Authored-By: Model <{email}>')
        if email == 'Noreply@anthropic.com':
            endpoint = ('search/commits?q=author-email%3A%22'
                        'Noreply%40anthropic.com%22&per_page=1')
            api = _ScriptedApi({
                ('GET', endpoint, None): _Response(
                    200, {'total_count': 0, 'items': []}),
            })
            reasons = pr_attribution.check_commits(
                api, 'owner/repo', 'alice', [commit])
        else:
            reasons, api = _check(commit)
        assert reasons == [], email
        assert api.calls == [], email


def test_non_noreply_model_trailer_shape_is_still_checked(tmp=None):
    del tmp
    email = 'me@example.com'
    endpoint = ('search/commits?q=author-email%3A%22me%40example.com%22'
                '&per_page=1')
    commit = _commit(
        author_login='alice', committer_login='alice',
        message=f'Change\n\nCo-Authored-By: Person <{email}>')
    api = _ScriptedApi({
        ('GET', endpoint, None): _Response(
            200, {'total_count': 0, 'items': []}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert len(reasons) == 1
    assert 'carries the Co-Authored-By trailer Person <me@example.com>' in reasons[0]
    assert api.calls == [('GET', endpoint, None)]


def test_new_style_id_mismatch_falls_back_to_commit_search(tmp=None):
    del tmp
    email = '112233+alice@users.noreply.github.com'
    endpoint = ('search/commits?q=author-email%3A%22'
                '112233%2Balice%40users.noreply.github.com%22&per_page=1')
    commit = _commit(
        author_login='alice', committer_login='alice',
        message=f'Change\n\nCo-Authored-By: Alice <{email}>')
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(
            200, {'login': 'alice', 'id': 999}),
        ('GET', endpoint, None): _Response(
            200, {'total_count': 1,
                  'items': [{'author': {'login': 'alice'}, 'committer': None}]}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == []
    assert api.calls == [
        ('GET', 'users/alice', None), ('GET', endpoint, None)]


def test_new_style_noreply_user_404_falls_back_to_commit_search(tmp=None):
    del tmp
    email = '112233+alice@users.noreply.github.com'
    endpoint = ('search/commits?q=author-email%3A%22'
                '112233%2Balice%40users.noreply.github.com%22&per_page=1')
    commit = _commit(
        author_login='alice', committer_login='alice',
        message=f'Change\n\nCo-Authored-By: Alice <{email}>')
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(404, None),
        ('GET', endpoint, None): _Response(
            200, {'total_count': 1,
                  'items': [{'author': {'login': 'alice'}}]}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == []
    assert api.calls == [
        ('GET', 'users/alice', None), ('GET', endpoint, None)]


def test_new_style_id_mismatch_without_search_match_is_unresolvable(tmp=None):
    del tmp
    email = '112233+alice@users.noreply.github.com'
    endpoint = ('search/commits?q=author-email%3A%22'
                '112233%2Balice%40users.noreply.github.com%22&per_page=1')
    commit = _commit(
        author_login='alice', committer_login='alice',
        message=f'Change\n\nCo-Authored-By: Alice <{email}>')
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(
            200, {'login': 'alice', 'id': 999}),
        ('GET', endpoint, None): _Response(
            200, {'total_count': 0, 'items': []}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == [
        'Commit 0123456 carries the Co-Authored-By trailer Alice '
        '<112233+alice@users.noreply.github.com>, which does not resolve to a '
        'GitHub account.']


def test_search_commits_does_not_use_committer_with_a_different_email(tmp=None):
    del tmp
    email = 'peter+tag@example.com'
    endpoint = ('search/commits?q=author-email%3A%22'
                'peter%2Btag%40example.com%22&per_page=1')
    commit = _commit(
        author_login='Pleng', committer_login='Pleng',
        author_email='pleng@example.com',
        committer_email='pleng-commit@example.com',
        message=f'Change\n\nCo-Authored-By: Alice <{email}>')
    api = _ScriptedApi({
        ('GET', endpoint, None): _Response(
            200, {'total_count': 1, 'items': [
                {
                    'author': None,
                    'committer': {'login': 'alice'},
                    'commit': {
                        'author': {'email': email},
                        'committer': {'email': 'different@example.com'},
                    },
                },
            ]}),
        ('GET', 'repos/owner/repo/collaborators/Pleng/permission', None):
            _Response(403, {'message': 'no push access'}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == [
        'Commit 0123456 carries the Co-Authored-By trailer Alice '
        '<peter+tag@example.com>, which does not resolve to a GitHub account.',
        'Commit 0123456 is authored by Pleng, who is not you (@alice), has '
        'no Co-Authored-By trailer naming you, and has no push access to this '
        'repository.',
    ]
    assert api.calls == [
        ('GET', endpoint, None),
        ('GET', 'repos/owner/repo/collaborators/Pleng/permission', None),
    ]


def test_search_commits_uses_committer_login_when_its_email_matches(tmp=None):
    del tmp
    email = 'peter+tag@example.com'
    endpoint = ('search/commits?q=author-email%3A%22'
                'peter%2Btag%40example.com%22&per_page=1')
    commit = _commit(
        author_login='Pleng', committer_login='Pleng',
        author_email='pleng@example.com',
        committer_email='pleng-commit@example.com',
        message=f'Change\n\nCo-Authored-By: Alice <{email}>')
    api = _ScriptedApi({
        ('GET', endpoint, None): _Response(
            200, {'total_count': 1, 'items': [
                {
                    'author': None,
                    'committer': {'login': 'alice'},
                    'commit': {
                        'author': {'email': email},
                        'committer': {'email': email},
                    },
                },
            ]}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == []
    assert api.calls == [('GET', endpoint, None)]


def test_old_style_noreply_without_a_matching_user_is_not_trusted(tmp=None):
    del tmp
    commit = _commit(
        message='Change\n\nCo-Authored-By: Alice '
        '<alice@users.noreply.github.com>')
    api = _attribution_api(
        [commit], users={'alice-renamed': {'login': 'alice-renamed', 'id': 112233}})

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    body = api.comments[-1]['body']
    assert 'Commit 0123456 is authored by Pleng' in body
    assert ('carries the Co-Authored-By trailer Alice '
            '<alice@users.noreply.github.com>, which does not resolve') in body
    assert ('GET', 'users/alice', None) in api.calls
    assert any(call[1].startswith('search/commits?') for call in api.calls)


def test_trailer_resolution_is_cached_per_email_for_the_run(tmp=None):
    del tmp
    email = 'alice@users.noreply.github.com'
    message = f'Change\n\nCo-Authored-By: Alice <{email}>'
    commits = [_commit(sha=f'{index:040x}', message=message)
               for index in (1, 2)]
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(
            200, {'login': 'alice', 'id': 112233}),
    })

    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', commits)

    assert reasons == []
    assert api.calls == [('GET', 'users/alice', None)]


def test_commit_list_api_failure_aborts_without_closing_or_commenting(tmp):
    del tmp
    api = _attribution_api(
        [_commit()], fail=('GET repos/owner/repo/pulls/99/commits',))

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 1
    assert api.pull['state'] == 'open'
    assert writes == []


def test_permission_api_failure_aborts_without_closing_or_commenting(tmp):
    del tmp
    api = _attribution_api(
        [_commit()], fail=('GET repos/owner/repo/collaborators/Pleng/permission',))

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 1
    assert api.pull['state'] == 'open'
    assert writes == []


def test_user_lookup_api_failure_aborts_attribution_resolution(tmp=None):
    del tmp
    commit = _commit(
        message='Change\n\nCo-Authored-By: Alice '
        '<alice@users.noreply.github.com>')
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(500, {'message': 'server error'}),
    })

    try:
        pr_attribution.check_commits(api, 'owner/repo', 'alice', [commit])
    except pr_attribution.AttributionError as error:
        assert str(error) == 'GitHub returned 500 for users/alice'
    else:
        raise AssertionError('a failed user lookup was treated as an email miss')


def test_commit_search_api_failure_aborts_attribution_resolution(tmp=None):
    del tmp
    email = 'peter@example.com'
    endpoint = ('search/commits?q=author-email%3A%22'
                'peter%40example.com%22&per_page=1')
    commit = _commit(
        author_login='alice', committer_login='alice',
        message=f'Change\n\nCo-Authored-By: Peter <{email}>')
    api = _ScriptedApi({
        ('GET', endpoint, None): _Response(500, {'message': 'server error'}),
    })

    try:
        pr_attribution.check_commits(api, 'owner/repo', 'alice', [commit])
    except pr_attribution.AttributionError as error:
        assert str(error) == f'GitHub returned 500 for {endpoint}'
    else:
        raise AssertionError('a failed commit search was treated as no match')


def test_more_than_250_commits_refuses_without_fetching_commit_list(tmp):
    del tmp
    api = _attribution_api([], count=251)

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    assert code == 2
    assert api.pull['state'] == 'closed'
    assert len(writes) == 2
    assert ('This pull request carries 251 commits; the commit-attribution '
            'check can verify at most 250, so it cannot verify this pull '
            'request.') in api.comments[-1]['body']
    assert not any(call[1] == 'repos/owner/repo/pulls/99/commits'
                   for call in api.calls)


def test_issue_72_shape_emits_one_reason_per_commit_and_closes(tmp):
    del tmp
    commits = [
        _commit(sha=f'{index:040x}', author_login='Pleng',
                committer_login='Pleng', author_email='pleng@example.com',
                committer_email='pleng@example.com')
        for index in range(1, 9)
    ]
    api = _attribution_api(commits)

    code, writes = run_gate(
        api, _valid_body(), require_commit_attribution=True)

    body = api.comments[-1]['body']
    refusal_count = len(re.findall(r'Commit [0-9a-f]{7} is authored by Pleng', body))
    assert code == 2
    assert api.pull['state'] == 'closed'
    assert refusal_count == 8
    assert len(writes) == 2
    assert sum('/collaborators/Pleng/permission' in call[1]
               for call in api.calls) == 1


def test_commit_trailer_parser_matches_git_for_a_glued_body_paragraph(tmp=None):
    del tmp
    message = (
        'Change implementation\n\n'
        'Body prose without a colon\n'
        'Co-Authored-By: Alice <alice@users.noreply.github.com>\n')
    git_output = subprocess.run(
        ['git', 'interpret-trailers', '--parse'], input=message,
        capture_output=True, text=True, check=True).stdout
    assert git_output == ''

    commit = _commit(message=message)
    api = _ScriptedApi({
        ('GET', 'repos/owner/repo/collaborators/Pleng/permission', None):
            _Response(403, {'message': 'no push access'}),
    })
    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons and 'is authored by Pleng' in reasons[0]
    assert ('GET', 'users/alice', None) not in api.calls


def test_commit_trailer_parser_recognizes_separated_raw_trailer_paragraph(tmp=None):
    del tmp
    message = (
        'Change implementation\n\n'
        'Body prose without a colon\n\n'
        'Co-Authored-By: Alice <alice@users.noreply.github.com>\n')
    git_output = subprocess.run(
        ['git', 'interpret-trailers', '--parse'], input=message,
        capture_output=True, text=True, check=True).stdout
    assert git_output == (
        'Co-Authored-By: Alice <alice@users.noreply.github.com>\n')

    commit = _commit(message=message)
    api = _ScriptedApi({
        ('GET', 'users/alice', None): _Response(
            200, {'login': 'alice', 'id': 112233}),
    })
    reasons = pr_attribution.check_commits(
        api, 'owner/repo', 'alice', [commit])

    assert reasons == []
    assert api.calls == [('GET', 'users/alice', None)]


def test_script_search_resolution_transmits_accept_header_and_parses_query(tmp):
    email = 'peter+tag@example.com'
    fixture = _script_fixtures()
    fixture['pull']['commits'] = 1
    fixture['commits'] = [_commit(
        author_login='alice', committer_login='alice',
        message=f'Change\n\nCo-Authored-By: Peter <{email}>')]
    fixture['commit_search'] = {email: [{'author': {'login': 'alice'}}]}

    result, calls = _run_script(
        tmp, fixture, require_commit_attribution=True)

    assert result.returncode == 0, (result.stdout, result.stderr, calls)
    search = [call for call in calls if call['argv'][4].startswith('search/commits?')]
    assert len(search) == 1
    assert 'Accept: application/vnd.github+json' in search[0]['argv']
    assert 'q=author-email%3A%22peter%2Btag%40example.com%22' in (
        search[0]['argv'][4])


def test_exactly_250_commits_are_fetched_in_no_more_than_three_pages(tmp):
    fixture = _script_fixtures()
    fixture['pull']['commits'] = 250
    fixture['commits'] = [_commit(
        author_login='alice', committer_login='alice',
        author_email='alice@example.com', committer_email='alice@example.com')]
    fixture['commits'] *= 250

    result, calls = _run_script(
        tmp, fixture, require_commit_attribution=True)

    assert result.returncode == 0, (result.stdout, result.stderr, calls)
    pages = [int(next(argument for argument in call['argv']
                      if argument.startswith('page=')).split('=', 1)[1])
             for call in calls
             if call['argv'][4] == 'repos/owner/repo/pulls/99/commits'
             and any(argument.startswith('page=') for argument in call['argv'])]
    assert pages == [1, 2, 3]


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prattr_'))
