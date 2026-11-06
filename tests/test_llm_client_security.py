"""A06/A07: the base LLM client must never forward or persist credentials."""
from __future__ import annotations

import json
import ssl
import urllib.error
import threading
from contextlib import contextmanager
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import DEFAULT, Mock

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


@pytest.mark.parametrize('failure_kind', ['http_status', 'incomplete_read', 'tls_eof', 'wrapped_tls_eof', 'proxy_tls_eof'])
@pytest.mark.parametrize('retry_limit', [0, 1, 2])
def test_direct_status_error_and_retry_exhaustion_are_redacted(tmp_path, monkeypatch, failure_kind, retry_limit):
    monkeypatch.setenv(ENV, KEY)
    partial = ('UNSAVED_PARTIAL_RESPONSE ' + KEY).encode()
    errors = {'http_status': None, 'incomplete_read': IncompleteRead(partial),
              'tls_eof': ssl.SSLEOFError(8, 'fixture EOF ' + KEY),
              'wrapped_tls_eof': urllib.error.URLError(ssl.SSLEOFError(8, 'fixture EOF ' + KEY)),
              'proxy_tls_eof': urllib.error.URLError(ssl.SSLEOFError(8,
                  'fixture http://proxy-user:proxy-password@127.0.0.1:1082/ proxy-password ' + KEY))}
    transport = Mock(return_value=(500, 'upstream echoed ' + KEY),
                     side_effect=errors[failure_kind])
    sleeper = Mock()
    monkeypatch.setattr(llm_client.time, 'sleep', sleeper)
    client = make_client(tmp_path, settings('http://127.0.0.1:1/v1', max_retries=retry_limit,
                                          max_schema_retries=2, cache=True), transport=transport)
    if failure_kind == 'proxy_tls_eof':
        monkeypatch.setattr(llm_client.urllib.request, 'getproxies', Mock(return_value={
            'http': 'http://proxy-user:proxy-password@127.0.0.1:1082/'}))
        monkeypatch.setattr(llm_client.urllib.request, 'proxy_bypass', Mock(return_value=False))
        monkeypatch.setattr(llm_client.urllib.request, 'build_opener',
                            Mock(return_value=Mock(open=Mock(side_effect=errors[failure_kind]))))
        transport.side_effect = None
        transport.return_value = DEFAULT
        transport._mock_wraps = client._http_post
    with pytest.raises(LLMTransportError) as failure:
        call(client)
    assert transport.call_count == retry_limit + 1 and KEY not in str(failure.value)
    assert failure.value.transport_details['attempt_count'] == retry_limit + 1
    if failure_kind != 'http_status':
        assert failure.value.transport_details['interrupted'] is True
    assert [item.args[0] for item in sleeper.call_args_list] == [min(2 ** i, 8) for i in range(retry_limit)]
    assert not list((tmp_path / '.cache/llm').glob('*.json'))
    for path in (tmp_path / 'audit').rglob('*.json'):
        text = path.read_text(encoding='utf-8')
        assert KEY not in text and 'UNSAVED_PARTIAL_RESPONSE' not in text
        assert 'proxy-user' not in text and 'proxy-password' not in text
        record = json.loads(text)
        assert record['validation'] == 'failed'
        assert len(record['attempts']) == retry_limit + 1
        for index, attempt in enumerate(record['attempts'], 1):
            assert attempt['transport_attempt'] == index and attempt['schema_attempt'] == 1
            assert attempt['latency_s'] is not None and 'response_content' not in attempt
            if failure_kind == 'incomplete_read':
                assert 'IncompleteRead' in attempt['error']

    direct = make_client(tmp_path / 'direct', settings('http://127.0.0.1:1/v1'),
                         transport=Mock(return_value=(401, 'denied ' + KEY)))
    with pytest.raises(LLMTransportError) as failure:
        call(direct)
    assert KEY not in str(failure.value)
    for path in (tmp_path / 'direct/audit').rglob('*.json'):
        assert KEY not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('interrupted', [False, True, 'tls_eof', 'wrapped_tls_eof', 'read_then_tls'])
def test_success_audit_and_cache_do_not_contain_a_credential(tmp_path, monkeypatch, interrupted):
    monkeypatch.setenv(ENV, KEY)
    sleeper = Mock()
    monkeypatch.setattr(llm_client.time, 'sleep', sleeper)
    with server() as (url, requests):
        client = make_client(tmp_path, settings(url, cache=True, max_retries=2))
        failure = (IncompleteRead(('UNSAVED_PARTIAL_RESPONSE ' + KEY).encode()) if interrupted is True
                   else ssl.SSLEOFError(8, 'fixture EOF'))
        if interrupted == 'wrapped_tls_eof':
            failure = urllib.error.URLError(failure)
        client.transport = Mock(wraps=client.transport,
                                side_effect=[failure, DEFAULT]
                                if interrupted else None)
        # Exercise the real reader, then a failed open, then a successful request.
        # The second attempt must not retain the first response's status/stage.
        if interrupted == 'read_then_tls':
            response = Mock(status=200)
            response.read.side_effect = IncompleteRead(('UNSAVED_PARTIAL_RESPONSE ' + KEY).encode())
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            opener = llm_client.urllib.request.build_opener(llm_client._NoRedirect)
            mocked = Mock(wraps=opener)
            mocked.open = Mock(wraps=opener.open, side_effect=[response, failure, DEFAULT])
            monkeypatch.setattr(llm_client.urllib.request, 'build_opener', Mock(return_value=mocked))
            client.transport.side_effect = None
        result = call(client)
        assert result['parsed'] == {'ok': True}
        retries = 2 if interrupted == 'read_then_tls' else int(bool(interrupted))
        assert client.transport.call_count == 1 + retries
        assert sleeper.call_count == retries
        assert requests and requests[0]['headers']['Authorization'] == 'Bearer ' + KEY
        records = [json.loads(p.read_text()) for p in (tmp_path / 'audit').glob('*.json')]
        assert len(records) == 1 and records[0]['validation'] == 'pass'
        attempts = records[0]['attempts']
        assert len(attempts) == 1 + retries
        if interrupted:
            assert attempts[0]['interrupted'] and 'response_content' not in attempts[0]
        if interrupted == 'read_then_tls':
            assert attempts[0]['stage'] == 'reading_response' and attempts[0]['http_status'] == 200
            assert attempts[1]['stage'] == 'connecting' and attempts[1]['http_status'] is None
        assert attempts[-1]['stage'] == 'complete'
        assert json.loads(attempts[-1]['response_content']) == {'ok': True}
        cached = call(client)
        assert cached['cache_hit'] and cached['parsed'] == {'ok': True}
        assert client.transport.call_count == 1 + retries
    for path in tmp_path.rglob('*'):
        if path.is_file():
            text = path.read_text(encoding='utf-8', errors='replace')
            assert KEY not in text and 'UNSAVED_PARTIAL_RESPONSE' not in text
