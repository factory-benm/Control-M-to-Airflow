#!/usr/bin/env bash
#
# Run one payments scenario through the selected Control-M runtime.
#
# Usage:
#   controlm-run.sh <scenario> [--run-id <id>] [--mode auto|workbench|harness]
#
# The runtime is whatever scripts/controlm-up.sh selected. If no selection has
# been recorded yet, this script performs the selection first so that the mode
# is always explicit and always persisted.
#
# Exit codes:
#   0   the scenario completed and its observed invariants held
#   1   the scenario failed, or an invariant did not hold
#   2   usage error
#   78  the required runtime is unavailable

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO=""
RUN_ID=""
MODE=""

usage() {
  sed -n '3,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --run-id=*) RUN_ID="${1#*=}"; shift ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --mode=*) MODE="${1#*=}"; shift ;;
    -h|--help) usage; exit "$EXIT_OK" ;;
    -*) err "unrecognized argument '$1'"; usage; exit "$EXIT_USAGE" ;;
    *)
      if [ -n "$SCENARIO" ]; then
        err "more than one scenario given ('$SCENARIO' and '$1')"
        exit "$EXIT_USAGE"
      fi
      SCENARIO="$1"; shift ;;
  esac
done

if [ -z "$SCENARIO" ]; then
  err "a scenario is required"
  usage
  exit "$EXIT_USAGE"
fi
require_scenario "$SCENARIO" || exit "$EXIT_USAGE"

# Ensure a runtime has been selected, and honour an explicit --mode request.
if [ -n "$MODE" ] || [ ! -f "$SELECTED_RUNTIME_FILE" ]; then
  "$KIT_ROOT/scripts/controlm-up.sh" --mode "${MODE:-auto}" || exit $?
fi

RUNTIME_MODE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["mode"])' "$SELECTED_RUNTIME_FILE")"
: "${RUN_ID:=${SCENARIO}-$(date -u +%Y%m%dT%H%M%SZ)}"
REPOS_ROOT="$(resolve_repos_root)"

heading "Running scenario '$SCENARIO'"
info "runtime mode: $RUNTIME_MODE"
info "run id:       $RUN_ID"
info "repositories: ${REPOS_ROOT#"$KIT_ROOT/"}"

case "$RUNTIME_MODE" in
  control-m-workbench)
    # Workbench executes the jobs itself. The harness is not involved, and its
    # output must never be presented as Workbench output.
    err "Workbench job execution is not implemented."
    info "Workbench availability is recorded, but scenarios are driven through"
    info "the compatibility harness. Re-run with --mode harness."
    exit "$EXIT_UNAVAILABLE"
    ;;
  compatibility-harness)
    exec python3 "$KIT_ROOT/runtimes/control-m/harness/harness.py" \
      --scenario "$SCENARIO" \
      --run-id "$RUN_ID" \
      --kit-root "$KIT_ROOT" \
      --repos-root "$REPOS_ROOT" \
      --workbench-note "$WORKBENCH_PROBE_FILE"
    ;;
  *)
    err "unrecognized runtime mode '$RUNTIME_MODE' in $SELECTED_RUNTIME_FILE"
    exit "$EXIT_FAILED"
    ;;
esac
