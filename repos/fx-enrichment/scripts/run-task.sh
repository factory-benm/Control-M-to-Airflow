#!/usr/bin/env bash
# run-task.sh — entry point for fx-enrichment tasks.
# Emits exactly one JSON object on stdout; all diagnostics go to stderr.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m fx_enrichment.cli "$@"
