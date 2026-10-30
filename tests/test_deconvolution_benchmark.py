"""Simulated deconvolution benchmark for the corrected reference/signature contract.

The benchmark needs Rscript plus ``nnls``, ``limma`` and ``Seurat`` so that it exercises the
same estimator/limma code paths as ``scripts/05_scrna_celltype.R``. When any dependency is
missing the R benchmark is NOT_EVALUABLE in this environment and is skipped; nothing is
installed and no network is used. A dependency-free property check of the pseudobulk unit
contract (Jensen bound used by the validator) always runs.

The benchmark is diagnostic, not a validation of the atlas data: synthetic reference/mixture
profiles with a fixed seed only show that the corrected union-marker signature recovers known
proportions and that the old zero-filled matrix does not.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_R_LIB = ROOT / '.runtime/r-library'
REQUIRED_R_PACKAGES = ('nnls', 'limma', 'Seurat')

BENCHMARK_SCRIPT = r'''
args <- commandArgs(trailingOnly=TRUE)
outdir <- args[1]
libs <- args[2]
if(nzchar(libs)) .libPaths(c(strsplit(libs, .Platform$path.sep, fixed=TRUE)[[1]], .libPaths()))
suppressPackageStartupMessages(library(nnls))
suppressPackageStartupMessages(library(limma))
set.seed(20241028)

# ---- synthetic reference: 4 cell types x 15 exclusive marker genes + shared baseline ----
n_ct <- 4L; n_gene <- 60L; n_donor <- 10L
ct_names <- sprintf('CT%d', seq_len(n_ct))
gene_names <- sprintf('G%02d', seq_len(n_gene))
base <- matrix(20, n_gene, n_ct, dimnames=list(gene_names, ct_names))
marker_index <- list()
for(k in seq_len(n_ct)) {
  idx <- ((k-1L)*15L + 1L):(k*15L)
  marker_index[[ct_names[k]]] <- idx
  base[idx, k] <- 500 + seq_along(idx)*10
}
ref <- array(0, dim=c(n_gene, n_ct, n_donor),
             dimnames=list(gene_names, ct_names, sprintf('D%02d', seq_len(n_donor))))
for(d in seq_len(n_donor)) for(k in seq_len(n_ct))
  ref[, k, d] <- base[, k]*exp(rnorm(n_gene, 0, 0.15))

# Corrected signature: every union marker gene uses its real mean CPM in every cell type.
sig <- vapply(ct_names, function(k) rowMeans(ref[, k, , drop=FALSE]), numeric(n_gene))
colnames(sig) <- ct_names; rownames(sig) <- gene_names
# Legacy (buggy) signature: genes that are not markers of the cell type are forced to zero.
legacy <- matrix(0, n_gene, n_ct, dimnames=list(gene_names, ct_names))
for(k in seq_len(n_ct)) {
  mk <- marker_index[[ct_names[k]]]
  legacy[mk, k] <- rowMeans(ref[mk, k, , drop=FALSE])
}

# ---- mixtures: 20 patients x tumor/adjacent pair with known proportions ----
n_patient <- 20L
prop_t <- matrix(runif(n_patient*n_ct, 0.2, 1), n_patient, n_ct)
prop_t <- prop_t/rowSums(prop_t)
prop_a <- prop_t*exp(matrix(rnorm(n_patient*n_ct, 0, 0.25), n_patient, n_ct))
prop_a <- prop_a/rowSums(prop_a)
noise <- function() matrix(exp(rnorm(n_gene*n_patient, 0, 0.05)), n_gene, n_patient)
bulk_t <- sig %*% t(prop_t)*noise()
bulk_a <- sig %*% t(prop_a)*noise()
recover <- function(A, B) t(vapply(seq_len(ncol(B)), function(j) {
  fit <- nnls::nnls(A, B[, j])$x
  s <- sum(fit)
  if(is.finite(s) && s > 0) fit/s else rep(NA_real_, ncol(A))
}, numeric(ncol(A))))
est_t <- recover(sig, bulk_t)
est_a <- recover(sig, bulk_a)
legacy_t <- recover(legacy, bulk_t)

abs_error <- function(est, truth) mean(abs(est - truth))
# ---- pseudobulk unit audit on simulated donor pseudobulk (Jensen bound) ----
mean_cpm <- apply(ref, c(1, 2), mean)
mean_logcpm <- apply(log1p(ref), c(1, 2), mean)
jensen_violation <- max(mean_logcpm - log1p(mean_cpm))
# ---- donor-level limma marker sensitivity (same ranking idea as the production script) ----
overlap <- numeric(n_ct)
for(k in seq_len(n_ct)) {
  in_mat <- log1p(ref[, k, , drop=FALSE]); dim(in_mat) <- c(n_gene, n_donor)
  out_ct <- setdiff(seq_len(n_ct), k)
  out_mat <- log1p(ref[, out_ct, , drop=FALSE]); dim(out_mat) <- c(n_gene, n_donor*length(out_ct))
  mat <- cbind(in_mat, out_mat)
  design <- model.matrix(~ factor(c(rep(1, n_donor), rep(0, ncol(out_mat))), levels=c(0, 1)))
  tt <- limma::topTable(limma::eBayes(limma::lmFit(mat, design)), coef=2, number=15, sort.by='P')
  overlap[k] <- length(intersect(rownames(tt), gene_names[marker_index[[ct_names[k]]]]))/15
}
# ---- reference sensitivity: pancancer (10 donors) vs source-restricted (5 donors) panels ----
sig_src <- vapply(ct_names, function(k) rowMeans(ref[, k, 1:5, drop=FALSE]), numeric(n_gene))
colnames(sig_src) <- ct_names; rownames(sig_src) <- gene_names
src_t <- recover(sig_src, bulk_t)
src_a <- recover(sig_src, bulk_a)
sens <- data.frame(comparison='pancancer_vs_source_restricted_5donors', celltype=ct_names,
  n=n_patient, spearman_rho=NA_real_, mean_abs_difference=NA_real_, direction_concordance=NA_real_,
  stringsAsFactors=FALSE)
for(k in seq_along(ct_names)) {
  a <- est_t[, k]; b <- src_t[, k]
  delta_a <- est_t[, k] - est_a[, k]; delta_b <- src_t[, k] - src_a[, k]
  sens$spearman_rho[k] <- if(stats::sd(a) > 0 && stats::sd(b) > 0) stats::cor(a, b, method='spearman') else NA_real_
  sens$mean_abs_difference[k] <- mean(abs(a - b))
  sens$direction_concordance[k] <- mean(sign(delta_a) == sign(delta_b))
}
legacy_matrix <- legacy
forced_zero_corrected <- sum(sig == 0 & apply(ref, c(1, 2), mean) > 0)
forced_zero_legacy <- sum(legacy == 0 & apply(ref, c(1, 2), mean) > 0)
metrics <- data.frame(metric=c('n_patients','n_celltypes','n_genes','mean_abs_error_corrected',
  'mean_abs_error_legacy_zero_filled','forced_zero_entries_corrected','forced_zero_entries_legacy',
  'max_jensen_violation','min_limma_marker_overlap','min_signature_value'),
  value=c(n_patient, n_ct, n_gene, abs_error(est_t, prop_t), abs_error(legacy_t, prop_t),
          forced_zero_corrected, forced_zero_legacy, jensen_violation, min(overlap), min(sig)),
  stringsAsFactors=FALSE)
write.csv(metrics, file.path(outdir, 'metrics.csv'), row.names=FALSE)
write.csv(sens, file.path(outdir, 'reference_sensitivity.csv'), row.names=FALSE, na='')
write.csv(data.frame(gene=gene_names, mean_cpm, check.names=FALSE),
          file.path(outdir, 'pseudobulk_means.csv'), row.names=FALSE)
write.csv(data.frame(gene=gene_names, sig, check.names=FALSE),
          file.path(outdir, 'signature_pancancer.csv'), row.names=FALSE)
write.csv(data.frame(gene=gene_names, legacy, check.names=FALSE),
          file.path(outdir, 'signature_zero_filled_legacy.csv'), row.names=FALSE)
write.csv(as.data.frame.table(mean_cpm), file.path(outdir, 'reference_means_long.csv'), row.names=FALSE)
'''


def _r_package_status():
    """Probe Rscript and the required packages without installing or touching the network."""
    rscript = shutil.which('Rscript')
    if rscript is None:
        return None, list(REQUIRED_R_PACKAGES), 'Rscript not found'
    libraries = [str(PROJECT_R_LIB)] if PROJECT_R_LIB.is_dir() else []
    if libraries:
        prefix = ('.libPaths(c('
                  + ', '.join(json.dumps(path) for path in libraries) + ')); ')
    else:
        prefix = ''
    expr = prefix + 'for (p in c(' + ', '.join(f'"{p}"' for p in REQUIRED_R_PACKAGES) + ')) cat(p, requireNamespace(p, quietly=TRUE), "\\n")'
    try:
        completed = subprocess.run([rscript, '--vanilla', '-e', expr],
                                   capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        return None, list(REQUIRED_R_PACKAGES), f'R invocation failed: {exc}'
    status = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            status[parts[0]] = parts[1] == 'TRUE'
    missing = [package for package in REQUIRED_R_PACKAGES if not status.get(package)]
    return rscript, missing, None if not missing else 'missing R package(s): ' + ', '.join(missing)


_RSCRIPT, _MISSING_R, _R_REASON = _r_package_status()
R_BENCHMARK_AVAILABLE = _RSCRIPT is not None and not _MISSING_R
R_BENCHMARK_SKIP = (
    'NOT_EVALUABLE: simulated deconvolution benchmark requires Rscript with '
    + ', '.join(REQUIRED_R_PACKAGES)
    + ('; ' + _R_REASON if _R_REASON else '')
    + '. Nothing was installed (offline contract); the corrected deconvolution benchmark '
      'stays unevaluated in this environment.')
requires_r_benchmark = pytest.mark.skipif(not R_BENCHMARK_AVAILABLE, reason=R_BENCHMARK_SKIP)


# --------------------------------------------------------------------------------------
# Dependency-free contract property (runs everywhere)
# --------------------------------------------------------------------------------------
def test_pseudobulk_unit_jensen_bound_is_a_real_constraint():
    """mean(log1p(CPM)) <= log1p(mean(CPM)); the validator's pseudobulk inequality is non-trivial."""
    rng = np.random.default_rng(20241028)
    cpm = rng.lognormal(mean=3.0, sigma=1.2, size=(500, 12))
    mean_cpm = cpm.mean(axis=1)
    mean_logcpm = np.log1p(cpm).mean(axis=1)
    assert (mean_cpm >= 0).all() and (mean_logcpm >= 0).all()
    assert (mean_logcpm <= np.log1p(mean_cpm) + 1e-12).all()
    # The two units are not interchangeable: reporting one in the other's column is detectable.
    assert np.abs(mean_cpm - mean_logcpm).max() > 1.0


