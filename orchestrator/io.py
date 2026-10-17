"""Paths, provenance and atomic output handling. No raw input writes."""
from __future__ import annotations
import hashlib
import json
import os
import platform
import importlib.metadata
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
import yaml


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        f.write(data + '\n')
        temp = f.name
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_config(root, path=None):
    root = Path(root).resolve()
    p = Path(path).resolve() if path else root / 'config/study_config.yaml'
    if not p.is_file() and path is None:
        p = root / 'config/study_config.example.yaml'
    cfg = yaml.safe_load(p.read_text(encoding='utf-8'))
    local = root / 'config/runtime.local.yaml'
    if local.exists():
        cfg['runtime'].update(yaml.safe_load(local.read_text()).get('runtime', {}))
    return cfg


def project_file(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'Input outside project root: {relative}')
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def code_files(root):
    root = Path(root)
    for folder in ['orchestrator', 'scripts', 'schemas', 'prompts', 'knowledge']:
        for p in sorted((root / folder).rglob('*')):
            if p.is_file() and p.suffix in {'.py', '.R', '.json', '.yaml', '.md', '.sh', '.gmt', '.csv', '.tsv'} and '__pycache__' not in p.parts:
                yield p


def provenance(root, cfg):
    root = Path(root).resolve()
    files = {k: {'path': v, 'sha256': sha256(project_file(root, v))} for k, v in cfg['inputs'].items()}
    code = {str(p.relative_to(root)): sha256(p) for p in code_files(root)}
    python_packages = {p: importlib.metadata.version(p) for p in ['numpy','pandas','scipy','PyYAML','jsonschema','langgraph','matplotlib']}
    r_packages = {}
    r_executable=shutil.which(cfg['runtime']['rscript']) or cfg['runtime']['rscript']
    r_home=Path(r_executable).resolve().parent.parent
    blas_candidates=[r_home/'lib/libRblas.0.dylib',r_home/'lib/libRblas.so',r_home/'lib/libRblas.so.0']
    r_files={str(p):sha256(p) for p in [r_home/'bin/Rscript',r_home/'bin/exec/R',*blas_candidates] if p.is_file()}
    for lib in cfg['runtime']['r_library_paths']:
        for package in ['DESeq2','limma','fgsea','GSVA','AnnotationDbi','org.Hs.eg.db','jsonlite','BiocParallel']:
            description = Path(lib)/package/'DESCRIPTION'
            if description.exists() and package not in r_packages:
                r_packages[package] = sha256(description)
    payload = {'inputs': files, 'code': code, 'config': cfg, 'environment': {'python': platform.python_version(), 'python_packages':python_packages,'R_package_description_hashes':r_packages,'R_runtime_file_hashes':r_files}}
    payload['fingerprint'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    payload['created_at'] = now()
    payload['python'] = platform.python_version()
    return payload


def artifact(run, path, **extra):
    path = Path(path).resolve()
    run = Path(run).resolve()
    if not path.is_relative_to(run):
        raise ValueError('Artifact must be inside run directory')
    return {'path': str(path.relative_to(run)), 'sha256': sha256(path), 'bytes': path.stat().st_size, **extra}
