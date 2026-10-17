"""Validate/copy only the chosen interpreter's project dependency closure."""
from __future__ import annotations
import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from .environment import requirements


def assess(specs):
    selected = {}
    expanded = set()
    missing = set()
    pending = [(Requirement(s),'') for s in specs]
    while pending:
        req, parent_extra = pending.pop()
        if req.marker and not req.marker.evaluate({'extra':parent_extra}):
            continue
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            missing.add(str(req).split(';')[0].strip())
            continue
        if Version(dist.version) not in req.specifier:
            missing.add(str(req).split(';')[0].strip())
        name = canonicalize_name(req.name)
        selected[name] = dist
        context = (name, tuple(sorted(req.extras)))
        if context in expanded:
            continue
        expanded.add(context)
        for extra in {'', *req.extras}:
            pending.extend((Requirement(s),extra) for s in (dist.requires or []))
    return selected, sorted(missing)


def closure(specs):
    selected, missing = assess(specs)
    if missing:
        raise RuntimeError('依赖缺少或版本不兼容：'+', '.join(missing))
    return selected


def copy_packages(selected, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for dist in selected.values():
        base = Path(dist.locate_file('')).resolve()
        if base.name not in {'site-packages','dist-packages'} or not dist.files:
            raise RuntimeError(f'无法安全复制此依赖（可能为 editable 安装）：{dist.name}')
        for name in dist.files:
            source = Path(dist.locate_file(name))
            relative = Path(str(name))
            if relative.is_absolute() or '..' in relative.parts:
                # Console entry points outside site-packages aren't runtime files.
                continue
            if source.is_file():
                if not source.resolve().is_relative_to(base):
                    raise RuntimeError('依赖含外部链接：'+dist.name)
                target = destination/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project-root',type=Path,required=True)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--report',action='store_true')
    args=parser.parse_args()
    if args.report:
        selected, missing = assess(requirements(args.project_root))
        print(json.dumps({'dependency_closure':{n:d.version for n,d in sorted(selected.items())},
                          'missing_requirements':missing},indent=2))
        return
    selected=closure(requirements(args.project_root))
    if args.destination:
        copy_packages(selected,args.destination)
    print(json.dumps({'dependency_closure':{n:d.version for n,d in sorted(selected.items())}},indent=2))


if __name__=='__main__':
    main()
