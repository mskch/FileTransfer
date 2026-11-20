#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m secure_transfer.gui --config "runtime/${1:-alice}.json"
