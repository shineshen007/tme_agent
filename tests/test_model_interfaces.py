"""Real HTTP protocol and headless settings checks; no external API calls."""
import asyncio
import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from multiomics_tui import model_config as config
from multiomics_tui.llm import load_tui_config
from multiomics_tui.model_client import CompatibleClient, test_connection as probe_connection
from orchestrator.llm_client import LLMConfigError, LLMTransportError, LLMValidationError


@pytest.fixture(autouse=True)
def isolated_keys(monkeypatch):
    monkeypatch.setattr(config, '_keys', {})
    monkeypatch.setattr(config, '_env_bindings', {})
    monkeypatch.setattr(config, '_known_secrets', set())
    # 测试不得依赖或消费环境中真实存在的密钥。
    for name in ('DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'OLLAMA_API_KEY', 'LM_STUDIO_API_KEY'):
        monkeypatch.delenv(name, raising=False)


@contextmanager
def server(status=200, content='{"ok": true}', raw=None, location=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append({'path': self.path, 'headers': dict(self.headers), 'body': request})
            self.send_response(status)
            if location:
                self.send_header('Location', location)
            self.end_headers()
            payload = raw if raw is not None else json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 8}})
            self.wfile.write(payload.encode())
        def log_message(self, *args): pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}/v1', received
    finally:
        http.shutdown(); thread.join(); http.server_close()


def settings(url, **extra):
    return {'llm': config.normalize({'provider': 'ollama', 'base_url': url, 'model': 'fixture-model',
                                   'max_retries': 0, 'max_schema_retries': 0, 'cache': False, **extra})}


def client(root, cfg):
    return CompatibleClient(root=root, run=root/'run', cfg=cfg)


def call(model):
    return model.chat_json('fixture', 'Reply with JSON only.', 'Return {"ok": true}.', 'fixture')


def test_local_no_key_actual_http(tmp_path, monkeypatch):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    with server() as (url, requests):
        result = call(client(tmp_path, settings(url)))
        assert result['parsed'] == {'ok': True}
    request = requests[0]
    assert request['path'] == '/v1/chat/completions'
    assert 'Authorization' not in request['headers']
    assert request['body']['model'] == 'fixture-model'
    assert request['body']['response_format'] == {'type': 'json_object'}


def test_bearer_and_parameter_compatibility(tmp_path):
    with server() as (url, requests):
        cfg = settings(url, provider='openai_compatible', auth_mode='bearer',
                       temperature=None, max_tokens_field='max_completion_tokens', json_mode='prompt', thinking='enabled')
        config.store_key(cfg, 'synthetic-compatible-value')
        call(client(tmp_path, cfg))
    assert requests[0]['headers']['Authorization'] == 'Bearer synthetic-compatible-value'
    body = requests[0]['body']
    assert body['max_completion_tokens'] == 8000
    assert not {'thinking', 'temperature', 'max_tokens', 'response_format'} & body.keys()


def test_deepseek_legacy_parameter(tmp_path):
    with server() as (url, requests):
        cfg = settings(url, provider='deepseek', thinking='disabled')
        config.store_key(cfg, 'synthetic-deepseek-value')
        call(client(tmp_path, cfg))
    assert requests[0]['body']['thinking'] == {'type': 'disabled'}


def test_short_secret_named_environment_value_is_not_a_credential(monkeypatch):
    """CI 注入的短环境值不得把普通提示词误判为密钥泄漏。"""
    monkeypatch.setenv('BUILD_TOKEN', 'ci-7')
    monkeypatch.setenv('RUNNER_KEY', 'abcd')
    assert config.redact('ci-7 abcd unchanged') == 'ci-7 abcd unchanged'


def test_long_environment_secret_is_still_redacted(monkeypatch):
    value = 'sk-' + 'x' * 32
    monkeypatch.setenv('DEEPSEEK_API_KEY', value)
    assert config.redact('leak ' + value) == 'leak [REDACTED]'


def test_endpoint_key_isolation_and_replacement(monkeypatch):
    one = settings('http://localhost:10001/v1', auth_mode='bearer')
    two = settings('http://localhost:10002/v1', auth_mode='bearer')
    monkeypatch.setenv('OLLAMA_API_KEY', 'ambient-credential-value')
    assert config.credential(one) == 'ambient-credential-value'
    assert config.credential(two) is None
    config.store_key(two, 'replacement-credential-value')
    assert config.credential(two) == 'replacement-credential-value'
    # Environment binding now belongs to two; one cannot inherit its replacement.
    assert config.credential(one) is None
    config.store_key(one, 'separate-credential-value')
    assert config.credential(two) == 'replacement-credential-value'


