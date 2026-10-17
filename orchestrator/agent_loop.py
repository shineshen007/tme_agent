"""Reusable interpretation phases used by the bounded LangGraph reasoning loop."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .agents import (ANALYST_OMICS, _analyst_input, _analyst_validator, _private_ids, _prompt,
                     evidence_brief, literature_brief, pathway_brief, qualitative_evidence_records,
                     render_agent_report, scrna_brief)
from .checkers import (adapt_reasoner_output, validate_agent_review, validate_evidence_clusters,
                       validate_no_numbers, validate_schema)
from .io import read_json, write_json
from .literature import fetch_additional
from .llm_client import LLMClient
from .evidence_graph import load_evidence_graph, integrator_graph_view

LOG = logging.getLogger(__name__)


class AgentReasoningSession:
    """Run-scoped LLM client; each method is one independently routed graph phase."""

    def __init__(self, root, run, cfg, axes, transport=None):
        self.root, self.run, self.cfg, self.axes = Path(root), Path(run), cfg, axes
        self.out = self.run / 'report/agent'
        self.out.mkdir(parents=True, exist_ok=True)
        self.registry = pd.read_csv(self.run / 'integration/evidence_registry.csv')
        axis = self.run / 'integration/axis_evidence.csv'
        self.axis_evidence = pd.read_csv(axis) if axis.exists() else None
        self.client = LLMClient(root, run, cfg, transport=transport, registry_ids=_private_ids(run))
        self.shared = _prompt(root, 'shared_prefix.md')
        self.analyses, self.errors = [], []
        self.additional_result = {'status': 'skipped'}

    def literature(self):
        path = self.run / 'literature/records.json'
        return read_json(path) if path.exists() else {'records': []}

    def analysts_and_librarian(self):
        outputs = {}
        for omics in ANALYST_OMICS:
            available = ((self.run / omics / 'triple109/de.csv').exists()
                         if omics != 'scrna' else (self.run / 'scrna/statistics.json').exists())
            if not available:
                self.analyses.append({'omics': omics, 'status': 'skipped_no_results'})
                continue
            try:
                result = self.client.chat_json(
                    role=f'analyst_{omics}',
                    system=f'{self.shared}\n\n{_prompt(self.root, "agent_analyst.md")}',
                    user=f'组学分支：{omics}\n\n{_analyst_input(self.run, self.cfg, omics, self.axes, self.registry)}\n\n只输出 JSON。',
                    schema_name='agent_analyst',
                    validator=_analyst_validator(self.registry, self.root / 'schemas/agent_analyst.schema.json', omics))
                outputs[omics] = result['parsed']
                write_json(self.out / f'omics_interpretation_{omics}.json', result['parsed'])
                self.analyses.append({'omics': omics, 'status': 'complete'})
            except Exception as exc:
                LOG.exception('Analyst failed for %s', omics)
                self.errors.append({'step': f'analyst_{omics}', 'message': str(exc), 'resolved': False})
                self.analyses.append({'omics': omics, 'status': 'failed'})
        if outputs and self.cfg.get('benchmark', {}).get('frozen_literature'):
            self.additional_result = {'status': 'skipped_frozen_benchmark_literature', 'n_records': 0}
        elif outputs:
            executed = [q['query'] for q in self.literature().get('queries', [])]
            try:
                proposal = self.client.chat_json(
                    role='librarian', system=f'{self.shared}\n\n{_prompt(self.root, "agent_librarian.md")}',
                    user=(f'预设轴与关键词：{json.dumps({a["axis"]: a.get("genes", []) for a in self.axes}, ensure_ascii=False)}\n'
                          f'已执行查询：{json.dumps(executed, ensure_ascii=False)}\n\n'
                          f'{evidence_brief(self.run, self.cfg, self.registry, self.axes)}\n\n只输出 JSON。'),
                    schema_name='agent_librarian',
                    validator=lambda p: (validate_schema(p, self.root / 'schemas/agent_librarian.schema.json'),
                                         validate_no_numbers(p)))
                queries = [q for q in proposal['parsed'].get('queries', []) if q['query'] not in executed]
                write_json(self.out / 'additional_queries.json', {'queries': queries, 'via': 'librarian_agent'})
                self.additional_result = (fetch_additional(self.root, self.run, self.cfg, queries)
                                          if queries else {'status': 'skipped', 'n_records': 0})
            except Exception as exc:
                LOG.exception('Librarian failed')
                self.errors.append({'step': 'librarian', 'message': str(exc), 'resolved': False})
        return outputs

    def integrate(self, analyst_outputs, active_ids=(), lookup_results=()):
        graph_path = self.run / 'integration/evidence_graph.json'
        if graph_path.is_file():
            graph = load_evidence_graph(self.root, self.run)
            view = integrator_graph_view(graph, active_ids)
            benchmark_axis = self.cfg.get('benchmark', {}).get('axis')
            if benchmark_axis:
                view['axes'] = [item for item in view['axes'] if item['axis'] == benchmark_axis]
            user = (f'组学解读：\n{json.dumps(analyst_outputs, ensure_ascii=False)}\n\n'
                    f'来源已校验的科学证据图轴视图（缺口表示尚无登记证据，不代表阴性）：\n'
                    f'{json.dumps(view, ensure_ascii=False)}\n\n'
                    f'冻结查询状态与来源行（不是新证据）：\n'
                    f'{json.dumps(list(lookup_results), ensure_ascii=False)}\n\n'
                    '外部背景与派生估计不得计作独立实测组学。只输出 JSON。')
            return self.client.chat_json(
                role='evidence_integrator', system=f'{self.shared}\n\n{_prompt(self.root, "agent_evidence_integrator.md")}',
                user=user, schema_name='evidence_cluster',
                validator=lambda p: validate_evidence_clusters(
                    p, self.registry, self.root / 'schemas/evidence_cluster.schema.json',
                    self.axis_evidence, self.axes))['parsed']
        if (self.run / 'integration/evidence_registry.v2.jsonl').is_file():
            raise ValueError('Evidence graph missing for a v2 reasoning run')
        # Historical synthetic fixtures without EvidenceRecord v2 keep the old prompt path.
        primary = []
        if {'evidence_layer', 'model_type', 'axis'} <= set(self.registry.columns):
            rows = self.registry[(self.registry.evidence_layer == 'prognosis') &
                                 (self.registry.model_type == 'univariable') &
                                 (self.registry.cohort_id == 'triple109') &
                                 (self.registry.selected.fillna(False).astype(bool))]
            primary = rows[['axis', 'evidence_id', 'omics', 'direction', 'selected', 'feature_id']].to_dict('records')
        extra = qualitative_evidence_records(self.run, self.registry, set(active_ids) &
                                             set(self.registry.evidence_id.astype(str)))
        user = (f'组学解读：\n{json.dumps(analyst_outputs, ensure_ascii=False)}\n\n'
                f'{evidence_brief(self.run, self.cfg, self.registry, self.axes)}\n\n'
                f'{pathway_brief(self.run, self.cfg)}\n\n{scrna_brief(self.run, self.cfg, self.axes)}\n\n'
                f'冻结轴表：\n{self.axis_evidence[["axis", "evidence_id", "omics", "direction", "selected", "feature_id"]].to_json(orient="records", force_ascii=False) if self.axis_evidence is not None else "[]"}\n\n'
                f'已选中主预后轴行：\n{json.dumps(primary, ensure_ascii=False)}\n\n'
                f'本轮补充的既有 EvidenceRecords：\n{json.dumps(extra, ensure_ascii=False)}\n\n'
                f'冻结查询状态与来源行（不是新证据）：\n{json.dumps(list(lookup_results), ensure_ascii=False)}\n\n只输出 JSON。')
        return self.client.chat_json(
            role='evidence_integrator', system=f'{self.shared}\n\n{_prompt(self.root, "agent_evidence_integrator.md")}',
            user=user, schema_name='evidence_cluster',
            validator=lambda p: validate_evidence_clusters(
                p, self.registry, self.root / 'schemas/evidence_cluster.schema.json',
                self.axis_evidence, self.axes))['parsed']

    def reason(self, clusters, prior_review=None):
        ids = {eid for c in clusters['evidence_clusters']
               for eid in c['supporting_evidence_ids'] + c['counterevidence_ids']}
        user = (f'已校验 EvidenceClusters：\n{json.dumps(clusters, ensure_ascii=False)}\n\n'
                f'定性 EvidenceRecords：\n{json.dumps(qualitative_evidence_records(self.run, self.registry, ids), ensure_ascii=False)}\n\n'
                f'{scrna_brief(self.run, self.cfg, self.axes)}\n\n'
                f'{literature_brief(self.run, self.cfg, self.cfg["llm"]["briefs"]["literature_per_axis"])}\n\n'
                f'上一轮 Critic 待修正问题：\n{json.dumps(prior_review or {}, ensure_ascii=False)}\n\n'
                '固定约束：仅观察性、不得修改统计或证据、不得升级验证状态。只输出 JSON。')
        pmids = {str(r['pmid']) for r in self.literature().get('records', [])}
        def adapt(payload):
            return adapt_reasoner_output(payload, clusters, self.registry,
                                         self.root / 'schemas/agent_reasoner.schema.json',
                                         self.root / 'schemas/agent_synthesis.schema.json',
                                         self.axis_evidence, pmids)
        internal = self.client.chat_json(
            role='hypothesis_reasoner', system=f'{self.shared}\n\n{_prompt(self.root, "agent_hypothesis_reasoner.md")}',
            user=user, schema_name='agent_reasoner', validator=lambda p: adapt(p))['parsed']
        synthesis, rejected = adapt(internal)
        return internal, synthesis, rejected

    def critic(self, synthesis, rejected):
        user = (f'待复核产出：\n{json.dumps(synthesis, ensure_ascii=False)}\n\n'
                f'被确定性校验器拒绝的草稿：\n{json.dumps(rejected, ensure_ascii=False)}\n\n'
                f'冻结证据简报：\n{evidence_brief(self.run, self.cfg, self.registry, self.axes)}\n\n只输出 JSON。')
        return self.client.chat_json(
            role='critic', system=f'{self.shared}\n\n{_prompt(self.root, "agent_critic.md")}',
            user=user, schema_name='agent_review',
            validator=lambda p: validate_agent_review(p, self.root / 'schemas/agent_review.schema.json'))['parsed']

    def propose_requests(self, review, synthesis, clusters, iteration, history):
        allowed_issues = {i['issue_id']: i for i in review['issues'] if i['resolvable_by'] == 'reanalysis'}
        known_hypotheses = {h['id'] for h in synthesis['hypotheses']}
        next_id = max((int(r['request_id'][1:]) for r in history), default=0) + 1
        user = (f'待查询 Critic 问题：\n{json.dumps(list(allowed_issues.values()), ensure_ascii=False)}\n\n'
                f'现有假说 ID：{json.dumps(sorted(known_hypotheses))}\n'
                f'已校验 EvidenceClusters：\n{json.dumps(clusters, ensure_ascii=False)}\n'
                f'允许的请求起始 ID：R{next_id}；iteration={iteration}。只输出 JSON。')
        request_schema = self.root / 'schemas/reanalysis_request.schema.json'
        proposal_schema = self.root / 'schemas/reanalysis_proposal.schema.json'
        limit = self.cfg.get('reasoning', {}).get('max_reanalysis_requests_per_iteration', 3)
        def validate(payload):
            validate_schema(payload, proposal_schema)
            if len(payload['requests']) > limit:
                raise ValueError('Reanalysis proposal exceeds per-iteration limit')
            seen = set()
            for request in payload['requests']:
                validate_schema(request, request_schema)
                if request['request_id'] in seen or request['request_id'] in {r['request_id'] for r in history}:
                    raise ValueError('Duplicate reanalysis request ID')
                seen.add(request['request_id'])
                issue = allowed_issues.get(request.get('trigger_issue_id'))
                if issue is None or request['trigger_hypothesis_id'] != issue['target_hypothesis_id']:
                    raise ValueError('Reanalysis request does not match a current Critic issue')
                if request['trigger_hypothesis_id'] not in known_hypotheses:
                    raise ValueError('Reanalysis trigger hypothesis is absent from current draft')
                if request['iteration'] != iteration or request['scientific_question'] != issue['scientific_question']:
                    raise ValueError('Reanalysis proposal changed Critic question or iteration')
            validate_no_numbers(payload)
        result = self.client.chat_json(
            role='reanalysis_proposer', system=f'{self.shared}\n\n{_prompt(self.root, "agent_reanalysis_proposer.md")}',
            user=user, schema_name='reanalysis_proposal', validator=validate)['parsed']
        return result['requests']

    def finalize(self, synthesis, review, rejected, clusters):
        if review.get('decision') != 'accept' or review.get('issues') or review.get('unresolved_issues'):
            raise ValueError('Final model output requires a clean Critic accept')
        write_json(self.out / 'evidence_clusters.json', clusters)
        if rejected:
            write_json(self.out / 'rejected_hypotheses.json', {
                'policy': 'evidence_anchored_rule_enforced_by_code',
                'note': 'Rejected hypotheses never enter the report; reasons listed for audit.',
                'rejected': rejected})
        write_json(self.out / 'hypotheses_agent.json', {
            'status': 'review_only', 'model_generated': True,
            'n_rejected_by_validator': len(rejected), 'hypotheses': synthesis['hypotheses'],
            'target_groups': synthesis['target_groups'],
            'evidence_boundary': 'model_interpretation_of_frozen_evidence'})
        write_json(self.out / 'targets.json', {'status': 'review_only', 'model_generated': True,
                                               'grouped_only_no_ranking': True,
                                               'groups': synthesis['target_groups']})
        write_json(self.out / 'literature_stances.json', {
            'label': 'machine_assessed_pending_human_review', 'stance_field_unchanged': 'unreviewed',
            'stances': synthesis['literature_stances']})
        write_json(self.out / 'review.json', review)
        rendered = render_agent_report(self.root, self.run, self.cfg, self.axes, self.registry,
                                       self.axis_evidence, synthesis, review, self.literature(),
                                       self.analyses, self.errors, self.client, False, rejected)
        return self.result('review_only', synthesis, review, rejected, rendered)

    def blocked(self, review, reason, rejected=()):
        write_json(self.out / 'blocked.json', {'status': 'blocked', 'reason': reason,
                                                'review': review, 'rejected': list(rejected)})
        write_json(self.out / 'agent_manifest.json', {
            'status': 'blocked', 'reason': reason, 'token_usage': self.client.tokens_used,
            'calls': [{'call': c['call'], 'role': c['role'], 'validation': c['validation']}
                      for c in self.client.calls], 'iterations': sorted(
                          str(p.relative_to(self.run)) for p in (self.run / 'reasoning').glob('iteration_*/iteration_manifest.json'))})
        return self.result('blocked', None, review, rejected, [])

    def result(self, status, synthesis, review, rejected, rendered):
        return {'status': status, 'model': self.cfg['llm']['model'],
                'base_url': self.cfg['llm']['base_url'],
                'n_hypotheses': len(synthesis['hypotheses']) if synthesis else 0,
                'n_rejected_hypotheses': len(rejected),
                'critic_blocking_issues': sum(i['severity'] == 'blocking' for i in (review or {}).get('issues', [])),
                'n_target_groups': len(synthesis['target_groups']) if synthesis else 0,
                'n_review_issues': len((review or {}).get('issues', [])),
                'tokens_used': self.client.tokens_used, 'n_calls': len(self.client.calls),
                'cache_hits': sum(bool(c.get('cache_hit')) for c in self.client.calls),
                'additional_literature': self.additional_result,
                'analyses': self.analyses, 'errors': self.errors, 'artifacts': rendered}
