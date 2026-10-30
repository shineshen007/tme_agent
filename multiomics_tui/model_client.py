"""TUI compatibility adapter with shared audited transport and endpoint isolation."""
from __future__ import annotations

import hashlib
import json

from orchestrator.llm_client import LLMClient, LLMConfigError, LLMError, LLMValidationError
from .model_config import credential, normalize, redact


class CompatibleClient(LLMClient):
    def __init__(self, *args, **kwargs):
        provided_transport = kwargs.pop('transport', None)
        super().__init__(*args, transport=self._compatible_post, **kwargs)
        self.cfg = normalize(self.cfg)
        self._send = provided_transport or self._http_post

    def _api_key_for_request(self):
        if self.cfg['auth_mode'] == 'none':
            return ''
        key = credential({'llm': self.cfg})
        if not key:
            raise LLMConfigError('当前接口缺少密钥，请通过 /key 输入。')
        return key

    def _compatible_post(self, url, headers, body, timeout):
        payload = json.loads(body)
        headers = dict(headers)
        if self.cfg['auth_mode'] == 'none':
            headers.pop('Authorization', None)
        if self.cfg['provider'] != 'deepseek':
            payload.pop('thinking', None)
        if self.cfg['temperature'] is None:
            payload.pop('temperature', None)
        if self.cfg['max_tokens_field'] == 'max_completion_tokens':
            payload['max_completion_tokens'] = payload.pop('max_tokens')
        if self.cfg['json_mode'] == 'prompt':
            payload.pop('response_format', None)
        status, text = self._send(url, headers, json.dumps(payload).encode('utf-8'), timeout)
        if status < 400:
            if redact(text) != text:
                raise LLMValidationError('服务回复中含密钥，已拒绝保存。')
            try:
                reply = json.loads(text)
                content = reply['choices'][0]['message']['content']
                if not isinstance(content, str) or not isinstance(reply.get('usage', {}), dict):
                    raise ValueError('Invalid response envelope')
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMValidationError('服务未返回有效的 OpenAI compatible JSON 回复。') from exc
        return status, text

    def _cache_key(self, role, messages, schema_name):
        old = super()._cache_key(role, messages, schema_name)
        protocol = {name: self.cfg[name] for name in ('provider', 'json_mode', 'max_tokens_field', 'max_tokens', 'temperature')}
        return hashlib.sha256((old + json.dumps(protocol, sort_keys=True)).encode()).hexdigest()[:32]

    def _guard(self, text):
        if redact(text) != text:
            raise LLMConfigError('请求中含密钥，请移除后重试。')
        return super()._guard(text)

    def _audit(self, record):
        # Provider errors can echo credentials, including keys without an sk- prefix.
        safe = json.loads(redact(json.dumps(record, ensure_ascii=False)))
        return super()._audit(safe)

    def chat_json(self, *args, **kwargs):
        try:
            return super().chat_json(*args, **kwargs)
        except LLMError as exc:
            raise type(exc)(redact(str(exc))) from None


def test_connection(root, cfg):
    from pathlib import Path
    import tempfile
    import copy
    value = copy.deepcopy(cfg)
    value['llm'].update(cache=False, max_retries=0, max_schema_retries=0, token_budget=0,
                        max_tokens=min(value['llm'].get('max_tokens', 32000), 256))
    # A probe must not use project data, cached responses or persist its messages.
    with tempfile.TemporaryDirectory(prefix='multiomics_model_probe_') as temp:
        client = CompatibleClient(root=Path(temp), run=Path(temp), cfg=value, audit_dir=Path(temp)/'calls')
        def validate(answer):
            if answer != {'ok': True}:
                raise ValueError('JSON 连通性回复不符合预期 {"ok": true}。')
        try:
            client.chat_json('connection_probe', 'Return valid JSON only: {"ok": true}.',
                             'Connection test; return {"ok": true}.', 'connection_probe', validator=validate)
        except LLMError as exc:
            message = redact(str(exc))
            if '401' in message or '403' in message:
                category = '认证失败'
            elif '404' in message:
                category = '地址或模型未找到'
            elif 'JSON' in message or 'json' in message:
                category = 'JSON 返回检查失败'
            elif '400' in message or '422' in message:
                category = '请求参数不受支持；请核对 JSON 模式和高级设置'
            else:
                category = '连接失败'
            return {'status': 'failed', 'message': category + '：' + message}
    return {'status': 'passed', 'message': '连接、认证、模型调用和 JSON 返回检查通过。'}
