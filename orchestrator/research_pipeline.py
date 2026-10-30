"""Trusted research continuation of a completed, approved analysis task."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import uuid
from pathlib import Path

if __package__ in (None, ''):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'orchestrator'

from .agent_loop import AgentReasoningSession
from .io import sha256
from .knowledge_context import knowledge_service, knowledge_snapshot
from .llm_client import PrivacyViolation
from .privacy import sensitive_matches
from .reasoning import write_immutable_json
from .research_context import TaskResearchAdapter, verify_context_sources
from .research_literature import LiteratureService

LOG = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _state(store, task_id, status, **extra):
    state = store.get(task_id)
    state.update(research_status=status, **extra)
    store.save(state)


def verify_research_attempt(root, attempt):
    root, attempt = Path(root).resolve(), Path(attempt).resolve()
    if not attempt.is_relative_to(root / 'analysis'):
        raise ValueError('Research attempt is outside the workspace')
    manifest = json.loads((attempt / 'research_manifest.json').read_text())
    from multiomics_tui.task_sessions import TaskStore
    anchor = TaskStore(root).get(manifest['task_id']).get('research_attempts', {}).get(str(attempt.relative_to(root)))
    if anchor != sha256(attempt / 'research_manifest.json'):
        raise ValueError('Research manifest hash mismatch or missing receipt')
    listed = [item['path'] for item in manifest['artifacts']]
    actual = {str(p.relative_to(attempt)) for p in attempt.rglob('*')
              if p.is_file() and p != attempt / 'research_manifest.json'}
    if len(set(listed)) != len(listed) or set(listed) != actual:
        raise ValueError('Research manifest artifact coverage mismatch')
    required = {'research_context.json', 'evidence_graph.json', 'knowledge_snapshot.json',
                'prompt_contracts.json', 'model_config.json', 'research_config.json', 'environment.json',
                'code_sources.json', 'code_snapshot.json', 'model_calls.json'}
    if manifest['research_status'] in {'complete', 'partial'}:
        required |= {'literature.json', 'synthesis.json', 'critic.json', 'research_report.md', 'knowledge_proposals.json'}
    if not required <= actual and manifest['research_status'] != 'blocked':
        raise ValueError('Research manifest lacks required frozen artifacts')
    for item in manifest['artifacts']:
        path = (attempt / item['path']).resolve()
        if not path.is_relative_to(attempt) or sha256(path) != item['sha256']:
            raise ValueError('Research artifact hash mismatch: ' + item['path'])
    context = json.loads((attempt / 'research_context.json').read_text())
    if manifest['task_id'] != context['task_id'] or manifest['plan_hash'] != context['plan_hash']:
        raise ValueError('Research manifest identity mismatch')
    for item in context['artifacts']:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root) or sha256(path) != item['sha256']:
            raise ValueError('Analysis source changed: ' + item['path'])
    return {'status': 'verified', 'research_status': manifest['research_status'],
            'scientific_status': manifest['scientific_status']}


def _knowledge(root, context, cfg):
    background = {k: getattr(context, k) for k in ('disease', 'species', 'tissue')}
    stages = {}
    for stage, suffix in [('interpretation', ' mechanism biological context'),
                          ('hypothesis', ' mechanism alternative explanation'),
                          ('experiment', ' model perturbation controls failure know how')]:
        stages[stage] = knowledge_snapshot(root, context.question + suffix, background,
                                           cfg.get('knowledge'))
    entries = {}
    for snapshot in stages.values():
        for entry in snapshot.get('entries', []):
            entries[(entry.get('entry_id', entry.get('id')), entry.get('revision'), entry.get('scope'))] = entry
    return {'entries': list(entries.values()), 'stages': stages}


def _proposals(root, context, draft, literature, attempt, cfg):
    from difflib import SequenceMatcher
    from .research_literature import _normalise

    def exact_quote(text, quoted):
        """Recover source characters while preserving the source text/hash.

        Alignment may expand a compatibility character (for example a ligature)
        to its complete original unit. Record that expansion explicitly rather
        than presenting normalized model text as a verbatim original quotation.
        """
        normalized = _normalise(quoted)
        if not normalized:
            raise ValueError('Empty normalized literature quotation')
        if normalized in text:
            return normalized, False
        normalized_source = _normalise(text)
        start = normalized_source.find(normalized)
        if start < 0:
            raise ValueError('Normalized quotation is absent from the original source')
        end = start + len(normalized)
        spans = []
        for tag, left, right, norm_left, norm_right in SequenceMatcher(
                None, text, normalized_source, autojunk=False).get_opcodes():
            if norm_left >= end or norm_right <= start:
                continue
            if tag == 'equal':
                spans.append((left + max(start, norm_left) - norm_left,
                              left + min(end, norm_right) - norm_left))
            elif right > left:
                spans.append((left, right))
        restored = text[min(s[0] for s in spans):max(s[1] for s in spans)] if spans else text
        if normalized not in _normalise(restored):
            # Rare ambiguous alignment: retain the full, exact source paragraph
            # and disclose the broader span to the human knowledge reviewer.
            restored = text
        return restored, _normalise(restored) != normalized

    service = knowledge_service(root, cfg.get('knowledge'), read_only=False)
    records = {r['source_id']: r for r in literature.get('records', [])}
    source_path = attempt / 'synthesis.json'
    report_source = {'source_id': 'research:' + context.task_id + ':' + attempt.name,
        'source_type': 'generated_report', 'locator': str(source_path.relative_to(root)),
        'sha256': sha256(source_path), 'primary': False}
    proposals, errors = [], []
    for claim in draft['interpretations'] + draft['hypotheses']:
        try:
            sources, seen = [report_source], set()
            for relation in draft['literature_claims']:
                if relation['claim_id'] != claim['id']:
                    continue
                record = records[relation['source_id']]
                passage = next(p for p in record['passages'] if p['locator'] == relation['locator'])
                quote, expanded = exact_quote(passage['text'], relation['quote'])
                normalized_quote = _normalise(relation['quote'])
                citation_id = 'citation:' + _digest({
                    'document': record['source_id'], 'passage': passage['locator'],
                    'relation': relation['relation'], 'directness': relation['directness'],
                    'quote': normalized_quote})[:32]
                if citation_id in seen:
                    continue
                seen.add(citation_id)
                sources.append({'source_id': citation_id, 'source_type': 'publication',
                    'locator': record['url'], 'sha256': passage['sha256'], 'text': passage['text'],
                    'quote': quote, 'primary': True,
                    'metadata': {'content_scope': 'full_text' if passage['kind'] == 'full_text' else 'abstract',
                                 'document_source_id': record['source_id'],
                                 'pmid': record.get('pmid'), 'doi': record.get('doi'),
                                 'passage_locator': passage['locator'], 'relation': relation['relation'],
                                 'directness': relation['directness'], 'semantic_review': 'machine_reviewed',
                                 'normalized_quote': normalized_quote,
                                 'quote_span_expanded': expanded}})
            payload = {'title': claim['statement'][:100], 'kind': claim['kind'], 'statement': claim['statement'],
                'context': {k: getattr(context, k) for k in ('disease', 'species', 'tissue')},
                'sources': sources, 'entities': [], 'relations': [], 'shareable': False}
            if sensitive_matches(json.dumps(payload, ensure_ascii=False),
                                 context.private_ids, channel='model'):
                raise ValueError('Knowledge candidate contains private or clinical content')
            proposals.append(service.propose(payload))
        except (ValueError, OSError, KeyError, StopIteration) as exc:
            LOG.warning('Knowledge candidate rejected for %s: %s', claim['id'], exc)
            errors.append({'claim_id': claim['id'], 'error': str(exc)})
    return {'status': 'partial' if errors else 'pending_human_review', 'proposals': proposals, 'errors': errors}


def render_research_report(context, state, literature, proposals):
    draft = state.get('draft', {})
    scientific_status = 'EXPLORATORY' if state.get('action') in {'accept', 'request_analysis'} else 'NOT_EVALUABLE'
    lines = ['# 科研解释 / Research interpretation', '', f'**Scientific status: {scientific_status}**', '',
             context.question, '', '## 统计事实（由冻结结果注入）', '',
             '| Evidence | Entity | Effect | Scale | Adjusted p | n | Direction | Selection |',
             '|---|---|---|---|---|---|---|---|']
    for record in context.evidence:
        obs = record['observation']
        selection = obs.get('selection') or {}
        selection_label = selection.get('status', 'not_verifiable')
        values = [record['evidence_id'], record['biological_entity']['entity_id'], obs.get('effect'),
                  obs.get('effect_scale'), obs.get('adjusted_p'), obs.get('n'), obs.get('direction'),
                  selection_label]
        lines.append('| ' + ' | '.join('NA' if v is None else str(v).replace('|', '\\|') for v in values) + ' |')
    unverified = [record['evidence_id'] for record in context.evidence
                  if (record['observation'].get('selection') or {}).get('verified') is not True]
    if unverified:
        lines += ['', '未核验显著性：' + ', '.join(unverified) +
                  '（没有冻结阈值，selected 不作为确定性证据）。']
    lines += ['', '## 生物学解释与未经验证的假说', '']
    for claim in draft.get('interpretations', []) + draft.get('hypotheses', []):
        lines += [f"### {claim['id']} · {claim['kind']}", '', claim['statement'], '',
                  'Evidence: ' + ', '.join(claim['evidence_ids']),
                  'Knowledge: ' + ', '.join(claim['knowledge_ids']), '',
                  '替代解释：' + '; '.join(claim['alternative_explanations'])]
        if claim['kind'] == 'hypothesis':
            lines += ['证据基础：' + claim['evidence_basis'],
                      '成立条件：' + '; '.join(claim['conditions'])]
            for prediction in claim['testable_predictions']:
                lines += ['', '可证伪预测：' + prediction['prediction'],
                          '支持时：' + prediction['expected_if_true'],
                          '推翻时：' + prediction['expected_if_false']]
            for design in claim['validation_experiments']:
                lines += ['', '实验设计：', '', *[f'- {k}: ' + ('; '.join(v) if isinstance(v, list) else v)
                                                for k, v in design.items()]]
            lines += ['未知：' + '; '.join(claim['unknowns'])]
    if not draft.get('hypotheses'):
        lines += ['', '未形成满足当前证据要求的机制假说；解释结果仍予保留。']
    lines += ['', '## 文献核验', '', '检索状态：' + literature['status']]
    for relation in draft.get('literature_claims', []):
        lines += [f"- {relation['claim_id']} ← {relation['source_id']}：{relation['relation']} / "
                  f"{relation['directness']}；定位 {relation['locator']}；{relation['rationale']}"]
    for record in literature.get('records', []):
        lines.append(f"- [{record['source_id']}: {record['title']}]({record['url']})")
    review = state.get('review') or {}
    lines += ['', '## 反证与阴性结果审查', '']
    if review.get('evidence_review'):
        for item in review['evidence_review']:
            lines.append(f"- {item['claim_id']}：{item['assessment']}；反证 {len(item['counterevidence_ids'])} 条；"
                         f"阴性/不可估计 {len(item['negative_result_ids'])} 条；{item['rationale']}")
    else:
        lines.append('- 本次没有可用的 Critic 反证审查记录，不能视为反证已排除。')
    lines += ['', '## 创新性（仅限所记录检索范围）', '']
    for novelty in draft.get('novelty', []):
        lines.append(f"- {novelty['claim_id']}: {novelty['assessment']} — {novelty['rationale']}")
    lines += ['', '## 限制与审查', '',
              *['- ' + item for item in context.limitations + draft.get('limitations', [])],
              'Critic: ' + str(state.get('action', 'blocked')),
              '知识候选：' + proposals.get('status', 'not_created'),
              '人工批准知识不等同于独立实验验证。', '']
    return '\n'.join(lines)


def run_task_research(root, task_id, *, cfg=None, client=None, literature_service=None):
    """A process lease permits recovery after interruption and rejects concurrent retries."""
    from multiomics_tui.task_sessions import TaskStore
    from multiomics_tui.platform_support import lock_fd, unlock_fd
    folder = TaskStore(root).directory(task_id)
    if not folder.is_dir():
        raise ValueError('Unknown research task')
    with (folder / 'research.lock').open('a+b') as lease:
        try:
            lock_fd(lease.fileno(), blocking=False)
        except OSError as exc:
            raise ValueError('Research is already running for this task') from exc
        previous_attempt = TaskStore(root).get(task_id).get('research_attempt')
        try:
            return _run_task_research(root, task_id, cfg=cfg, client=client, literature_service=literature_service)
        except Exception as exc:
            # Approval/source checks can fail before an attempt exists. Surface
            # that failed retry without overwriting statistical completion or
            # changing a concurrently held lease's state.
            from multiomics_tui.task_sessions import redact_secrets
            current = TaskStore(root).get(task_id)
            if current.get('research_status') != 'blocked' or current.get('research_attempt') == previous_attempt:
                _state(TaskStore(root), task_id, 'blocked', scientific_status='NOT_EVALUABLE',
                       research_error=redact_secrets(str(exc)))
            raise
        finally:
            unlock_fd(lease.fileno())


def _run_task_research(root, task_id, *, cfg=None, client=None, literature_service=None):
    """New attempts only; statistical outputs and their manifests are never edited."""
    from multiomics_tui.task_sessions import TaskStore
    from multiomics_tui.llm import load_tui_config, build_client
    from .evidence_graph import build_context_evidence_graph
    from .loop_graph import run_context_reasoning

    root = Path(root).resolve()
    store = TaskStore(root)
    plan = store.plan(task_id)
    if not plan.get('research', {}).get('enabled'):
        return {'research_status': 'not_requested', 'scientific_status': 'NOT_EVALUABLE'}
    task_dir = root / 'analysis' / task_id
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise ValueError('Research task directory is missing or is a symbolic link')
    research_dir = task_dir / 'research'
    if research_dir.is_symlink() or (research_dir.exists() and not research_dir.is_dir()):
        raise ValueError('Research directory is a symbolic link or not a directory')
    model_snapshot = store.directory(task_id) / 'model_config.json'
    cfg = cfg or load_tui_config(root, snapshot=model_snapshot if model_snapshot.is_file() else None)
    # No research folder exists until the approved computation and its manifests verify.
    context = TaskResearchAdapter(root, task_id).load()
    research_dir.mkdir(mode=0o700, exist_ok=True)
    attempt = research_dir / ('attempt_' + uuid.uuid4().hex)
    if attempt.exists() or attempt.is_symlink():
        raise ValueError('Research attempt path already exists')
    attempt.mkdir(mode=0o700)
    _state(store, task_id, 'running', scientific_status='EXPLORATORY', research_attempt=str(attempt.relative_to(root)))
    status, scientific_status, error = 'blocked', 'NOT_EVALUABLE', None
    frozen = {}
    code_sources = {}
    app_root = Path(__file__).resolve().parents[1]
    call_start = len(getattr(client, 'calls', [])) if client is not None else 0
    def save(relative, value):
        write_immutable_json(attempt / relative, value)
    def verify():
        verify_context_sources(root, context)
        for relative, expected in code_sources.items():
            if sha256(app_root / relative) != expected:
                raise ValueError('Research implementation changed during execution: ' + relative)
        for relative, expected in frozen.items():
            if sha256(attempt / relative) != expected:
                raise ValueError('Frozen research source changed: ' + relative)
    try:
        save('research_context.json', context.to_dict())
        save('evidence_graph.json', build_context_evidence_graph(context))
        knowledge = _knowledge(root, context, cfg)
        save('knowledge_snapshot.json', knowledge)
        from .research_phases import SCHEMAS, INSTRUCTIONS
        save('prompt_contracts.json', {'schemas': SCHEMAS, 'instructions': INSTRUCTIONS})
        from multiomics_tui.model_config import FIELDS
        from .research_literature import DEFAULTS
        import importlib.metadata
        import platform
        save('model_config.json', {key: cfg.get('llm', {}).get(key) for key in sorted(
             FIELDS | {'token_budget', 'max_retries', 'max_schema_retries', 'cache', 'thinking'})})
        save('research_config.json', {'literature': {**DEFAULTS, **cfg.get('research', {}).get('literature', {})},
                                      'reasoning': cfg.get('reasoning', {}), 'knowledge': cfg.get('knowledge', {})})
        save('environment.json', {'python': platform.python_version(), 'platform': platform.system(),
             'packages': {name: importlib.metadata.version(name) for name in ('jsonschema', 'langgraph', 'PyYAML')}})
        code_files = sorted([*app_root.glob('orchestrator/*.py'),
                             *app_root.glob('multiomics_tui/*.py'), *app_root.glob('native_dist/*.py'),
                             *app_root.glob('schemas/*.json'), app_root / 'requirements.txt'])
        code_text = {str(p.relative_to(app_root)): p.read_text(encoding='utf-8') for p in code_files}
        code_sources.update({name: hashlib.sha256(content.encode('utf-8')).hexdigest()
                             for name, content in code_text.items()})
        save('code_sources.json', code_sources)
        save('code_snapshot.json', code_text)
        frozen.update({p.name: sha256(p) for p in attempt.glob('*.json')})
        if client is None:
            client = build_client(root, cfg, context.private_ids, audit_dir=attempt / 'calls')
        # Also guard injected/mock clients, and preserve the caller's cumulative budget.
        class GuardedClient:
            def chat_json(self, *args, **kwargs):
                text = json.dumps([args, {k: v for k, v in kwargs.items() if k != 'validator'}], ensure_ascii=False)
                if sensitive_matches(text, context.private_ids, channel='model'):
                    raise PrivacyViolation('Research context contains private or clinical content')
                return client.chat_json(*args, **kwargs)
        service = literature_service or LiteratureService(root / '.cache/research_literature',
                                                          cfg.get('research', {}).get('literature', {}))
        session = AgentReasoningSession(root, attempt, cfg, [], context=context,
            knowledge_context=knowledge, client=GuardedClient(), literature_service=service)
        verify()
        session.research_analysts = session.analysts_and_librarian()
        save('analysts.json', session.research_analysts)
        save('phase_errors.json', session.errors)
        save('queries.json', session.query_proposal)
        save('literature.json', session.literature_result)
        frozen['literature.json'] = sha256(attempt / 'literature.json')
        verify()
        result = run_context_reasoning(session, verify, save,
                    cfg.get('reasoning', {}).get('max_iterations', 2))
        save('synthesis.json', result.get('draft', {}))
        save('critic.json', result.get('review', {}))
        save('analysis_requests.json', {'status': 'requires_new_approved_plan',
                                       'requests': result.get('review', {}).get('analysis_requests', [])})
        proposals = {'status': 'not_created', 'proposals': [], 'errors': []}
        if result['action'] == 'accept':
            proposals = _proposals(root, context, result['draft'], session.literature_result, attempt, cfg)
            status = 'complete' if session.literature_result['status'] == 'complete' and not proposals['errors'] and not session.errors else 'partial'
        elif result['action'] == 'request_analysis':
            status = 'partial'
        save('knowledge_proposals.json', proposals)
        scientific_status = 'EXPLORATORY' if status in {'complete', 'partial'} else 'NOT_EVALUABLE'
        report = render_research_report(context, result, session.literature_result, proposals)
        (attempt / 'research_report.md').write_text(report, encoding='utf-8')
        verify()
        error = result.get('error') or ('critic_blocked' if status == 'blocked' else None)
    except Exception as exc:
        LOG.exception('Research continuation failed; completed analysis is preserved')
        from multiomics_tui.task_sessions import redact_secrets
        status, scientific_status = 'blocked', 'NOT_EVALUABLE'
        error = redact_secrets(str(exc))
        save('failure.json', {'error': error, 'research_status': 'blocked'})
    finally:
        # Existing clients may audit outside this attempt (for shared budget
        # accounting). Copy the exact research calls into its immutable bundle.
        try:
            calls = getattr(client, 'calls', [])[call_start:] if client is not None else []
            audit_dir = getattr(client, 'audit_dir', None)
            for record in calls:
                if audit_dir is not None:
                    source = Path(audit_dir) / f"{record['call']:03d}_{record['role']}.json"
                    if not source.is_file():
                        raise ValueError('Missing audited research model call: ' + source.name)
                    save('calls/' + source.name, json.loads(source.read_text(encoding='utf-8')))
            save('model_calls.json', {'calls': calls, 'tokens_used': getattr(client, 'tokens_used', None)})
            manifest = {'schema_version': '1.0', 'task_id': task_id, 'plan_hash': context.plan_hash,
                        'research_status': status, 'scientific_status': scientific_status, 'error': error,
                        'artifacts': [{'path': str(p.relative_to(attempt)), 'sha256': sha256(p)}
                                      for p in sorted(attempt.rglob('*')) if p.is_file()]}
            save('research_manifest.json', manifest)
            receipts = store.get(task_id).get('research_attempts', {})
            receipts[str(attempt.relative_to(root))] = sha256(attempt / 'research_manifest.json')
            _state(store, task_id, status, scientific_status=scientific_status, research_error=error,
                   research_attempts=receipts)
            store.event(task_id, 'system', f'Research {status}: {attempt.relative_to(root)}')
        except Exception as exc:
            from multiomics_tui.task_sessions import redact_secrets
            _state(store, task_id, 'blocked', scientific_status='NOT_EVALUABLE',
                   research_error='Could not finalize immutable research audit: ' + redact_secrets(str(exc)))
            raise
    return {'research_status': status, 'scientific_status': scientific_status,
            'attempt': str(attempt), 'error': error}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Continue research without rerunning statistics')
    parser.add_argument('root', type=Path)
    parser.add_argument('task_id')
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args(argv)
    result = verify_research_attempt(args.root, args.verify) if args.verify else run_task_research(args.root, args.task_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('status') == 'verified' or result.get('research_status') == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
