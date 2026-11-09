"""Model connection editor. No secrets are returned or written to settings."""
from __future__ import annotations

import asyncio
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Select, Static

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
                yield Static('Provider')
                yield Select([('DeepSeek', 'deepseek'), ('OpenAI compatible', 'openai_compatible'), ('Ollama', 'ollama'), ('LM Studio', 'lm_studio')], value=cfg['provider'], allow_blank=False, id='model-provider')
                yield Static('Base URL')
                yield Input(cfg['base_url'], id='model-url')
                yield Static('Model name available from this provider')
                yield Input(cfg['model'], id='model-name')
                yield Static('Authentication')
                yield Select([('Bearer API key', 'bearer'), ('None', 'none')], value=cfg['auth_mode'], allow_blank=False, id='model-auth')
                yield Static('API key (leave blank to use the existing key)')
                yield Input(password=True, id='model-key')
                with Collapsible(title='Advanced', collapsed=True):
                    yield Static('Model request route (model requests only)')
                    yield Select([('Inherit current proxy', 'inherit'), ('Direct connection', 'direct')],
                                 value=cfg['proxy_mode'], allow_blank=False, id='model-proxy')
                yield Static('Advanced parameters stay in config/api.local.yaml.',
                             id='model-advanced-hint')
            yield Static('Settings only; connection has not been tested.', id='model-notice')
            with Horizontal(id='model-buttons'):
                yield Button('Save', variant='primary', id='model-save')
                yield Button('Test connection', id='model-test')
                yield Button('Cancel', id='model-cancel')

    def on_mount(self):
        self._loading = False

    def on_select_changed(self, event: Select.Changed):
        if not self._loading and event.select.id == 'model-proxy':
            self.query_one('#model-notice', Static).update('Route changed; settings are untested and take effect after saving.')
            return
        if self._loading or event.select.id != 'model-provider':
            return
        if str(event.value) == self._provider:
            return
        self._provider = str(event.value)
        preset = PRESETS[str(event.value)]
        self.settings['api_key_env'] = preset['api_key_env']
        self.settings['auth_mode'] = preset['auth_mode']
        for name, field in [('base_url', 'url'), ('model', 'name')]:
            self.query_one('#model-' + field, Input).value = preset[name]
        self.query_one('#model-auth', Select).value = preset['auth_mode']
        self.query_one('#model-key', Input).value = ''
        self.query_one('#model-notice', Static).update('Provider is untested; enter an available model name, then save or test.')

    def collect(self):
        def text(field): return self.query_one('#model-' + field, Input).value.strip()
        # Technical parameters are intentionally not shown. Keep their current
        # values so a simple provider/model edit never resets advanced settings.
        value = dict(self.settings)
        value.update({
            'provider': str(self.query_one('#model-provider', Select).value),
            'base_url': text('url'), 'model': text('name'),
            'auth_mode': str(self.query_one('#model-auth', Select).value),
            'proxy_mode': str(self.query_one('#model-proxy', Select).value),
        })
        return value

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
                self.query_one('#model-notice', Static).update('Testing… No project data is sent. Cloud providers may charge for this request.')
                result = await asyncio.to_thread(self.session.test_model, settings, key)
                self.query_one('#model-key', Input).value = ''
                self.query_one('#model-notice', Static).update(result['message'])
        except (ValueError, OSError) as exc:
            self.query_one('#model-notice', Static).update(redact(str(exc)))
        finally:
            self._testing = False
            for widget in self.query('Input, Select, Button'):
                widget.disabled = False
