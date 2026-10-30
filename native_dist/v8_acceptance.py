"""Offline v8 pipeline acceptance on new synthetic files, never study data.

Extends the v7 synthetic CSV/RDS task with a gene-set scoring step: it verifies
that GSVA (a Bioconductor dependency) is discoverable and usable inside the
sandboxed task environment, which is the behavior v8 adds.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import uuid


def _gene_set_scoring(root):
    """Run GSVA PLAGE on synthetic data under the real task guard; return summary.

    All synthetic inputs live inside the task directory (which the guard grants
    read+write), so the step needs no external read allowlist.
    """
    from multiomics_tui.actions import rscript_path
    from multiomics_tui.task_guard import available as guard_available
    from multiomics_tui.llm import load_tui_config
    from multiomics_tui import task_runner
    guard_available()
    task = root / 'analysis' / ('v8_gsva_' + uuid.uuid4().hex)
    (task / 'tables').mkdir(parents=True)
    (task / 'tmp').mkdir()
    (task / 'expr.csv').write_text(
        'gene,' + ','.join(f's{i}' for i in range(4)) + '\n'
        + '\n'.join(f'g{g},' + ','.join(str(g + s) for s in range(4)) for g in range(6)) + '\n',
        encoding='utf-8')
    script = task / 'gsva.R'
    script.write_text(
        'expr <- as.matrix(read.csv(file.path(Sys.getenv("TASK_DIR"),"expr.csv"), row.names=1, check.names=FALSE)); '
        'sets <- list(setA=c("g0","g1","g2"), setB=c("g3","g4","g5")); '
        'stopifnot(requireNamespace("GSVA", quietly=TRUE)); '
        'methods <- c("ssgsea","plage","zscore","gsva"); available <- character(); status <- character(); '
        # GSVA >= 1.47/2.x requires a parameter object (plageParam); older
        # releases accept method="plage" directly. Support both so the probe
        # reflects whichever GSVA the host actually installed.
        'constructors <- list(ssgsea=GSVA::ssgseaParam, plage=GSVA::plageParam, '
        'zscore=GSVA::zscoreParam, gsva=GSVA::gsvaParam); '
        'for (m in methods) { '
        'ok <- exists(paste0(m, "Param"), where=asNamespace("GSVA")); '
        'if (ok) ok <- tryCatch({ GSVA::gsva(constructors[[m]](expr, sets), verbose=FALSE); TRUE }, '
        'error=function(e) FALSE); '
        'if (!ok && m == "plage") ok <- tryCatch({ GSVA::gsva(expr, sets, method="plage", verbose=FALSE); TRUE }, '
        'error=function(e) FALSE); '
        'status[m] <- if (ok) "ok" else "failed"; if (ok) available <- c(available, m) }; '
        'if (!length(available)) stop("no GSVA method produced scores"); '
        # PLAGE stays the canonical probe output so downstream checks keep one shape.
        'scores <- GSVA::gsva(constructors[["plage"]](expr, sets), verbose=FALSE); '
        'write.csv(as.data.frame(scores), file.path(Sys.getenv("TASK_DIR"),"tables/scores.csv")); '
        'writeLines(paste0(names(status), "=", status), '
        'file.path(Sys.getenv("TASK_DIR"),"tables/methods.txt"))\n',
        encoding='utf-8')
    env = task_runner._step_environment(root, task, load_tui_config(root), [])
    argv = [__import__('sys').executable, str(Path(task_runner.__file__).with_name('task_guard.py')),
            str(task), '--', rscript_path(root), '--vanilla', str(script)]
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=300, check=False)
    (task / 'gsva.log').write_text((result.stdout or '') + (result.stderr or ''), encoding='utf-8')
    scores = task / 'tables/scores.csv'
    if result.returncode or not scores.is_file():
        raise RuntimeError('GSVA scoring step failed: ' + (result.stderr or result.stdout or '')[-1500:])
    lines = [line for line in scores.read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(lines) != 3:
        raise RuntimeError('GSVA scoring output unexpected: ' + str(lines))
    # Each method is reported separately; a missing method is recorded, never hidden.
    method_lines = (task / 'tables/methods.txt').read_text(encoding='utf-8').split()
    methods = dict(line.split('=', 1) for line in method_lines if '=' in line)
    return {'gsva_available': True, 'gene_sets': 2, 'samples': 4, 'rows': len(lines) - 1,
            'gsva_methods': methods,
            'gsva_methods_available': [name for name, state in methods.items() if state == 'ok']}


def acceptance(root):
    from native_dist.v7_acceptance import acceptance as v7_acceptance
    from multiomics_tui.r_env import available_packages
    from multiomics_tui.llm import load_tui_config
    base = v7_acceptance(root)
    cfg = load_tui_config(root)
    packages = available_packages(cfg)
    scoring = _gene_set_scoring(root)
    base.update(record_path='.runtime/v8_acceptance.json', r_package_count=len(packages),
                gsva_discoverable='GSVA' in packages, **scoring)
    return base


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--project-root', type=Path, required=True)
    root = parser.parse_args().project_root.resolve()
    record = {'status': 'failed'}
    try:
        record = acceptance(root)
    except Exception as exc:
        from multiomics_tui.model_config import redact
        record.update(error_type=type(exc).__name__, error=redact(str(exc)))
    (root / '.runtime').mkdir(exist_ok=True)
    (root / '.runtime/v8_acceptance.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
