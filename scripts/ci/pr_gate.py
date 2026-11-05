#!/usr/bin/env python3
"""Apply the pull-request body admission gate through GitHub's API."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

if __package__:
    # pylint: disable-next=relative-beyond-top-level
    from .pr_policy import base_template
    # pylint: disable-next=relative-beyond-top-level
    from .pr_content import bug_issue_errors, section_content
    # pylint: disable-next=relative-beyond-top-level
    from .pr_attribution import AttributionError
    # pylint: disable-next=relative-beyond-top-level
    from .pr_commits import commit_reasons
    # pylint: disable-next=relative-beyond-top-level
    from .pr_body import (
        closing_issues, code_span, layout_errors, parse_rendered,
        referenced_issues, retains_instruction_comment,
    )
else:
    from pr_policy import base_template
    from pr_content import bug_issue_errors, section_content
    from pr_attribution import AttributionError
    from pr_commits import commit_reasons
    from pr_body import (
        closing_issues, code_span, layout_errors, parse_rendered,
        referenced_issues, retains_instruction_comment,
    )


BOT = 'github-actions[bot]'
MARKER = '<!-- pr-gate -->'
OVERFLOW_REASON = (
    'This body names more than 20 issue references, so only the first 20 '
    'were checked.')
UNCLAIMED_REASON = 'No checked issue is assigned to you.'
INSTRUCTION_REASON = 'Remove the template instruction comments.'
# An inadmissible pull request fails the gate's own check with this code,
# distinct from the 1 an aborted run returns, so a ruleset that requires
# the check holds the merge until the body is fixed.
INADMISSIBLE_EXIT = 2
_STATUS_LINE = re.compile(r'^HTTP/\S+ ([0-9]{3})(?: |$)')


class Response(NamedTuple):
    status: int
    data: object


class GhApi:
    """Runs `gh api`. request() never raises for an HTTP status; it raises
    RuntimeError only when gh could not be run or its output is unparsable.
    """

    def __init__(self):
        self.gh = shutil.which('gh')

    def request(self, method: str, endpoint: str,
                payload: dict | None = None) -> Response:
        if endpoint.startswith('search/commits?'):
            return self._request(
                method, endpoint, payload, fields=('per_page=1',),
                headers=('Accept: application/vnd.github+json',))
        return self._request(method, endpoint, payload)

    def _request(self, method, endpoint, payload=None, fields=(), headers=()):
        if self.gh is None:
            raise RuntimeError('gh was not found on PATH')
        arguments = [
            self.gh, 'api', '--include', '-X', method, endpoint]
        for header in headers:
            arguments.extend(('-H', header))
        for field in fields:
            arguments.extend(('-f', field))
        with tempfile.TemporaryDirectory(prefix='pr-gate-') as directory:
            if payload:
                path = Path(directory) / 'payload.json'
                path.write_text(json.dumps(payload), encoding='utf-8')
                arguments.extend(('--input', str(path)))
            try:
                completed = subprocess.run(
                    arguments, capture_output=True, check=False)
                # Decode in this thread: Windows text-mode pipe readers can
                # otherwise lose decoding exceptions in their reader threads.
                stdout = completed.stdout.decode('utf-8', errors='strict')
                stderr = completed.stderr.decode('utf-8', errors='strict')
            except OSError as error:
                raise RuntimeError(f'could not run gh: {error}') from error
            except UnicodeError as error:
                raise RuntimeError('could not decode gh response as UTF-8') from error
        lines = stdout.splitlines()
        match = _STATUS_LINE.match(lines[0]) if lines else None
        if match is None:
            detail = (' '.join(stderr.split())
                      or 'no HTTP status in output')
            raise RuntimeError(f'could not read gh response: {detail}')
        try:
            separator = lines.index('')
        except ValueError as error:
            raise RuntimeError('could not read gh response headers') from error
        headers = {}
        for line in lines[1:separator]:
            name, found, value = line.partition(':')
            if found:
                key = name.lower()
                if key == 'content-type' and key in headers:
                    raise RuntimeError(
                        'duplicate gh response header: content-type')
                headers[key] = value.strip()
        body = '\n'.join(lines[separator + 1:])
        media_type = headers.get('content-type', '').partition(';')[0].lower()
        if media_type == 'text/html':
            data = body
        elif media_type == 'application/json' or media_type.endswith('+json'):
            try:
                data = json.loads(body) if body else None
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    'could not parse gh response body') from error
        else:
            detail = media_type or 'missing'
            raise RuntimeError(
                f'unsupported gh response media type: {detail}')
        return Response(int(match.group(1)), data)

    def paginate(self, endpoint: str) -> Response:
        items = []
        commit_list = re.search(r'/pulls/[0-9]+/commits$', endpoint) is not None
        page_limit = 3 if commit_list else 50
        for page in range(1, page_limit + 1):
            response = self._request(
                'GET', endpoint,
                fields=('per_page=100', f'page={page}'))
            if response.status != 200:
                return response
            if not isinstance(response.data, list):
                raise RuntimeError('paginated gh response is not a list')
            items.extend(response.data)
            if len(response.data) < 100:
                return Response(200, items)
        if commit_list:
            raise RuntimeError('gh pagination exceeded 3 commit pages')
        raise RuntimeError('gh pagination exceeded 50 pages')


class _GateError(RuntimeError):
    """A gate-level API failure: GitHub's refusal or a failed transport."""


