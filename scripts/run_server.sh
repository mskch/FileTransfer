#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m secure_transfer.server --config runtime/server.json
