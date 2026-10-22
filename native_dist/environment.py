"""Read-only runtime discovery and dependency inventory (stdlib bootstrap)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

IMPORTS = {'PyYAML':'yaml', 'langgraph':'langgraph.graph'}

# R --vanilla skips the environ files that normally add R_LIBS_USER. This
# snippet is shared by discovery and bootstrap so user-installed packages are
# visible to both the inventory and the later "missing package" check.
R_USER_LIBRARY_CODE = (
    'extra <- unlist(strsplit(Sys.getenv("R_LIBS_USER"), '
    '.Platform$path.sep, fixed=TRUE)); '
    'extra <- path.expand(extra[nzchar(extra)]); '
    'extra <- extra[file.exists(extra)]; '
    '.libPaths(unique(c(extra, .libPaths()))); '
)


def requirements(root):
    rows = []
    for path in (Path(root)/'requirements.txt', Path(root)/'native_dist/requirements-native.txt'):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith(('#', '-r')):
                rows.append(line)
    return list(dict.fromkeys(rows))


def checked(argv, timeout=90, env_extra=None, env_remove=()):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONUTF8':'1'}
    if env_extra:
        env.update(env_extra)
    for name in env_remove:
        env.pop(name, None)
    return subprocess.run(list(map(str, argv)), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          env=env)


def _version_key(path):
    """Sort interpreter paths such as python3.14 and cpython-3.13.2 naturally."""
    match = re.search(r'(?:python|cpython-|R-|/)(\d+)\.(\d+)(?:\.(\d+))?', str(path), re.I)
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())


def _sorted_paths(paths):
    unique = {}
    for path in paths:
        unique.setdefault(path, path)
    return sorted(unique, key=_version_key, reverse=True)


def _user_python_candidates():
    """Known per-user interpreter locations, newest first."""
    home = Path.home()
    patterns = []
    if os.name == 'nt':
        local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))
        patterns.extend([
            (home / '.local/bin', 'python.exe'),
            (home / '.pyenv/pyenv-win/versions', '*/python.exe'),
            (local / 'uv/python', '*/python.exe'),
            (home / 'scoop/apps', 'python*/current/python.exe'),
            (local / 'Programs/Python', 'Python*/python.exe'),
            (home / 'miniconda3', 'python.exe'),
            (home / 'anaconda3', 'python.exe'),
            (home / 'mambaforge', 'python.exe'),
        ])
    else:
        patterns.extend([
            (home / '.local/bin', 'python*'),
            (home / '.pyenv/versions', '*/bin/python3'),
            (home / '.local/share/uv/python', '*/bin/python3'),
            (home / '.asdf/installs/python', '*/bin/python3'),
            (home / '.local/share/mise/installs/python', '*/bin/python3'),
            (home / 'miniconda3/bin', 'python3'),
            (home / 'anaconda3/bin', 'python3'),
            (home / 'mambaforge/bin', 'python3'),
        ])
        if sys.platform == 'darwin':
            patterns.append((home / 'Library/Python', '*/bin/python3'))
    found = []
    for base, pattern in patterns:
        if base.is_dir():
            found.extend(base.glob(pattern))
    for name in ('miniconda3', 'anaconda3', 'mambaforge', 'micromamba'):
        base = home / name / 'envs'
        if base.is_dir():
            found.extend(base.glob('*/python.exe' if os.name == 'nt' else '*/bin/python3'))
    pattern = re.compile(r'python(3(\.\d+)?)?(\.exe)?$', re.I)
    return _sorted_paths(path for path in found if path.is_file() and pattern.fullmatch(path.name))


def _path_python_candidates():
    found = []
    for name in ('python3.14', 'python3.13', 'python3.12', 'python3', 'python'):
        path = shutil.which(name)
        if path:
            found.append(Path(path))
    return found


def _dedup_existing(paths):
    result, seen = [], set()
    for path in paths:
        try:
            if not Path(path).is_file():
                continue
            key = Path(path).resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(str(Path(path).absolute()))
    return result


def python_candidates(root):
    root = Path(root)
    paths = []
    if os.environ.get('MULTIOMICS_PYTHON'):
        paths.append(Path(os.environ['MULTIOMICS_PYTHON']))
    paths.append(root/('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python'))
    paths.extend(_user_python_candidates())
    if os.name == 'nt':
        launcher = shutil.which('py')
        if launcher:
            result = checked([launcher, '-0p'], timeout=15)
            for line in result.stdout.splitlines():
                match = re.search(r'([A-Za-z]:\\.*python(?:\d*)\.exe)\s*$', line, re.I)
                if match:
                    paths.append(Path(match[1]))
    paths.extend(_path_python_candidates())
    paths.append(Path(sys.executable))
    if os.name == 'nt':
        for base in (Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Programs/Python',
                     Path(os.environ.get('ProgramFiles', 'C:/Program Files'))):
            paths.extend(sorted(base.glob('Python*/python.exe'), reverse=True))
    else:
        paths.extend(sorted(Path('/Library/Frameworks/Python.framework/Versions').glob('3.*/bin/python3'), reverse=True))
        paths.extend(Path(p) for p in ('/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3'))
    return _dedup_existing(paths)


def inspect_python(path, specs, include_user_site=True):
    code = r'''import importlib,importlib.metadata as m,json,os,platform,re,site,sys
try:
 if os.environ.get('MULTIOMICS_INSPECT_USER_SITE','1') != '0':
  user_site=site.getusersitepackages()
  if isinstance(user_site,str) and user_site and os.path.isdir(user_site) and user_site not in sys.path:
   if sys.prefix==sys.base_prefix:
    sys.path.insert(0,user_site)
   else:
    sys.path.append(user_site)
except Exception:
 pass
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
def dist_version(name):
 key=name.lower().replace('_','-')
 try:
  return m.version(name)
 except m.PackageNotFoundError:
  for dist in m.distributions():
   raw=dist.metadata['Name']
   if raw and raw.lower().replace('_','-')==key:
    return dist.version
  raise
for spec in specs:
 name=re.split(r'[<>=!~;\[]',spec)[0].strip()
 row={'requirement':spec,'name':name,'status':'missing'}
 try:
  version=dist_version(name); row['version']=version
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
        result = checked([path, '-B', '-c', code, json.dumps(specs), json.dumps(IMPORTS)],
                         env_extra=None if include_user_site else {'MULTIOMICS_INSPECT_USER_SITE': '0'})
        if result.returncode:
            return {'path':str(path), 'compatible':False, 'error':'exit '+str(result.returncode)}
        return json.loads(result.stdout.splitlines()[-1])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'path':str(path), 'compatible':False, 'error':type(exc).__name__}


def _user_r_candidates():
    """Known per-user Rscript locations, newest first."""
    home = Path.home()
    patterns = []
    if os.name == 'nt':
        local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))
        patterns.extend([
            (home / 'AppData/Local/Programs/R', 'R-*/bin/**/Rscript.exe'),
            (home / 'scoop/apps', 'r*/current/bin/Rscript.exe'),
            (home / 'miniconda3', 'Scripts/Rscript.exe'),
            (home / 'anaconda3', 'Scripts/Rscript.exe'),
            (home / 'miniconda3', 'Lib/R/bin/Rscript.exe'),
            (home / 'anaconda3', 'Lib/R/bin/Rscript.exe'),
            (local / 'Programs/R', 'R-*/bin/**/Rscript.exe'),
        ])
    else:
        patterns.extend([
            (home / '.local/bin', 'Rscript'),
            (home / 'bin', 'Rscript'),
            (home / 'miniconda3/bin', 'Rscript'),
            (home / 'anaconda3/bin', 'Rscript'),
            (home / 'mambaforge/bin', 'Rscript'),
            (home / '.local/share/R', '*/bin/Rscript'),
        ])
    found = []
    for base, pattern in patterns:
        if base.is_dir():
            found.extend(base.glob(pattern))
    for name in ('miniconda3', 'anaconda3', 'mambaforge', 'micromamba'):
        base = home / name / 'envs'
        if base.is_dir():
            found.extend(base.glob('*/Scripts/Rscript.exe' if os.name == 'nt' else '*/bin/Rscript'))
    return _sorted_paths(path for path in found if path.is_file())


def r_candidates(root=None):
    paths = []
    if os.environ.get('MULTIOMICS_RSCRIPT'):
        paths.append(Path(os.environ['MULTIOMICS_RSCRIPT']))
    if root:
        local = Path(root)/'config/runtime.local.yaml'
        if local.is_file():
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
    paths.extend(_user_r_candidates())
    found = shutil.which('Rscript')
    if found:
        paths.append(Path(found))
    if os.name == 'nt':
        for base in (Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'R',
                     Path.home()/'AppData/Local/Programs/R', Path.home()/'R'):
            paths.extend(sorted(base.glob('R-*/bin/**/Rscript.exe'), reverse=True))
    else:
        paths.extend(Path(p) for p in ('/Library/Frameworks/R.framework/Resources/bin/Rscript',
                                      '/usr/lib/R/bin/Rscript', '/opt/local/bin/Rscript',
                                      '/opt/homebrew/bin/Rscript', '/usr/local/bin/Rscript'))
    # Rebuild from a host R installation, never from a damaged sandbox copy.
    return [path for path in _dedup_existing(paths) if '.runtime' not in Path(path).parts]


def r_packages(root):
    """Declared R packages: CRAN list plus Bioconductor list, in stable order."""
    names = []
    for relative in ('native_dist/r-packages.txt', 'native_dist/r-bioc-packages.txt'):
        path = Path(root)/relative
        if path.is_file():
            names.extend(path.read_text().split())
    return list(dict.fromkeys(names))


def r_user_library_paths():
    """Existing R library roots from user/system environment, for literal use in R code."""
    paths = [Path.home()/'.local/lib/R/site-library', Path.home()/'R/library']
    for name in ('R_LIBS_USER', 'R_LIBS_SITE', 'R_LIBS'):
        for part in str(os.environ.get(name, '')).split(os.pathsep):
            if part:
                paths.append(Path(part).expanduser())
    return list(dict.fromkeys(str(path) for path in paths if path.is_dir()))


def _r_package_in_library(library, name):
    description = Path(library) / name / 'DESCRIPTION'
    if not description.is_file():
        return None
    version = 'unknown'
    try:
        for line in description.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('Version:'):
                version = line.split(':', 1)[1].strip() or 'unknown'
                break
    except OSError:
        pass
    return {'name': name, 'version': version, 'path': str(description.parent)}


def inspect_r(path, root):
    packages = r_packages(root)
    private = Path(root).resolve()/'.runtime/r-library'
    user_libraries = r_user_library_paths()
    code = ('cat("R_VERSION=",as.character(getRversion()),"\\n",sep=""); '
            'cat("R_HOME=",R.home(),"\\n",sep=""); ')
    code += 'for(p in commandArgs(TRUE)) { ok <- requireNamespace(p,quietly=TRUE); cat(p,if(ok) as.character(packageVersion(p)) else "missing",if(ok) find.package(p) else "",sep="\\t");cat("\\n") }; cat("R_LIBS=",paste(.libPaths(),collapse="|"),"\\n",sep="")'
    try:
        result = checked([path, '--vanilla', '-e', code, *packages],
                         env_remove=('R_LIBS_USER', 'R_LIBS_SITE', 'R_LIBS'))
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
        libraries = [str(private)] if private.is_dir() else []
        libraries.extend(user_libraries)
        for line in lines:
            if line.startswith('R_LIBS='):
                libraries.extend(line.split('=',1)[1].split('|'))
                break
        libraries = list(dict.fromkeys(str(Path(item).resolve()) for item in libraries if item))
        for row in rows:
            if row['status'] != 'missing':
                continue
            for library in libraries:
                found = _r_package_in_library(library, row['name'])
                if found:
                    row.update(found, status='reusable')
                    break
        return {'path':str(path), 'version':version,
                'compatible':tuple(map(int,version.split('.')[:2])) >= (4,4),
                'home':next(line.split('=',1)[1] for line in lines if line.startswith('R_HOME=')),
                'libraries':libraries, 'packages':rows}
    except (OSError, ValueError, StopIteration, subprocess.TimeoutExpired) as exc:
        result = locals().get('result')
        return {'path':str(path), 'compatible':False, 'error':type(exc).__name__,
                'stdout': (getattr(result, 'stdout', '') or '')[-2000:],
                'stderr': (getattr(result, 'stderr', '') or '')[-2000:],
                'code': code}


def _reusable_count(candidate):
    return sum(1 for row in candidate.get('packages') or [] if row.get('status') == 'reusable')


def choose_candidate(candidates):
    """Prefer a compatible candidate that already has the most dependencies."""
    compatible = [(index, item) for index, item in enumerate(candidates or [])
                  if item.get('compatible')]
    if not compatible:
        return None
    complete = [(index, item) for index, item in compatible
                if item.get('packages') and all(row.get('status') == 'reusable'
                                                for row in item['packages'])]
    pool = complete or compatible
    pool.sort(key=lambda row: (row[0] not in {item[0] for item in complete},
                               -_reusable_count(row[1]), row[0]))
    return pool[0][1]


def _preferred_candidate(candidates, env_name):
    value = os.environ.get(env_name)
    if not value:
        return None
    try:
        target = Path(value).expanduser().resolve()
    except OSError:
        return None
    for candidate in candidates:
        try:
            if Path(candidate.get('path', '')).resolve() == target and candidate.get('compatible'):
                return candidate
        except OSError:
            continue
    return None


def _inspect_until_complete(paths, inspector):
    """Inspect in priority order; stop once one candidate has every dependency."""
    rows = []
    for path in paths:
        row = inspector(path)
        rows.append(row)
        if (row.get('compatible') and row.get('packages')
                and all(item.get('status') == 'reusable' for item in row['packages'])):
            break
    return rows


def inventory(root):
    py_paths = python_candidates(root)
    r_paths = r_candidates(root)
    py = _inspect_until_complete(py_paths, lambda path: inspect_python(path, requirements(root)))
    r = _inspect_until_complete(r_paths, lambda path: inspect_r(path, root))
    return {'schema_version':1, 'system':sys.platform, 'machine':platform.machine(),
            'project_root':str(Path(root).resolve()), 'python_paths':py_paths, 'r_paths':r_paths,
            'python_candidates':py, 'r_candidates':r,
            'python':_preferred_candidate(py, 'MULTIOMICS_PYTHON') or choose_candidate(py),
            'r':_preferred_candidate(r, 'MULTIOMICS_RSCRIPT') or choose_candidate(r)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = inventory(args.project_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['python'] and result['r'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
