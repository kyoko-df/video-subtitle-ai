#!/bin/zsh
# Double-click after installing the project dependencies into .venv.
cd -- "$(dirname -- "$0")" || exit 1
if [[ ! -x .venv/bin/shengmu ]]; then
  print '请先按 README 安装项目：'
  print 'python3.12 -m venv .venv'
  print 'source .venv/bin/activate'
  print 'python -m pip install -e ".[all]"'
  read '?按回车退出'
  exit 1
fi
export SHENGMU_MODEL_DIR="${SHENGMU_MODEL_DIR:-$PWD/models}"
.venv/bin/shengmu gui
