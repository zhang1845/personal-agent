#!/bin/zsh
cd "$(dirname "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo "缺少项目依赖环境，请先按 README 安装 .venv。"
  exit 1
fi
exec .venv/bin/python -B code/manage_service.py start --open
