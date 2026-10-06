#!/usr/bin/env bash
# ledger-reconciliation task runner.
# Self-locating; delegates to the Python CLI. Emits one JSON object on stdout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PAYOPS_REPO_ROOT="$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat >&2 <<'EOF'
Usage: ./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task <task-name> \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]

Owned tasks: apply_business_day_cutoff, post_pending_ledger, reconcile_nostro_ledger
EOF
  exit 0
fi

exec python3 -m ledger_reconciliation.cli "$@"
