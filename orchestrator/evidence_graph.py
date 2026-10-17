"""Source-verified scientific graph over EvidenceRecord v2, separate from LangGraph.

Edges describe registered observations or annotations, never inferred mechanisms.
An absent edge means no registered relationship, not a negative biological result.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import yaml

from .checkers import validate_evidence_registry_v2, validate_schema
from .io import read_json, sha256, write_json


NODE_TYPES = {'transcriptomics': 'gene', 'proteomics': 'protein',
              'metabolomics': 'metabolite', 'scrna_context': 'gene'}
UNKNOWN = {'', 'unknown', 'not_applicable'}


def _id(kind, value):
    return f'{kind}:{value}'


def _edge_id(kind, source, target, evidence_id):
    raw = json.dumps([kind, source, target, evidence_id], ensure_ascii=False,
                     separators=(',', ':'))
    return 'edge_' + hashlib.sha256(raw.encode()).hexdigest()[:20]


def _source_path(root, run, relative):
    if not isinstance(relative, str) or relative in UNKNOWN:
        raise ValueError(f'Evidence source path unavailable: {relative}')
    base = Path(root).resolve() if relative.startswith('knowledge/') else Path(run).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError(f'Evidence source unavailable or outside allowed root: {relative}')
    return path


def _row_key(record, row):
    layer = record['source']['evidence_layer']
    if layer == 'de':
        return row.get('feature_id')
    if layer == 'scrna_context':
        return f"{row.get('gene', '')}|{row.get('celltype', '')}"
    return row.get('evidence_id')


def _verified_row(root, run, record, source_hashes, cache):
    relative = record['provenance']['artifact_path']
    if relative not in cache:
        path = _source_path(root, run, relative)
        before = sha256(path)
        with path.open(encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))
        if sha256(path) != before:
            raise ValueError(f'Evidence source changed during read: {relative}')
        indexed = {}
        for row in rows:
            key = _row_key(record, row)
            indexed.setdefault(key, []).append(row)
        cache[relative] = indexed
        source_hashes[relative] = before
    matches = cache[relative].get(record['provenance']['row_key'], [])
    if len(matches) != 1:
        raise ValueError(f'Evidence source row missing or ambiguous: {relative} '
                         f"{record['provenance']['row_key']}")
    row = matches[0]
    entity = record['biological_entity']['entity_id']
    feature = row.get('gene') if record['source']['evidence_layer'] == 'scrna_context' else row.get('feature_id')
    if feature != entity:
        raise ValueError(f'Evidence source entity mismatch: {record["evidence_id"]}')
    return row


def _validate_graph(graph, root, run, *, check_sources=True):
    validate_schema(graph, Path(root) / 'schemas/evidence_graph.schema.json')
    nodes = {node['id']: node for node in graph['nodes']}
    evidence = {record['evidence_id']: record for record in graph['evidence_records']}
    artifacts = {item['path']: item['sha256'] for item in graph['source_artifacts']}
    if (len(nodes) != len(graph['nodes']) or len(evidence) != len(graph['evidence_records']) or
            len(artifacts) != len(graph['source_artifacts'])):
        raise ValueError('Duplicate graph node, evidence ID or source artifact')
    if any(node['id'] != _id(node['type'], node['entity_id']) for node in nodes.values()):
        raise ValueError('Graph node ID does not match its type and entity')
    seen = set()
    for edge in graph['edges']:
        if edge['id'] in seen or edge['id'] != _edge_id(edge['type'], edge['source'], edge['target'], edge['evidence_id']):
            raise ValueError('Duplicate or unstable graph edge ID')
        seen.add(edge['id'])
        if edge['source'] not in nodes or edge['target'] not in nodes or edge['evidence_id'] not in evidence:
            raise ValueError('Graph edge has an unknown endpoint or evidence ID')
        record = evidence[edge['evidence_id']]
        provenance = edge['provenance']
        annotation = edge.get('annotation_source')
        if edge['type'] == 'same_axis':
            if nodes[edge['target']]['type'] != 'axis' or edge['source'] != record['entity_node_id']:
                raise ValueError('Axis edge endpoints are invalid')
            layer = record['evidence_layer']
            axis = nodes[edge['target']]['entity_id']
            expected_annotation = (
                ('integration/axis_evidence.csv', f"{edge['evidence_id']}|{axis}")
                if layer == 'de' else
                ('knowledge/tme_metabolism_axes.yaml',
                 f"{axis}|{nodes[edge['source']]['entity_id']}")
                if layer == 'scrna_context' else
                ('integration/evidence_registry.csv', edge['evidence_id']))
            if annotation is None or (annotation['artifact_path'], annotation['row_key']) != expected_annotation:
                raise ValueError('Axis edge lacks its registered annotation source')
        elif annotation is not None:
            raise ValueError('Non-axis edge has an unexpected annotation source')
        if annotation is not None and artifacts.get(annotation['artifact_path']) != annotation['sha256']:
            raise ValueError('Graph edge annotation provenance mismatch')
        if (record['evidence_context'] != edge['evidence_context'] or
                record['direction'] != edge['direction'] or record['selected'] != edge['selected'] or
                artifacts.get(provenance['artifact_path']) != provenance['sha256'] or
                record['provenance']['path'] != provenance['artifact_path'] or
                record['provenance']['row_key'] != provenance['row_key'] or
                record['provenance']['sha256'] != provenance['sha256']):
            raise ValueError('Graph edge provenance or evidence metadata mismatch')
    for record in evidence.values():
        if record['entity_node_id'] not in nodes or record['provenance']['path'] not in artifacts:
            raise ValueError('Graph evidence has an unknown node or source')
    for item in graph['axis_coverage']:
        if artifacts.get(item['source_path']) != item['source_sha256'] or any(
                eid not in evidence for eid in item['evidence_ids']):
            raise ValueError('Graph axis coverage provenance mismatch')
    if check_sources:
        for relative, expected in artifacts.items():
            if sha256(_source_path(root, run, relative)) != expected:
                raise ValueError(f'Evidence graph source hash mismatch: {relative}')
    return graph


def load_evidence_graph(root, run):
    """Read the graph only if every recorded input still matches its SHA256."""
    graph = read_json(Path(run) / 'integration/evidence_graph.json')
    return _validate_graph(graph, root, run)


def build_evidence_graph(root, run):
    """Project existing v2 records and frozen source rows; never calculate statistics."""
    root, run = Path(root).resolve(), Path(run).resolve()
    v1 = run / 'integration/evidence_registry.csv'
    v2 = run / 'integration/evidence_registry.v2.jsonl'
    validate_evidence_registry_v2(v1, v2, root / 'schemas/evidence_v2.schema.json')
    axes_path = root / 'knowledge/tme_metabolism_axes.yaml'
    axes = yaml.safe_load(axes_path.read_text(encoding='utf-8'))['axes']
    axis_names = {axis['axis'] for axis in axes}
    axis_path = run / 'integration/axis_evidence.csv'
    if not axis_path.is_file():
        raise ValueError('Frozen axis evidence table is required for the graph')
    with axis_path.open(encoding='utf-8', newline='') as handle:
        axis_rows = list(csv.DictReader(handle))
    with v1.open(encoding='utf-8', newline='') as handle:
        legacy_rows = {row['evidence_id']: row for row in csv.DictReader(handle)}
    source_hashes = {
        'integration/evidence_registry.csv': sha256(v1),
        'integration/evidence_registry.v2.jsonl': sha256(v2),
        'integration/axis_evidence.csv': sha256(axis_path),
        'knowledge/tme_metabolism_axes.yaml': sha256(axes_path),
    }
    nodes, edges, summaries = {}, {}, {}
    cache = {}

    def node(kind, entity_id, label=None):
        key = _id(kind, entity_id)
        nodes.setdefault(key, {'id': key, 'type': kind, 'entity_id': entity_id,
                               'label': label or entity_id})
        return key

    def edge(kind, source, target, record, path, annotation=None):
        eid = record['evidence_id']
        key = _edge_id(kind, source, target, eid)
        item = {
            'id': key, 'type': kind, 'source': source, 'target': target,
            'evidence_id': eid,
            'evidence_context': record['evidence_quality']['evidence_context'],
            'relation_status': 'factual',
            'direction': record['observation']['direction'],
            'selected': record['observation']['selected'],
            'provenance': {'artifact_path': path, 'row_key': record['provenance']['row_key'],
                           'sha256': source_hashes[path],
                           'analysis_version': record['provenance']['analysis_version']},
        }
        if annotation is not None:
            item['annotation_source'] = annotation
        if key in edges and edges[key] != item:
            raise ValueError(f'Graph edge ID collision: {key}')
        edges[key] = item

    with v2.open(encoding='utf-8') as handle:
        for line in handle:
            record = json.loads(line)
            eid = record['evidence_id']
            if eid in summaries:
                raise ValueError(f'Duplicate EvidenceRecord v2 ID: {eid}')
            if record['source']['evidence_layer'] == 'de':
                old = legacy_rows[eid]
                if (record['provenance']['artifact_path'] != old.get('artifact_path') or
                        record['provenance']['row_key'] != old.get('row_key')):
                    raise ValueError(f'Evidence provenance differs from v1 registry: {eid}')
            row = _verified_row(root, run, record, source_hashes, cache)
            entity = record['biological_entity']
            omics = record['source']['omics']
            entity_node = node(NODE_TYPES[omics], entity['entity_id'],
                               entity['symbol'] if entity['symbol'] not in UNKNOWN else None)
            raw_axes = record['relationship']['axis']
            assigned = sorted({part.strip() for part in raw_axes.split(';') if part.strip() not in UNKNOWN})
            if set(assigned) - axis_names:
                raise ValueError(f'Unknown registered axis for {eid}: {assigned}')
            layer = record['source']['evidence_layer']
            if layer == 'de':
                expected_axes = {item['axis'] for item in axis_rows if item.get('evidence_id') == eid}
            elif layer == 'scrna_context':
                expected_axes = {item['axis'] for item in axes
                                 if entity['entity_id'] in item['genes']}
            else:
                raw_expected = legacy_rows[eid].get('axis') or ''
                expected_axes = {part.strip() for part in raw_expected.split(';')
                                 if part.strip() not in UNKNOWN}
            if set(assigned) != expected_axes:
                raise ValueError(f'Axis assignment differs from frozen annotation: {eid}')
            path = record['provenance']['artifact_path']
            summary = {
                'evidence_id': eid, 'entity_node_id': entity_node,
                'omics': omics, 'cohort': record['source']['cohort'],
                'evidence_layer': record['source']['evidence_layer'],
                'evidence_context': record['evidence_quality']['evidence_context'],
                'axis': assigned, 'direction': record['observation']['direction'],
                'selected': record['observation']['selected'],
                'provenance': {'path': path, 'row_key': record['provenance']['row_key'],
                               'sha256': source_hashes[path],
                               'analysis_version': record['provenance']['analysis_version']},
            }
            summaries[eid] = summary
            for axis in assigned:
                if layer == 'de':
                    annotation_path = 'integration/axis_evidence.csv'
                    annotation_key = f'{eid}|{axis}'
                elif layer == 'scrna_context':
                    annotation_path = 'knowledge/tme_metabolism_axes.yaml'
                    annotation_key = f"{axis}|{entity['entity_id']}"
                else:
                    annotation_path = 'integration/evidence_registry.csv'
                    annotation_key = eid
                annotation = {'artifact_path': annotation_path, 'row_key': annotation_key,
                              'sha256': source_hashes[annotation_path]}
                edge('same_axis', entity_node, node('axis', axis), record, path,
                     annotation=annotation)
            if layer == 'de':
                contrast = row.get('contrast', 'tumor_vs_adjacent')
                if contrast != 'tumor_vs_adjacent':
                    raise ValueError(f'Unexpected frozen DE contrast: {eid}')
                if row.get('test_status', 'tested') == 'tested' and record['observation']['effect'] is not None:
                    edge('measured_association', entity_node,
                         node('condition_contrast', contrast), record, path)
            elif layer == 'scrna_context' and row.get('status') == 'tested':
                cell_type = entity['cell_type']
                if cell_type not in UNKNOWN and row.get('celltype') == cell_type:
                    edge('expression_localization', entity_node,
                         node('cell_type', cell_type), record, path)
            elif layer == 'prognosis' and row.get('status') == 'tested':
                outcome = row.get('outcome', '')
                if outcome not in UNKNOWN:
                    edge('estimated_association', entity_node,
                         node('clinical_phenotype', outcome), record, path)
            elif layer == 'tcell_correlation' and row.get('status') == 'tested':
                readout, kind = row.get('readout', ''), row.get('readout_kind', '')
                if readout not in UNKNOWN and kind in {'tcell_proportion', 'tcell_signature'}:
                    edge('estimated_association', entity_node,
                         node('derived_readout', f'{kind}:{readout}'), record, path)

    for row in axis_rows:
        if row.get('evidence_id') and row['evidence_id'] not in summaries:
            raise ValueError(f'Axis table cites unknown evidence ID: {row["evidence_id"]}')
    coverage = []
    for axis in axes:
        specs = ([(gene, omics) for gene in axis['genes']
                  for omics in ('transcriptomics', 'proteomics')]
                 + [(compound, 'metabolomics') for compound in axis['compounds']])
        for entity, omics in specs:
            ids = sorted({row['evidence_id'] for row in axis_rows
                          if row.get('axis') == axis['axis'] and row.get('entity') == entity
                          and row.get('omics') == omics and row.get('evidence_id') in summaries})
            selected = any(summaries[eid]['selected'] is True for eid in ids)
            coverage.append({
                'axis': axis['axis'], 'entity': entity, 'omics': omics,
                'status': 'selected' if selected else 'observed_unselected' if ids else 'not_registered',
                'evidence_ids': ids, 'source_path': 'knowledge/tme_metabolism_axes.yaml',
                'source_sha256': source_hashes['knowledge/tme_metabolism_axes.yaml'],
            })
    graph = {
        'schema_version': '1.0',
        'source_artifacts': [{'path': path, 'sha256': digest}
                             for path, digest in sorted(source_hashes.items())],
        'nodes': sorted(nodes.values(), key=lambda item: item['id']),
        'edges': sorted(edges.values(), key=lambda item: item['id']),
        'evidence_records': sorted(summaries.values(), key=lambda item: item['evidence_id']),
        'axis_coverage': sorted(coverage, key=lambda item: (item['axis'], item['omics'], item['entity'])),
    }
    _validate_graph(graph, root, run)
    target = run / 'integration/evidence_graph.json'
    if target.exists():
        previous = load_evidence_graph(root, run)
        if previous != graph:
            raise ValueError('Existing evidence graph differs from frozen inputs')
        return target
    write_json(target, graph)
    return target


def axis_evidence(graph, axis, cohort='triple109'):
    """Return registered observations and the modalities they cover on one axis."""
    records = [item for item in graph['evidence_records'] if axis in item['axis']]
    if axis not in {node['entity_id'] for node in graph['nodes'] if node['type'] == 'axis'} and not any(
            item['axis'] == axis for item in graph['axis_coverage']):
        raise ValueError(f'Unknown graph axis: {axis}')
    measured = sorted({r['omics'] for r in records if r['evidence_layer'] == 'de'
                       and r['evidence_context'] == 'measured_evidence' and r['selected'] is True
                       and r['cohort'] == cohort
                       and r['omics'] in {'transcriptomics', 'proteomics', 'metabolomics'}})
    return {'axis': axis, 'evidence': records, 'selected_measured_omics': measured,
            'coverage': [item for item in graph['axis_coverage'] if item['axis'] == axis]}


def missing_links(graph, axis):
    """Coverage gaps mean no registered evidence, never demonstrated biological absence."""
    return [item for item in axis_evidence(graph, axis)['coverage']
            if item['status'] == 'not_registered']


def external_only_relations(graph):
    """Relations with no cohort edge for the same ordered endpoints."""
    other = {(edge['type'], edge['source'], edge['target']) for edge in graph['edges']
             if edge['evidence_context'] != 'external_context'}
    return [edge for edge in graph['edges']
            if edge['evidence_context'] == 'external_context'
            and (edge['type'], edge['source'], edge['target']) not in other]


def hypothesis_evidence(graph, hypothesis):
    """Join a validated draft's IDs at query time; do not add hypothesis graph edges."""
    known = {item['evidence_id']: item for item in graph['evidence_records']}
    support = hypothesis.get('supporting_evidence_ids', hypothesis.get('evidence_ids', []))
    counter = hypothesis.get('counterevidence_ids', [])
    if set(support) & set(counter) or set(support + counter) - set(known):
        raise ValueError('Hypothesis cites unknown or overlapping evidence IDs')
    return {'hypothesis_id': hypothesis['id'],
            'supporting_evidence': [known[eid] for eid in support],
            'counterevidence': [known[eid] for eid in counter]}


