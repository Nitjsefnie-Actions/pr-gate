"""Check whether pull-request commits have attributable GitHub accounts."""
import re
from urllib.parse import quote, urlencode


MAX_COMMITS = 250
_PUSH_PERMISSIONS = frozenset(('write', 'maintain', 'admin'))
_TRAILER_LINE = re.compile(
    r'^(?P<token>[A-Za-z0-9-]+):[ \t]*(?P<value>.*?)\s*$')
_COAUTHOR_VALUE = re.compile(r'(?P<name>.+?)\s*<(?P<email>[^<>]+)>\s*$')
_NEW_STYLE_NOREPLY = re.compile(
    r'(?P<id>[0-9]+)\+(?P<login>[^@]+)@users\.noreply\.github\.com$',
    re.IGNORECASE)
_USERS_NOREPLY = re.compile(
    r'(?P<login>[^@]+)@users\.noreply\.github\.com$', re.IGNORECASE)


class AttributionError(RuntimeError):
    """A GitHub API failure prevented a commit-attribution decision."""


class _Checker:
    def __init__(self, api, repository, actor):
        self.api = api
        self.repository = repository
        self.actor = actor
        self.email_logins = {}
        self.push_access = {}

    def check(self, commits):
        reasons = []
        for commit in commits:
            if not isinstance(commit, dict):
                raise AttributionError('commit response item is not an object')
            reasons.extend(self._check_commit(commit))
        return reasons

    def _check_commit(self, commit):
        commit_data = commit.get('commit')
        if not isinstance(commit_data, dict):
            commit_data = {}
        sha = str(commit.get('sha') or '')[:7]
        trailers = _coauthor_trailers(commit_data.get('message'))
        reasons = []
        coauthored_by_actor = False
        for name, email in trailers:
            if _is_model_trailer(email):
                continue
            login = self._resolve_trailer_email(email, commit, commit_data)
            if login is None:
                reasons.append(
                    f'Commit {sha} carries the Co-Authored-By trailer '
                    f'{name} <{email}>, which does not resolve to a GitHub account.')
            elif _same_login(login, self.actor):
                coauthored_by_actor = True

        unrelated_accounts = set()
        for role in ('author', 'committer'):
            identity = commit_data.get(role)
            if not isinstance(identity, dict):
                identity = {}
            user = commit.get(role)
            login = user.get('login') if isinstance(user, dict) else None
            if not isinstance(login, str) or not login:
                name = identity.get('name') or f'Unknown {role}'
                email = identity.get('email') or 'unknown'
                verb = 'authored' if role == 'author' else 'committed'
                reasons.append(
                    f'Commit {sha} is {verb} by {name} <{email}>, which does '
                    'not resolve to a GitHub account.')
                continue
            if (_same_login(login, self.actor) or _same_login(login, 'web-flow')
                    or coauthored_by_actor):
                continue
            if self._has_push_access(login):
                continue
            normalized_login = login.casefold()
            if normalized_login in unrelated_accounts:
                # The same unrelated GitHub account in both fields is one
                # offending identity for this commit; report it once.
                continue
            unrelated_accounts.add(normalized_login)
            verb = 'authored' if role == 'author' else 'committed'
            reasons.append(
                f'Commit {sha} is {verb} by {login}, who is not you (@{self.actor}), '
                'has no Co-Authored-By trailer naming you, and has no push '
                'access to this repository.')
        return reasons

    def _resolve_trailer_email(self, email, commit, commit_data):
        for role in ('author', 'committer'):
            details = commit_data.get(role)
            user = commit.get(role)
            if not isinstance(details, dict) or not isinstance(user, dict):
                continue
            login = user.get('login')
            if (isinstance(login, str) and login
                    and email == details.get('email')):
                return login

        if email in self.email_logins:
            return self.email_logins[email]
        login = self._resolve_external_email(email)
        self.email_logins[email] = login
        return login

    def _resolve_external_email(self, email):
        new_style = _NEW_STYLE_NOREPLY.fullmatch(email)
        old_style = None if new_style else _USERS_NOREPLY.fullmatch(email)
        if new_style is not None or old_style is not None:
            match = new_style if new_style is not None else old_style
            assert match is not None
            login = match.group('login')
            endpoint = f'users/{quote(login, safe="")}'
            response = self._request(endpoint)
            if response.status == 200 and isinstance(response.data, dict):
                resolved_login = response.data.get('login')
                if new_style is not None:
                    if (response.data.get('id') == int(new_style.group('id'))
                            and isinstance(resolved_login, str)
                            and resolved_login):
                        return resolved_login
                elif isinstance(resolved_login, str) and resolved_login:
                    return resolved_login
            elif response.status not in (200, 404):
                raise AttributionError(
                    f'GitHub returned {response.status} for {endpoint}')

        query = urlencode({'q': f'author-email:"{email}"'})
        endpoint = f'search/commits?{query}'
        response = self._request(endpoint)
        if response.status != 200 or not isinstance(response.data, dict):
            raise AttributionError(
                f'GitHub returned {response.status} for {endpoint}')
        items = response.data.get('items')
        if not isinstance(items, list):
            raise AttributionError(f'GitHub returned malformed search results for {endpoint}')
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get('author')
            author_login = (
                author.get('login') if isinstance(author, dict) else None)
            if isinstance(author_login, str) and author_login:
                return author_login

            committer = item.get('committer')
            committer_login = (
                committer.get('login') if isinstance(committer, dict) else None)
            commit = item.get('commit')
            commit_committer = (
                commit.get('committer') if isinstance(commit, dict) else None)
            committer_email = (
                commit_committer.get('email')
                if isinstance(commit_committer, dict) else None)
            if (isinstance(committer_login, str) and committer_login
                    and committer_email == email):
                return committer_login
        return None

    def _has_push_access(self, login):
        cache_key = login.casefold()
        if cache_key in self.push_access:
            return self.push_access[cache_key]
        endpoint = (
            f'repos/{self.repository}/collaborators/'
            f'{quote(login, safe="")}/permission')
        response = self._request(endpoint)
        if response.status in (403, 404):
            allowed = False
        elif response.status == 200:
            # GitHub documents this top-level field; ignore nested permissions.
            permission = (response.data.get('permission')
                          if isinstance(response.data, dict) else None)
            allowed = permission in _PUSH_PERMISSIONS
        else:
            raise AttributionError(
                f'GitHub returned {response.status} for {endpoint}')
        self.push_access[cache_key] = allowed
        return allowed

    def _request(self, endpoint):
        try:
            return self.api.request('GET', endpoint)
        except RuntimeError as error:
            raise AttributionError(str(error)) from error


