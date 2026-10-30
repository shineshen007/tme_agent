"""Numerical contract for ``signature_matrix()`` in scripts/05_scrna_celltype.R.

``signature_matrix()`` is evaluated in isolation: the R script is parsed and only that one
function definition is evaluated into a synthetic environment holding ``pb_cpm``, ``g_ct``,
``g_src`` and ``keep_group``. No Seurat/limma/presto/nnls is needed, so the bug that used to
zero-fill every gene that was not a marker of the cell type is testable without the full atlas.

The old behaviour (m[g_in] only, everything else forced to 0) is impossible to satisfy here:
the reference mean of a non-self marker is compared entry by entry against an independent
Python recomputation.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/05_scrna_celltype.R'

RSCRIPT = shutil.which('Rscript')

pytestmark = pytest.mark.skipif(
    RSCRIPT is None or not SCRIPT.is_file(),
    reason='Rscript or scripts/05_scrna_celltype.R unavailable: signature_matrix() NOT_EVALUABLE',
)

# Synthetic reference: genes g1..g6, three cell types A/B/C and two sources.
#   A: s1(src1, kept), s2(src1, dropped by keep_group), s3(src2, kept)
#   B: s4(src1, kept), s5(src2, kept)
#   C: s6(src2, kept)  -> no src1 reference, so C must disappear under restrict_source='src1'
# Markers are deliberately non-overlapping by cell type, and gX is a marker that is absent from
# pb_cpm (it must be dropped from the union instead of creating a NaN row).
HARNESS = r'''
args <- commandArgs(trailingOnly=TRUE)
src <- args[1]; outdir <- args[2]
exprs <- parse(file=src)
env <- new.env(parent=globalenv())
defs <- 0L
for(e in exprs) {
  if(is.call(e) && length(e)==3L && as.character(e[[1]]) %in% c('<-','=') &&
     identical(as.character(e[[2]]), 'signature_matrix')) {
    eval(e, envir=env); defs <- defs + 1L
  }
}
if(defs != 1L) stop('expected exactly one top-level signature_matrix definition, found ', defs)
genes <- sprintf('g%d', 1:6)
pb_cpm <- matrix(c(
  100,  90,  80,  10,  12,  20,
   50,  55,  60,  15,  18,  25,
   10,  12,  15, 120, 110, 100,
   20,  18,  25,  60,  70,  65,
   30,  25,  20,  40,  35,  30,
    5,   6,   7,   8,   9,  10
), nrow=6, byrow=TRUE, dimnames=list(genes, c('s1|A','s2|A','s3|A','s4|B','s5|B','s6|C')))
g_ct <- c('A','A','A','B','B','C')
g_src <- c('src1','src1','src2','src1','src2','src2')
keep_group <- c(TRUE, FALSE, TRUE, TRUE, TRUE, TRUE)
markers <- data.frame(feature=c('g1','g2','g3','g4','g5','g6','gX'),
                      group=c('A','A','B','B','C','C','C'), stringsAsFactors=FALSE)
assign('pb_cpm', pb_cpm, envir=env)
assign('g_ct', g_ct, envir=env)
assign('g_src', g_src, envir=env)
assign('keep_group', keep_group, envir=env)
fn <- get('signature_matrix', envir=env)
sig_pan <- fn(markers, NULL)
sig_src1 <- fn(markers, 'src1')
sig_src2 <- fn(markers, 'src2')
write.csv(data.frame(gene=rownames(pb_cpm), pb_cpm, check.names=FALSE),
          file.path(outdir, 'pb_cpm.csv'), row.names=FALSE)
write.csv(data.frame(group=colnames(pb_cpm), celltype=g_ct, source=g_src, kept=keep_group,
                     stringsAsFactors=FALSE),
          file.path(outdir, 'groups.csv'), row.names=FALSE)
write.csv(markers, file.path(outdir, 'markers.csv'), row.names=FALSE)
write.csv(data.frame(gene=rownames(sig_pan), sig_pan, check.names=FALSE),
          file.path(outdir, 'sig_pan.csv'), row.names=FALSE)
write.csv(data.frame(gene=rownames(sig_src1), sig_src1, check.names=FALSE),
          file.path(outdir, 'sig_src1.csv'), row.names=FALSE)
write.csv(data.frame(gene=rownames(sig_src2), sig_src2, check.names=FALSE),
          file.path(outdir, 'sig_src2.csv'), row.names=FALSE)
'''


def _expected_signature(pb_cpm, groups, markers, restrict_source=None):
    """Independent Python recomputation: full union-marker means, no zero fill."""
    features = sorted(set(markers['feature']) & set(pb_cpm.index))
    columns = {}
    for celltype in sorted(markers['group'].unique()):
        selected = groups[(groups['celltype'] == celltype) & groups['kept'].astype(bool)]
        if restrict_source is not None:
            selected = selected[selected['source'] == restrict_source]
        if selected.empty:
            continue
        columns[celltype] = pb_cpm.loc[features, list(selected['group'])].mean(axis=1).to_numpy()
    if not columns:
        raise AssertionError('synthetic scenario produced no signature columns')
    return pd.DataFrame(columns, index=features)


@pytest.fixture(scope='module')
def signature_run(tmp_path_factory):
    workdir = tmp_path_factory.mktemp('scrna_signature_math')
    harness = workdir / 'harness.R'
    harness.write_text(HARNESS, encoding='utf-8')
    completed = subprocess.run([RSCRIPT, '--vanilla', str(harness), str(SCRIPT), str(workdir)],
                               capture_output=True, text=True, timeout=240)
    assert completed.returncode == 0, (
        'isolated signature_matrix() evaluation failed:\n'
        f'STDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}')
    pb_cpm = pd.read_csv(workdir / 'pb_cpm.csv', index_col='gene')
    groups = pd.read_csv(workdir / 'groups.csv')
    markers = pd.read_csv(workdir / 'markers.csv')
    outputs = {name: pd.read_csv(workdir / f'sig_{name}.csv', index_col='gene')
               for name in ('pan', 'src1', 'src2')}
    return {'pb_cpm': pb_cpm, 'groups': groups, 'markers': markers, 'outputs': outputs}


def test_all_union_markers_use_real_mean_cpm(signature_run):
    """Every celltype column must contain the real pseudobulk mean CPM for the whole union."""
    data = signature_run
    expected = _expected_signature(data['pb_cpm'], data['groups'], data['markers'])
    observed = data['outputs']['pan']
    assert list(observed.index) == list(expected.index)
    assert set(observed.columns) == set(expected.columns)
    np.testing.assert_allclose(observed[expected.columns].to_numpy(),
                               expected.to_numpy(), rtol=0, atol=1e-9)


def test_non_self_markers_are_not_zero_filled(signature_run):
    """A gene that is a marker of another cell type keeps its real reference mean, never 0."""
    data = signature_run
    markers = data['markers']
    observed = data['outputs']['pan']
    expected = _expected_signature(data['pb_cpm'], data['groups'], data['markers'])
    checked = 0
    for celltype in observed.columns:
        self_markers = set(markers.loc[markers['group'] == celltype, 'feature'])
        non_self = [gene for gene in expected.index if gene not in self_markers]
        assert non_self, f'celltype {celltype} has no non-self markers in the fixture'
        for gene in non_self:
            assert expected.loc[gene, celltype] > 0, 'fixture must express non-self markers'
            assert observed.loc[gene, celltype] > 0, (
                f'{gene} was zero-filled in celltype {celltype} although its reference mean is positive')
            assert observed.loc[gene, celltype] == pytest.approx(expected.loc[gene, celltype],
                                                                 abs=1e-9)
            checked += 1
    assert checked > 0
    # The audit-style statement: no entry is zero where the independent reference mean is positive.
    forced_zero = (observed.to_numpy() == 0) & (expected.to_numpy() > 0)
    assert not forced_zero.any()


def test_keep_group_and_union_marker_selection(signature_run):
    """Only kept pseudobulk groups enter the mean; marker genes absent from pb_cpm are dropped."""
    data = signature_run
    assert 'gX' not in data['outputs']['pan'].index
    expected_a = _expected_signature(data['pb_cpm'], data['groups'], data['markers'])
    observed = data['outputs']['pan']
    # g1 in A = mean of s1 and s3 (s2 is not kept): (100 + 80) / 2, not (100+90+80)/3.
    assert observed.loc['g1', 'A'] == pytest.approx((100 + 80) / 2, abs=1e-9)
    np.testing.assert_allclose(observed.loc['g1', 'A'], expected_a.loc['g1', 'A'], atol=1e-9)


def test_source_restriction_uses_only_that_source_and_drops_absent_celltypes(signature_run):
    """restrict_source must filter reference groups and drop celltypes with no groups there."""
    data = signature_run
    markers, groups, pb_cpm = data['markers'], data['groups'], data['pb_cpm']
    expected_src1 = _expected_signature(pb_cpm, groups, markers, 'src1')
    observed_src1 = data['outputs']['src1']
    assert set(observed_src1.columns) == set(expected_src1.columns)
    assert set(observed_src1.columns) == {'A', 'B'}, 'C has no src1 reference and must be dropped'
    np.testing.assert_allclose(observed_src1[expected_src1.columns].to_numpy(),
                               expected_src1.to_numpy(), rtol=0, atol=1e-9)
    # A in src1 = s1 only (s2 excluded by keep_group, s3 is src2); B in src1 = s4.
    assert observed_src1.loc['g1', 'A'] == pytest.approx(100.0, abs=1e-9)
    assert observed_src1.loc['g3', 'B'] == pytest.approx(120.0, abs=1e-9)
    assert observed_src1.loc['g1', 'B'] == pytest.approx(10.0, abs=1e-9)

    expected_src2 = _expected_signature(pb_cpm, groups, markers, 'src2')
    observed_src2 = data['outputs']['src2']
    assert set(observed_src2.columns) == {'A', 'B', 'C'}
    np.testing.assert_allclose(observed_src2[expected_src2.columns].to_numpy(),
                               expected_src2.to_numpy(), rtol=0, atol=1e-9)
    assert observed_src2.loc['g1', 'A'] == pytest.approx(80.0, abs=1e-9)
    assert observed_src2.loc['g5', 'C'] == pytest.approx(30.0, abs=1e-9)


def test_signature_values_are_nonnegative_and_scale_is_cpm(signature_run):
    for name, frame in signature_run['outputs'].items():
        values = frame.to_numpy(dtype=float)
        assert np.isfinite(values).all(), f'{name} contains nonfinite signature values'
        assert (values >= 0).all(), f'{name} contains negative signature values'
        # CPM-scale reference: at least one entry above 1 CPM (all fixture means are >= 5).
        assert values.max() > 1.0
