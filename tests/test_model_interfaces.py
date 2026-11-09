"""Real HTTP protocol and headless settings checks; no external API calls."""
import asyncio
import copy
import json
import os
import threading
import urllib.request
from urllib.parse import urlsplit
from unittest.mock import Mock
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
            if request.get('response_format') == {'type': 'json_object'} and not any(
                    'json' in message['content'].lower() for message in request['messages']):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Prompt must contain the word json"}}')
                return
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


def call(model, system='只返回一个对象。', role='fixture'):
    return model.chat_json(role, system, '只返回结果。', 'fixture')


@pytest.mark.parametrize('mode,bypass', [('inherit', False), ('direct', False), ('inherit', True)])
@pytest.mark.parametrize('prompt', ['plain', 'method_search'])
def test_local_no_key_actual_http(tmp_path, monkeypatch, mode, bypass, prompt):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    environment = dict(os.environ)
    with server() as (proxy_url, proxy_requests), server() as (url, requests):
        proxy = urlsplit(proxy_url)
        monkeypatch.setattr(urllib.request, 'getproxies', Mock(return_value={
            'http': f'http://synthetic-proxy-user:synthetic-proxy-password@{proxy.netloc}'}))
        monkeypatch.setattr(urllib.request, 'proxy_bypass', Mock(return_value=bypass))
        system = '只返回一个对象。'
        role = 'fixture'
        if prompt == 'method_search':
            from multiomics_tui.planning_evidence import PROPOSAL
            from multiomics_tui.task_planner import CLARIFICATIONS
            system = PROPOSAL + '{"profiles": []}\n' + CLARIFICATIONS
            role = 'tui_method_search'
        result = call(client(tmp_path, settings(url, proxy_mode=mode)), system, role)
        assert result['parsed'] == {'ok': True}
        routed = mode == 'inherit' and not bypass
        assert len(proxy_requests) == int(routed) and len(requests) == int(not routed)
    request = (proxy_requests if routed else requests)[0]
    assert request['path'] == (url + '/chat/completions' if routed else '/v1/chat/completions')
    assert 'Authorization' not in request['headers']
    assert request['body']['model'] == 'fixture-model'
    assert request['body']['response_format'] == {'type': 'json_object'}
    assert request['body']['messages'][0]['content'] == system + '\nReturn a valid JSON object only.'
    assert dict(os.environ) == environment
    record = json.loads(next((tmp_path / 'run/report/agent/calls').glob('*.json')).read_text())
    assert record['request_messages'] == request['body']['messages']
    assert record['role'] == role
    attempt = record['attempts'][0]
    assert attempt['proxy_mode'] == mode
    assert attempt['proxy_host'] == (proxy.hostname if routed else None)
    assert attempt['proxy_port'] == (proxy.port if routed else None)
    assert attempt['proxy_bypassed'] == (bypass and mode == 'inherit')
    assert 'synthetic-proxy-user' not in json.dumps(record)
    assert 'synthetic-proxy-password' not in json.dumps(record)


def test_bearer_and_parameter_compatibility(tmp_path):
    with server() as (url, requests):
        cfg = settings(url, provider='openai_compatible', auth_mode='bearer',
                       temperature=None, max_tokens_field='max_completion_tokens', json_mode='prompt', thinking='enabled')
        config.store_key(cfg, 'synthetic-compatible-value')
        call(client(tmp_path, cfg))
    assert requests[0]['headers']['Authorization'] == 'Bearer synthetic-compatible-value'
    body = requests[0]['body']
    assert body['max_completion_tokens'] == 32000
    assert not {'thinking', 'temperature', 'max_tokens', 'response_format'} & body.keys()
    assert 'JSON' in body['messages'][0]['content']


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


@pytest.mark.parametrize('status,label', [(401, 'Authentication failed'), (404, 'Endpoint or model'), (400, 'Unsupported request parameters')])
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
    with pytest.raises(ValueError, match='running'):
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
            app.screen.query_one('#model-proxy', Select).value = 'direct'
            app.screen.query_one('#model-save', Button).press(); await pilot.pause()
            assert session.cfg['llm']['auth_mode'] == 'none'
            assert session.cfg['llm']['proxy_mode'] == 'direct'
            assert load_tui_config(project_root)['llm']['proxy_mode'] == 'direct'
            session._model_checked = config.public_settings(session.cfg)
            assert session.connection_tested()
            session.save_model({**config.public_settings(session.cfg), 'proxy_mode': 'inherit'})
            assert not session.connection_tested()
            assert session._model_checked is None
            # Saved settings are also carried through the existing worker snapshot.
            snapshot = project_root / 'worker-model.json'
            snapshot.write_text(json.dumps(config.public_settings(session.cfg)))
            assert load_tui_config(project_root, snapshot)['llm']['proxy_mode'] == 'inherit'
            assert session.client is not None
            assert 'ollama/fixture-local' in str(app.query_one('#status-bar', Static).renderable)
            assert 'has not been tested' in str(app.query_one('#reply-message', Static).renderable)
            assert session.handle('/key').kind == 'text'
    asyncio.run(scenario())


