# Independent GSVA multi-method reference/verification. Inputs and the produced
# task directory are explicit; nothing is fetched, installed or rescaled.
#
# Usage:
#   Rscript --vanilla scripts/32_gsva_multimethod_reference.R \
#     <expression.rds|csv> <gene_sets.rds> <protocol.json> <scores_long.csv> <output.json>
#
# The script recomputes every frozen method with the GSVA parameter-object API and
# asserts that the supplied sample-level scores reproduce it within numerical
# precision. It never writes scores back: the task's own tables stay authoritative.
a <- commandArgs(TRUE)
stopifnot(length(a) == 5)
expr_path <- a[1]; sets_path <- a[2]; protocol_path <- a[3]; long_path <- a[4]; out_path <- a[5]
for (p in c('GSVA', 'jsonlite')) {
  if (!requireNamespace(p, quietly = TRUE)) stop('Missing package: ', p)
}
protocol <- jsonlite::fromJSON(protocol_path, simplifyVector = FALSE)
methods <- unlist(protocol$methods, use.names = FALSE)
if (!length(methods) || !all(methods %in% c('ssgsea', 'plage', 'zscore', 'gsva'))) {
  stop('Protocol must name one or more of ssgsea, plage, zscore, gsva')
}
min_size <- as.integer(protocol$min_size)
max_size <- as.integer(protocol$max_size)
stopifnot(is.finite(min_size), is.finite(max_size), min_size >= 1, max_size > min_size)

read_matrix <- function(path) {
  ext <- tolower(tools::file_ext(path))
  if (ext == 'rds') {
    m <- readRDS(path)
  } else if (ext %in% c('csv', 'tsv')) {
    m <- as.matrix(utils::read.csv(path, row.names = 1, check.names = FALSE))
  } else {
    stop('Unsupported expression input: ', ext)
  }
  if (!is.matrix(m) || !is.numeric(m) || any(dim(m) == 0L)) stop('Expression input must be a nonempty numeric matrix')
  if (is.null(rownames(m)) || is.null(colnames(m))) stop('Expression matrix needs row and column names')
  if (anyDuplicated(rownames(m)) || anyDuplicated(colnames(m))) stop('Duplicate matrix axis names')
  m
}

expr <- read_matrix(expr_path)
sets <- readRDS(sets_path)
if (!is.list(sets) || is.null(names(sets)) || any(!nzchar(names(sets)))) stop('Gene sets must be a named list')
if (anyDuplicated(names(sets))) stop('Duplicate gene-set names')

# plage, zscore and gsva fail outright on features that never vary; the protocol
# therefore drops them before scoring and records the count.
variable <- apply(expr, 1L, function(v) all(is.finite(v)) && length(unique(v)) > 1L)
expr_scored <- expr[variable, , drop = FALSE]
dropped_features <- sum(!variable)
if (!nrow(expr_scored)) stop('No variable feature remains after zero-variance filtering')

params <- list(
  ssgsea = function() GSVA::ssgseaParam(expr_scored, sets, minSize = min_size, maxSize = max_size, normalize = TRUE),
  plage = function() GSVA::plageParam(expr_scored, sets, minSize = min_size, maxSize = max_size),
  zscore = function() GSVA::zscoreParam(expr_scored, sets, minSize = min_size, maxSize = max_size),
  gsva = function() GSVA::gsvaParam(expr_scored, sets, minSize = min_size, maxSize = max_size, kcdf = 'Gaussian')
)

reference <- list()
skipped <- character()
for (method in methods) {
  scores <- tryCatch(
    GSVA::gsva(params[[method]](), verbose = FALSE, BPPARAM = BiocParallel::SerialParam()),
    error = function(e) e)
  if (inherits(scores, 'error')) {
    # A method that cannot run is recorded, never silently replaced.
    skipped <- c(skipped, method)
    reference[[method]] <- NULL
    next
  }
  if (!is.matrix(scores) || anyNA(scores)) stop('GSVA returned an unusable score matrix for ', method)
  reference[[method]] <- scores
}
if (!length(reference)) stop('No GSVA method produced scores on this matrix')

long <- utils::read.csv(long_path, check.names = FALSE, stringsAsFactors = FALSE)
required <- c('gene_set', 'sample_id', 'score', 'method')
if (!all(required %in% names(long))) stop('Score table lacks columns: ', paste(setdiff(required, names(long)), collapse = ', '))
if (anyDuplicated(long[c('method', 'gene_set', 'sample_id')])) stop('Score table repeats a method x gene_set x sample key')
if (!all(long$method %in% methods)) stop('Score table contains a method outside the frozen protocol')
if (!all(methods %in% long$method)) stop('Score table omits a frozen method: ', paste(setdiff(methods, long$method), collapse = ', '))

result <- list(status = 'passed', methods = methods, scored_methods = names(reference),
               skipped_methods = as.list(skipped), matrix_dim = as.integer(dim(expr)),
               scored_matrix_dim = as.integer(dim(expr_scored)), dropped_features = as.integer(dropped_features),
               long_rows = nrow(long), GSVA = as.character(utils::packageVersion('GSVA')),
               R = R.version.string, seed = protocol$seed, min_size = min_size, max_size = max_size,
               max_abs_error = list())
for (method in names(reference)) {
  scores <- reference[[method]]
  block <- long[long$method == method, , drop = FALSE]
  if (!setequal(block$gene_set, rownames(scores))) stop(method, ': gene-set set differs from the recomputation')
  if (!setequal(block$sample_id, colnames(scores))) stop(method, ': sample set differs from the recomputation')
  index <- cbind(match(block$gene_set, rownames(scores)), match(block$sample_id, colnames(scores)))
  # Match within numerical precision; no rescaling or direction change is permitted.
  error <- max(abs(scores[index] - block$score))
  if (!is.finite(error) || error > 1e-8) stop(method, ': score mismatch, max abs error ', signif(error, 6))
  result$max_abs_error[[method]] <- error
}
# Independent re-derivation of one frozen method also proves the whole matrix is
# reproducible from the same inputs, not just the compared cells.
jsonlite::write_json(result, out_path, auto_unbox = TRUE, pretty = TRUE)
