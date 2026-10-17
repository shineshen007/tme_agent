"""Immutable provenance for a bounded interpretation loop over frozen results."""
from __future__ import annotations

import json
from pathlib import Path

from .io import artifact, read_json, sha256, write_json


SOURCE_PATHS = (
    'run.json', 'integration/evidence_registry.csv',
    'integration/evidence_registry.v2.jsonl', 'integration/axis_evidence.csv',
)
PROMPT_SOURCE_PATHS = (
    'scrna/axis_localization.csv',
    'scrna/deconvolution/deconvolution_paired.csv',
    'pathways/transcriptomics/triple109/gene_mapping_audit.csv',
    'inputs/metabolite_annotations.csv',
    'transcriptomics/full159/model_status.json',
)
PATHWAY_PATHS = (
    'pathways/transcriptomics/triple109/gsea_full.csv',
    'pathways/proteomics/triple109/gsea_full.csv',
    'pathways/metabolomics/kegg_ora_full.csv',
)
ITERATION_FILES = (
    'evidence_clusters.json', 'hypotheses_internal.json', 'hypotheses.json',
    'rejected_hypotheses.json', 'critic_review.json', 'reanalysis_requests.json',
    'reanalysis_results.json', 'evidence_changes.json',
)


def write_immutable_json(path, payload):
    """Replay identical content; reject any attempted overwrite of an audit record."""
    path = Path(path)
    if path.exists():
        if read_json(path) != payload:
            raise ValueError(f'Immutable reasoning artifact changed: {path}')
        return
    write_json(path, payload)


def freeze_reasoning_sources(run, cohort='triple109', *, filename='frozen_sources.json', include_literature=True):
    """Snapshot all existing source rows reachable by the v2 provenance and pathway lookups."""
    run = Path(run).resolve()
    required = set(SOURCE_PATHS)
    if (run / 'integration/evidence_graph.json').is_file():
        # Existing synthetic fixtures may predate the graph; new integrations always write it.
        required.add('integration/evidence_graph.json')
    registry = run / 'integration/evidence_registry.v2.jsonl'
    if not registry.is_file():
        raise ValueError('EvidenceRecord v2 registry is required for the reasoning loop')
    with registry.open(encoding='utf-8') as handle:
        for line in handle:
            record = json.loads(line)
            relative = record['provenance']['artifact_path']
            if relative not in {'unknown', 'not_applicable', ''}:
                required.add(relative)
    required.update(relative.replace('triple109', cohort) for relative in PROMPT_SOURCE_PATHS
                    if (run / relative.replace('triple109', cohort)).is_file())
    required.update(relative.replace('triple109', cohort) for relative in PATHWAY_PATHS
                    if (run / relative.replace('triple109', cohort)).is_file())
    if include_literature and (run / 'literature/records.json').is_file():
        required.add('literature/records.json')
    entries = []
    for relative in sorted(required):
        path = (run / relative).resolve()
        if not path.is_relative_to(run) or not path.is_file():
            raise ValueError(f'Frozen reasoning source unavailable or outside run: {relative}')
        entries.append(artifact(run, path))
    payload = {'schema_version': '1.0', 'artifacts': entries}
    if filename == 'frozen_sources.json':
        baseline = run / 'reasoning/pre_interpretation_sources.json'
        if baseline.is_file():
            verify_reasoning_sources(run, baseline)
            payload['pre_interpretation_sha256'] = sha256(baseline)
    if filename not in {'frozen_sources.json', 'pre_interpretation_sources.json'}:
        raise ValueError('Invalid reasoning snapshot name')
    target = run / 'reasoning' / filename
    write_immutable_json(target, payload)
    return target


def save_iteration(run, iteration, *, clusters, internal, synthesis, rejected, review,
                   requests=(), results=(), evidence_changes=None, snapshot=None,
                   snapshot_digest=None):
    """Write one complete iteration and a hash chain, never replacing differing content."""
    run = Path(run).resolve()
    verify_iterations(run)
    directory = run / 'reasoning' / f'iteration_{iteration:02d}'
    values = {
        'evidence_clusters.json': clusters,
        'hypotheses_internal.json': internal,
        'hypotheses.json': synthesis,
        'rejected_hypotheses.json': {'rejected': list(rejected)},
        'critic_review.json': review,
        'reanalysis_requests.json': {'requests': list(requests)},
        'reanalysis_results.json': {'results': list(results)},
        'evidence_changes.json': evidence_changes or {
            'newly_referenced_existing_ids': [], 'source_rows': [], 'new_evidence': []},
    }
    for filename in ITERATION_FILES:
        write_immutable_json(directory / filename, values[filename])
    previous = run / 'reasoning' / f'iteration_{iteration - 1:02d}' / 'iteration_manifest.json'
    payload = {
        'schema_version': '1.0', 'iteration': iteration,
        'frozen_sources_sha256': snapshot_digest or sha256(snapshot or run / 'reasoning/frozen_sources.json'),
        'previous_iteration_sha256': sha256(previous) if previous.is_file() else None,
        'artifacts': [artifact(run, directory / filename) for filename in ITERATION_FILES],
    }
    write_immutable_json(directory / 'iteration_manifest.json', payload)
    return directory / 'iteration_manifest.json'


def verify_reasoning_sources(run, snapshot):
    """Fail closed if any frozen source changed or disappeared during model reasoning."""
    run = Path(run).resolve()
    payload = read_json(snapshot)
    baseline_digest = payload.get('pre_interpretation_sha256')
    if baseline_digest:
        baseline = run / 'reasoning/pre_interpretation_sources.json'
        if not baseline.is_file() or sha256(baseline) != baseline_digest:
            raise ValueError('Pre-interpretation snapshot hash mismatch')
        verify_reasoning_sources(run, baseline)
    paths = [item['path'] for item in payload['artifacts']]
    if len(paths) != len(set(paths)):
        raise ValueError('Duplicate frozen reasoning source path')
    for item in payload['artifacts']:
        path = (run / item['path']).resolve()
        if not path.is_relative_to(run) or not path.is_file():
            raise ValueError(f'Frozen reasoning source unavailable: {item["path"]}')
        if path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
            raise ValueError(f'Frozen reasoning source hash mismatch: {item["path"]}')


def verify_iterations(run):
    """Validate previously written iteration manifests and their hash chain."""
    run = Path(run).resolve()
    previous = None
    for expected, manifest_path in enumerate(sorted((run / 'reasoning').glob('iteration_*/iteration_manifest.json')), 1):
        manifest = read_json(manifest_path)
        if manifest.get('iteration') != expected or manifest_path.parent.name != f'iteration_{expected:02d}':
            raise ValueError(f'Iteration sequence mismatch: {manifest_path}')
        snapshot = run / 'reasoning/frozen_sources.json'
        if not snapshot.is_file() or sha256(snapshot) != manifest['frozen_sources_sha256']:
            raise ValueError(f'Frozen snapshot hash mismatch: {manifest_path}')
        if manifest['previous_iteration_sha256'] != previous:
            raise ValueError(f'Iteration manifest chain mismatch: {manifest_path}')
        for item in manifest['artifacts']:
            path = (run / item['path']).resolve()
            if not path.is_relative_to(run) or not path.is_file():
                raise ValueError(f'Iteration artifact unavailable: {item["path"]}')
            if path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
                raise ValueError(f'Iteration artifact hash mismatch: {item["path"]}')
        previous = sha256(manifest_path)
