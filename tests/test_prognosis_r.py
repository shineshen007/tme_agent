"""Real-Rscript contract tests for the pre-registered candidate-axis prognosis tool.

The tool is the authority for family-wise BH adjustment, the not-estimable boundary and
the HR/CI arithmetic. These tests build a small synthetic job, execute
``scripts/06_prognosis.R`` with the project R library, and independently recompute the
contract in Python. They skip (never fake a pass) when Rscript or a required R package is
unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/06_prognosis.R'
REQUIRED_PACKAGES = ('survival', 'GSVA', 'BiocParallel', 'jsonlite')


def _rscript():
    candidate = shutil.which('Rscript')
    if candidate:
        return candidate
    local = ROOT / 'config/runtime.local.yaml'
    if local.exists():
        try:
            import yaml
            runtime = (yaml.safe_load(local.read_text(encoding='utf-8')) or {}).get('runtime') or {}
            configured = str(runtime.get('rscript') or '')
            if configured and Path(configured).is_file():
                return configured
        except (OSError, ValueError):
            return None
    return None


def _r_libraries():
    """Project bootstrap library first, then every library the host R can see."""
    libraries = []
    local = ROOT / 'config/runtime.local.yaml'
    if local.exists():
        try:
            import yaml
            runtime = (yaml.safe_load(local.read_text(encoding='utf-8')) or {}).get('runtime') or {}
            libraries += [str(p) for p in runtime.get('r_library_paths') or [] if p and Path(p).is_dir()]
        except (OSError, ValueError):
            pass
    project = ROOT / '.runtime/r-library'
    if project.is_dir():
        libraries.append(str(project))
    libraries = list(dict.fromkeys(libraries))
    rscript = _rscript()
    if rscript:
        probe = subprocess.run([rscript, '--vanilla', '-e', 'cat(.libPaths(), sep="\\n")'],
                               capture_output=True, text=True, timeout=120,
                               env={**os.environ, 'R_LIBS_USER': os.pathsep.join(libraries)})
        if probe.returncode == 0:
            libraries += [line.strip() for line in probe.stdout.splitlines()
                          if line.strip() and Path(line.strip()).is_dir()]
    return list(dict.fromkeys(libraries))


@lru_cache(maxsize=1)
def _r_environment():
    rscript = _rscript()
    if not rscript:
        return None, {}, 'Rscript is not installed'
    libraries = _r_libraries()
    env = {**os.environ, 'R_LIBS_USER': os.pathsep.join(libraries),
           'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'}
    code = ("pkgs <- c('jsonlite','survival','GSVA','BiocParallel');"
            "cat(paste(as.integer(vapply(pkgs, requireNamespace, logical(1), quietly=TRUE)), collapse=''))")
    probe = subprocess.run([rscript, '--vanilla', '-e', code], capture_output=True,
                           text=True, timeout=300, env=env)
    found = probe.stdout.strip().splitlines()[-1] if probe.returncode == 0 and probe.stdout.strip() else ''
    if probe.returncode != 0 or len(found) != len(REQUIRED_PACKAGES):
        return None, env, f'R dependency probe failed: {(probe.stderr or probe.stdout)[-400:]}'
    missing = [name for name, ok in zip(REQUIRED_PACKAGES, found) if ok != '1']
    if missing:
        return None, env, f'Missing R packages: {", ".join(missing)}'
    return rscript, env, ''


_RSCRIPT, _R_ENV, _SKIP_REASON = _r_environment()
pytestmark = pytest.mark.skipif(_RSCRIPT is None, reason=_SKIP_REASON or 'Rscript unavailable')


def _bh(pvalues):
    """Independent Benjamini-Hochberg implementation used only for assertions."""
    values = np.asarray(pvalues, dtype=float)
    m = len(values)
    order = np.argsort(values)
    ranked = values[order]
    scaled = ranked * m / np.arange(1, m + 1)
    monotone = np.minimum.accumulate(scaled[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(monotone, 0.0, 1.0)
    return out


def _synthetic_job(tmp_path, fdr=0.05):
    """15 patients x 2 tissues, 5 transcriptomics features, min_events=1.

    GENE5 is constant so at least one zero-variance not-estimable row per family is
    guaranteed, while the other four genes produce tested models.
    """
    out = tmp_path / 'prognosis_run'
    (out / 'data').mkdir(parents=True)
    data = out / 'data'
    rng = np.random.default_rng(20240215)
    n = 15
    patient = [f'P{i:02d}' for i in range(1, n + 1)]
    tumor = [f'T{i:02d}' for i in range(1, n + 1)]
    adjacent = [f'A{i:02d}' for i in range(1, n + 1)]
    clinical = pd.DataFrame({
        'patient_id': patient,
        'age': rng.integers(40, 76, n),
        'sex': ['male' if i % 2 else 'female' for i in range(n)],
        'cirrhosis': [1 if i % 3 else 0 for i in range(n)],
        'os_months': np.round(rng.uniform(5, 80, n), 2),
        'os_event': rng.integers(0, 2, n),
        'rfs_months': np.round(rng.uniform(3, 70, n), 2),
        'rfs_event': rng.integers(0, 2, n),
        'tumor_purity': np.round(rng.uniform(0.3, 0.9, n), 3),
        'tnm_stage': [['I', 'II', 'III', 'IV'][i % 4] for i in range(n)],
        'tnm_ordinal': [i % 4 + 1 for i in range(n)],
    })
    for event in ('os_event', 'rfs_event'):
        clinical.loc[clinical.index[:3], event] = 1
    clinical.to_csv(data / 'clinical.csv', index=False)
    pd.DataFrame({'patient_id': patient, 'in_triple109': True}).to_csv(data / 'patients.csv', index=False)
    rows = []
    for pid, tumor_id, adjacent_id in zip(patient, tumor, adjacent):
        rows.append({'patient_id': pid, 'biospecimen_id': tumor_id, 'tissue': 'tumor'})
        rows.append({'patient_id': pid, 'biospecimen_id': adjacent_id, 'tissue': 'adjacent'})
    pd.DataFrame(rows).to_csv(data / 'samples.csv', index=False)
    genes = ['GENE1', 'GENE2', 'GENE3', 'GENE4']
    matrix = pd.DataFrame(rng.normal(6, 1.2, (len(genes), len(tumor) + len(adjacent))), index=genes)
    matrix = pd.concat([pd.DataFrame([[5.0] * (len(tumor) + len(adjacent))], index=['GENE5'],
                                     columns=matrix.columns), matrix])
    matrix.columns = tumor + adjacent
    matrix.to_csv(data / 'expression.csv')
    features = [{'feature_id': gene, 'omics': 'transcriptomics', 'entity': gene, 'axis': 'axis_demo'}
                for gene in genes + ['GENE5']]
    job = {
        'omics': 'prognosis_candidate_axis', 'cohort_id': 'triple109', 'axis': 'axis_demo',
        'features': features, 'missing_features': [],
        'expression': {'transcriptomics': str(data / 'expression.csv')},
        'sample_metadata': {'transcriptomics': str(data / 'samples.csv')},
        'clinical': str(data / 'clinical.csv'), 'patients': str(data / 'patients.csv'),
        'same_batch_metadata': str(data / 'samples.csv'),
        'proportions': {}, 'deconvolution_celltypes': [],
        'signature_gmt': str(data / 'missing_signatures.gmt'), 'signature_features': [],
        'signature_min_size': 2, 'min_events': 1, 'fdr': fdr,
        'output_dir': str(out / 'prognosis'), 'seed': 2024,
        'r_library_paths': list(_r_libraries()),
    }
    job_path = out / 'job.json'
    job_path.write_text(json.dumps(job, indent=2))
    return job_path, out / 'prognosis'


def _run_script(job_path, tmp_path, expected_rc=0):
    result = subprocess.run([_RSCRIPT, '--vanilla', str(SCRIPT), str(job_path)],
                            capture_output=True, text=True, env=_R_ENV,
                            cwd=str(ROOT), timeout=900)
    if expected_rc == 0:
        assert result.returncode == 0, f'Rscript failed:\n{result.stdout[-1500:]}\n{result.stderr[-3000:]}'
    return result


def _read_outputs(out):
    cox = pd.read_csv(out / 'cox_models.csv')
    stats = json.loads((out / 'statistics.json').read_text(encoding='utf-8'))
    return cox, stats


def test_r_prognosis_computes_family_wise_bh_and_retains_not_estimable(tmp_path):
    job_path, out = _synthetic_job(tmp_path, fdr=0.05)
    _run_script(job_path, tmp_path)
    cox, stats = _read_outputs(out)

    for column in ['family', 'padj', 'selected', 'coef', 'se', 'hr', 'ci_low', 'ci_high',
                   'pvalue', 'ph_p', 'status', 'reason', 'n', 'n_events']:
        assert column in cox.columns, f'cox_models.csv missing {column}'

    expected_family = ['prognosis_' + str(c) + '_' + str(o) + '_' + str(m)
                       for c, o, m in zip(cox.cohort, cox.outcome, cox.model_type)]
    assert cox.family.astype(str).tolist() == expected_family
    assert set(cox.cohort) == {'triple109'}
    assert set(cox.outcome) == {'os', 'rfs'}
    assert set(cox.model_type) == {'univariable', 'adjusted'}
    assert cox.status.isin(['tested', 'not_estimable']).all()
    assert not cox.duplicated(['cohort', 'outcome', 'model_type', 'feature_id']).any()

    # Independent within-family BH recomputation; a global adjustment does not match.
    tested = cox.status.eq('tested')
    assert tested.any() and (~tested).any()
    observed = cox.padj.to_numpy(dtype=float)
    expected = np.full(len(cox), np.nan)
    for _, group in cox.groupby('family'):
        group_tested = group.status.eq('tested') & np.isfinite(group.pvalue.to_numpy(dtype=float))
        expected[group.index[group_tested]] = _bh(group.pvalue[group_tested].to_numpy(dtype=float))
    assert np.allclose(expected[tested], observed[tested], atol=1e-8, equal_nan=True)
    assert expected[~tested].size == 0 or np.isnan(expected[~tested]).all()
    assert cox.loc[~tested, 'padj'].isna().all()
    global_expected = _bh(cox.loc[tested, 'pvalue'].to_numpy(dtype=float))
    assert not np.allclose(global_expected, observed[tested], atol=1e-8), \
        'R padj matches a global adjustment; families were not applied'

    assert cox.selected.dtype == bool
    expected_selected = tested & cox.padj.notna() & (cox.padj <= 0.05)
    assert cox.selected.equals(expected_selected)

    z = 1.959963984540054  # scipy.stats.norm.ppf(0.975)
    tested_rows = cox.loc[tested]
    for column in ['coef', 'se', 'hr', 'ci_low', 'ci_high', 'pvalue', 'padj']:
        assert tested_rows[column].notna().all(), f'tested row has missing {column}'
        assert np.isfinite(tested_rows[column].to_numpy(dtype=float)).all()
    assert tested_rows.se.gt(0).all()
    assert tested_rows.pvalue.between(0, 1).all()
    assert tested_rows.padj.between(0, 1).all()
    assert np.allclose(tested_rows.hr, np.exp(tested_rows.coef), rtol=1e-9, atol=1e-12)
    assert np.allclose(tested_rows.ci_low, np.exp(tested_rows.coef - z * tested_rows.se), rtol=1e-8, atol=1e-12)
    assert np.allclose(tested_rows.ci_high, np.exp(tested_rows.coef + z * tested_rows.se), rtol=1e-8, atol=1e-12)
    assert ((tested_rows.ci_low <= tested_rows.hr) & (tested_rows.hr <= tested_rows.ci_high)).all()
    assert (tested_rows.n_events <= tested_rows.n).all()
    assert tested_rows.reason.isna().all()

    not_estimable = cox.loc[~tested]
    for column in ['coef', 'se', 'hr', 'ci_low', 'ci_high', 'pvalue', 'padj']:
        assert not_estimable[column].isna().all(), f'not-estimable row carries {column}'
    assert not_estimable.selected.eq(False).all()
    assert not_estimable.reason.notna().all()
    assert not_estimable.reason.astype(str).str.strip().ne('').all()
    assert set(not_estimable.reason).issubset({
        'insufficient_n_or_events', 'zero_variance', 'coxph_failed', 'exposure_not_estimable',
        'separation_or_nonconvergence', 'nonfinite_estimate'})
    constant_rows = cox.loc[cox.feature_id == 'GENE5']
    assert len(constant_rows) == 4
    assert constant_rows.status.eq('not_estimable').all()
    assert set(constant_rows.reason) == {'zero_variance'}

    # statistics.json states the actual method and the frozen level.
    assert stats['status'] == 'complete'
    assert stats['fdr'] == 0.05
    declaration = (str(stats.get('method', '')) + ' ' + str(stats.get('multiple_testing', ''))).upper()
    assert 'BH' in declaration or 'BENJAMINI' in declaration
    assert 'FAMIL' in declaration
    assert stats['n_cox_rows'] == len(cox)
    assert stats['n_cox_tested'] == int(tested.sum())
    assert stats['n_cox_padj'] == int(cox.padj.notna().sum())
    assert stats['n_cox_selected'] == int(cox.selected.sum())
    assert stats['n_not_estimable'] == int((~tested).sum())
    assert set(stats['families']) == set(cox.family.astype(str))

    # The R output must satisfy the Lead-owned Python validator unchanged.
    from orchestrator.checkers import validate_prognosis_outputs
    summary = validate_prognosis_outputs(out, stats)
    assert summary['n_cox_rows'] == len(cox)


@pytest.mark.parametrize('mutation', [
    {'delete': 'fdr'}, {'fdr': 0}, {'fdr': 1}, {'fdr': 1.5}, {'fdr': -0.1}, {'fdr': 'not-a-number'},
])
def test_r_prognosis_refuses_missing_or_invalid_fdr(tmp_path, mutation):
    job_path, out = _synthetic_job(tmp_path)
    job = json.loads(job_path.read_text(encoding='utf-8'))
    if 'delete' in mutation:
        job.pop(mutation['delete'])
    else:
        job.update(mutation)
    job_path.write_text(json.dumps(job, indent=2))
    result = _run_script(job_path, tmp_path, expected_rc=1)
    assert result.returncode != 0
    assert 'fdr' in result.stderr.lower()
    assert not (out / 'cox_models.csv').exists()


def test_r_prognosis_selected_uses_frozen_fdr(tmp_path):
    job_path, out = _synthetic_job(tmp_path, fdr=0.25)
    _run_script(job_path, tmp_path)
    cox, stats = _read_outputs(out)
    assert stats['fdr'] == 0.25
    tested = cox.status.eq('tested')
    assert cox.selected.equals(tested & cox.padj.notna() & (cox.padj <= 0.25))
    # With this frozen level at least one tested model is selected; selected stays False
    # for every not-estimable row.
    assert cox.loc[tested, 'selected'].any()
    assert cox.loc[~tested, 'selected'].eq(False).all()


def _extract_fit_cox():
    text = SCRIPT.read_text(encoding='utf-8')
    start = text.index('fit_cox <- function')
    depth = 0
    for index in range(start, len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError('fit_cox function block not found')


def test_fit_cox_flags_separation_and_nonfinite_estimates(tmp_path):
    """Parse/evaluate only fit_cox() so separation handling is deterministic.

    A synthetic dataset where all events fall in one exposure group is monotone
    likelihood separation; it must be not_estimable with NA estimates, never tested.
    """
    function_text = _extract_fit_cox()
    assert 'withCallingHandlers' in function_text, 'coxph warnings must be captured'
    driver = tmp_path / 'fit_cox_driver.R'
    driver.write_text(
        "suppressPackageStartupMessages(library(survival))\n"
        "job <- list(min_events=1L)\n"
        + function_text + "\n"
        "emit <- function(label, d) {\n"
        "  r <- fit_cox(d, 'time', 'event', 'univariable')\n"
        "  cat(jsonlite::toJSON(list(label=label, status=r$status, reason=r$reason, n=r$n,\n"
        "    n_events=r$n_events, coef=r$coef, se=r$se, hr=r$hr, ci_low=r$ci_low,\n"
        "    ci_high=r$ci_high, pvalue=r$pvalue, ph_p=r$ph_p), auto_unbox=TRUE, na='null', digits=NA), '\\n')\n"
        "}\n"
        "emit('separation', data.frame(time=c(1:6,7:12), event=c(rep(1,6),rep(0,6)),\n"
        "                              value=c(rep(1,6),rep(0,6))))\n"
        "emit('zero_variance', data.frame(time=1:12, event=c(rep(1,6),rep(0,6)), value=rep(1,12)))\n"
        "emit('no_events', data.frame(time=1:12, event=rep(0,12), value=1:12))\n"
        "emit('regular', data.frame(time=c(2,4,6,8,10,12,14,16,18,20,22,24),\n"
        "                           event=c(1,0,1,0,1,0,1,0,1,0,1,0),\n"
        "                           value=c(0.1,-0.4,0.3,-0.2,0.5,-0.7,0.2,-0.1,0.9,-0.3,0.4,-0.6)))\n",
        encoding='utf-8')
    result = subprocess.run([_RSCRIPT, '--vanilla', str(driver)], capture_output=True,
                            text=True, env=_R_ENV, timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]
    cases = {row['label']: row for row in
             (json.loads(line) for line in result.stdout.splitlines() if line.strip())}

    separation = cases['separation']
    assert separation['status'] == 'not_estimable'
    assert separation['reason'] == 'separation_or_nonconvergence'
    for key in ('coef', 'se', 'hr', 'ci_low', 'ci_high', 'pvalue'):
        assert separation[key] is None, f'separation row leaked {key}'
    assert separation['n_events'] <= separation['n']

    assert cases['zero_variance']['status'] == 'not_estimable'
    assert cases['zero_variance']['reason'] == 'zero_variance'
    assert cases['no_events']['status'] == 'not_estimable'
    assert cases['no_events']['reason'] == 'insufficient_n_or_events'
    assert cases['no_events']['n_events'] == 0

    regular = cases['regular']
    assert regular['status'] == 'tested'
    assert np.isfinite([regular['coef'], regular['se'], regular['hr'],
                        regular['ci_low'], regular['ci_high'], regular['pvalue']]).all()
    assert regular['se'] > 0
    z = 1.959963984540054
    assert np.isclose(regular['hr'], np.exp(regular['coef']), rtol=1e-9, atol=1e-12)
    assert np.isclose(regular['ci_low'], np.exp(regular['coef'] - z * regular['se']), rtol=1e-8)
    assert np.isclose(regular['ci_high'], np.exp(regular['coef'] + z * regular['se']), rtol=1e-8)


def test_r_prognosis_signature_uses_the_full_expression_background(tmp_path):
    """A nonempty signature must be scored on all mapped genes, not the signature subset."""
    job_path, out = _synthetic_job(tmp_path)
    job = json.loads(job_path.read_text(encoding='utf-8'))
    expression_path = Path(job['expression']['transcriptomics'])
    expression = pd.read_csv(expression_path, index_col=0)
    rng = np.random.default_rng(20261030)
    extra = pd.DataFrame(rng.normal(6, 1.2, (40, expression.shape[1])),
                         index=['BG' + str(i) for i in range(40)], columns=expression.columns)
    expression = pd.concat([expression, extra])
    expression.to_csv(expression_path)
    job['signature_features'] = [{'symbol': 'GENE' + str(i), 'feature_id': 'GENE' + str(i)}
                                 for i in range(1, 5)]
    job['signature_background'] = [{'symbol': symbol, 'feature_id': symbol}
                                   for symbol in expression.index.astype(str)]
    job['signature_min_size'] = 2
    gmt = Path(job['signature_gmt'])
    gmt.write_text('SIGNATURE\tdemo\tGENE1\tGENE2\tGENE3\tGENE4\n', encoding='utf-8')
    job_path.write_text(json.dumps(job, indent=2))

    _run_script(job_path, tmp_path)
    features = pd.read_csv(out / 'patient_features.csv')
    produced = features.loc[features.value_type.eq('tcell_signature'), 'value'].to_numpy(dtype=float)
    stats = json.loads((out / 'statistics.json').read_text(encoding='utf-8'))
    assert stats['signature_status'] == 'complete'
    assert stats['signature_background_genes'] == 44
    assert stats['signature_dropped_constant_features'] == 1
    assert len(produced) == 15 and np.isfinite(produced).all()

    reference = tmp_path / 'reference.json'
    code = """a<-commandArgs(TRUE)
