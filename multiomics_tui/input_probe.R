# Trusted structural probe. Never emit observed expression or clinical values.
args <- commandArgs(TRUE)
request <- jsonlite::read_json(args[1], simplifyVector=FALSE)
hash <- function(x) vapply(as.character(x), digest::digest, character(1), algo='sha256', serialize=FALSE)
key <- function(x) list(values=unname(hash(x)), missing=sum(is.na(x) | !nzchar(as.character(x))), duplicates=sum(duplicated(x)))
known <- c('T','P','N','Tumor','Non_Tumor','Non-Tumor','tumor','normal','Normal','adjacent','Adjacent','control','Control')
profiles <- list()
objects <- list()
for (item in request$inputs) {
  path <- file.path(Sys.getenv('PROJECT_ROOT'), item$path)
  ext <- tolower(tools::file_ext(path))
  x <- switch(ext, rds=readRDS(path), csv=utils::read.csv(path, check.names=FALSE, stringsAsFactors=FALSE),
    tsv=utils::read.delim(path, check.names=FALSE, stringsAsFactors=FALSE),
    gmt={lines <- strsplit(readLines(path, warn=FALSE), '\t', fixed=TRUE); if(any(lengths(lines)<3)) stop('Invalid GMT');
      names <- vapply(lines, `[`, '', 1); if(anyDuplicated(names)) stop('Duplicate GMT set names'); setNames(lapply(lines, function(v)v[-c(1,2)]), names)},
    stop('Unsupported input type'))
  objects[length(objects)+1L] <- list(x)
}
for (object_index in seq_along(objects)) {
  item <- request$inputs[[object_index]]
  x <- objects[[object_index]]
  p <- list(path=item$path, sha256=item$sha256, object_class=class(x), dimensions=as.integer(dim(x)), keys=list(), columns=list())
  if (is.matrix(x) || is.data.frame(x)) {
    d <- as.data.frame(x, stringsAsFactors=FALSE)
    numeric_matrix <- all(vapply(d,is.numeric,logical(1))) && !is.null(rownames(x))
    p$numeric_matrix <- numeric_matrix
    p$keys$rows <- key(rownames(x)); p$keys$columns <- key(colnames(x))
    # Wide numeric expression frames have sample IDs as columns: do not export them as field names.
    numeric_columns <- vapply(d,is.numeric,logical(1))
    # Privacy only: mixed CSV expression frames can have one feature-ID column.
    # Withhold possible sample headers; this does not infer a data role or orientation.
    p$matrix_like <- numeric_matrix || (sum(numeric_columns)>=1L && sum(!numeric_columns)<=1L)
    if (!numeric_matrix) for (other_index in setdiff(seq_along(objects), object_index)) {
      other <- objects[[other_index]]
      if (!is.data.frame(other)) next
      for (field in other) if (is.atomic(field) && any(colnames(x) %in% as.character(field))) p$matrix_like <- TRUE
    }
    p$column_issues <- list()
    # as.data.frame.matrix may repair names; inspect the original axis instead.
    column_names <- colnames(x)
    if (is.null(column_names)) column_names <- rep('', ncol(d))
    for (j in seq_along(d)) {
      nm <- column_names[j]
      v <- d[[j]]
      if (!is.atomic(v)) stop(sprintf('Nested table columns are unsupported: file=%s column_position=%d type=%s',
                                     item$path, j, paste(class(v), collapse=',')))
      unnamed <- is.na(nm) || !nzchar(trimws(nm))
      duplicate <- !unnamed && sum(column_names==nm, na.rm=TRUE)>1L
      if (unnamed || duplicate) {
        p$column_issues[[length(p$column_issues)+1L]] <- list(position=j, type=class(v), missing=sum(is.na(v)),
          reason=if (unnamed) {if (all(is.na(v))) 'unnamed_all_missing' else 'unnamed_nonempty'} else 'duplicate_name')
        next
      }
      if (p$matrix_like) next
      counts <- table(as.character(v), useNA='no')
      p$columns[[nm]] <- list(type=class(v), missing=sum(is.na(v)), unique=length(unique(v)),
        known_levels=as.list(counts[intersect(names(counts),known)]), level_hashes=unname(hash(names(counts))),
        level_counts=unname(as.integer(counts)))
      p$keys[[paste0('column:',nm)]] <- key(v)
    }
    if(numeric_matrix) {
      m <- as.matrix(x)
      p$finite <- all(is.finite(m)); p$negative_count <- sum(m<0,na.rm=TRUE)
      p$zero_rows <- sum(rowSums(m!=0,na.rm=TRUE)==0)
      constant_row <- apply(m,1,function(v)all(is.finite(v)) && length(unique(v))==1)
      p$constant_rows <- sum(constant_row)
      p$constant_row_sha256 <- unname(hash(rownames(m)[constant_row]))
      if(!is.null(colnames(m))) {
        constant_col <- apply(m,2,function(v)all(is.finite(v)) && length(unique(v))==1)
        p$constant_columns <- sum(constant_col)
        p$constant_column_sha256 <- unname(hash(colnames(m)[constant_col]))
      }
    }
  } else if(is.list(x) && !is.null(names(x)) && all(vapply(x,is.character,logical(1)))) {
    if(anyDuplicated(names(x)) || any(!nzchar(names(x)))) stop('Invalid gene set names')
    p$gene_sets <- list(count=length(x), sizes=unname(lengths(x)), unique_members=length(unique(unlist(x))))
    p$set_name_sha256 <- unname(hash(names(x)))
    p$keys$members <- key(unique(unlist(x)))
    p$set_members <- lapply(x, function(v) unname(hash(unique(v[!is.na(v) & nzchar(v)])))); names(p$set_members)<-NULL
  } else stop('Unsupported R object structure; supply an explicit data dictionary')
  profiles[[length(profiles)+1L]] <- p
}
# Views are exact structural joins, never a selection of all numeric columns.
# Keep raw labels and ordered hashes local; public_inspection allowlists aggregates.
for (i in seq_along(profiles)) {
  x <- objects[[i]]
  if (!is.data.frame(x) || isTRUE(profiles[[i]]$numeric_matrix)) next
  profiles[[i]]$matrix_views <- list()
  for (j in seq_along(profiles)) {
    if (i == j || !is.data.frame(objects[[j]])) next
    meta <- objects[[j]]
    for (sample_key in names(profiles[[j]]$columns)) {
      ids <- as.character(meta[[sample_key]])
      # Partial matches mark headers private even when no valid view can be built.
      if (any(names(x) %in% ids)) profiles[[i]]$matrix_like <- TRUE
      if (length(ids)<2L || anyNA(ids) || any(!nzchar(ids)) || anyDuplicated(ids)) next
      pos <- match(ids, names(x))
      if (anyNA(pos) || anyDuplicated(names(x)) || any(!nzchar(trimws(names(x))))) next
      if (!all(vapply(x[pos], is.numeric, logical(1)))) next
      m <- as.matrix(x[pos])
      constant <- rowSums(!is.finite(m))==0 & apply(m,1,min)==apply(m,1,max)
      for (f in setdiff(seq_along(x), pos)) {
        feature <- x[[f]]
        if (anyNA(feature) || any(!nzchar(trimws(as.character(feature)))) || anyDuplicated(as.character(feature))) next
        view_id <- paste0('view_', digest::digest(paste(profiles[[i]]$path, profiles[[i]]$sha256,
          profiles[[j]]$path, profiles[[j]]$sha256, sample_key, f, sep='|'), algo='sha256', serialize=FALSE))
        v <- list(view_id=view_id, path=profiles[[i]]$path, sha256=profiles[[i]]$sha256,
          metadata_path=profiles[[j]]$path, metadata_sha256=profiles[[j]]$sha256,
          sample_key=sample_key, sample_axis='columns', feature_ref=paste0('column_',f),
          feature_position=f, feature_key=names(x)[f], sample_positions=as.integer(pos),
          annotation_keys=as.list(names(x)[setdiff(seq_along(x),pos)]),
          dimensions=as.integer(dim(m)), keys=list(rows=key(feature), columns=key(ids)),
          finite=all(is.finite(m)), negative_count=sum(m<0,na.rm=TRUE),
          zero_rows=sum(rowSums(m!=0,na.rm=TRUE)==0), constant_rows=sum(constant),
          constant_row_sha256=as.list(unname(hash(feature[constant]))))
        profiles[[i]]$matrix_views[[length(profiles[[i]]$matrix_views)+1L]] <- v
      }
    }
  }
}
# Trusted preparation runs only after approval, under the same input sandbox.
if (length(request$materialize)) {
  for (entry in request$materialize) {
    found <- FALSE
    for (i in seq_along(profiles)) for (v in profiles[[i]]$matrix_views) {
      if (!identical(v$view_id, entry$view_id)) next
      if (!isTRUE(v$finite)) stop('Matrix view contains nonfinite values')
      x <- objects[[i]]
      m <- as.matrix(x[v$sample_positions]); rownames(m) <- as.character(x[[v$feature_position]])
      folder <- file.path(Sys.getenv('TASK_DIR'), 'objects', 'input_views', as.character(entry$binding_index))
      dir.create(folder, recursive=TRUE, showWarnings=FALSE)
      saveRDS(m, file.path(folder,'matrix.rds'))
      # Seventeen significant digits preserve IEEE doubles for Python consumers.
      utils::write.csv(format(m,digits=17,trim=TRUE,scientific=TRUE), file.path(folder,'matrix.csv'))
      utils::write.csv(x[setdiff(seq_along(x),v$sample_positions)], file.path(folder,'annotations.csv'), row.names=FALSE)
      found <- TRUE
    }
    if (!found) stop('Approved matrix view is unavailable; re-inspect inputs')
  }
}
versions <- list(R=R.version.string)
for (pkg in c('GSVA','ggplot2')) if(requireNamespace(pkg,quietly=TRUE)) versions[[pkg]] <- as.character(utils::packageVersion(pkg))
apis <- list()
if(requireNamespace('GSVA',quietly=TRUE)) apis$plageParam <- names(formals(GSVA::plageParam))
jsonlite::write_json(list(version='input_probe_v4',profiles=profiles,software=versions,apis=apis), args[2], auto_unbox=TRUE, pretty=TRUE, null='null', na='null')