_BODY_DETAIL_LIMIT = 300


def _body_detail(data):
    """Renders a failed write's response body as one bounded line.

    GitHub's refusal reason travels in the body a 422 returns, so the
    error string must carry the body's `message` and `errors` beside
    the status: without them the run's error line quotes a status code
    and nothing else. Rendering is compact JSON with sorted keys, so
    the same body always renders identically, collapsed to one line and
    cut at `_BODY_DETAIL_LIMIT` characters. An absent or empty body
    renders as the empty string and the message stays status-only.
    """
    if not data:
        return ''
    if isinstance(data, dict):
        parts = []
        message = data.get('message')
        if isinstance(message, str) and message:
            parts.append(message)
        errors = data.get('errors')
        if isinstance(errors, list) and errors:
            parts.append('; '.join(
                error if isinstance(error, str) else json.dumps(
                    error, sort_keys=True, separators=(',', ':'))
                for error in errors))
        detail = '; '.join(parts)
    else:
        detail = json.dumps(data, sort_keys=True, separators=(',', ':'))
    detail = ' '.join(detail.split())
    if len(detail) > _BODY_DETAIL_LIMIT:
        detail = detail[:_BODY_DETAIL_LIMIT - 3] + '...'
    return detail


def _response(api, method, endpoint, payload=None):
    try:
        response = api.request(method, endpoint, payload)
    except RuntimeError as error:
        raise _GateError(str(error)) from error
    return response


def _page(api, endpoint):
    try:
        response = api.paginate(endpoint)
    except RuntimeError as error:
        raise _GateError(str(error)) from error
    if response.status != 200 or not isinstance(response.data, list):
        raise _GateError(f'GitHub returned {response.status} for {endpoint}')
    return response.data


def _read(api, method, endpoint, payload=None):
    response = _response(api, method, endpoint, payload)
    if response.status != 200:
        raise _GateError(f'GitHub returned {response.status} for {endpoint}')
    return response.data


def _write(api, method, endpoint, payload):
    response = _response(api, method, endpoint, payload)
    if not 200 <= response.status < 300:
        detail = _body_detail(response.data)
        suffix = f': {detail}' if detail else ''
        raise _GateError(
            f'GitHub returned {response.status} for {endpoint}{suffix}')


def _gate_comment(comments):
    for comment in comments:
        user = comment.get('user') or {}
        body = comment.get('body') or ''
        lines = (line.rstrip('\r') for line in body.splitlines())
        if user.get('login') == BOT and MARKER in lines:
            return comment
    return None


def _revalidate(api, pull_endpoint):
    pull = _read(api, 'GET', pull_endpoint)
    if not isinstance(pull, dict):
        raise _GateError('pull request response is not an object')
    if pull.get('merged'):
        raise _GateError('pull request was merged during analysis')
    if pull.get('state') != 'open':
        raise _GateError('pull request state changed during analysis')


