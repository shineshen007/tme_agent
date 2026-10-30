"""Provider-agnostic OpenAI-compatible LLM client with strict audit and guardrails.

Design rules enforced here:
- Numbers are never authored by the model; this client only transports structured JSON.
- Private identifiers (patient/biospecimen/assay ids) are refused before any network call.
- Every call is written to an auditable per-run record (request, response, usage, cache state).
- Only transient infrastructure errors are retried; 4xx fails immediately.
- Per-run token budget is a hard stop.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .privacy import redact_credentials, sensitive_matches

LOG = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an authenticated request to another origin or a downgrade."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LLMError(RuntimeError):
    pass


class LLMConfigError(LLMError):
    pass


class LLMTransportError(LLMError):
    pass


class LLMValidationError(LLMError):
    pass


class LLMNonRetryableValidationError(LLMValidationError):
    """Policy/environment rejection: never ask a model to bypass it."""
    pass


class LLMBudgetExceeded(LLMError):
    pass


class PrivacyViolation(LLMError):
    pass


def read_api_key(cfg):
    """Resolve the API key from the configured environment variable only."""
    env_name = cfg['llm'].get('api_key_env', '')
    if not env_name:
        raise LLMConfigError('llm.api_key_env is not configured')
    key = os.environ.get(env_name)
    if not key:
        raise LLMConfigError(
            f'Missing API key: environment variable {env_name} is unset. '
            f'Export it before running (e.g. export {env_name}=...); the key is never written to disk.')
    return key


def llm_preflight(cfg):
    """Cheap fail-fast check used before starting an expensive run."""
    if not cfg.get('llm', {}).get('enabled'):
        return {'enabled': False, 'status': 'disabled'}
    key_env = cfg['llm'].get('api_key_env', '')
    if not os.environ.get(key_env):
        raise LLMConfigError(
            f'llm.enabled=true but environment variable {key_env} is unset; refusing to start. '
            f'Export the key or set llm.enabled=false.')
    return {'enabled': True, 'status': 'configured', 'model': cfg['llm'].get('model')}


def private_id_pattern(registry_ids):
    """Word-boundary matcher for cohort identifiers; used to block outbound leaks."""
    ids = sorted({str(i) for i in registry_ids if str(i)})
    if not ids:
        return None
    return re.compile(r'(?<![A-Za-z0-9_])(' + '|'.join(re.escape(i) for i in ids) + r')(?![A-Za-z0-9_])')


class LLMClient:
    def __init__(self, root, run, cfg, transport=None, registry_ids=None, audit_dir=None):
        self.root = Path(root)
        self.run = Path(run)
        self.cfg = cfg['llm']
        self.transport = transport or self._http_post
        self.private_ids = [str(i) for i in (registry_ids or []) if str(i)]
        self.private = private_id_pattern(self.private_ids)
        self.audit_dir = Path(audit_dir) if audit_dir else self.run / 'report/agent/calls'
        self.cache_dir = self.root / '.cache/llm'
        self.seq = 0
        self.tokens_used = 0
        self.calls = []
        self.offline_reason = None

    # ---------- transport ----------
    def _http_post(self, url, headers, body, timeout):
        request = urllib.request.Request(url, data=body, headers=headers, method='POST')
        # A redirect is an error: credentials must never cross origins. Both the
        # base client and CompatibleClient inherit this transport.
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8')

    def _endpoint(self):
        base = self.cfg['base_url'].rstrip('/')
        return f'{base}/chat/completions'

    def _guard(self, text):
        try:
            credential = self._api_key_for_request()
        except LLMError:
            credential = ''
        if credential and credential in text:
            raise LLMConfigError('Outbound payload contains the configured credential; remove it and retry.')
        hits = sensitive_matches(text, self.private_ids, channel='model')
        if hits:
            # Never echo the sensitive value into an error, log or audit record.
            kinds = ", ".join(sorted({kind for kind, _ in hits}))
            raise PrivacyViolation(f'Outbound payload contains private or clinical content ({kinds})')
        return text

    # ---------- caching ----------
    def _cache_key(self, role, messages, schema_name):
        key_fields = {'role': role, 'model': self.cfg['model'], 'base_url': self.cfg['base_url'],
                      'temperature': self.cfg.get('temperature', 0), 'schema': schema_name,
                      'messages': messages}
        if self.cfg.get('thinking') in {'enabled', 'disabled'}:
            key_fields['thinking'] = self.cfg['thinking']
        payload = json.dumps(key_fields, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _cache_read(self, key):
        if not self.cfg.get('cache', True):
            return None
        path = self.cache_dir / f'{key}.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def _cache_write(self, key, value):
        if not self.cfg.get('cache', True):
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f'{key}.json').write_text(
            json.dumps(value, ensure_ascii=False, indent=1), encoding='utf-8')

    # ---------- main call ----------
    def _correction(self, role, error, parsed=None):
        if role == 'tui_task_codegen':
            instruction = (
                f'Code generation validation failed: {error}. Return a concise COMPLETE JSON object '
                'using the original requested schema. Preserve the current step ID, frozen requirements, '
                'all declared outputs and requested output contracts. Generate only the requested script; '
                'use small helpers for repeated checks. Do not omit requirements or invent analysis results. '
                'Write executable code that computes results from inputs, without explanatory prose.')
        elif role.startswith('tui_task_plan'):
            instruction = f'Planning validation failed: {error}. Return corrected JSON, preserving the user requirements and inspected facts.'
        elif parsed is None:
            instruction = (f'Your previous reply was invalid: {error}. Reply with JSON only, '
                           'no statistics numbers, citing only evidence ids from the provided list.')
        else:
            instruction = (f'Validation failed: {error}. Fix exactly that problem and reply with JSON only, '
                           'with no statistics numbers.')
        messages = [] if parsed is None else [{'role': 'assistant', 'content': json.dumps(parsed, ensure_ascii=False)}]
        return messages + [{'role': 'user', 'content': instruction}]

    def chat_json(self, role, system, user, schema_name, validator=None, messages_extra=None):
        """One JSON completion; retain every attempted response's usage on failure."""
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        if messages_extra:
            messages.extend(messages_extra)
        for message in messages:
            self._guard(message['content'])
        key = self._cache_key(role, messages, schema_name)
        cached = self._cache_read(key)
        self.seq += 1
        record = {'call': self.seq, 'role': role, 'schema': schema_name, 'model': self.cfg['model'],
                  'base_url': self.cfg['base_url'], 'temperature': self.cfg.get('temperature', 0),
                  'cache_key': key, 'cache_hit': bool(cached), 'request_messages': messages,
                  'max_tokens': self.cfg.get('max_tokens', 32000),
                  'thinking_mode': self._thinking_mode(), 'token_budget': self.cfg.get('token_budget'),
                  'attempts': [], 'tokens_before': self.tokens_used, 'schema_retries': 0}
        corrective = []
        if cached:
            record.update({'parsed': cached['parsed'], 'usage': cached.get('usage', {}), 'latency_s': 0.0,
                           'retries': 0, 'validation': 'cached_pass'})
            try:
                if validator is not None:
                    validator(cached['parsed'])
            except Exception as exc:
                if role != 'tui_task_codegen' or isinstance(exc, LLMNonRetryableValidationError):
                    record.update(error=str(exc), validation='failed')
                    self._audit(record)
                    raise
                # Preserve the rejected entry. This correction gets its own cache key.
                record.update(cache_hit=False, rejected_cache_key=key, rejected_cache_error=str(exc))
                corrective = self._correction(role, exc, cached['parsed'])
                key = self._cache_key(role, messages + corrective, schema_name + ':revalidated')
                record['cache_key'] = key
                repaired = self._cache_read(key)
                if repaired:
                    try:
                        if validator is not None: validator(repaired['parsed'])
                    except LLMNonRetryableValidationError:
                        record.update(validation='failed', error='Repaired cache rejected by policy/environment')
                        self._audit(record)
                        raise
                    except Exception as repair_error:
                        record['rejected_repaired_cache_error'] = str(repair_error)
                    else:
                        record.update(parsed=repaired['parsed'], usage=repaired.get('usage',{}), cache_hit=True, validation='cached_pass')
                        self._audit(record)
                        return {'parsed':repaired['parsed'], 'usage':repaired.get('usage',{}), 'cache_hit':True, 'call':self.seq}
            else:
                self._audit(record)
                return {'parsed': cached['parsed'], 'usage': cached.get('usage', {}), 'cache_hit': True,
                        'call': self.seq}

        retries = 0
        try:
            retry_limit = int(self.cfg.get('max_schema_retries', 2))
            if role == 'tui_task_codegen': retry_limit = min(2, retry_limit)
            for attempt in range(retry_limit + 1):
                record['schema_retries'] = attempt
                # Corrections resend the prompt plus the rejected response. Use
                # provider-reported input usage and a conservative UTF-8 bound
                # for added messages before committing another paid request.
                # This is a reservation estimate, not a replacement tokenizer.
                budget = int(self.cfg.get('token_budget', 0) or 0)
                prior_usage = [item.get('usage') for item in record['attempts']
                               if isinstance(item.get('usage'), dict) and item['usage'].get('prompt_tokens')]
                if budget and corrective and prior_usage:
                    prompt_bound = int(prior_usage[-1]['prompt_tokens']) + sum(
                        len(message['content'].encode('utf-8')) + 64 for message in corrective)
                    reserved = prompt_bound + int(self.cfg.get('max_tokens', 32000))
                    record['retry_budget_reservation'] = {'estimated_prompt_bound': prompt_bound,
                        'requested_completion_limit': self.cfg.get('max_tokens', 32000),
                        'remaining': max(0, budget - self.tokens_used), 'required': reserved}
                    if reserved > budget - self.tokens_used:
                        raise LLMBudgetExceeded('Insufficient remaining budget for a schema correction: '
                                                f'reserve {reserved}, remaining {max(0, budget - self.tokens_used)}')
                try:
                    parsed, usage, latency, retries = self._request(
                        role, messages + corrective, attempts=record['attempts'], schema_attempt=attempt + 1)
                except LLMValidationError as exc:
                    if attempt >= retry_limit:
                        raise
                    corrective = self._correction(role, exc)
                    continue
                try:
                    if validator is not None:
                        validator(parsed)
                except Exception as exc:
                    record['attempts'][-1]['validation_error'] = str(exc)
                    if isinstance(exc, LLMNonRetryableValidationError): raise
                    if attempt >= retry_limit:
                        record['parsed'] = parsed
                        raise LLMValidationError(str(exc)) from exc
                    corrective = self._correction(role, exc, parsed)
                    continue
                self._cache_write(key, {'parsed': parsed, 'usage': usage})
                record.update({'parsed': parsed, 'usage': usage, 'latency_s': latency, 'retries': retries,
                               'validation': 'pass'})
                self._audit(record)
                return {'parsed': parsed, 'usage': usage, 'cache_hit': False, 'call': self.seq}
        except Exception as exc:
            record.update(error=str(exc), validation='failed', retries=retries)
            self._audit(record)
            raise

    def _thinking_mode(self):
        if self.cfg.get('provider') not in {None, 'deepseek'}:
            return 'not_sent'
        return self.cfg.get('thinking', 'enabled' if self.cfg.get('provider') == 'deepseek' else 'provider_default')

    def _request(self, role, messages, *, attempts=None, schema_attempt=1):
        budget = int(self.cfg.get('token_budget', 0) or 0)
        if budget and self.tokens_used >= budget:
            raise LLMBudgetExceeded(f'Token budget exhausted: {self.tokens_used}/{budget}')
        request_body = {'model': self.cfg['model'], 'messages': messages,
                        'temperature': self.cfg.get('temperature', 0),
                        'max_tokens': self.cfg.get('max_tokens', 32000),
                        'response_format': {'type': 'json_object'}, 'stream': False}
        mode = self._thinking_mode()
        if mode in {'enabled', 'disabled'}:
            request_body['thinking'] = {'type': mode}
        body = json.dumps(request_body).encode()
        self._guard(body.decode('utf-8'))
        credential = self._api_key_for_request()
        headers = {'Content-Type': 'application/json',
                   'Authorization': f'Bearer {credential}'}
        timeout = self.cfg.get('timeout_seconds', 120)
        last = None
        for attempt in range(int(self.cfg.get('max_retries', 2)) + 1):
            started = time.time()
            entry = {'schema_attempt': schema_attempt, 'transport_attempt': attempt + 1,
                     'max_tokens': self.cfg.get('max_tokens', 32000), 'thinking_mode': self._thinking_mode(),
                     'timeout_seconds': timeout, 'http_status': None, 'finish_reason': None,
                     'usage': None, 'reasoning_tokens': None, 'answer_tokens': None, 'latency_s': None,
                     'request_messages': messages}
            if attempts is not None:
                attempts.append(entry)
            try:
                status, text = self.transport(self._endpoint(), headers, body, timeout)
                latency = round(time.time() - started, 2)
                entry.update(http_status=status, latency_s=latency)
                if status >= 400:
                    detail = redact_credentials(text[:400], [credential])
                    entry['error'] = f'HTTP {status}: {detail}'
                    if status in {429, 500, 502, 503, 504}:
                        last = LLMTransportError(f'HTTP {status}: {detail[:200]}')
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    raise LLMTransportError(entry['error'])
                if (credential and credential in text) or redact_credentials(text, [credential]) != text:
                    raise LLMValidationError(
                        'Service response echoed a credential; refusing to cache or audit it.')
                payload = json.loads(text)
                choice = payload['choices'][0]
                finish = choice.get('finish_reason')
                content = choice['message']['content']
                usage = payload.get('usage', {})
                details = usage.get('completion_tokens_details') or {}
                reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
                completion = usage.get('completion_tokens')
                answer = completion - reasoning if isinstance(completion, int) and isinstance(reasoning, int) else None
                entry.update(finish_reason=finish, usage=usage, reasoning_tokens=reasoning,
                             answer_tokens=answer, content_characters=len(content),
                             response_content=redact_credentials(content, [credential]))
                # Charge once before checking length/JSON; failed replies still cost tokens.
                self._charge(usage)
                if finish == 'length':
                    raise LLMValidationError(
                        f'Response was truncated by the token limit (finish_reason=length, '
                        f'max_tokens={entry["max_tokens"]}, thinking={entry["thinking_mode"]}); '
                        'reply with a shorter, complete JSON object')
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise LLMValidationError(f'Model did not return valid JSON: {exc}') from exc
                return parsed, usage, latency, attempt
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode('utf-8', 'replace')[:200]
                except Exception:
                    detail = str(exc)
                detail = redact_credentials(detail, [credential])
                entry.update(http_status=exc.code, error=f'HTTP {exc.code}: {detail}',
                             latency_s=round(time.time() - started, 2))
                if exc.code in {429, 500, 502, 503, 504}:
                    last = LLMTransportError(entry['error'])
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMTransportError(entry['error']) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = LLMTransportError(redact_credentials(f'network error: {exc}', [credential]))
                entry.update(error=str(last), latency_s=round(time.time() - started, 2))
                time.sleep(min(2 ** attempt, 8))
                continue
            except Exception as exc:
                entry['error'] = redact_credentials(str(exc), [credential])
                raise
            finally:
                if entry['latency_s'] is None:
                    entry['latency_s'] = round(time.time() - started, 2)
                entry['tokens_used_after'] = self.tokens_used
        raise LLMTransportError(redact_credentials(str(last), [credential]))

    def _api_key_for_request(self):
        return read_api_key({'llm': self.cfg})

    def _charge(self, usage):
        total = int(usage.get('total_tokens', 0) or 0)
        self.tokens_used += total
        budget = int(self.cfg.get('token_budget', 0) or 0)
        if budget and self.tokens_used > budget:
            raise LLMBudgetExceeded(f'Token budget exceeded: {self.tokens_used}/{budget}')

    def _audit(self, record):
        attempts = record.get('attempts', [])
        usages = [entry['usage'] for entry in attempts if entry.get('usage') is not None]
        if not record.get('cache_hit') and usages:
            record.setdefault('usage', usages[-1])
        record['total_usage'] = {name: sum(int(usage.get(name, 0) or 0) for usage in usages)
                                 for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        record['attempt_count'] = len(attempts)
        record['transport_retries'] = sum(entry['transport_attempt'] > 1 for entry in attempts)
        # A shared client may serve concurrent read-only requests. Use this
        # call's responses, rather than differences in the shared budget counter.
        record['charged_tokens'] = record['total_usage']['total_tokens']
        record['total_latency_s'] = round(sum(entry.get('latency_s', 0) or 0 for entry in attempts), 2)
        try:
            credential = self._api_key_for_request()
        except LLMError:
            credential = ''
        # Provider bodies, successful content and request messages may all echo
        # a credential. Redact the complete record before it reaches disk or the
        # in-memory call summary.
        text = redact_credentials(json.dumps(record, ensure_ascii=False, indent=1), [credential])
        record = json.loads(text)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        path = self.audit_dir / f'{record["call"]:03d}_{record["role"]}.json'
        path.write_text(text, encoding='utf-8')
        self.calls.append({k: record.get(k) for k in
                           ['call', 'role', 'schema', 'cache_hit', 'latency_s', 'retries', 'validation', 'usage', 'error']})
        LOG.info('LLM call %s role=%s cache_hit=%s', record['call'], record['role'], record['cache_hit'])
