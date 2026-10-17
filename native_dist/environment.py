"""Read-only runtime discovery and dependency inventory (stdlib bootstrap)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

IMPORTS = {'PyYAML':'yaml', 'langgraph':'langgraph.graph'}


def requirements(root):
    rows = []
    for path in (Path(root)/'requirements.txt', Path(root)/'native_dist/requirements-native.txt'):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith(('#', '-r')):
                rows.append(line)
    return list(dict.fromkeys(rows))


def checked(argv, timeout=90):
    return subprocess.run(list(map(str, argv)), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONUTF8':'1'})


def python_candidates(root):
    root = Path(root)
    paths = [root/('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')]
    if os.environ.get('MULTIOMICS_PYTHON'):
        paths.append(Path(os.environ['MULTIOMICS_PYTHON']))
    paths.append(Path(sys.executable))
    for name in ('python3.13', 'python3.14', 'python3.12', 'python3', 'python'):
        found = shutil.which(name)
        if found:
            paths.append(Path(found))
    if os.name == 'nt':
        launcher = shutil.which('py')
        if launcher:
            result = checked([launcher, '-0p'], timeout=15)
            import re
            for line in result.stdout.splitlines():
                match = re.search(r'([A-Za-z]:\\.*python(?:\d*)\.exe)\s*$', line, re.I)
                if match:
                    paths.append(Path(match[1]))
        for base in (Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Programs/Python',
                     Path(os.environ.get('ProgramFiles', 'C:/Program Files'))):
            paths.extend(sorted(base.glob('Python*/python.exe'), reverse=True))
    else:
        paths.extend(sorted(Path('/Library/Frameworks/Python.framework/Versions').glob('3.*/bin/python3'), reverse=True))
        paths.extend(Path(p) for p in ('/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3'))
    return list(dict.fromkeys(str(p.absolute()) for p in paths if p.is_file()))


def inspect_python(path, specs):
    code = r'''import importlib,importlib.metadata as m,json,platform,re,sys
try:
 from packaging.requirements import Requirement
 from packaging.version import Version
except ImportError:
 try:
  from pip._vendor.packaging.requirements import Requirement
  from pip._vendor.packaging.version import Version
 except ImportError:
  Requirement=None
rows=[]; specs=json.loads(sys.argv[1]); aliases=json.loads(sys.argv[2])
for spec in specs:
 name=re.split(r'[<>=!~;\[]',spec)[0].strip()
 row={'requirement':spec,'name':name,'status':'missing'}
 try:
  version=m.version(name); row['version']=version
  if Requirement is None:
   row['status']='needs_version_check'
  elif Version(version) not in Requirement(spec).specifier:
   row['status']='incompatible'
  else:
   importlib.import_module(aliases.get(name,name.replace('-','_')))
   row['status']='reusable'
 except m.PackageNotFoundError: pass
 except Exception as e: row.update(status='incompatible',error_type=type(e).__name__)
 rows.append(row)
print(json.dumps({'path':sys.executable,'version':platform.python_version(),'machine':platform.machine(),
 'prefix':sys.prefix,'base_prefix':sys.base_prefix,'compatible':(3,12)<=sys.version_info[:2]<(3,15),
 'packages':rows,'library_paths':[p for p in sys.path if p and ('site-packages' in p or 'dist-packages' in p)]}))'''
    try:
        result = checked([path, '-B', '-c', code, json.dumps(specs), json.dumps(IMPORTS)])
        if result.returncode:
            return {'path':str(path), 'compatible':False, 'error':'exit '+str(result.returncode)}
        return json.loads(result.stdout.splitlines()[-1])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'path':str(path), 'compatible':False, 'error':type(exc).__name__}


def r_candidates(root=None):
    paths = []
    if os.environ.get('MULTIOMICS_RSCRIPT'):
        paths.append(Path(os.environ['MULTIOMICS_RSCRIPT']))
    if root:
        local = Path(root)/'config/runtime.local.yaml'
        if local.is_file():
            import re
            match = re.search(r'^\s+rscript:\s*(".*")\s*$',local.read_text(encoding='utf-8'),re.M)
            if match:
                paths.append(Path(json.loads(match[1])))
    if root:
        # Old runtime.local.yaml may point only to a now-damaged private copy.
        # Recover the previously selected host R from its installation audit.
        report = Path(root)/'.runtime/install_report.json'
        try:
            saved = json.loads(report.read_text(encoding='utf-8'))
            for key in ('r', 'r_after'):
                value = (saved.get(key) or {}).get('path')
                if isinstance(value, str) and Path(value).is_absolute():
                    paths.append(Path(value))
        except (OSError, ValueError, TypeError):
            pass
    found = shutil.which('Rscript')
    if found:
        paths.append(Path(found))
    if os.name == 'nt':
        for base in (Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'R',
                     Path.home()/'AppData/Local/Programs/R', Path.home()/'R'):
            paths.extend(sorted(base.glob('R-*/bin/**/Rscript.exe'), reverse=True))
    else:
        paths.extend(Path(p) for p in ('/Library/Frameworks/R.framework/Resources/bin/Rscript',
                                      '/usr/lib/R/bin/Rscript', '/opt/homebrew/bin/Rscript', '/usr/local/bin/Rscript'))
    # Rebuild from a host R installation, never from a damaged sandbox copy.
    return list(dict.fromkeys(str(p.absolute()) for p in paths if '.runtime' not in p.parts and p.is_file()))


def r_packages(root):
    """Declared R packages: CRAN list plus Bioconductor list, in stable order."""
    names = []
    for relative in ('native_dist/r-packages.txt', 'native_dist/r-bioc-packages.txt'):
        path = Path(root)/relative
        if path.is_file():
            names.extend(path.read_text().split())
    return list(dict.fromkeys(names))


def inspect_r(path, root):
    packages = r_packages(root)
    private = str(Path(root).resolve()/'.runtime/r-library')
    code = 'lib <- ' + json.dumps(private) + '; if(dir.exists(lib)) .libPaths(c(lib,.libPaths())); '
    code += 'cat("R_VERSION=",as.character(getRversion()),"\\n",sep=""); cat("R_HOME=",R.home(),"\\n",sep=""); '
    code += 'for(p in commandArgs(TRUE)) { ok <- requireNamespace(p,quietly=TRUE); cat(p,if(ok) as.character(packageVersion(p)) else "missing",if(ok) find.package(p) else "",sep="\\t");cat("\\n") }; cat("R_LIBS=",paste(.libPaths(),collapse="|"),"\\n",sep="")'
    try:
        result = checked([path, '--vanilla', '-e', code, *packages])
        lines = result.stdout.splitlines()
        version = next(line.split('=',1)[1] for line in lines if line.startswith('R_VERSION='))
        rows = []
        for line in lines:
            parts = line.split('\t')
            if len(parts) == 3 and parts[0] in packages:
                rows.append({'name':parts[0], 'version':parts[1], 'path':parts[2],
                             'status':'missing' if parts[1] == 'missing' else 'reusable'})
        if result.returncode or len(rows) != len(packages):
            raise ValueError('R inventory incomplete')
        return {'path':str(path), 'version':version, 'compatible':tuple(map(int,version.split('.')[:2])) >= (4,4),
                'home':next(line.split('=',1)[1] for line in lines if line.startswith('R_HOME=')),
                'libraries':next(line.split('=',1)[1].split('|') for line in lines if line.startswith('R_LIBS=')), 'packages':rows}
    except (OSError, ValueError, StopIteration, subprocess.TimeoutExpired) as exc:
        return {'path':str(path), 'compatible':False, 'error':type(exc).__name__}


def inventory(root):
    py = [inspect_python(p, requirements(root)) for p in python_candidates(root)]
    r = [inspect_r(p, root) for p in r_candidates(root)]
    return {'schema_version':1, 'system':sys.platform, 'machine':platform.machine(),
            'project_root':str(Path(root).resolve()), 'python_candidates':py, 'r_candidates':r,
            'python':next((p for p in py if p['compatible']), None),
            'r':next((p for p in r if p['compatible']), None)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = inventory(args.project_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['python'] and result['r'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
