"""Deprecated path kept for compatibility; use scripts/04_literature.py.

The job-driven implementation lives in scripts/04_literature.py. This shim preserves the
original one-off behaviour (8 axis queries -> knowledge/literature/pubmed_candidates.json).
"""
import importlib.util
import sys
from pathlib import Path


def main(argv):
    if len(argv) != 2:
        raise SystemExit('Usage: 04_retrieve_literature.py <project_root>')
    spec = importlib.util.spec_from_file_location('literature_tool', Path(__file__).with_name('04_literature.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.run_legacy(Path(argv[1]))


if __name__ == '__main__':
    main(sys.argv)
