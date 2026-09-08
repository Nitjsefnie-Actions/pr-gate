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


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prrendered_'))
