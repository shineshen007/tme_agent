"""Create user-owned configuration, synthetic examples and built-in data.

Nothing here replaces an existing file: every copy opens the destination
exclusively, so a second setup run and a user-edited file are both safe.
"""
from pathlib import Path
from .public_release import safe_path


def initialize(root):
    root = Path(root)
    copies = {'config/study_config.yaml': 'config/study_config.example.yaml',
              'data/raw/examples/synthetic_expression.csv': 'examples/synthetic_expression.csv',
              'data/metadata/examples/synthetic_samples.csv': 'examples/synthetic_samples.csv'}
    created = []
    for destination, source in copies.items():
        path = safe_path(root, destination)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation protects user edits even if setup runs twice.
            try:
                with path.open('xb') as stream:
                    stream.write(safe_path(root, source).read_bytes())
                created.append(destination)
            except FileExistsError:
                pass
    created.extend(_seed_datasets(root))
    return created


def _seed_datasets(root):
    """Copy installed built-in datasets into ``data/`` without replacing anything.

    The installed ``datasets/`` directory is read directly rather than through the
    workspace abstraction: setup runs before any workspace exists, and the copy
    must come from the release tree that is being installed.
    """
    from multiomics_tui.datasets import DatasetError, datasets_root, seeds
    try:
        mapping = seeds(root)
    except DatasetError as exc:
        raise RuntimeError(f'内置数据集校验失败：{exc}') from exc
    base = datasets_root(root).resolve()
    created = []
    for destination, source in sorted(mapping.items()):
        path = safe_path(root, destination)
        if path.exists():
            continue
        origin = Path(source).resolve()
        if not origin.is_file() or not origin.is_relative_to(base):
            raise RuntimeError(f'内置数据集源文件不可用：{source}')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with origin.open('rb') as src, path.open('xb') as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            created.append(destination)
        except FileExistsError:
            pass
    return created
