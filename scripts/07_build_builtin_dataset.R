# Build the built-in TCGA-LIHC dataset from the GDC STAR - Counts bundle.
#
# Structure normalisation only: the GDC per-sample TSVs become matrices whose rows
# are gene symbols and whose columns are TCGA sample barcodes. Counts stay integer,
# TPM stays the upstream `tpm_unstranded` value, and nothing is rescaled, logged or
# rounded. Only protein_coding rows are kept (documented scope), and gene rows that
# share one symbol are SUMMED so paralogous/PAR copies are not silently dropped.
#
# Inputs are the read-only GDC cache written by scripts/08_fetch_gdc_tcga_lihc.py;
# the cache is git-ignored and is never modified here.
#
# Usage: Rscript --vanilla scripts/07_build_builtin_dataset.R <project-root> [built-at]
args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 1L) stop('Usage: Rscript 07_build_builtin_dataset.R <project-root> [built-at]')
root <- normalizePath(args[1], mustWork = TRUE)
built_at <- if (length(args) >= 2L) args[2] else format(Sys.time(), '%Y-%m-%dT%H:%M:%S%z')
for (pkg in c('jsonlite', 'digest')) {
  if (!requireNamespace(pkg, quietly = TRUE)) stop(pkg, ' is required')
}

cache <- file.path(root, 'data', 'raw', '_gdc_cache')
report_path <- file.path(cache, 'fetch_report.json')
if (!file.exists(report_path)) stop('Missing GDC fetch report; run scripts/08_fetch_gdc_tcga_lihc.py')
report <- jsonlite::fromJSON(report_path, simplifyDataFrame = TRUE)
meta <- report$files
if (nrow(meta) != 424L) stop('Expected 424 GDC files, found ', nrow(meta))
if (anyDuplicated(meta$sample)) stop('Duplicate sample barcode in GDC metadata')

extract_dir <- file.path(cache, 'extract')
# Each downloaded batch keeps its own MANIFEST; merge them into one id -> file map.
manifest_paths <- Sys.glob(file.path(extract_dir, 'MANIFEST*.txt'))
if (length(manifest_paths) == 0L) stop('Missing bundle MANIFEST.txt; run scripts/08_fetch_gdc_tcga_lihc.py')
manifest <- do.call(rbind, lapply(manifest_paths, function(path) {
  utils::read.delim(path, stringsAsFactors = FALSE, colClasses = 'character')
}))
manifest <- manifest[manifest$state == 'validated', ]
manifest <- manifest[!duplicated(manifest$id), ]
path_of <- function(file_id) file.path(extract_dir, manifest$filename[match(file_id, manifest$id)])
if (any(is.na(match(meta$file_id, manifest$id)))) stop('A GDC file is absent from the bundle MANIFEST')

out_dir <- file.path(root, 'datasets', 'TCGA-LIHC')
expr_dir <- file.path(out_dir, 'expression')
meta_dir <- file.path(out_dir, 'metadata')
for (path in c(expr_dir, meta_dir)) dir.create(path, recursive = TRUE, showWarnings = FALSE)

# One GDC TSV -> the protein_coding rows, with duplicate symbols summed.
# GENCODE order is preserved (not alphabetical) so the matrices stay comparable to
# the upstream file order and a rebuild is byte-identical.
read_sample <- function(file_id, gene_order = NULL) {
  table <- utils::read.delim(path_of(file_id), comment.char = '#', stringsAsFactors = FALSE,
                             colClasses = c(gene_id = 'character', gene_name = 'character',
                                            gene_type = 'character', unstranded = 'integer',
                                            tpm_unstranded = 'numeric'))
  table <- table[grepl('^ENSG', table$gene_id) & table$gene_type == 'protein_coding', ]
  if (any(!nzchar(table$gene_name))) stop('Blank gene symbol in ', file_id)
  if (any(!is.finite(table$unstranded)) || any(!is.finite(table$tpm_unstranded))) {
    stop('Nonfinite value in ', file_id)
  }
  if (any(table$unstranded < 0) || any(table$tpm_unstranded < 0)) stop('Negative value in ', file_id)
  symbols <- if (is.null(gene_order)) unique(table$gene_name) else gene_order
  counts <- as.numeric(tapply(as.numeric(table$unstranded), factor(table$gene_name, levels = symbols), sum))
  tpm <- as.numeric(tapply(table$tpm_unstranded, factor(table$gene_name, levels = symbols), sum))
  list(symbols = symbols, counts = counts, tpm = tpm, table = table)
}

