"""Real gh protocol bytes are strict UTF-8, independent of locale defaults."""
import json
import os
import subprocess
import sys
from pathlib import Path

import _util
from _prgate import ROOT, _write_gh_stub


def _request_in_c_locale(tmp, payload, checks):
    directory, command = _write_gh_stub(tmp)
    script = directory / 'gh.py' if os.name == 'nt' else command
    script.write_text('#!/usr/bin/env python3\nimport os\nos.write(1, '
                      + repr(payload) + ')\n', encoding='utf-8')
    probe = '''import json, locale, os, sys
from scripts.ci.pr_gate import GhApi
assert sys.flags.utf8_mode == 0
encoding = locale.getencoding()
if os.name != 'nt':
    assert encoding.lower().replace('-', '') not in ('utf8', 'cp65001'), encoding
api = GhApi()
api.gh = sys.argv[1]
''' + checks
    environment = {**os.environ, 'LC_ALL': 'C', 'PYTHONUTF8': '0',
                   'PYTHONCOERCECLOCALE': '0'}
    return subprocess.run([sys.executable, '-c', probe, str(command)], cwd=ROOT,
                          env=environment, capture_output=True, text=True,
                          encoding='utf-8', errors='strict', timeout=30)


def test_utf8_response_decodes_in_a_real_non_utf8_locale(tmp):
    payload = (b'HTTP/2 200 OK\ncontent-type: application/json; charset=utf-8\n\n'
               b'{"title":"caf\xc3\xa9"}\n')
    checks = r'''
response = api.request('GET', 'repos/owner/repo/issues/1')
assert response.status == 200
assert response.data == {'title': 'caf\u00e9'}, response.data
print(json.dumps({'encoding': encoding, 'title': response.data['title']}))
'''
    result = _request_in_c_locale(tmp, payload, checks)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)['title'] == 'caf\u00e9'


def test_malformed_utf8_response_is_an_analysis_error(tmp):
    payload = (b'HTTP/2 200 OK\ncontent-type: application/json; charset=utf-8\n\n'
               b'{"title":"caf\xff"}\n')
    checks = '''
try:
    api.request('GET', 'repos/owner/repo/issues/1')
except RuntimeError as error:
    assert 'UTF-8' in str(error), str(error)
else:
    raise AssertionError('malformed UTF-8 protocol response was accepted')
'''
    result = _request_in_c_locale(tmp, payload, checks)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_protocol_is_collected_as_bytes_before_caller_decoding(tmp):
    payload = (b'HTTP/2 200 OK\ncontent-type: application/json; charset=utf-8\n\n'
               b'{"title":"caf\xc3\xa9"}\n')
    checks = r'''
import subprocess
actual_run = subprocess.run
observed = []
def observe_run(*args, **kwargs):
    result = actual_run(*args, **kwargs)
    assert isinstance(result.stdout, bytes), type(result.stdout).__name__
    assert isinstance(result.stderr, bytes), type(result.stderr).__name__
    observed.append(result)
    return result
subprocess.run = observe_run
response = api.request('GET', 'repos/owner/repo/issues/1')
assert len(observed) == 1
assert response.data == {'title': 'caf\u00e9'}, response.data
'''
    result = _request_in_c_locale(tmp, payload, checks)
    assert result.returncode == 0, (result.stdout, result.stderr)


if __name__ == '__main__':
    raise SystemExit(_util.runner(_util.collect(globals()), tmp_prefix='prtransport_'))
