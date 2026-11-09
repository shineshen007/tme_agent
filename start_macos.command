#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Open setup_macos.command to configure the environment first."
  read -r -p "Press Enter to close" _
  exit 1
fi
if .venv/bin/python native_dist/bootstrap.py start; then
  exit 0
else
  echo "Startup failed; see .runtime/install.log."
  read -r -p "Press Enter to close" _
  exit 1
fi
