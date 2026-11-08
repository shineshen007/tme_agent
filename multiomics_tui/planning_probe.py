"""Trusted, read-only API inspection; executed under the existing task guard."""
import importlib
import importlib.metadata
import inspect
import hashlib
import json
import platform
import sys
from pathlib import Path
from packaging.utils import canonicalize_name

if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    rows = []
    for candidate in request:
        row = {'id': candidate['id'], 'language': 'python', 'package': candidate['package'],
               'interpreter': str(Path(sys.executable).resolve()), 'runtime': platform.python_version(),
               'status': 'missing', 'apis': {}}
        with Path(sys.executable).resolve().open('rb') as handle:
            row['interpreter_sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
        try:
            package = candidate['package']
            if package == 'stdlib':
                row['version'] = platform.python_version()
                row['location'] = str(Path(sys.base_prefix).resolve())
            else:
                distribution = importlib.metadata.distribution(package)
                row['version'] = distribution.version
                row['location'] = str(distribution.locate_file('').resolve())
                row['requires_python'] = distribution.metadata.get('Requires-Python', '')
            for api in candidate['apis']:
                parts = api.split('.')
                if package == 'stdlib':
                    if parts[0] not in sys.stdlib_module_names:
                        raise ValueError('API does not belong to Python stdlib')
                else:
                    owners = importlib.metadata.packages_distributions().get(parts[0], [])
                    if canonicalize_name(package) not in {canonicalize_name(owner) for owner in owners}:
                        raise ValueError('API module is not owned by the declared distribution')
                value = None
                for n in range(len(parts), 0, -1):
                    try:
                        value = importlib.import_module('.'.join(parts[:n]))
                    except ModuleNotFoundError as exc:
                        if exc.name != '.'.join(parts[:n]):
                            raise
                        continue
                    for part in parts[n:]:
                        value = getattr(value, part)
                    break
                if not callable(value):
                    raise ValueError('API is not callable: ' + api)
                row['apis'][api] = {'signature': str(inspect.signature(value)),
                                    'documentation': inspect.getdoc(value) or ''}
                source = inspect.getsourcefile(inspect.unwrap(value)) if inspect.isfunction(inspect.unwrap(value)) else None
                if source:
                    with Path(source).open('rb') as handle:
                        row['apis'][api]['source_sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
                module_file = getattr(inspect.getmodule(value), '__file__', None)
                if module_file:
                    with Path(module_file).open('rb') as handle:
                        row['apis'][api]['module_sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
            row['status'] = 'verified'
        except (ImportError, AttributeError, ValueError, TypeError) as exc:
            if 'version' in row:
                row['status'] = 'api_incompatible'
            row['error'] = type(exc).__name__ + ': ' + str(exc)
        rows.append(row)
    Path(sys.argv[2]).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
