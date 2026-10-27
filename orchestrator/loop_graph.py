"""Bounded conditional interpretation loop over a frozen deterministic run."""
from pathlib import Path

from langgraph.graph import END

from .agent_loop import AgentReasoningSession
from .checkers import validate_agent_review
from .io import sha256, write_json
from .reanalysis import ReanalysisPlanner
from .reasoning import (freeze_reasoning_sources, save_iteration,
                        verify_reasoning_sources, verify_iterations)


def run_context_reasoning(session, verify, save, maximum=2):
    """Use the existing phase topology for public tasks, without legacy cohort paths.

    The caller freezes retrieval before this graph starts. New analysis requests
    are saved for approval, never executed by this interpretation graph.
    """
    from typing import TypedDict
    from langgraph.graph import StateGraph, START

    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 5:
        raise ValueError('Research reasoning maximum must be between one and five')

    class ContextState(TypedDict, total=False):
        analysts: dict
        clusters: dict
        raw: dict
        draft: dict
        rejected: list
        review: dict
        iteration: int
        action: str
        error: str

    def guarded(function):
        def invoke(state):
            verify()
            result = function(state)
            verify()
            return result
        return invoke

    @guarded
    def integrate(state):
        value = session.integrate(state['analysts'])
        save(f"iteration_{state['iteration']:02d}/clusters.json", value)
        return {'clusters': value}

    @guarded
    def reason(state):
        raw, draft, rejected = session.reason(state['clusters'], state.get('review'))
        save(f"iteration_{state['iteration']:02d}/draft.json", draft)
        save(f"iteration_{state['iteration']:02d}/raw.json", raw)
        save(f"iteration_{state['iteration']:02d}/rejected.json", rejected)
        return {'raw': raw, 'draft': draft, 'rejected': rejected}

    @guarded
    def critic(state):
        review = session.critic(state['draft'], state['rejected'])
        save(f"iteration_{state['iteration']:02d}/review.json", review)
        decision = review['decision']
        if decision == 'revise_reasoning' and state['iteration'] >= maximum:
            return {'review': review, 'action': 'block', 'error': 'reasoning_iteration_limit'}
        return {'review': review, 'action': decision, 'iteration': state['iteration'] + 1}

    graph = StateGraph(ContextState)
    graph.add_node('integrate', integrate)
    graph.add_node('reason', reason)
    graph.add_node('critic', critic)
    graph.add_edge(START, 'integrate')
    graph.add_edge('integrate', 'reason')
    graph.add_edge('reason', 'critic')
    graph.add_conditional_edges('critic', lambda state: state['action'], {
        'accept': END, 'block': END, 'request_analysis': END, 'revise_reasoning': 'integrate'})
    return graph.compile().invoke({'analysts': session.research_analysts, 'iteration': 1},
                                  {'recursion_limit': maximum * 4 + 4})


