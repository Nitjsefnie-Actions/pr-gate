"""Public composite inputs and base-pinned consumer policy through real code."""
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml
import _util
from _prgate import (
    ROOT, TEMPLATE, FakeApi, _Response, _gate_module, _issue,
    _pull, _recorded_writes, _script_fixtures, _valid_body, _valid_html,
    _write_gh_stub,
)

BASE = '1234567890abcdef1234567890abcdef12345678'


class TemplateApi(FakeApi):
    def __init__(self, status=200, content=None):
        super().__init__(pull={**_pull(), 'base': {'sha': BASE}},
                         issues={'101': _issue('alice')})
        self.template_reads = []
        self.template_status = status
        self.template = content if content is not None else {
            'type': 'file', 'encoding': 'base64',
            'content': base64.b64encode(TEMPLATE.encode()).decode()}
        self.pull['body'] = _valid_body()

    def request(self, method, endpoint, payload=None):
        if '/contents/' in endpoint:
            self.template_reads.append((method, endpoint))
            return _Response(self.template_status, self.template)
        return super().request(method, endpoint, payload)


def test_template_is_fetched_from_actual_base_with_custom_path(tmp):
    del tmp
    api = TemplateApi()
    assert _gate_module().run(api, 'owner/repo', '99', 'alice', None,
                              'policy/PR template.md') == 0
    assert api.template_reads == [(
        'GET', f'repos/owner/repo/contents/policy/PR%20template.md?ref={BASE}')]
    assert api.writes == []


def test_unavailable_or_invalid_base_template_prevents_writes(tmp):
    del tmp
    cases = ((404, None), (200, {'type': 'dir'}),
             (200, {'type': 'file', 'encoding': 'base64', 'content': '!bad!'}),
             (200, {'type': 'file', 'encoding': 'base64',
                    'content': base64.b64encode(b'## Summary\nNo rule').decode()}))
    for status, content in cases:
        api = TemplateApi(status, content)
        assert _gate_module().run(api, 'owner/repo', '99', 'alice', None) == 1
        assert api.writes == []


def test_template_ref_requires_an_immutable_base_sha(tmp):
    del tmp
    for base in ({}, {'sha': 'main'}, {'sha': '../head'}):
        api = TemplateApi()
        api.pull['base'] = base
        assert _gate_module().run(api, 'owner/repo', '99', 'alice', None) == 1
        assert api.template_reads == [] and api.writes == []


def _expression(value, inputs):
    if value == '${{ github.action_path }}':
        return str(ROOT)
    match = re.fullmatch(r'\$\{\{ inputs\.([a-z-]+) \}\}', value)
    assert match, value
    return inputs[match[1]]


def test_composite_runs_packaged_code_without_a_consumer_checkout(tmp):
    metadata = yaml.safe_load((ROOT / 'action.yml').read_text(encoding='utf-8'))
    assert metadata['runs']['using'] == 'composite'
    for name in ('github-token', 'repository', 'pull-request-number', 'pull-request-author'):
        assert metadata['inputs'][name]['required'] is True
    assert metadata['inputs']['template-path']['default'] == '.github/PULL_REQUEST_TEMPLATE.md'
    steps = metadata['runs']['steps']
    assert len(steps) == 1 and steps[0]['shell'] == 'bash'
    inputs = {'github-token': 'stub-token', 'repository': 'another-owner/project',
              'pull-request-number': '42', 'pull-request-author': 'sasha',
              'template-path': 'policy/PR template.md'}
    directory, _command = _write_gh_stub(tmp)
    fixture = _script_fixtures()
    fixture.update(repository=inputs['repository'], pull_number='42', expected_token='stub-token',
                   template_path=inputs['template-path'], template=TEMPLATE,
                   rendered=_valid_html(repo=inputs['repository']),
                   issues={'101': _issue('sasha')})
    fixture['pull']['body'] = _valid_body()
    fixture['pull']['base'] = {'sha': BASE}
    fixture_path = Path(tmp) / 'fixture.json'
    fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
    calls_path = Path(tmp) / 'calls.jsonl'
    calls_path.write_text('', encoding='utf-8')
    consumer = Path(tmp) / 'consumer'
    (consumer / 'scripts/ci').mkdir(parents=True)
    (consumer / 'scripts/ci/pr_gate.py').write_text('raise SystemExit(79)\n', encoding='utf-8')
    environment = {**os.environ, 'PATH': str(directory) + os.pathsep + os.environ['PATH'],
                   'STUB_FIXTURES': str(fixture_path), 'STUB_CALLS': str(calls_path)}
    environment.update({key: _expression(value, inputs)
                        for key, value in steps[0]['env'].items()})
    result = subprocess.run(['bash', '-c', steps[0]['run']], cwd=consumer,
                            env=environment, capture_output=True, text=True, timeout=30)
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert result.returncode == 0, (result.stdout, result.stderr, calls)
    assert _recorded_writes(calls) == []
    assert any(call['argv'][4] ==
               f'repos/another-owner/project/contents/policy/PR%20template.md?ref={BASE}'
               for call in calls), calls
    assert any(call['input'] == {'text': _valid_body(), 'mode': 'gfm',
                                 'context': 'another-owner/project'} for call in calls)


def test_empty_discovery_is_nonzero(tmp):
    del tmp
    assert _util.runner(_util.collect({}), tmp_prefix='empty_') == 1


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='praction_'))