def _issue_records(api, repo, numbers):
    records = {}
    for number in numbers[:20]:
        endpoint = f'repos/{repo}/issues/{number}'
        response = _response(api, 'GET', endpoint)
        if response.status == 404:
            records[number] = None
            continue
        if response.status != 200 or not isinstance(response.data, dict):
            raise _GateError(
                f'GitHub returned {response.status} for {endpoint}')
        records[number] = response.data
    return records


def _claim(issues, closing, actor, records):
    claimed = None
    unassigned = []
    for number in issues[:20]:
        issue = records[number]
        if issue is None or 'pull_request' in issue:
            continue
        assignees = issue.get('assignees') or []
        if any(item.get('login') == actor for item in assignees):
            claimed = claimed or number
        elif number in closing:
            unassigned.append(number)
    return claimed, unassigned


def _unassigned_reason(numbers):
    # The numbers are code-spanned: a bare #N in a posted comment is a live
    # issue reference, and the gate must not cross-reference the very issues
    # it is refusing on behalf of.
    quoted = [code_span(f'#{number}') for number in numbers]
    if len(quoted) == 1:
        names = quoted[0]
    else:
        names = ', '.join(quoted[:-1]) + f' and {quoted[-1]}'
    label = 'Issues' if len(quoted) > 1 else 'Issue'
    verb = 'are' if len(quoted) > 1 else 'is'
    return f'{label} {names} {verb} not assigned to you.'


def _reasons_block(reasons):
    return '\n'.join(f'- {reason}' for reason in reasons)


def _refused_exit(reasons):
    """Reports an inadmissible pull request and fails the gate's check.

    The stderr line states the observation, each reason whole, never a
    diagnosis of cause.
    """
    print(
        'pr gate: the pull request is inadmissible '
        f'(exit {INADMISSIBLE_EXIT}): {"; ".join(reasons)}',
        file=sys.stderr)
    return INADMISSIBLE_EXIT


def _inadmissible_text(actor, reasons):
    return f"""@{actor} — this pull request needs changes before it can be reviewed.
{MARKER}

{_reasons_block(reasons)}

Fix every item above, including these two repository requirements:

1. Make sure the issue you are fixing is assigned to you. Follow the
   repository's contribution instructions or ask its maintainers.
2. Edit this same pull request so its sections match the pull request
   template. Its **Related Issues and Pull Requests** section must name
   the claimed issue by its real number. Any reference counts, so write
   `Fixes #<issue>` when merging this should close the issue and
   `References #<issue>` when it should not.

Until then this gate's check fails, and a ruleset that requires the check
holds the merge. The gate re-checks every edit and push; there is no need
to open a second pull request.
"""


def _resolved_text(actor):
    return (
        f'@{actor} — every condition now passes; nothing further is needed '
        'from you.\n'
        f'{MARKER}\n')


def _write_comment(api, repo, pr, comment, body):
    if comment is None:
        endpoint = f'repos/{repo}/issues/{pr}/comments'
        _write(api, 'POST', endpoint, {'body': body})
    else:
        endpoint = f"repos/{repo}/issues/comments/{comment['id']}"
        _write(api, 'PATCH', endpoint, {'body': body})