first <- read_sample(meta$file_id[1])
gene_order <- first$symbols
n_genes <- length(gene_order)
n_samples <- nrow(meta)
if (n_genes != 19938L) stop('Unexpected protein_coding symbol count: ', n_genes)

counts_full <- matrix(0L, nrow = n_genes, ncol = n_samples,
                      dimnames = list(gene_order, meta$sample))
tpm_full <- matrix(0, nrow = n_genes, ncol = n_samples,
                   dimnames = list(gene_order, meta$sample))
for (i in seq_len(n_samples)) {
  item <- if (i == 1L) first else read_sample(meta$file_id[i], gene_order)
  if (!identical(item$symbols, gene_order)) stop('Gene order differs in ', meta$file_id[i])
  if (any(item$counts > .Machine$integer.max)) stop('Count exceeds R integer range')
  counts_full[, i] <- as.integer(item$counts)
  tpm_full[, i] <- item$tpm
}
if (any(counts_full < 0) || any(tpm_full < 0)) stop('Negative value after aggregation')

# Duplicate symbols are reported, never hidden: the sum rule is the documented policy.
dup_symbols <- names(which(table(first$table$gene_name) > 1))
if (length(dup_symbols) == 0) stop('Expected duplicated gene symbols; aggregation rule unverified')

clinical <- meta
clinical$sample_id <- clinical$sample
clinical$patient <- clinical$case
clinical$group <- ifelse(clinical$sample_type == 'Primary Tumor', 'Tumor',
                  ifelse(clinical$sample_type == 'Solid Tissue Normal', 'Non_Tumor',
                         'Recurrent_Tumor'))
tumor_patients <- clinical$case[clinical$sample_type == 'Primary Tumor']
normal_patients <- clinical$case[clinical$sample_type == 'Solid Tissue Normal']
paired_patients <- intersect(tumor_patients, normal_patients)
clinical$pairing <- ifelse(clinical$case %in% paired_patients, 'paired', 'unpaired')
clinical$pair_id <- ifelse(clinical$case %in% paired_patients, clinical$case, NA)

paired <- clinical[clinical$case %in% paired_patients &
                     clinical$sample_type %in% c('Primary Tumor', 'Solid Tissue Normal'), ]
paired <- paired[order(paired$case, paired$group == 'Tumor', decreasing = TRUE), ]
if (nrow(paired) != 2L * length(paired_patients)) stop('Paired cohort is not two samples per patient')
if (!all(tapply(paired$group, paired$pair_id,
                function(v) setequal(v, c('Tumor', 'Non_Tumor'))))) {
  stop('A paired patient lacks exactly one tumour and one adjacent sample')
}
counts_paired <- counts_full[, paired$sample, drop = FALSE]
if (!identical(colnames(counts_paired), paired$sample)) stop('Paired matrix order differs from metadata')

full_columns <- c('sample_id', 'patient', 'group', 'pairing', 'pair_id', 'sample_type',
                  'tissue_type', 'file_id')
paired_columns <- c('sample_id', 'patient', 'pair_id', 'group', 'sample_type',
                    'tissue_type', 'file_id')
utils::write.csv(clinical[, full_columns], file.path(meta_dir, 'samples_full.csv'),
                 row.names = FALSE, na = 'NA')
utils::write.csv(paired[, paired_columns],
                 file.path(meta_dir, 'samples_paired.csv'), row.names = FALSE, na = 'NA')

annotation <- unique(first$table[, c('gene_id', 'gene_name', 'gene_type')])
annotation <- annotation[!duplicated(annotation$gene_name), c('gene_id', 'gene_name', 'gene_type')]
annotation <- annotation[match(gene_order, annotation$gene_name), ]
if (any(is.na(annotation$gene_id))) stop('A gene symbol has no representative gene_id')
utils::write.csv(annotation, file.path(expr_dir, 'gene_annotation.csv'), row.names = FALSE)

