# Trusted structural probe. Never emit observed expression or clinical values.
args <- commandArgs(TRUE)
request <- jsonlite::read_json(args[1], simplifyVector=FALSE)
hash <- function(x) vapply(as.character(x), digest::digest, character(1), algo='sha256', serialize=FALSE)
key <- function(x) list(values=unname(hash(x)), missing=sum(is.na(x) | !nzchar(as.character(x))), duplicates=sum(duplicated(x)))
known <- c('T','P','N','Tumor','Non_Tumor','Non-Tumor','tumor','normal','Normal','adjacent','Adjacent','control','Control')
profiles <- list()
for (item in request$inputs) {
  path <- file.path(Sys.getenv('PROJECT_ROOT'), item$path)
  ext <- tolower(tools::file_ext(path))
  x <- switch(ext, rds=readRDS(path), csv=utils::read.csv(path, check.names=FALSE, stringsAsFactors=FALSE),
    tsv=utils::read.delim(path, check.names=FALSE, stringsAsFactors=FALSE),
    gmt={lines <- strsplit(readLines(path, warn=FALSE), '\t', fixed=TRUE); if(any(lengths(lines)<3)) stop('Invalid GMT');
      names <- vapply(lines, `[`, '', 1); if(anyDuplicated(names)) stop('Duplicate GMT set names'); setNames(lapply(lines, function(v)v[-c(1,2)]), names)},
    stop('Unsupported input type'))
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
    if (!p$matrix_like) for (nm in names(d)) {
      v <- d[[nm]]
      if (!is.atomic(v)) stop('Nested table columns are unsupported')
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
versions <- list(R=R.version.string)
for (pkg in c('GSVA','ggplot2')) if(requireNamespace(pkg,quietly=TRUE)) versions[[pkg]] <- as.character(utils::packageVersion(pkg))
apis <- list()
if(requireNamespace('GSVA',quietly=TRUE)) apis$plageParam <- names(formals(GSVA::plageParam))
jsonlite::write_json(list(profiles=profiles,software=versions,apis=apis), args[2], auto_unbox=TRUE, pretty=TRUE, null='null', na='null')