def _run(api, repo, pr, actor, template, template_path,
         require_commit_attribution=False, conventional_commits=False):
    pull_endpoint = f'repos/{repo}/pulls/{pr}'
    pull = _read(api, 'GET', pull_endpoint)
    if not isinstance(pull, dict):
        raise _GateError('pull request response is not an object')
    body = pull.get('body') or ''
    if pull.get('merged') or pull.get('state') != 'open':
        print('nothing to do')
        return 0

    comments = _page(
        api, f'repos/{repo}/issues/{pr}/comments')
    comment = _gate_comment(comments)

    if template is None:
        try:
            template = base_template(
                lambda endpoint: _read(api, 'GET', endpoint), repo, pull, template_path)
        except ValueError as error:
            raise _GateError(str(error)) from error

    rendered = _read(
        api, 'POST', 'markdown',
        {'text': body, 'mode': 'gfm', 'context': repo})
    if not isinstance(rendered, str):
        raise _GateError('markdown response is not text')
    try:
        parsed = parse_rendered(rendered, repo)
    except ValueError as error:
        raise _GateError(
            f'could not analyze rendered body: {error}') from error
    sections = parsed.sections

    layout = layout_errors(sections, template) + list(parsed.notes)
    if retains_instruction_comment(body, template):
        layout.append(INSTRUCTION_REASON)
    content_errors, bug_pointers = section_content(sections)
    layout.extend(content_errors)
    references = referenced_issues(sections)
    closing = closing_issues(parsed)
    known = set(references)
    for number in closing:
        if number not in known:
            known.add(number)
            references.append(number)
    all_references = list(dict.fromkeys([
        *references, *(number for number, _title in bug_pointers)]))
    records = _issue_records(api, repo, all_references)
    claimed, unassigned = _claim(references, set(closing), actor, records)
    layout.extend(bug_issue_errors(bug_pointers, records))
    if require_commit_attribution or conventional_commits:
        try:
            layout.extend(commit_reasons(
                api, repo, pr, actor, pull,
                attribution=require_commit_attribution,
                conventional=conventional_commits))
        except AttributionError as error:
            raise _GateError(str(error)) from error
    reasons = list(layout)
    if len(all_references) > 20:
        reasons.append(OVERFLOW_REASON)
    elif unassigned:
        reasons.append(_unassigned_reason(unassigned))
    elif claimed is None:
        reasons.append(UNCLAIMED_REASON)

    if not reasons:
        if comment is not None:
            _revalidate(api, pull_endpoint)
            _write_comment(api, repo, pr, comment, _resolved_text(actor))
        print(f'covered by claimed issue {claimed}')
        return 0

    _revalidate(api, pull_endpoint)
    _write_comment(
        api, repo, pr, comment, _inadmissible_text(actor, reasons))
    print('commented')
    return _refused_exit(reasons)


def run(api, repo: str, pr: str, actor: str, template: str | None = None,
        template_path: str = '.github/PULL_REQUEST_TEMPLATE.md',
        require_commit_attribution: bool = False,
        conventional_commits: bool = False) -> int:
    """Runs one gate pass and returns the process exit code.

    Zero when the pull request is admissible, merged or not open.
    `INADMISSIBLE_EXIT` when the gate found defects and commented on
    them, with one stderr line naming the observed defects. One when the
    analysis or an API call failed.
    """
    try:
        return _run(
            api, repo, pr, actor, template, template_path,
            require_commit_attribution=require_commit_attribution,
            conventional_commits=conventional_commits)
    except _GateError as error:
        print(f'pr gate failed: {error}', file=sys.stderr)
        return 1


def _environment(name: str, default: str | None = None) -> str:
    try:
        return os.environ[f'PR_GATE_{name}']
    except KeyError:
        if default is None:
            return os.environ[name]
        return os.environ.get(name, default)


def main() -> int:
    try:
        repo = _environment('REPO')
        pr = _environment('PR')
        actor = _environment('ACTOR')
    except (KeyError, OSError) as error:
        print(f'pr gate failed: {error}', file=sys.stderr)
        return 1
    require_commit_attribution = (
        _environment('REQUIRE_COMMIT_ATTRIBUTION', 'false')
        .strip().casefold() == 'true')
    # Only 'false' opts out; the input has no unprefixed legacy name.
    conventional_commits = os.environ.get(
        'PR_GATE_CONVENTIONAL_COMMITS', 'true').strip().casefold() != 'false'
    return run(
        GhApi(), repo, pr, actor,
        template_path=_environment(
            'TEMPLATE_PATH', '.github/PULL_REQUEST_TEMPLATE.md'),
        require_commit_attribution=require_commit_attribution,
        conventional_commits=conventional_commits)


if __name__ == '__main__':
    raise SystemExit(main())