# --------------------------------------------------------------------------------------
# R benchmark (skipped as NOT_EVALUABLE when dependencies are missing)
# --------------------------------------------------------------------------------------
@pytest.fixture(scope='module')
def benchmark_output(tmp_path_factory):
    workdir = tmp_path_factory.mktemp('deconv_benchmark')
    script = workdir / 'benchmark.R'
    script.write_text(BENCHMARK_SCRIPT, encoding='utf-8')
    libraries = str(PROJECT_R_LIB) if PROJECT_R_LIB.is_dir() else ''
    completed = subprocess.run([_RSCRIPT, '--vanilla', str(script), str(workdir), libraries],
                               capture_output=True, text=True, timeout=900)
    assert completed.returncode == 0, (
        'simulated deconvolution benchmark failed:\n'
        f'STDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}')
    metrics = pd.read_csv(workdir/'metrics.csv').set_index('metric')['value']
    sensitivity = pd.read_csv(workdir/'reference_sensitivity.csv')
    signature = pd.read_csv(workdir/'signature_pancancer.csv', index_col='gene')
    legacy = pd.read_csv(workdir/'signature_zero_filled_legacy.csv', index_col='gene')
    means_long = pd.read_csv(workdir/'reference_means_long.csv')
    return {'metrics': metrics, 'sensitivity': sensitivity, 'signature': signature,
            'legacy': legacy, 'means_long': means_long, 'workdir': workdir}


