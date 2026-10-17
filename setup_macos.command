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
  echo "未发现兼容 Python 3.12–3.14。请阅读 docs/INSTALL_UPDATE_zh.md，只补装缺少的软件。"
  read -r -p "按回车关闭" _
  exit 1
fi
echo "先检查本机 Python/R 与依赖包，不修改已有软件："
if ! "$MULTIOMICS_PY" -B native_dist/bootstrap.py check; then
  echo "缺少兼容环境。请按 Agent 安装提示词补齐后重试。"
  read -r -p "按回车关闭" _
  exit 1
fi
if "$MULTIOMICS_PY" native_dist/bootstrap.py setup "$@"; then
  echo "安装完成。"
  MULTIOMICS_STATUS=0
else
  echo "安装未完成，请查看 .runtime/install.log。"
  MULTIOMICS_STATUS=1
fi
read -r -p "按回车关闭" _
exit "$MULTIOMICS_STATUS"
