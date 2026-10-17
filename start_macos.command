#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "请先双击 setup_macos.command 配置环境。"
  read -r -p "按回车关闭" _
  exit 1
fi
if .venv/bin/python native_dist/bootstrap.py start; then
  exit 0
else
  echo "启动未完成，请查看 .runtime/install.log。"
  read -r -p "按回车关闭" _
  exit 1
fi
