#!/usr/bin/env bash
# Run payment-notifications unit tests.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"

exec python3 -m unittest discover -s "$REPO_ROOT/tests" -p "test_*.py" -v
