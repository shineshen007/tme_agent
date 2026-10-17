"""Create user-owned configuration and synthetic examples, never replace them."""
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
    return created
