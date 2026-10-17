# Parse only: never source/evaluate generated expressions.
a <- commandArgs(TRUE)
expr <- tryCatch(parse(file=a[1],keep.source=TRUE),error=function(e){cat(conditionMessage(e),file=stderr());quit(status=2)})
operators <- character(); defined <- character(); libraries <- character(); pdf_font_risk <- FALSE; named_fonts <- FALSE
walk <- function(x) {
 if(missing(x)) return(invisible(NULL))
 if(is.expression(x) || is.pairlist(x)) {for(v in x) walk(v);return(invisible(NULL))}
 if(!is.call(x)) return(invisible(NULL))
 head <- if(is.symbol(x[[1]])) as.character(x[[1]]) else ''
 if(grepl('^%.*%$',head)) operators <<- c(operators,head)
 if(head %in% c('<-','=','<<-') && length(x)>=3 && (is.symbol(x[[2]])||is.character(x[[2]]))) defined <<- c(defined,as.character(x[[2]]))
 if(head %in% c('library','require') && length(x)>=2 && (is.symbol(x[[2]])||is.character(x[[2]]))) libraries <<- c(libraries,as.character(x[[2]]))
 if(head %in% c('::',':::') && length(x)>=3 && as.character(x[[2]])=='systemfonts') named_fonts <<- TRUE
 # Qualified and unqualified ggsave calls are both recognized.
 name <- head
 if(is.call(x[[1]]) && as.character(x[[1]][[1]]) %in% c('::',':::')) name<-as.character(x[[1]][[3]])
 if(name=='ggsave') {
  z<-as.list(x)[-1];if('device'%in%names(z) && is.character(z$device) && identical(z$device,'pdf')) pdf_font_risk<<-TRUE
 }
 for(v in as.list(x)) walk(v)
 invisible(NULL)
}
walk(expr)
packages <- unique(libraries)
missing <- packages[!vapply(packages,requireNamespace,logical(1),quietly=TRUE)]
if(length(missing)) {cat('MISSING_PACKAGE: ',paste(missing,collapse=', '),file=stderr());quit(status=4)}
exports <- unlist(lapply(packages,getNamespaceExports),use.names=FALSE)
unknown <- setdiff(unique(operators),c(defined,exports,ls(baseenv(),all.names=TRUE)))
if(length(unknown)) {cat('UNDEFINED_OPERATOR: ',paste(unknown,collapse=', '),file=stderr());quit(status=3)}
if(named_fonts && pdf_font_risk) {cat('PDF_FONT_DEVICE: use grDevices::cairo_pdf for installed named fonts',file=stderr());quit(status=3)}
