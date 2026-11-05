"""Fetch a pull request's commits once for every commit check that is on."""
import re

if __package__:
    # pylint: disable-next=relative-beyond-top-level
    from .pr_attribution import AttributionError, check_commits
    # pylint: disable-next=relative-beyond-top-level
    from .pr_body import code_span
else:
    from pr_attribution import AttributionError, check_commits
    from pr_body import code_span


MAX_COMMITS = 250
TYPES = ('build', 'chore', 'ci', 'docs', 'feat', 'fix', 'perf', 'refactor',
         'revert', 'style', 'test')
# ASCII letters only, so a Unicode case fold ('ſtyle') never reads as a type.
_SUBJECT = re.compile(
    r'(?P<type>[A-Za-z]+)(?:\([^()\s]+\))?!?: (?P<description>.*)')
FORMAT_REASON = (
    'A commit subject follows Conventional Commits as '
    '`type(scope)!: description`, where the scope and `!` are optional and '
    f'the type is one of {", ".join(TYPES[:-1])} or {TYPES[-1]}; reword each '
    'commit named above.')


def subject_reasons(commits):
    """Name every single-parent commit whose subject is not Conventional."""
    reasons = []
    for commit in commits:
        parents = commit.get('parents')
        if isinstance(parents, list) and len(parents) > 1:
            continue
        commit_data = commit.get('commit')
        message = (commit_data.get('message')
                   if isinstance(commit_data, dict) else None)
        subject = message.split('\n', 1)[0].rstrip('\r') if isinstance(
            message, str) else ''
        match = _SUBJECT.fullmatch(subject)
        if (match is None or match.group('type').lower() not in TYPES
                or not match.group('description').strip()):
            sha = str(commit.get('sha') or '')[:7]
            reasons.append(
                f'Commit {sha} has the subject {code_span(subject)}, which '
                'does not follow Conventional Commits.')
    return reasons + [FORMAT_REASON] if reasons else []


def commit_reasons(api, repository, pull_number, actor, pull, *,
                   attribution, conventional):
    """Fetch the commit list once, refusing above the 250-commit cap."""
    count = pull.get('commits') or 0
    if count > MAX_COMMITS:
        names = [name for name, on in (('commit-attribution', attribution),
                                       ('Conventional Commits', conventional)) if on]
        checks = (f'the {names[0]} check can verify at most 250, so it'
                  if len(names) == 1 else
                  f'the {" and ".join(names)} checks can verify at most 250, so they')
        return [f'This pull request carries {count} commits; {checks} cannot '
                'verify this pull request.']

    endpoint = f'repos/{repository}/pulls/{pull_number}/commits'
    try:
        response = api.paginate(endpoint)
    except RuntimeError as error:
        raise AttributionError(str(error)) from error
    if response.status != 200 or not isinstance(response.data, list):
        raise AttributionError(
            f'GitHub returned {response.status} for {endpoint}')
    commits = response.data
    if not all(isinstance(commit, dict) for commit in commits):
        raise AttributionError('commit response item is not an object')
    reasons = check_commits(api, repository, actor, commits) if attribution else []
    return reasons + subject_reasons(commits) if conventional else reasons
