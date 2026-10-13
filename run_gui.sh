#!/bin/bash
# 启动菜单栏图形开关。图标会出现在屏幕右上角，默认是暂停状态。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "首次使用请先创建环境："
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

exec .venv/bin/python menubar_app.py