def test_settings_panel_hides_advanced_fields(project_root):
    pytest.importorskip('textual')
    from multiomics_tui.app import MultiomicsApp
    from multiomics_tui.controller import Session
    from multiomics_tui.model_panel import ModelPanel

    async def scenario():
        session = Session(project_root, allow_llm=False)
        app = MultiomicsApp(session)
        async with app.run_test(size=(90, 32)) as pilot:
            await app._handle_async('/model'); await pilot.pause()
            assert isinstance(app.screen, ModelPanel)
            for ident in ('model-env', 'model-json', 'model-timeout', 'model-tokens',
                          'model-token-field', 'model-temperature'):
                assert not app.screen.query('#' + ident)
            for ident in ('model-provider', 'model-url', 'model-name', 'model-auth', 'model-key', 'model-proxy'):
                assert app.screen.query('#' + ident)
    asyncio.run(scenario())


def test_settings_panel_preserves_hidden_parameters_and_switches_preset(project_root):
    pytest.importorskip('textual')
    import yaml
    from textual.widgets import Button, Select
    from multiomics_tui.app import MultiomicsApp
    from multiomics_tui.controller import Session

    custom = config.normalize({
        'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
        'model': 'stub-custom', 'auth_mode': 'bearer', 'api_key_env': 'CUSTOM_KEY',
        'json_mode': 'prompt', 'timeout_seconds': 300, 'max_tokens': 32000,
        'max_tokens_field': 'max_completion_tokens', 'temperature': 0.2,
    })
    config.save_settings(project_root, custom)

    async def scenario():
        session = Session(project_root, allow_llm=False)
        app = MultiomicsApp(session)
        async with app.run_test(size=(90, 32)) as pilot:
            await app._handle_async('/model'); await pilot.pause()
            panel = app.screen
            assert panel.settings['json_mode'] == 'prompt'
            assert panel.settings['timeout_seconds'] == 300
            assert panel.settings['max_tokens'] == 32000
            assert panel.settings['max_tokens_field'] == 'max_completion_tokens'
            assert panel.settings['temperature'] == 0.2

            panel.query_one('#model-save', Button).press(); await pilot.pause()
            saved = yaml.safe_load((project_root / 'config/api.local.yaml').read_text())['llm']
            assert saved['api_key_env'] == 'CUSTOM_KEY'
            assert saved['json_mode'] == 'prompt'
            assert saved['timeout_seconds'] == 300
            assert saved['max_tokens'] == 32000
            assert saved['max_tokens_field'] == 'max_completion_tokens'
            assert saved['temperature'] == 0.2

            await app._handle_async('/model'); await pilot.pause()
            panel = app.screen
            panel.query_one('#model-provider', Select).value = 'ollama'
            await pilot.pause()
            assert panel.settings['api_key_env'] == 'OLLAMA_API_KEY'
            assert panel.query_one('#model-auth', Select).value == 'none'
    asyncio.run(scenario())


def test_default_output_limit_is_32k():
    value = config.normalize({
        'provider': 'deepseek', 'base_url': 'https://api.deepseek.com', 'model': 'stub',
    })
    assert value['max_tokens'] == 32000
    assert value['proxy_mode'] == 'inherit'
    for mode in ('auto', '', None):
        with pytest.raises(ValueError, match='proxy_mode'):
            config.normalize({**value, 'proxy_mode': mode})


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
                if 'checks passed' in str(app.screen.query_one('#model-notice', Static).renderable):
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
    with pytest.raises(ValueError, match='running'):
        session.save_model(settings('http://localhost:1234/v1')['llm'])


