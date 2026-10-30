"""A06/A07: the base LLM client must never forward or persist credentials."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import llm_client
from orchestrator.llm_client import LLMClient, LLMTransportError, LLMValidationError


KEY = "synthetic-credential-value"
ENV = "FIXTURE_CREDENTIAL"


@contextmanager
def server(status=200, content='{"ok": true}', raw=None, location=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
            received.append({'method': self.command, 'path': self.path,
                             'headers': dict(self.headers), 'body': body})
            self.send_response(status)
            if location:
                self.send_header('Location', location)
            self.end_headers()
            payload = raw if raw is not None else json.dumps({
                'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                'usage': {'total_tokens': 8}})
            self.wfile.write(payload.encode())
        def do_POST(self): self._record()
        def do_GET(self): self._record()
        def log_message(self, *args): pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}/v1', received
    finally:
        http.shutdown(); thread.join(); http.server_close()


def settings(url, **extra):
    return {'llm': {'provider': 'openai_compatible', 'base_url': url, 'model': 'fixture-model',
                    'api_key_env': ENV, 'max_retries': 0, 'max_schema_retries': 0,
                    'cache': False, 'timeout_seconds': 5, **extra}}


def make_client(tmp_path: Path, cfg: dict, **kwargs) -> LLMClient:
    return LLMClient(root=tmp_path, run=tmp_path / 'run', cfg=cfg,
                     audit_dir=tmp_path / 'audit', **kwargs)


def call(client):
    return client.chat_json('fixture', 'Reply with JSON only.', 'Return {"ok": true}.', 'fixture')


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_base_client_does_not_follow_cross_origin_redirects(tmp_path, monkeypatch, status):
    monkeypatch.setenv(ENV, KEY)
    with server() as (target, target_requests):
        with server(status=status, raw='redirect', location=target + '/chat/completions') as (url, source_requests):
            with pytest.raises(LLMTransportError):
                call(make_client(tmp_path, settings(url)))
        assert len(source_requests) == 1
        assert source_requests[0]['headers'].get('Authorization') == 'Bearer ' + KEY
        assert target_requests == []


def test_redirect_handler_refuses_https_to_http_downgrade():
    handler = llm_client._NoRedirect()
    request = llm_client.urllib.request.Request(
        'https://trusted.example/v1/chat/completions', data=b'{}',
        headers={'Authorization': 'Bearer ' + KEY}, method='POST')
    assert handler.redirect_request(request, None, 302, 'Found', {},
                                    'http://other.example/capture') is None


def test_success_echo_is_rejected_and_audit_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    raw = json.dumps({'choices': [{'message': {'content': KEY}, 'finish_reason': 'stop'}],
                      'usage': {'total_tokens': 8}})
    with server(status=200, raw=raw) as (url, _):
        with pytest.raises(LLMValidationError, match='credential'):
            call(make_client(tmp_path, settings(url, cache=True)))
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert KEY not in path.read_text(encoding='utf-8', errors='replace')


@pytest.mark.parametrize('status', [401, 403])
def test_http_error_echo_is_redacted(tmp_path, monkeypatch, status):
    monkeypatch.setenv(ENV, KEY)
    with server(status=status, raw='rejected ' + KEY) as (url, _):
        with pytest.raises(LLMTransportError) as failure:
            call(make_client(tmp_path, settings(url)))
    assert KEY not in str(failure.value)
    for path in (tmp_path / 'audit').rglob('*.json'):
        assert KEY not in path.read_text(encoding='utf-8')


def test_direct_status_error_and_retry_exhaustion_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    calls = []
    def transport(url, headers, body, timeout):
        calls.append(url)
        return 500, 'upstream echoed ' + KEY
    monkeypatch.setattr(llm_client.time, 'sleep', lambda seconds: None)
    client = make_client(tmp_path, settings('http://127.0.0.1:1/v1', max_retries=1), transport=transport)
    with pytest.raises(LLMTransportError) as failure:
        call(client)
    assert len(calls) == 2 and KEY not in str(failure.value)
    for path in (tmp_path / 'audit').rglob('*.json'):
        assert KEY not in path.read_text(encoding='utf-8')

    direct = make_client(tmp_path / 'direct', settings('http://127.0.0.1:1/v1'),
                         transport=lambda url, headers, body, timeout: (401, 'denied ' + KEY))
    with pytest.raises(LLMTransportError) as failure:
        call(direct)
    assert KEY not in str(failure.value)
    for path in (tmp_path / 'direct/audit').rglob('*.json'):
        assert KEY not in path.read_text(encoding='utf-8')


def test_success_audit_and_cache_do_not_contain_a_credential(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    with server() as (url, requests):
        result = call(make_client(tmp_path, settings(url, cache=True)))
        assert result['parsed'] == {'ok': True}
        assert requests and requests[0]['headers']['Authorization'] == 'Bearer ' + KEY
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert KEY not in path.read_text(encoding='utf-8', errors='replace')