def test_missing_key_refuses_request(tmp_path):
    with server() as (url, requests):
        with pytest.raises(LLMConfigError):
            call(client(tmp_path, settings(url, auth_mode='bearer')))
        assert requests == []


@pytest.mark.parametrize('status,label', [(401, '认证失败'), (404, '地址或模型'), (400, '参数不受支持')])
def test_connection_http_failures(tmp_path, status, label):
    with server(status=status, raw='fixture error') as (url, _):
        result = probe_connection(tmp_path, settings(url))
    assert result['status'] == 'failed' and label in result['message']


@pytest.mark.parametrize('content,raw', [('not json', None), ('{"unexpected": true}', None), ('', '<html>not API</html>')])
def test_connection_invalid_json(tmp_path, content, raw):
    with server(content=content, raw=raw) as (url, _):
        result = probe_connection(tmp_path, settings(url))
    assert result['status'] == 'failed' and 'JSON' in result['message']


def test_timeout_is_connection_failure(tmp_path):
    model = client(tmp_path, settings('http://localhost:1/v1'))
    def timeout(*args): raise TimeoutError('fixture timeout')
    model._send = timeout
    with pytest.raises(LLMTransportError, match='timeout'):
        call(model)


def test_no_redirect_can_forward_key(tmp_path):
    with server() as (target, incoming):
        with server(status=307, raw='redirect', location=target+'/chat/completions') as (url, requests):
            cfg = settings(url, auth_mode='bearer')
            config.store_key(cfg, 'synthetic-secret-for-redirect')
            with pytest.raises(LLMTransportError):
                call(client(tmp_path, cfg))
        assert len(requests) == 1 and incoming == []


def test_errors_and_audits_redact_arbitrary_key(tmp_path):
    secret = 'arbitrary-bearer-credential'
    with server(status=401, raw='echo '+secret) as (url, _):
        cfg = settings(url, auth_mode='bearer')
        config.store_key(cfg, secret)
        with pytest.raises(LLMTransportError) as exc:
            call(client(tmp_path, cfg))
    assert secret not in str(exc.value)
    assert all(secret not in path.read_text() for path in tmp_path.rglob('*.json'))
    assert config.redact('my key '+secret) == 'my key [REDACTED]'


def test_saved_yaml_and_preset_defaults(project_root):
    original = (project_root/'config/study_config.yaml').read_bytes()
    cfg = config.overlay({'provider': 'deepseek', **config.PRESETS['deepseek']}, {'provider': 'lm_studio', 'model': 'loaded-model'})
    assert cfg['auth_mode'] == 'none' and cfg['api_key_env'] == 'LM_STUDIO_API_KEY'
    config.save_settings(project_root, cfg)
    saved = load_tui_config(project_root)['llm']
    assert saved['model'] == 'loaded-model' and saved['base_url'] == 'http://localhost:1234/v1'
    assert (project_root/'config/study_config.yaml').read_bytes() == original


@pytest.mark.parametrize('extra', [{'api_key': 'forbidden'}, {'base_url': 'https://user:pass@example.com/v1'}, {'base_url': 'http://localhost:1234/v1/chat/completions'}, {'max_tokens': 0}, {'temperature': float('nan')}])
def test_invalid_config_rejected(extra):
    with pytest.raises(ValueError):
        config.overlay(config.normalize({'provider': 'ollama', 'model': 'fixture'}), extra)


def test_task_snapshot_ignores_changed_or_invalid_local(project_root, tmp_path):
    snapshot = tmp_path/'model_config.json'
    cfg = settings('http://localhost:1234/v1')
    snapshot.write_text(json.dumps(cfg['llm']))
    (project_root/'config/api.local.yaml').write_text('broken: [yaml')
    frozen = load_tui_config(project_root, snapshot=snapshot)
    assert frozen['llm']['base_url'] == cfg['llm']['base_url']
    assert frozen['llm']['api_key_env'] == config.WORKER_KEY


def test_worker_only_receives_selected_key(monkeypatch):
    cfg = settings('http://localhost:1234/v1', auth_mode='bearer', api_key_env='CUSTOM_CREDENTIAL')
    monkeypatch.setenv('OTHER_API_KEY', 'unrelated-credential')
    config.store_key(cfg, 'selected-credential')
    env = config.task_environment()
    env.update(config.worker_environment(cfg))
    assert env[config.WORKER_KEY] == 'selected-credential'
    assert 'OTHER_API_KEY' not in env and 'CUSTOM_CREDENTIAL' not in env
    assert config.worker_environment(settings('http://localhost:1234/v1')) == {}