@requires_r_benchmark
def test_corrected_signature_has_no_forced_zeros(benchmark_output):
    """Union markers keep their real reference mean CPM in non-self columns (the old bug)."""
    signature = benchmark_output['signature']
    legacy = benchmark_output['legacy']
    assert (signature.to_numpy() >= 0).all()
    assert np.isfinite(signature.to_numpy()).all()
    forced_corrected = benchmark_output['metrics']['forced_zero_entries_corrected']
    forced_legacy = benchmark_output['metrics']['forced_zero_entries_legacy']
    assert forced_corrected == 0
    assert forced_legacy > 0, 'simulation must exercise the old zero-fill failure mode'
    # Every zero-filled legacy column left many genes at zero that the corrected matrix keeps.
    assert (legacy.to_numpy() == 0).sum() > (signature.to_numpy() == 0).sum()


@requires_r_benchmark
def test_signature_equals_independent_reference_means(benchmark_output):
    """No zero fill: each signature entry equals the mean of that cell type's donor pseudobulk CPM."""
    signature = benchmark_output['signature']
    long = benchmark_output['means_long']
    expected = long.pivot(index='Var1', columns='Var2', values='Freq')
    expected.columns.name = None
    expected = expected.rename_axis('gene').loc[signature.index]
    assert set(expected.columns) == set(signature.columns)
    np.testing.assert_allclose(signature[expected.columns].to_numpy(),
                               expected.to_numpy(dtype=float), rtol=0, atol=1e-9)


