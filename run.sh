#!/bin/bash
# 启动自动确认（AX 方案）。参数会原样传给 auto_confirm.py。
#   ./run.sh --dry-run -v     先观察，不真点
#   ./run.sh                  正式运行
#   ./run.sh --once -v        只跑一轮
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "首次使用请先创建环境："
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

exec .venv/bin/python auto_confirm.py "$@"
