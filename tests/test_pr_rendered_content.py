"""Actual GitHub renderings retain one authoritative section interpretation."""
import json
from pathlib import Path
import _util
from _prgate import PR_BODY, ROOT, _gate_module
from test_pr_gate_context import RepositoryApi

CASES = json.loads((Path(__file__).parent / 'pr_content_renderings.json').read_text(
    encoding='utf-8'))
TEMPLATE = (ROOT / '.github/PULL_REQUEST_TEMPLATE.md').read_text(encoding='utf-8')


def test_captured_rendered_section_boundaries_are_admitted(tmp):
    del tmp
    for case in CASES:
        parsed = PR_BODY.parse_rendered(case['rendered'], case['repository'])
        assert PR_BODY.layout_errors(parsed.sections, TEMPLATE) == []
        assert parsed.notes == ()
        api = RepositoryApi(case['repository'])
        api.pull['body'] = case['source']
        api.rendered = case['rendered']
        api.issues = case['issues']
        assert _gate_module().run(api, case['repository'], '99', 'alice', TEMPLATE) == 0
        assert api.pull['state'] == 'open' and api.writes == [], case['name']


def test_captured_code_whitespace_preserves_visible_line_breaks(tmp):
    del tmp
    capture = json.loads((Path(__file__).parent / 'pr_code_whitespace.json').read_text(
        encoding='utf-8'))
    expected = {'plain_paragraph_lf': 'open', 'single_line_code': 'open', 'code_lf': 'closed'}
    assert {case['name'] for case in capture['cases']} == set(expected)
    for case in capture['cases']:
        parsed = PR_BODY.parse_rendered(case['rendered'], 'owner/repo')
        assert PR_BODY.layout_errors(parsed.sections, TEMPLATE) == []
        assert parsed.notes == () and PR_BODY.closing_issues(parsed) == [101]
        api = RepositoryApi('owner/repo')
        api.pull['body'] = case['source']
        api.rendered = case['rendered']
        assert _gate_module().run(api, 'owner/repo', '99', 'alice', TEMPLATE) == 0
        assert api.pull['state'] == expected[case['name']], case['name']
        if expected[case['name']] == 'open':
            assert api.writes == [], case['name']
        else:
            assert 'Footer' in api.comments[0]['body']


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prrendered_'))