@requires_r_benchmark
def test_nnls_recovers_known_mixtures_and_zero_fill_is_biased(benchmark_output):
    """NNLS on the corrected signature recovers simulated proportions; legacy zero fill is worse."""
    metrics = benchmark_output['metrics']
    corrected = float(metrics['mean_abs_error_corrected'])
    legacy = float(metrics['mean_abs_error_legacy_zero_filled'])
    assert corrected < 0.05, f'mean absolute proportion error {corrected:.3f} is too large'
    assert corrected < legacy, (
        f'corrected signature error {corrected:.4f} should be below zero-filled error {legacy:.4f}')
    assert 0 < corrected < 1 and 0 < legacy < 1


@requires_r_benchmark
def test_pseudobulk_units_respect_jensen_bound(benchmark_output):
    metrics = benchmark_output['metrics']
    assert float(metrics['max_jensen_violation']) <= 1e-9
    assert float(metrics['min_signature_value']) >= 0


@requires_r_benchmark
def test_donor_limma_sensitivity_recovers_marker_ranking(benchmark_output):
    """Donor-pseudobulk limma ranking is labelled sensitivity; in simulation it finds true markers."""
    overlap = float(benchmark_output['metrics']['min_limma_marker_overlap'])
    assert 0.5 <= overlap <= 1.0, f'donor limma marker overlap {overlap:.2f} out of range'


@requires_r_benchmark
def test_reference_sensitivity_is_descriptive_and_bounded(benchmark_output):
    sensitivity = benchmark_output['sensitivity']
    assert len(sensitivity) == int(benchmark_output['metrics']['n_celltypes'])
    assert (sensitivity['n'] == int(benchmark_output['metrics']['n_patients'])).all()
    assert (sensitivity['mean_abs_difference'] >= 0).all()
    assert sensitivity['direction_concordance'].between(0, 1).all()
    rho = sensitivity['spearman_rho'].dropna()
    assert rho.between(-1, 1).all()
    # Pan-cancer vs 5-donor source-restricted panels should broadly agree in this simulation.
    assert sensitivity['direction_concordance'].min() >= 0.5
    assert sensitivity['mean_abs_difference'].max() <= 0.2
