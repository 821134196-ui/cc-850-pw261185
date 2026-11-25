#!/usr/bin/env bash
# 一键跑完整工作台：测试 + 8 段演示。要求 scripts/pg_local.sh start 已运行。
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-$HOME/venv/bin/python}"

echo "────────── pytest ──────────"
"$PY" -m pytest pagination_workbench/tests/ -q

echo "────────── demo ──────────"
"$PY" -m pagination_workbench.cli demo "$@"
