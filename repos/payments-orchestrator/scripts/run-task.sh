#!/usr/bin/env bash
# Scheduler wrapper invoked by the Control-M job definitions.
#
# Control-M job commands call this wrapper rather than a service path directly,
# so the task-to-repository mapping lives in controlm/task-commands.json instead
# of being duplicated across twelve job definitions.
#
# The wrapper resolves the owning repository, then execs that repository's own
# run-task.sh with the arguments unchanged.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAPPING="${REPO_ROOT}/controlm/task-commands.json"

usage() {
  cat <<'USAGE'
Usage:
  run-task.sh --task <canonical-task> --run-dir <abs-path> --scenario <name>
              [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>]
              [--attempt <n>]

Dispatches a canonical Control-M task to the repository that implements it,
resolved through controlm/task-commands.json.

Exit codes:
  0   success (propagated from the service task)
  2   usage error, or unknown task
  3   injected deterministic fault (propagated)
  5   the owning repository could not be located
  *   any other code is propagated unchanged from the service task
USAGE
}

TASK=""
KIT_ROOT="${PAYOPS_KIT_ROOT:-}"
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --task)
      [[ $# -ge 2 ]] || { echo "run-task.sh: --task needs a value" >&2; exit 2; }
      TASK="$2"
      shift 2
      ;;
    --kit-root)
      [[ $# -ge 2 ]] || { echo "run-task.sh: --kit-root needs a value" >&2; exit 2; }
      KIT_ROOT="$2"
      PASSTHROUGH+=("--kit-root" "$2")
      shift 2
      ;;
    --run-dir|--scenario|--run-id|--now|--attempt)
      [[ $# -ge 2 ]] || { echo "run-task.sh: $1 needs a value" >&2; exit 2; }
      PASSTHROUGH+=("$1" "$2")
      shift 2
      ;;
    *)
      echo "run-task.sh: unrecognized argument '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TASK" ]]; then
  echo "run-task.sh: --task is required" >&2
  exit 2
fi

if [[ ! -f "$MAPPING" ]]; then
  echo "run-task.sh: missing task mapping at ${MAPPING}" >&2
  exit 5
fi

# Resolve the owning repository through the orchestrator's own parser so the
# wrapper and the graph module cannot disagree.
if ! REPOSITORY="$(
  PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$MAPPING" "$TASK" <<'PY'
import sys
from pathlib import Path
from orchestrator.graph import GraphError, repository_for_task

try:
    print(repository_for_task(Path(sys.argv[1]), sys.argv[2]))
except GraphError as error:
    print(f"run-task.sh: {error}", file=sys.stderr)
    raise SystemExit(2)
PY
)"; then
  exit 2
fi

# Candidate locations, in priority order:
#   1. sibling of this repository
#   2. repos/ under an explicit kit root
CANDIDATES=("$(dirname "$REPO_ROOT")/${REPOSITORY}")
if [[ -n "$KIT_ROOT" ]]; then
  CANDIDATES+=("${KIT_ROOT}/repos/${REPOSITORY}")
fi

SERVICE=""
for candidate in "${CANDIDATES[@]}"; do
  if [[ -x "${candidate}/scripts/run-task.sh" ]]; then
    SERVICE="${candidate}"
    break
  fi
done

if [[ -z "$SERVICE" ]]; then
  {
    echo "run-task.sh: cannot locate repository '${REPOSITORY}' for task '${TASK}'"
    echo "searched:"
    printf '  %s\n' "${CANDIDATES[@]}"
  } >&2
  exit 5
fi

echo "orchestrator: dispatching ${TASK} -> ${REPOSITORY}" >&2
exec "${SERVICE}/scripts/run-task.sh" --task "$TASK" "${PASSTHROUGH[@]}"
