"""Read consumer policy at the immutable base commit in the PR snapshot."""
import base64
import re
from urllib.parse import quote

if __package__:
    from .pr_body import template_rules
else:
    from pr_body import template_rules


def base_template(read, repository, pull, path):
    base = pull.get('base')
    sha = base.get('sha') if isinstance(base, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r'[0-9a-fA-F]{40}', sha) is None:
        raise ValueError('pull request has no immutable base SHA')
    if (not path or any(part in ('', '.', '..') for part in path.split('/'))
            or any(ord(char) < 32 for char in path)):
        raise ValueError('template path must be a relative repository file path')
    endpoint = f'repos/{repository}/contents/{quote(path, safe="/")}?ref={sha}'
    data = read(endpoint)
    if (not isinstance(data, dict) or data.get('type') != 'file'
            or data.get('encoding') != 'base64' or not isinstance(data.get('content'), str)):
        raise ValueError('base template response is not a base64 file')
    try:
        template = base64.b64decode(''.join(data['content'].split()), validate=True).decode('utf-8')
    except (ValueError, UnicodeError) as error:
        raise ValueError('base template is not valid base64 UTF-8') from error
    if not template_rules(template):
        raise ValueError('base template defines no sections')
    return template
