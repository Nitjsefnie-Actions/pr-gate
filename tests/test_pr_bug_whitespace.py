"""GitHub's code-preserved Bugs text reaches the exact issue-title boundary."""
import json
import os
import subprocess
from pathlib import Path

import yaml
import _util
from _prgate import PR_BODY, ROOT, _gate_module, _recorded_writes, _script_fixtures, _write_gh_stub
from test_action import _action_bash, _expression
from test_pr_gate_context import RepositoryApi

CAPTURE = json.loads((Path(__file__).parent / 'pr_bug_whitespace.json').read_text(
    encoding='utf-8'))
CASES = {case['name']: case for case in CAPTURE['cases']}
TEMPLATE = (ROOT / '.github/PULL_REQUEST_TEMPLATE.md').read_text(encoding='utf-8')


def _decision(case, api_title, state, reason=None):
    repository = CAPTURE['repository']
    parsed = PR_BODY.parse_rendered(case['rendered'], repository)
    assert PR_BODY.layout_errors(parsed.sections, TEMPLATE) == []
    assert parsed.notes == () and PR_BODY.closing_issues(parsed) == [55]
    api = RepositoryApi(repository)
    api.pull['body'] = case['source']
    api.rendered = case['rendered']
    api.issues = {'55': {'assignees': [{'login': 'alice'}], 'title': api_title}}
    assert _gate_module().run(api, repository, '99', 'alice', TEMPLATE) == 0
    assert api.pull['state'] == state, (case['name'], api_title, api.writes)
    if state == 'open':
        assert api.writes == []
    else:
        assert reason in api.comments[0]['body'], api.comments


def test_captured_one_space_code_title_matches(tmp):
    del tmp
    _decision(CASES['code_one_space'], 'The bug', 'open')


def test_captured_two_space_code_title_does_not_match_one_space_api_title(tmp):
    del tmp
    _decision(CASES['code_two_spaces'], 'The bug', 'closed', 'copy its visible title exactly')


def test_captured_code_newline_is_not_a_single_line_bug_pointer(tmp):
    del tmp
    _decision(CASES['code_newline'], 'The bug', 'closed', 'only unordered list items')


def test_code_spaces_are_compared_exactly_not_blanket_rejected(tmp):
    del tmp
    _decision(CASES['code_two_spaces'], 'The  bug', 'open')


def test_bug_normal_flow_whitespace_still_collapses(tmp):
    del tmp
    case = CASES['code_two_spaces']
    for title in ('The  bug', 'The\nbug', 'The <em> \n</em>bug', '<code>The</code> \nbug'):
        rendered = case['rendered'].replace('<code class="notranslate">The  bug</code>', title)
        _decision({**case, 'rendered': rendered}, 'The bug', 'open')


def test_code_whitespace_survives_inline_nesting_and_list_containers(tmp):
    del tmp
    case = CASES['code_two_spaces']
    rendered = case['rendered'].replace(
        '<code class="notranslate">The  bug</code>', '<code>The<em>  </em>bug</code>')
    rendered = rendered.replace('<ul dir="auto"><li>', '<div><ul dir="auto"><li>')
    rendered = rendered.replace('</li></ul>', '</li></ul></div>')
    _decision({**case, 'rendered': rendered}, 'The bug', 'closed', 'copy its visible title exactly')
    _decision({**case, 'rendered': rendered}, 'The  bug', 'open')


def test_packaged_action_compares_preserved_titles_at_real_gh_boundary(tmp):
    metadata = yaml.safe_load((ROOT / 'action.yml').read_text(encoding='utf-8'))
    step, = metadata['runs']['steps']
    assert step['shell'] == 'bash'
    inputs = {'github-token': 'stub-token', 'repository': CAPTURE['repository'],
              'pull-request-number': '233', 'pull-request-author': 'alice',
              'template-path': metadata['inputs']['template-path']['default']}
    outcomes = (('code_one_space', 'The bug', False),
                ('code_two_spaces', 'The bug', True),
                ('code_newline', 'The bug', True),
                ('code_two_spaces', 'The  bug', False))
    for index, (name, title, closed) in enumerate(outcomes):
        directory = Path(tmp) / str(index)
        directory.mkdir()
        binary, _ = _write_gh_stub(directory)
        fixture = _script_fixtures()
        fixture.update(repository=inputs['repository'], pull_number='233',
                       expected_token='stub-token', template=TEMPLATE,
                       rendered=CASES[name]['rendered'],
                       issues={'55': {'assignees': [{'login': 'alice'}], 'title': title}})
        fixture['pull']['body'] = CASES[name]['source']
        fixture_path = directory / 'fixture.json'
        fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
        calls_path = directory / 'calls.jsonl'
        calls_path.write_text('', encoding='utf-8')
        environment = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                       'STUB_FIXTURES': str(fixture_path), 'STUB_CALLS': str(calls_path)}
        environment.update({key: _expression(value, inputs) for key, value in step['env'].items()})
        result = subprocess.run([_action_bash(), '-c', step['run']], cwd=directory,
                                env=environment, capture_output=True, text=True,
                                encoding='utf-8', errors='strict', timeout=30)
        calls = [json.loads(line) for line in calls_path.read_text(encoding='utf-8').splitlines()]
        assert result.returncode == 0, (result.stdout, result.stderr, calls)
        assert any(call['argv'][4] == 'repos/Nitjsefnie/Overflow/issues/55' for call in calls)
        writes = _recorded_writes(calls)
        states = [payload['state'] for method, endpoint, payload in writes
                  if method == 'PATCH' and endpoint == 'repos/Nitjsefnie/Overflow/pulls/233']
        assert states == (['closed'] if closed else []), (name, title, writes)
        if closed:
            assert any('Bugs Discovered' in payload.get('body', '') for _, _, payload in writes)
        else:
            assert writes == []


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prbugspace_'))
