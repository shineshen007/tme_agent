#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export MPLCONFIGDIR="$project_root/.cache/matplotlib"
export XDG_CACHE_HOME="$project_root/.cache"
export LANGSMITH_TRACING=false
export LANGCHAIN_TRACING_V2=false
exec "$project_root/.venv/bin/python" -m orchestrator "$@" --project-root "$project_root"