saveRDS(counts_full, file.path(expr_dir, 'counts_full.rds'), compress = 'xz')
saveRDS(counts_paired, file.path(expr_dir, 'counts_paired.rds'), compress = 'xz')
saveRDS(tpm_full, file.path(expr_dir, 'tpm_full.rds'), compress = 'xz')

sha256 <- function(path) digest::digest(file = path, algo = 'sha256')
gene_axis <- 'gene symbol (GDC/Ensembl gene_name; rows sharing a symbol are summed)'
sample_axis <- 'sample_id (16-character TCGA sample barcode)'
record <- function(path, relative, format, units, axes, rows, columns, dtype) {
  list(path = relative, sha256 = sha256(path), bytes = as.numeric(file.size(path)),
       format = format, rows = rows, columns = columns, dtype = dtype, units = units,
       preprocessing = 'none', axes = axes)
}
tpm_sums <- round(range(colSums(tpm_full)))
files <- list(
  record(file.path(expr_dir, 'counts_full.rds'), 'expression/counts_full.rds', 'rds_matrix',
         'GDC STAR unstranded read counts, protein_coding symbols only',
         list(rows = gene_axis, columns = sample_axis), nrow(counts_full), ncol(counts_full), 'integer'),
  record(file.path(expr_dir, 'counts_paired.rds'), 'expression/counts_paired.rds', 'rds_matrix',
         'GDC STAR unstranded read counts, protein_coding symbols only',
         list(rows = gene_axis, columns = sample_axis), nrow(counts_paired), ncol(counts_paired), 'integer'),
  record(file.path(expr_dir, 'tpm_full.rds'), 'expression/tpm_full.rds', 'rds_matrix',
         paste0('GDC tpm_unstranded as supplied; protein_coding subset column sums ',
                tpm_sums[1], '-', tpm_sums[2], ' of the full-transcriptome 1e6'),
         list(rows = gene_axis, columns = sample_axis), nrow(tpm_full), ncol(tpm_full), 'numeric'),
  record(file.path(expr_dir, 'gene_annotation.csv'), 'expression/gene_annotation.csv', 'csv_table',
         'one representative Ensembl gene_id per matrix row symbol',
         list(rows = gene_axis, columns = 'Ensembl gene_id and gene_type'), nrow(annotation),
         ncol(annotation), 'character'),
  record(file.path(meta_dir, 'samples_full.csv'), 'metadata/samples_full.csv', 'csv_table',
         'one row per matrix column, same order as counts_full/tpm_full',
         list(rows = sample_axis, columns = 'GDC sample field'), nrow(clinical),
         ncol(clinical[, full_columns]), 'character'),
  record(file.path(meta_dir, 'samples_paired.csv'), 'metadata/samples_paired.csv', 'csv_table',
         'one row per matrix column of counts_paired, same order',
         list(rows = sample_axis, columns = 'GDC sample field'), nrow(paired),
         ncol(paired[, paired_columns]), 'character')
)
bindings <- list(
  counts_paired = list(
    path = 'data/raw/TCGA-LIHC/counts_paired.rds', sample_axis = 'columns',
    metadata_path = 'data/metadata/TCGA-LIHC/samples_paired.csv', sample_key = 'sample_id',
    group_key = 'group', group_map = list(Tumor = 'Tumor', Non_Tumor = 'Non_Tumor'),
    pair_key = 'pair_id', preprocessing = 'none'),
  counts_full = list(
    path = 'data/raw/TCGA-LIHC/counts_full.rds', sample_axis = 'columns',
    metadata_path = 'data/metadata/TCGA-LIHC/samples_full.csv', sample_key = 'sample_id',
    group_key = 'group',
    group_map = list(Tumor = 'Tumor', Non_Tumor = 'Non_Tumor', Recurrent_Tumor = 'Recurrent_Tumor'),
    pair_key = NULL, preprocessing = 'none'),
  tpm_full = list(
    path = 'data/raw/TCGA-LIHC/tpm_full.rds', sample_axis = 'columns',
    metadata_path = 'data/metadata/TCGA-LIHC/samples_full.csv', sample_key = 'sample_id',
    group_key = 'group',
    group_map = list(Tumor = 'Tumor', Non_Tumor = 'Non_Tumor', Recurrent_Tumor = 'Recurrent_Tumor'),
    pair_key = NULL, preprocessing = 'none')
)
manifest <- list(
  schema_version = 1,
  dataset_id = 'TCGA-LIHC',
  title = 'TCGA-LIHC liver hepatocellular carcinoma bulk RNA-seq (GDC STAR - Counts)',
  provenance_status = 'recorded',
  source = list(
    repository = 'GDC (Genomic Data Commons), https://portal.gdc.cancer.gov/',
    project_id = report$project_id,
    access_tier = report$access,
    data_release = report$data_release,
    gdc_api_version = report$gdc_api_version,
    gdc_commit = report$gdc_commit,
    retrieved_at = built_at,
    upstream_pipeline = paste(report$workflow, collapse = '; '),
    gene_model = 'GENCODE v36',
    expression_columns = list(counts = 'unstranded', tpm = 'tpm_unstranded'),
    gene_scope = 'protein_coding only (19938 unique symbols from 19962 rows)',
    duplicate_symbol_rule = paste0('rows sharing one gene_name are summed (',
                                   length(dup_symbols), ' symbols, PAR copies included)'),
    clinical_rule = 'sample fields come from GDC sample metadata; no per-case diagnosis table is shipped'),
  license = list(
    code_license = 'GPL-3.0-or-later does not cover this dataset',
    data_terms = 'TCGA open-access tier under GDC data use terms',
    dbgap = NULL),
  upstream_files = lapply(seq_len(nrow(meta)), function(i) {
    list(file_id = meta$file_id[i], sample_id = meta$sample[i], role = meta$sample_type[i],
         md5 = meta$md5[i], bytes = as.numeric(meta$bytes[i]), local_only = TRUE)
  }),
  files = files,
  samples = list(total = nrow(clinical), tumor = sum(clinical$group == 'Tumor'),
                 normal = sum(clinical$group == 'Non_Tumor'),
                 recurrent = sum(clinical$group == 'Recurrent_Tumor'),
                 paired_patients = length(paired_patients)),
  groups = list(column = 'group', levels = c('Tumor', 'Non_Tumor', 'Recurrent_Tumor'),
                recommended_map = list(Tumor = 'Tumor', Non_Tumor = 'Non_Tumor',
                                       Recurrent_Tumor = 'Recurrent_Tumor')),
  bindings = bindings,
  quality_flags = c(
    paste0('Only protein_coding genes are shipped (', n_genes,
           ' symbols), so TPM column sums are ', tpm_sums[1], '-', tpm_sums[2],
           ' rather than the full-transcriptome 1e6. Never rescale them to 1e6.'),
    paste0(length(dup_symbols), ' gene symbols occur more than once in GENCODE v36 protein_coding rows (',
           paste(sort(dup_symbols), collapse = ', '),
           '); their rows are summed, which differs from taking only the first row.'),
    '3 recurrent-tumour samples have no paired adjacent tissue and stay in the full cohort only.',
    'counts are unstranded read counts; no library-size normalisation is applied here.',
    'Sample identifiers are 16-character TCGA sample barcodes, not 28-character aliquot barcodes.',
    'The clinical diagnosis table is not shipped; only sample-level fields are included.'),
  build = list(script = 'scripts/07_build_builtin_dataset.R',
               fetch_script = 'scripts/08_fetch_gdc_tcga_lihc.py',
               r_version = R.version.string, compression = 'xz',
               deterministic_bytes = TRUE, built_at = built_at)
)
text <- jsonlite::toJSON(manifest, pretty = TRUE, auto_unbox = TRUE, null = 'null',
                         na = 'null', digits = NA)
writeLines(text, file.path(out_dir, 'dataset.json'), useBytes = TRUE)

cat('Built', out_dir, '\n')
cat('genes', n_genes, 'samples', n_samples, 'paired', ncol(counts_paired), '\n')
for (item in files) cat(' ', item$path, item$bytes, 'bytes\n')
