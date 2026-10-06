#!/usr/bin/env bash
#
# Run a payments scenario through the local Apache Airflow scheduler.
#
# Usage:
#   airflow-run.sh <scenario> [--run-id <id>]
#   airflow-run.sh --trigger ['<conf json>'] [--dag-run-id <id>]
#
# With a scenario: prepares workspace/runtime/runs/<run-id>/, triggers one DAG
# run per pass with conf {scenario, run_id, kit_root, fixed_clock, pass}, waits
# for each, and writes runtime.json and run-manifest.json in the harness format
# (mode "airflow"). Grade the run with:
#   python3 runtimes/control-m/harness/verify_run.py --run-dir <run-dir> --kit-root .
#
# With --trigger: starts one DAG run with exactly the given conf (missing keys
# take the DAG's defaults), waits for it, and prints its task states and tries.
#
# Either way the DAG, which is paused on creation, is unpaused only while these
# runs execute and is paused again afterwards (design Decision 18).
#
# Exit codes:
#   0   the run succeeded (and, for a scenario, its observed invariants held)
#   1   the run failed, or an invariant did not hold
#   2   usage error
#   78  Airflow is not installed or not running (start it with scripts/airflow-up.sh)

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/airflow.sh
source "$KIT_ROOT/scripts/lib/airflow.sh"

usage() {
  sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

SCENARIO=""
RUN_ID=""
TRIGGER=0
CONF=""
DAG_RUN_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --dag-run-id) DAG_RUN_ID="${2:-}"; shift 2 ;;
    --trigger)
      TRIGGER=1
      if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then CONF="$2"; shift; fi
      shift
      ;;
    -h|--help) usage; exit "$EXIT_OK" ;;
    -*) err "unrecognized argument '$1'"; usage; exit "$EXIT_USAGE" ;;
    *)
      if [ -n "$SCENARIO" ]; then err "only one scenario may be given"; exit "$EXIT_USAGE"; fi
      SCENARIO="$1"; shift
      ;;
  esac
done

if [ "$TRIGGER" -eq 1 ] && [ -n "$SCENARIO" ]; then
  err "give a scenario or --trigger, not both"
  exit "$EXIT_USAGE"
fi
if [ "$TRIGGER" -eq 0 ] && [ -z "$SCENARIO" ]; then
  err "a scenario is required"
  usage
  exit "$EXIT_USAGE"
fi
[ -n "$SCENARIO" ] && { require_scenario "$SCENARIO" || exit "$EXIT_USAGE"; }

if ! airflow_installed; then
  err "Airflow is not installed; run scripts/airflow-up.sh"
  exit "$EXIT_UNAVAILABLE"
fi
if ! airflow_running; then
  err "Airflow is not running; start it with scripts/airflow-up.sh"
  exit "$EXIT_UNAVAILABLE"
fi

RUNNER=(env "PYTHONPATH=$KIT_ROOT/runtimes/control-m/harness" python3 "$KIT_ROOT/runtimes/airflow/runner.py")
if [ "$TRIGGER" -eq 1 ]; then
  ARGS=(trigger)
  [ -n "$CONF" ] && ARGS+=(--conf "$CONF")
  [ -n "$DAG_RUN_ID" ] && ARGS+=(--dag-run-id "$DAG_RUN_ID")
  exec "${RUNNER[@]}" "${ARGS[@]}"
fi

: "${RUN_ID:=${SCENARIO}-airflow-$(date -u +%Y%m%dT%H%M%SZ)}"
heading "Running scenario '$SCENARIO' on Airflow $AIRFLOW_VERSION"
info "runtime mode: airflow (not Control-M)"
info "run id:       $RUN_ID"
info "run dir:      workspace/runtime/runs/$RUN_ID"
exec "${RUNNER[@]}" run "$SCENARIO" --run-id "$RUN_ID"
