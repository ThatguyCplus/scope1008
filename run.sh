#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 tools/bootstrap.py
exec .venv/bin/python -m scope1008 "$@"
