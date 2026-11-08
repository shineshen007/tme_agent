# Trusted inspection only. Never evaluate model-generated R expressions.
a <- commandArgs(TRUE)
request <- jsonlite::read_json(a[1], simplifyVector=FALSE)
rows <- list()
for (candidate in request) {
  pkg <- candidate$package
  row <- list(id=candidate$id, language='r', package=pkg, interpreter=normalizePath(a[3],mustWork=TRUE),
              runtime=as.character(getRversion()), status='missing', apis=list())
  tryCatch({
    if (!requireNamespace(pkg,quietly=TRUE)) stop(paste('Missing package',pkg))
    row$version <- as.character(utils::packageVersion(pkg))
    row$location <- find.package(pkg)
    row$interpreter_sha256 <- digest::digest(file=row$interpreter,algo='sha256')
    files <- c(list.files(file.path(row$location,'R'),full.names=TRUE),
               list.files(file.path(row$location,'libs'),full.names=TRUE,recursive=TRUE),
               file.path(row$location,'DESCRIPTION'))
    row$package_hashes <- as.list(setNames(vapply(files,function(p) digest::digest(file=p,algo='sha256'),character(1)),basename(files)))
    row$metadata <- as.list(utils::packageDescription(pkg)[c('Package','Version','URL','BugReports','Repository','Depends','biocViews')])
    row$bioconductor_package <- !is.na(row$metadata$biocViews)
    row$bioconductor <- if(requireNamespace('BiocVersion',quietly=TRUE)) paste(head(strsplit(as.character(utils::packageVersion('BiocVersion')),'.',fixed=TRUE)[[1]],2),collapse='.') else ''
    docs <- tools::Rd_db(pkg)
    for (api in candidate$apis) {
      value <- getExportedValue(pkg,api)
      if (!is.function(value)) stop(paste('API is not a function',api))
      rd <- docs[[paste0(api,'.Rd')]]
      # Aliases often share a help topic; consult alias tags without evaluating Rd.
      if (is.null(rd)) for (topic in docs) {
        aliases <- vapply(topic, function(x) if(identical(attr(x,'Rd_tag'),'\\alias')) paste(unlist(x),collapse='') else '', '')
        if (api %in% aliases) {rd <- topic;break}
      }
      doc <- if(is.null(rd)) '' else paste(capture.output(tools::Rd2txt(rd)),collapse='\n')
      row$apis[[api]] <- list(signature=paste(capture.output(dput(formals(value))),collapse=' '),documentation=doc)
    }
    row$status <- 'verified'
  },error=function(e) {row$error <<- conditionMessage(e)})
  rows[[length(rows)+1L]] <- row
}
jsonlite::write_json(rows,a[2],auto_unbox=TRUE,pretty=TRUE,null='null',na='null')