def attach_reasoning_loop(graph, root, run, cfg, axes, transport=None):
    root, run = Path(root), Path(run)
    settings = cfg.get('reasoning', {})
    maximum = settings.get('max_iterations', settings.get('max_reasoning_iterations', 2))
    if settings.get('max_reasoning_iterations', maximum) != maximum:
        raise ValueError('reasoning iteration limits disagree')
    request_limit = settings.get('max_reanalysis_requests_per_iteration', 3)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 1
           for n in (maximum, request_limit)):
        raise ValueError('Reasoning limits must be positive integers')
    session = None

    def agent():
        nonlocal session
        if session is None:
            session = AgentReasoningSession(root, run, cfg, axes, transport=transport)
        return session

    def check_snapshot(state):
        snapshot = state['frozen_snapshot']
        if sha256(snapshot) != state['frozen_snapshot_sha256']:
            raise ValueError('Frozen reasoning snapshot hash mismatch')
        verify_reasoning_sources(run, snapshot)

    def prepare(state):
        try:
            cohort = cfg['analysis']['integration']['primary_cohort']
            pre_snapshot = freeze_reasoning_sources(
                run, cohort, filename='pre_interpretation_sources.json',
                include_literature=False)
            outputs = agent().analysts_and_librarian()
            verify_reasoning_sources(run, pre_snapshot)
            if not outputs:
                raise ValueError('No validated analyst interpretation')
            # The librarian may add public metadata; freeze it after retrieval while
            # preserving the earlier immutable deterministic-source snapshot.
            snapshot = freeze_reasoning_sources(run, cohort)
            verify_iterations(run)
            return {'analyst_outputs': outputs, 'frozen_snapshot': str(snapshot),
                    'frozen_snapshot_sha256': sha256(snapshot),
                    'reasoning_iteration': 1, 'active_evidence_ids': [],
                    'lookup_results': [], 'request_history': [], 'loop_error': None}
        except Exception as exc:
            return {'loop_error': str(exc), 'loop_action': 'block',
                    'reasoning_iteration': 1}

    def integrate(state):
        try:
            check_snapshot(state)
            clusters = agent().integrate(state['analyst_outputs'],
                                         state.get('active_evidence_ids', []),
                                         state.get('lookup_results', []))
            return {'evidence_clusters': clusters, 'loop_error': None}
        except Exception as exc:
            return {'loop_error': str(exc), 'loop_action': 'block'}

    def reason(state):
        try:
            check_snapshot(state)
            internal, draft, rejected = agent().reason(
                state['evidence_clusters'], state.get('prior_review'))
            return {'reasoner_internal': internal, 'synthesis_draft': draft,
                    'rejected_drafts': rejected, 'loop_error': None}
        except Exception as exc:
            return {'loop_error': str(exc), 'loop_action': 'block'}

    def critic(state):
        try:
            check_snapshot(state)
            review = agent().critic(state['synthesis_draft'], state['rejected_drafts'])
            known = {h['id'] for h in state['synthesis_draft']['hypotheses']}
            if any(issue['target_hypothesis_id'] not in known for issue in review['issues']):
                raise ValueError('Critic references unknown draft hypothesis')
            return {'critic_review': review, 'loop_error': None}
        except Exception as exc:
            return {'loop_error': str(exc), 'loop_action': 'block'}

    def gate(state):
        try:
            validate_agent_review(state['critic_review'], root / 'schemas/agent_review.schema.json')
            decision = state['critic_review']['decision']
            if decision == 'accept' and not state['synthesis_draft']['hypotheses']:
                raise ValueError('No validated hypothesis to accept')
            if decision in {'revise_reasoning', 'request_analysis'} and state['reasoning_iteration'] >= maximum:
                raise ValueError('reasoning_iteration_limit')
            return {'loop_action': decision, 'loop_error': None}
        except Exception as exc:
            return {'loop_action': 'block', 'loop_error': str(exc)}

    def lookup(state):
        requests, results, history = [], [], list(state.get('request_history', []))
        changes = {'newly_referenced_existing_ids': [], 'source_rows': [], 'new_evidence': []}
        try:
            check_snapshot(state)
            requests = agent().propose_requests(
                state['critic_review'], state['synthesis_draft'], state['evidence_clusters'],
                state['reasoning_iteration'], history)
            check_snapshot(state)
            if not requests:
                raise ValueError('Critic requested analysis but proposer returned no requests')
            if len(requests) > request_limit:
                raise ValueError('Reanalysis request limit exceeded')
            planner = ReanalysisPlanner(root, run, cfg,
                frozen_manifest=state['frozen_snapshot'],
                hypothesis_ids={h['id'] for h in state['synthesis_draft']['hypotheses']})
            cited = {evidence_id for cluster in state['evidence_clusters']['evidence_clusters']
                     for evidence_id in cluster.get('supporting_evidence_ids', []) +
                                        cluster.get('counterevidence_ids', [])}
            seen = set(state.get('active_evidence_ids', [])) | cited
            rows = []
            for request in requests:
                result = planner.execute(request, history)
                results.append(result)
                history.append(request)
                if result['status'] != 'no_new_evidence':
                    return {'reanalysis_requests': requests, 'reanalysis_results': results,
                            'request_history': history, 'evidence_changes': changes,
                            'loop_action': 'block', 'loop_error': result['reason_code']}
                for evidence_id in result['referenced_evidence_ids']:
                    if evidence_id not in seen:
                        changes['newly_referenced_existing_ids'].append(evidence_id)
                        seen.add(evidence_id)
                rows.extend(result['source_rows'])
            changes['source_rows'] = list({
                (row['artifact_path'], row['row_key'], row['sha256']): row
                for row in rows}.values())
            check_snapshot(state)
            return {'reanalysis_requests': requests, 'reanalysis_results': results,
                    'request_history': history, 'evidence_changes': changes,
                    'active_evidence_ids': sorted(seen),
                    'lookup_results': list(state.get('lookup_results', [])) + results,
                    'loop_action': 'request_analysis', 'loop_error': None}
        except Exception as exc:
            return {'reanalysis_requests': requests, 'reanalysis_results': results,
                    'request_history': history, 'evidence_changes': changes,
                    'loop_action': 'block', 'loop_error': str(exc)}

    def audit(state):
        try:
            snapshot = state.get('frozen_snapshot')
            if snapshot:
                try:
                    check_snapshot(state)
                except ValueError as exc:
                    state = {**state, 'loop_error': str(exc), 'loop_action': 'block'}
            save_iteration(run, state['reasoning_iteration'],
                clusters=state.get('evidence_clusters', {'evidence_clusters': []}),
                internal=state.get('reasoner_internal', {}),
                synthesis=state.get('synthesis_draft', {}),
                rejected=state.get('rejected_drafts', []),
                review=state.get('critic_review', {
                    'status': 'not_completed', 'error': state.get('loop_error')}),
                requests=state.get('reanalysis_requests', []),
                results=state.get('reanalysis_results', []),
                evidence_changes=state.get('evidence_changes'), snapshot=snapshot,
                snapshot_digest=state.get('frozen_snapshot_sha256'))
            return {'loop_action': state['loop_action'],
                    'loop_error': state.get('loop_error')}
        except Exception as exc:
            return {'loop_action': 'block', 'loop_error': str(exc)}

    def advance(state):
        return {'reasoning_iteration': state['reasoning_iteration'] + 1,
                'prior_review': state['critic_review'],
                'reanalysis_requests': [], 'reanalysis_results': [],
                'evidence_changes': {'newly_referenced_existing_ids': [],
                                     'source_rows': [], 'new_evidence': []}}

    final_files = [run / 'report/agent' / name for name in
                   ('hypotheses_agent.json', 'targets.json', 'literature_stances.json',
                    'review.json', 'evidence_clusters.json', 'agent_manifest.json')]
    final_files.append(run / 'report/agent_report.md')

    def accept(state):
        created = []
        try:
            check_snapshot(state)
            verify_iterations(run)
            if any(path.exists() for path in final_files):
                raise ValueError('Final model files already exist; use a new run-id')
            try:
                result = agent().finalize(state['synthesis_draft'], state['critic_review'],
                                          state['rejected_drafts'], state['evidence_clusters'])
                if result.get('status') != 'review_only':
                    raise ValueError('Accepted synthesis returned a non-final status')
            finally:
                created = [path for path in final_files if path.exists()]
            return {'agent_result': result}
        except Exception as exc:
            for path in created:
                path.unlink()
            return block({**state, 'loop_error': str(exc)})

    def block(state):
        review = state.get('critic_review')
        reason = state.get('loop_error') or (review or {}).get('overall_assessment') or 'blocked'
        try:
            result = agent().blocked(review, reason, state.get('rejected_drafts', []))
        except Exception as exc:
            write_json(run / 'report/agent/blocked.json',
                       {'status': 'blocked', 'reason': reason, 'recording_error': str(exc),
                        'review': review})
            result = {'status': 'blocked', 'model': cfg['llm'].get('model'),
                      'n_hypotheses': 0, 'reason': reason,
                      'errors': [{'step': 'agent_initialization', 'message': str(exc)}]}
        return {'agent_result': result}

    nodes = {'agent_preparation': prepare, 'evidence_integrator': integrate,
             'hypothesis_reasoner': reason, 'critic': critic, 'decision_gate': gate,
             'reanalysis_lookup': lookup, 'audit_iteration': audit,
             'next_iteration': advance, 'accepted_synthesis': accept,
             'blocked_reasoning': block}
    for name, function in nodes.items():
        graph.add_node(name, function)
    graph.add_conditional_edges('agent_preparation',
        lambda state: 'block' if state.get('loop_error') else 'continue',
        {'block': 'blocked_reasoning', 'continue': 'evidence_integrator'})
    for source, destination in [('evidence_integrator', 'hypothesis_reasoner'),
                                ('hypothesis_reasoner', 'critic'),
                                ('critic', 'decision_gate')]:
        graph.add_conditional_edges(source,
            lambda state: 'block' if state.get('loop_error') else 'continue',
            {'block': 'audit_iteration', 'continue': destination})
    graph.add_conditional_edges('decision_gate', lambda state: state['loop_action'],
        {'request_analysis': 'reanalysis_lookup', 'accept': 'audit_iteration',
         'revise_reasoning': 'audit_iteration', 'block': 'audit_iteration'})
    graph.add_edge('reanalysis_lookup', 'audit_iteration')
    graph.add_conditional_edges('audit_iteration', lambda state: state['loop_action'],
        {'accept': 'accepted_synthesis', 'block': 'blocked_reasoning',
         'revise_reasoning': 'next_iteration', 'request_analysis': 'next_iteration'})
    graph.add_conditional_edges('next_iteration', lambda state: state['loop_action'],
        {'revise_reasoning': 'hypothesis_reasoner',
         'request_analysis': 'evidence_integrator'})
    graph.add_conditional_edges('accepted_synthesis',
        lambda state: 'publish' if state['agent_result']['status'] == 'review_only' else 'block',
        {'publish': 'hub_dossiers', 'block': END})
    graph.add_edge('blocked_reasoning', END)
