"""GitHub's code-preserved Bugs text reaches the exact issue-title boundary."""
import json
import os
import subprocess
from pathlib import Path

import yaml
import _util
from _prgate import PR_BODY, ROOT, _gate_module, _recorded_writes, _script_fixtures, _write_gh_stub
from test_action import _action_bash, _action_steps, _expression
from test_pr_gate_context import RepositoryApi

CAPTURE = json.loads((Path(__file__).parent / 'pr_bug_whitespace.json').read_text(
    encoding='utf-8'))
CASES = {case['name']: case for case in CAPTURE['cases']}
TEMPLATE = (ROOT / '.github/PULL_REQUEST_TEMPLATE.md').read_text(encoding='utf-8')


def _decision(case, api_title, verdict, reason=None):
    repository = CAPTURE['repository']
    parsed = PR_BODY.parse_rendered(case['rendered'], repository)
    assert PR_BODY.layout_errors(parsed.sections, TEMPLATE) == []
    assert parsed.notes == () and PR_BODY.closing_issues(parsed) == [55]
    api = RepositoryApi(repository)
    api.pull['body'] = case['source']
    api.rendered = case['rendered']
    api.issues = {'55': {'assignees': [{'login': 'alice'}], 'title': api_title}}
    assert _gate_module().run(
        api, repository, '99', 'alice', TEMPLATE) == (
        0 if verdict == 'admitted' else 2)
    assert api.pull['state'] == 'open', (case['name'], api_title, api.writes)
    if verdict == 'admitted':
        assert api.writes == []
    else:
        assert reason in api.comments[0]['body'], api.comments


def test_captured_one_space_code_title_matches(tmp):
    del tmp
    _decision(CASES['code_one_space'], 'The bug', 'admitted')


def test_captured_two_space_code_title_does_not_match_one_space_api_title(tmp):
    del tmp
    _decision(CASES['code_two_spaces'], 'The bug', 'refused', 'copy its visible title exactly')


def test_captured_code_newline_is_not_a_single_line_bug_pointer(tmp):
    del tmp
    _decision(CASES['code_newline'], 'The bug', 'refused', 'only unordered list items')


def test_code_spaces_are_compared_exactly_not_blanket_rejected(tmp):
    del tmp
    _decision(CASES['code_two_spaces'], 'The  bug', 'admitted')


def test_bug_normal_flow_whitespace_still_collapses(tmp):
    del tmp
    case = CASES['code_two_spaces']
    for title in ('The  bug', 'The\nbug', 'The <em> \n</em>bug', '<code>The</code> \nbug'):
        rendered = case['rendered'].replace('<code class="notranslate">The  bug</code>', title)
        _decision({**case, 'rendered': rendered}, 'The bug', 'admitted')


def test_code_whitespace_survives_inline_nesting_and_list_containers(tmp):
    del tmp
    case = CASES['code_two_spaces']
    rendered = case['rendered'].replace(
        '<code class="notranslate">The  bug</code>', '<code>The<em>  </em>bug</code>')
    rendered = rendered.replace('<ul dir="auto"><li>', '<div><ul dir="auto"><li>')
    rendered = rendered.replace('</li></ul>', '</li></ul></div>')
    _decision({**case, 'rendered': rendered}, 'The bug', 'refused',
              'copy its visible title exactly')
    _decision({**case, 'rendered': rendered}, 'The  bug', 'admitted')


def test_packaged_action_compares_preserved_titles_at_real_gh_boundary(tmp):
    metadata = yaml.safe_load((ROOT / 'action.yml').read_text(encoding='utf-8'))
    _, step = _action_steps(metadata)
    assert step['shell'] == 'bash'
    inputs = {'github-token': 'stub-token', 'repository': CAPTURE['repository'],
              'pull-request-number': '233', 'pull-request-author': 'alice',
              'require-commit-attribution': 'false', 'conventional-commits': 'true',
              'template-path': metadata['inputs']['template-path']['default']}
    outcomes = (('code_one_space', 'The bug', False),
                ('code_two_spaces', 'The bug', True),
                ('code_newline', 'The bug', True),
                ('code_two_spaces', 'The  bug', False))
    for index, (name, title, refused) in enumerate(outcomes):
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
        assert result.returncode == (2 if refused else 0), (
            result.stdout, result.stderr, calls)
        assert any(call['argv'][4] == 'repos/Nitjsefnie/Overflow/issues/55' for call in calls)
        writes = _recorded_writes(calls)
        if refused:
            assert [(method, endpoint) for method, endpoint, _ in writes] == [
                ('POST', 'repos/Nitjsefnie/Overflow/issues/233/comments')], (name, title, writes)
            assert 'Bugs Discovered' in writes[0][2]['body']
        else:
            assert writes == []


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prbugspace_'))