def test_save_busy_refused_and_key_replace(project_root):
    from multiomics_tui.controller import Session
    session = Session(project_root, allow_llm=False)
    session.model_busy = True
    with pytest.raises(ValueError, match='执行'):
        session.save_model(settings('http://localhost:1234/v1')['llm'])
    assert not (project_root/'config/api.local.yaml').exists()
    session.model_busy = False
    assert session.handle('/key').kind == 'need_key'
    session.set_key('first-memory-credential')
    assert session.handle('/key').kind == 'need_key'
    session.set_key('second-memory-credential')
    assert config.credential(session.cfg) == 'second-memory-credential'


def test_settings_panel_save_cancel_and_local_readiness(project_root):
    pytest.importorskip('textual')
    from textual.widgets import Button, Input, Select, Static
    from multiomics_tui.app import MultiomicsApp
    from multiomics_tui.controller import Session
    from multiomics_tui.model_panel import ModelPanel
    async def scenario():
        session = Session(project_root)
        app = MultiomicsApp(session)
        async with app.run_test(size=(90, 32)) as pilot:
            await app._handle_async('/model'); await pilot.pause()
            assert isinstance(app.screen, ModelPanel)
            assert app.screen.query_one('#model-key', Input).password
            assert app.screen.query_one('#model-name', Input).value == 'stub'
            app.screen.query_one('#model-cancel', Button).press(); await pilot.pause()
            assert not (project_root/'config/api.local.yaml').exists()
            await app._handle_async('/model'); await pilot.pause()
            app.screen.query_one('#model-provider', Select).value = 'ollama'; await pilot.pause()
            assert app.screen.query_one('#model-url', Input).value == 'http://localhost:11434/v1'
            app.screen.query_one('#model-name', Input).value = 'fixture-local'
            app.screen.query_one('#model-save', Button).press(); await pilot.pause()
            assert session.cfg['llm']['auth_mode'] == 'none'
            assert session.client is not None
            assert 'ollama/fixture-local' in str(app.query_one('#status-bar', Static).renderable)
            assert '尚未测试' in str(app.query_one('#reply-message', Static).renderable)
            assert session.handle('/key').kind == 'text'
    asyncio.run(scenario())


def test_panel_connection_test_is_explicit_and_does_not_save(project_root):
    pytest.importorskip('textual')
    from textual.widgets import Button, Input, Select, Static
    from multiomics_tui.app import MultiomicsApp
    from multiomics_tui.controller import Session
    async def scenario(url, incoming):
        session = Session(project_root)
        app = MultiomicsApp(session)
        async with app.run_test(size=(50, 24)) as pilot:
            await app._handle_async('/model'); await pilot.pause()
            assert incoming == []
            for ident in ('model-save', 'model-test', 'model-cancel'):
                region = app.screen.query_one('#'+ident, Button).region
                assert 0 <= region.x and region.right <= 50
                assert 0 <= region.y and region.bottom <= 24
            app.screen.query_one('#model-provider', Select).value = 'ollama'; await pilot.pause()
            app.screen.query_one('#model-url', Input).value = url
            app.screen.query_one('#model-name', Input).value = 'fixture-model'
            app.screen.query_one('#model-test', Button).press()
            for _ in range(40):
                await pilot.pause()
                if '检查通过' in str(app.screen.query_one('#model-notice', Static).renderable):
                    break
            assert len(incoming) == 1
            assert not (project_root/'config/api.local.yaml').exists()
            assert session.cfg['llm'].get('provider') != 'ollama'
            app.screen.query_one('#model-save', Button).press(); await pilot.pause()
            assert session.connection_tested()
            assert 'AI checked' in str(app.query_one('#status-bar', Static).renderable)
    with server() as (url, incoming):
        asyncio.run(scenario(url, incoming))


def test_active_job_prevents_settings_save(project_root, monkeypatch):
    from multiomics_tui.controller import Session
    session = Session(project_root)
    monkeypatch.setattr(session.jobs, 'active_pipeline', lambda: {'job_id': 'fixture'})
    assert session.handle('/model').kind == 'text'
    with pytest.raises(ValueError, match='执行'):
        session.save_model(settings('http://localhost:1234/v1')['llm'])
