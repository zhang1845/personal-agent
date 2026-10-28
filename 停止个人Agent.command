#!/bin/zsh
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -B code/manage_service.py stop