@pytest.mark.parametrize('input_text,fallback,expected', [
    ('请用 GSVA ssGSEA PLAGE ZSCORE analyze data/met.csv', 'en', 'zh'),
    ('Analyze data/中文.csv with GSVA', 'zh', 'en'),
    ('Analyze 中文.RDS with GSVA', 'zh', 'en'),
    ('Read 中文.xlsx', 'zh', 'en'),
    ('Use 中文.gmt', 'zh', 'en'),
    ('Analyze `中文` using GSVA', 'zh', 'en'),
    ('Analyze "中文原文" using GSVA', 'zh', 'en'),
    ('Analyze “中文原文” using GSVA', 'zh', 'en'),
    ("Analyze '中文原文' using GSVA", 'zh', 'en'),
    ("Please don't translate '中文原文'", 'zh', 'en'),
    ('```python\nprint("中文")\n```', 'zh', 'zh'),
    ('data/中文.csv', 'zh', 'zh'),
    ('/retry', 'zh', 'zh'),
    ('/ask 请解释 GSVA', 'en', 'zh'),
    ('/ask --summary Explain `中文`', 'zh', 'en'),
    ('请检查 data/raw/input.csv', 'en', 'zh'),
    ('Use data/raw/input.csv', 'zh', 'en'),
    ('123', 'zh', 'zh'),
])
def test_reply_language_ignores_quoted_content_and_identifiers(input_text, fallback, expected):
    from multiomics_tui.language import detect_language
    assert detect_language(input_text, fallback) == expected


def test_model_and_fixed_controls_stay_english_for_chinese_task(project_root, monkeypatch):
    import re
    import asyncio
    from textual.widgets import Button, Collapsible, Input, Select, Static
    from multiomics_tui.app import MultiomicsApp
    from multiomics_tui.controller import Session
    from multiomics_tui.model_panel import ModelPanel
    from multiomics_tui import model_client

    async def scenario():
        session = Session(project_root)
        state = session.tasks.create('请分析 GSVA ssGSEA PLAGE ZSCORE')
        session.active_task = state['task_id']
        session.tasks.set_reply_language(state['task_id'], 'zh')
        app = MultiomicsApp(session)
        async with app.run_test(size=(90, 32)) as pilot:
            await app._handle_async('/session ' + state['task_id'])
            await app._handle_async('/model')
            await pilot.pause()
            assert isinstance(app.screen, ModelPanel)
            for widget in app.screen.query('Static, Button, Collapsible, Input, Select'):
                for attribute in ('renderable', 'label', 'title', 'placeholder', '_options'):
                    assert not re.search(r'[\u3400-\u9fff]', str(getattr(widget, attribute, '')))
            app.screen.query_one('#model-provider', Select).value = 'ollama'
            await pilot.pause()
            app.screen.query_one('#model-name', Input).value = 'fixture'
            app.screen.query_one('#model-proxy', Select).value = 'direct'
            await pilot.pause()
            assert not re.search(r'[\u3400-\u9fff]', str(app.screen.query_one('#model-notice', Static).renderable))
            monkeypatch.setattr(model_client, 'test_connection', lambda *a: {'status': 'passed', 'message': 'Connection checks passed.'})
            app.screen.query_one('#model-test', Button).press()
            await pilot.pause()
            assert 'passed' in str(app.screen.query_one('#model-notice', Static).renderable)
            app.screen.query_one('#model-name', Input).value = ''
            app.screen.query_one('#model-save', Button).press()
            await pilot.pause()
            assert 'Enter model' in str(app.screen.query_one('#model-notice', Static).renderable)
            app.screen.query_one('#model-cancel', Button).press()
            await pilot.pause()
            app._refresh_status()
            for widget in app.query('Button, Collapsible, Input'):
                for attribute in ('label', 'title', 'placeholder'):
                    assert not re.search(r'[\u3400-\u9fff]', str(getattr(widget, attribute, '')))
            assert not re.search(r'[\u3400-\u9fff]', str(app.query_one('#prompt-meta', Static).renderable))
            app._menu.refresh_for('/', busy=False, language='zh')
            await pilot.pause()
            for widget in app._menu.query(Static):
                assert not re.search(r'[\u3400-\u9fff]', str(widget.renderable))
            assert session.reply_language() == 'zh'
    asyncio.run(scenario())


def test_fixed_entrypoint_literals_are_english():
    """Supplement interaction checks; Chinese task content elsewhere remains valid."""
    import ast
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    files = list((root / 'native_dist').glob('*.py'))
    files += [root / 'multiomics_tui' / name for name in
              ('model_panel.py', 'model_client.py', 'model_config.py', 'credentials.py', 'wiki.py')]
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.search(r'[\u3400-\u9fff。；：，、]', node.value), (path.name, node.lineno)