def candidate_relationship(source, target, relationship):
    """Keep model candidates outside the factual graph and mark their status explicitly."""
    if not all(isinstance(v, str) and v for v in (source, target, relationship)):
        raise ValueError('Candidate relationship requires two nodes and a relationship')
    return {'source': source, 'target': target, 'relationship': relationship,
            'relation_status': 'hypothesis'}


def integrator_graph_view(graph, active_ids=()):
    """Compact, qualitative axis view; queried off-axis IDs stay visible."""
    active = set(active_ids)
    known = {item['evidence_id'] for item in graph['evidence_records']}
    if active - known:
        raise ValueError(f'Lookup returned unknown graph evidence IDs: {sorted(active - known)[:5]}')
    result = []
    for axis in sorted({item['axis'] for item in graph['axis_coverage']}):
        view = axis_evidence(graph, axis)
        included = [item for item in view['evidence']
                    if item['evidence_layer'] == 'de' or item['selected'] is True
                    or item['evidence_context'] == 'external_context'
                    or item['evidence_id'] in active]
        ids = {item['evidence_id'] for item in included}
        relations = [{'type': edge['type'], 'source': edge['source'],
                      'target': edge['target'], 'evidence_id': edge['evidence_id'],
                      'evidence_context': edge['evidence_context']}
                     for edge in graph['edges'] if edge['evidence_id'] in ids]
        result.append({'axis': axis, 'evidence': included, 'relationships': relations,
                       'selected_measured_omics': view['selected_measured_omics'],
                       'coverage': view['coverage'],
                       'unregistered_links': [item for item in view['coverage']
                                              if item['status'] == 'not_registered']})
    off_axis = [item for item in graph['evidence_records']
                if item['evidence_id'] in active and not item['axis']]
    off_ids = {item['evidence_id'] for item in off_axis}
    return {'axes': result, 'queried_off_axis_evidence': off_axis,
            'queried_off_axis_relationships': [
                {'type': edge['type'], 'source': edge['source'],
                 'target': edge['target'], 'evidence_id': edge['evidence_id'],
                 'evidence_context': edge['evidence_context']}
                for edge in graph['edges'] if edge['evidence_id'] in off_ids]}