def _is_model_trailer(email):
    local, separator, _domain = email.rpartition('@')
    return bool(separator) and local.casefold() == 'noreply'


def _coauthor_trailers(message):
    if not isinstance(message, str):
        return []
    lines = message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    separator = next(
        (index for index in range(len(lines) - 1, -1, -1)
         if not lines[index].strip()), None)
    if separator is None:
        return []
    trailer_lines = lines[separator + 1:]
    parsed = []
    for line in trailer_lines:
        match = _TRAILER_LINE.fullmatch(line)
        if match is not None:
            parsed.append([match.group('token'), match.group('value').strip()])
        elif line.startswith((' ', '\t')) and parsed:
            continuation = line.strip()
            if continuation:
                parsed[-1][1] = f'{parsed[-1][1]} {continuation}'.strip()
        else:
            return []
    if not trailer_lines:
        return []
    trailers = []
    for token, trailer_value in parsed:
        if token.casefold() != 'co-authored-by':
            continue
        value = _COAUTHOR_VALUE.fullmatch(trailer_value)
        if value is not None:
            name = value.group('name').strip()
            email = value.group('email').strip()
            if name and email:
                trailers.append((name, email))
    return trailers


def _same_login(left, right):
    return left.casefold() == right.casefold()


def check_commits(api, repository, actor, commits):
    """Return trailer and identity failures for the supplied commit records."""
    return _Checker(api, repository, actor).check(commits)


def commit_attribution_reasons(api, repository, pull_number, actor, pull):
    """Fetch and check commits, refusing requests above the 250-commit cap."""
    count = pull.get('commits') or 0
    if count > MAX_COMMITS:
        return [
            f'This pull request carries {count} commits; the commit-attribution '
            'check can verify at most 250, so it cannot verify this pull request.']

    endpoint = f'repos/{repository}/pulls/{pull_number}/commits'
    try:
        response = api.paginate(endpoint)
    except RuntimeError as error:
        raise AttributionError(str(error)) from error
    if response.status != 200 or not isinstance(response.data, list):
        raise AttributionError(
            f'GitHub returned {response.status} for {endpoint}')
    return check_commits(api, repository, actor, response.data)