m<-as.matrix(read.csv(a[1],row.names=1,check.names=FALSE))
m<-m[,grepl('^T',colnames(m)),drop=FALSE]
m<-m[apply(m,1,sd)>0,,drop=FALSE]
sets<-list(SIGNATURE=c('GENE1','GENE2','GENE3','GENE4'))
s<-GSVA::gsva(GSVA::ssgseaParam(m,sets,minSize=2,normalize=TRUE),verbose=FALSE,BPPARAM=BiocParallel::SerialParam())
jsonlite::write_json(list(finite=all(is.finite(s)),scores=as.numeric(s),background_genes=nrow(m)),a[2],pretty=TRUE,auto_unbox=TRUE,digits=NA)
"""
    script = tmp_path / 'reference.R'
    script.write_text(code, encoding='utf-8')
    run = subprocess.run([_RSCRIPT, '--vanilla', str(script), str(expression_path), str(reference)],
                         capture_output=True, text=True, env=_R_ENV, timeout=300)
    assert run.returncode == 0, run.stderr[-3000:]
    expected = json.loads(reference.read_text(encoding='utf-8'))
    assert expected['finite'] is True and expected['background_genes'] == 44
    assert np.allclose(produced, np.asarray(expected['scores'], dtype=float), atol=1e-8)
