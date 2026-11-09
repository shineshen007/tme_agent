#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
MULTIOMICS_PY=""
for candidate in "${MULTIOMICS_PYTHON:-}" "$PWD/.venv/bin/python" \
  "$(command -v python3.13 || true)" "$(command -v python3.14 || true)" \
  "$(command -v python3.12 || true)" "$(command -v python3 || true)" \
  /Library/Frameworks/Python.framework/Versions/3.{13,14,12}/bin/python3 \
  /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$candidate" ] && "$candidate" -B -c 'import sys;sys.exit(0 if (3,12)<=sys.version_info[:2]<(3,15) else 1)' 2>/dev/null; then
    MULTIOMICS_PY="$candidate"
    break
  fi
done
if [ -z "$MULTIOMICS_PY" ]; then
  echo "Compatible Python 3.12–3.14 was not found. Read docs/INSTALL_UPDATE_zh.md and install only missing software."
  read -r -p "Press Enter to close" _
  exit 1
fi
echo "Checking local Python/R and dependencies without changing existing software:"
if ! "$MULTIOMICS_PY" -B native_dist/bootstrap.py check; then
  echo "A compatible environment is missing. Follow the Agent installation instructions, then retry."
  read -r -p "Press Enter to close" _
  exit 1
fi
if "$MULTIOMICS_PY" native_dist/bootstrap.py setup "$@"; then
  echo "Installation complete."
  MULTIOMICS_STATUS=0
else
  echo "Installation failed; see .runtime/install.log."
  MULTIOMICS_STATUS=1
fi
read -r -p "Press Enter to close" _
exit "$MULTIOMICS_STATUS"
