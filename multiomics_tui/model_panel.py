"""Model connection editor. No secrets are returned or written to settings."""
from __future__ import annotations

import asyncio
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from .model_config import PRESETS, public_settings, redact


class ModelPanel(ModalScreen):
    DEFAULT_CSS = '''
    ModelPanel { align: center middle; background: #000000 75%; }
    ModelPanel #model-dialog { width: 96%; max-width: 90; height: 90%; border: solid #7ab7ff; background: #101010; padding: 1 2; }
    ModelPanel VerticalScroll { height: 1fr; }
    ModelPanel Static { height: auto; }
    ModelPanel Input, ModelPanel Select { margin-bottom: 1; }
    ModelPanel #model-buttons { height: 3; }
    ModelPanel Button { min-width: 10; margin-right: 1; }
    ModelPanel #model-notice { height: auto; max-height: 5; color: #ffb46b; }
    '''
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.settings = public_settings(session.cfg)
        self._loading = True
        self._testing = False
        self._provider = self.settings['provider']

    def compose(self) -> ComposeResult:
        cfg = self.settings
        with Vertical(id='model-dialog'):
            yield Static('Model connection · /model')
            with VerticalScroll():
                yield Static('Provider / 服务')
                yield Select([('DeepSeek', 'deepseek'), ('OpenAI compatible', 'openai_compatible'), ('Ollama', 'ollama'), ('LM Studio', 'lm_studio')], value=cfg['provider'], allow_blank=False, id='model-provider')
                yield Static('Base URL / 接口基础地址')
                yield Input(cfg['base_url'], id='model-url')
                yield Static('Model / 服务中实际可用的模型名')
                yield Input(cfg['model'], id='model-name')
                yield Static('Authentication / 认证')
                yield Select([('Bearer API key', 'bearer'), ('None / 无认证', 'none')], value=cfg['auth_mode'], allow_blank=False, id='model-auth')
                yield Static('API key / 仅本进程保存，可留空使用已有密钥')
                yield Input(password=True, id='model-key')
                yield Static('Key environment variable / 密钥环境变量名')
                yield Input(cfg['api_key_env'], id='model-env')
                yield Static('JSON response / 返回模式')
                yield Select([('JSON mode', 'json_object'), ('Prompt JSON / 提示词 JSON', 'prompt')], value=cfg['json_mode'], allow_blank=False, id='model-json')
                yield Static('Timeout seconds / 超时秒数')
                yield Input(str(cfg['timeout_seconds']), id='model-timeout')
                yield Static('Output token limit / 输出长度上限')
                yield Input(str(cfg['max_tokens']), id='model-tokens')
                yield Static('Token field / 长度参数字段')
                yield Select([('max_tokens', 'max_tokens'), ('max_completion_tokens', 'max_completion_tokens')], value=cfg['max_tokens_field'], allow_blank=False, id='model-token-field')
                yield Static('Temperature / 留空则不发送此参数')
                yield Input('' if cfg['temperature'] is None else str(cfg['temperature']), id='model-temperature')
            yield Static('Settings only; connection has not been tested. 设置不会自动请求模型。', id='model-notice')
            with Horizontal(id='model-buttons'):
                yield Button('Save', variant='primary', id='model-save')
                yield Button('Test connection', id='model-test')
                yield Button('Cancel', id='model-cancel')

    def on_mount(self):
        self._loading = False

    def on_select_changed(self, event: Select.Changed):
        if self._loading or event.select.id != 'model-provider':
            return
        if str(event.value) == self._provider:
            return
        self._provider = str(event.value)
        preset = PRESETS[str(event.value)]
        for name, field in [('base_url', 'url'), ('model', 'name'), ('api_key_env', 'env')]:
            self.query_one('#model-' + field, Input).value = preset[name]
        self.query_one('#model-auth', Select).value = preset['auth_mode']
        self.query_one('#model-key', Input).value = ''
        self.query_one('#model-notice', Static).update('新服务尚未测试；填写实际模型名后保存或测试。')

    def collect(self):
        def text(field): return self.query_one('#model-' + field, Input).value.strip()
        temperature = text('temperature')
        return {
            'provider': str(self.query_one('#model-provider', Select).value),
            'base_url': text('url'), 'model': text('name'),
            'auth_mode': str(self.query_one('#model-auth', Select).value),
            'api_key_env': text('env'),
            'json_mode': str(self.query_one('#model-json', Select).value),
            'timeout_seconds': int(text('timeout')), 'max_tokens': int(text('tokens')),
            'max_tokens_field': str(self.query_one('#model-token-field', Select).value),
            'temperature': float(temperature) if temperature else None,
        }

    def action_cancel(self):
        if self._testing:
            return
        self.query_one('#model-key', Input).value = ''
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if self._testing:
            return
        if event.button.id == 'model-cancel':
            self.action_cancel()
            return
        try:
            settings = self.collect()
            key = self.query_one('#model-key', Input).value
            if event.button.id == 'model-save':
                message = self.session.save_model(settings, key)
                self.query_one('#model-key', Input).value = ''
                self.dismiss(message)
            elif event.button.id == 'model-test':
                self._testing = True
                for widget in self.query('Input, Select, Button'):
                    widget.disabled = True
                self.query_one('#model-notice', Static).update('Testing… 此请求不包含项目数据，云端可能计费。')
                result = await asyncio.to_thread(self.session.test_model, settings, key)
                self.query_one('#model-key', Input).value = ''
                self.query_one('#model-notice', Static).update(result['message'])
        except (ValueError, OSError) as exc:
            self.query_one('#model-notice', Static).update(redact(str(exc)))
        finally:
            self._testing = False
            for widget in self.query('Input, Select, Button'):
                widget.disabled = False
