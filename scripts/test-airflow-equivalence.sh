#!/usr/bin/env bash
#
# Airflow equivalence suite for PAYOPS_CROSS_BORDER_RECONCILIATION: the checks
# of docs/migration-design.md section 6, plus the loopback and pin checks.
#
#   structure     DAG loads: 12 tasks, 11 edges, retries, callback, commands (unit tests)
#   schedule      timetable is the 254 business days of 2026 at 20:00 Asia/Singapore
#   oracle        every fixture scenario, run by the scheduler, passes verify_run.py
#   equivalence   normalized Airflow output equals the compatibility harness output
#   retry         partial-ledger-write: one retry after 60s, read from Airflow's records
#   rerun         duplicate-retry: two DAG runs, identical passes, ledger rows [6, 6]
#   no-retry      other failures (bad scenario id; undeclared exit 1 in post_pending_ledger)
#                 fail after one try, log the mail, and stop downstream tasks
#   determinism   two Airflow runs with one run id give byte-identical stages and output
#   listeners     every Airflow listener is on 127.0.0.1:$PAYOPS_AIRFLOW_PORT
#   pin           the installed Airflow matches airflow-version.env and its constraints
#
# Usage: test-airflow-equivalence.sh [check ...]
#   With no check names, runs them all. Scenario runs are made once per
#   invocation and shared by the checks that grade them. Starts Airflow if a
#   check needs it and stops it again at the end if this script started it.
#   Details land in workspace/airflow-equivalence/.
#
# Exit codes:
#   0   every selected check passed
#   1   a check failed, or Airflow could not be started
#   2   usage error
#   78  Airflow is not installed (install it with scripts/airflow-up.sh)

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/airflow.sh
source "$KIT_ROOT/scripts/lib/airflow.sh"

ALL_CHECKS=(structure schedule oracle equivalence retry rerun no-retry determinism listeners pin)
SERVICE_CHECKS=" oracle equivalence retry rerun no-retry determinism listeners "
# Named by design section 6 checks 5, 6, and 8.
RETRY_SCENARIO="partial-ledger-write"
RERUN_SCENARIO="duplicate-retry"
DETERMINISM_SCENARIOS=(happy-path reconciliation-breaks partial-ledger-write)
NO_RETRY_SCENARIO="no-such-scenario"
NO_RETRY_FAILED_TASK="watch_inbound_files"
# watch_inbound_files has no retries, so a second case fails the one task that
# retries (post_pending_ledger, retries=2) with an undeclared exit 1, caused by
# a file that is not SQLite at the run's ledger path. It must not retry either.
NO_RETRY_LEDGER_SCENARIO="happy-path"
NO_RETRY_LEDGER_TASK="post_pending_ledger"
NO_RETRY_LEDGER_EXIT=1

usage() { sed -n '3,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

SELECTED=()
for argument in "$@"; do
  case "$argument" in
    -h|--help) usage; exit "$EXIT_OK" ;;
  esac
  if [[ " ${ALL_CHECKS[*]} " != *" $argument "* ]]; then
    err "unknown check '$argument'"
    usage
    exit "$EXIT_USAGE"
  fi
  SELECTED+=("$argument")
done
[ "${#SELECTED[@]}" -gt 0 ] || SELECTED=("${ALL_CHECKS[@]}")
selected() { [[ " ${SELECTED[*]} " == *" $1 "* ]]; }

if ! airflow_installed; then
  warn "Airflow is not installed under ${PAYOPS_AIRFLOW_HOME}; skipping the Airflow suite."
  warn "install and start it with scripts/airflow-up.sh"
  exit "$EXIT_UNAVAILABLE"
fi

HARNESS="$KIT_ROOT/runtimes/control-m/harness/harness.py"
VERIFY="$KIT_ROOT/runtimes/control-m/harness/verify_run.py"
TESTS_DIR="$KIT_ROOT/runtimes/airflow/tests"
RUN_ROOT="$KIT_ROOT/workspace/runtime/runs"
OUT_DIR="$KIT_ROOT/workspace/airflow-equivalence"
SNAPSHOTS="$OUT_DIR/listeners.jsonl"
WINDOWS="$OUT_DIR/run-windows.txt"
# Outputs embed the run id and the normalized view keeps file sizes, so the
# Airflow and harness run ids of the equivalence check have the same length.
RUN_PREFIX="afeq-airflow"
HARNESS_PREFIX="afeq-harness"
STARTED_AIRFLOW=0
SAMPLER_PID=""

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR" "$RUN_ROOT"

cleanup() {
  if [ -n "$SAMPLER_PID" ]; then
    kill "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
  if [ "$STARTED_AIRFLOW" -eq 1 ]; then
    log ""
    log "==> stopping the Airflow this suite started"
    "$KIT_ROOT/scripts/airflow-down.sh" || true
  fi
}
trap cleanup EXIT

checks_py() {
  python3 "$KIT_ROOT/runtimes/airflow/equivalence_checks.py" --kit-root "$KIT_ROOT" "$@"
}

# Run unittest modules; pass only when every collected test ran and passed.
run_unittests() {
  local label="$1" python="$2" pythonpath="$3"; shift 3
  local output="$OUT_DIR/unittest-$label.log" code=0
  (cd "$TESTS_DIR" && PYTHONPATH="$pythonpath" "$python" -m unittest -v "$@") >"$output" 2>&1 \
    || code=$?
  local ran status
  ran="$(sed -n 's/^Ran \([0-9]*\) tests\{0,1\} in .*/\1/p' "$output" | tail -n 1)"
  status="$(tail -n 1 "$output")"
  info "$(printf '%-30s %s tests, %s' "$label" "${ran:-0}" "$status")"
  if [ "$code" -ne 0 ] || [ "$status" != "OK" ] || [ "${ran:-0}" -eq 0 ]; then
    grep -E '^(FAIL|ERROR):|Error|skipped' "$output" | head -n 20 | sed 's/^/    /' >&2 || true
    err "$label: unit tests did not all pass (see ${output#"$KIT_ROOT/"})"
    return 1
  fi
}

airflow_unittests() {
  run_unittests "$1" "$AIRFLOW_PYTHON" "" "${@:2}"
}

stdlib_unittests() {
  run_unittests "$1" python3 \
    "$KIT_ROOT/runtimes/airflow:$KIT_ROOT/runtimes/control-m/harness" "${@:2}"
}

verify_run() {
  python3 "$VERIFY" --run-dir "$1" --kit-root "$KIT_ROOT"
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }

# One Airflow run per scenario per invocation, graded by the oracle once. The
# run directory is cleared first, so nothing from an earlier invocation can be
# graded if this run fails early. Each run's time window goes to WINDOWS for
# the listeners check.
airflow_run() {
  local scenario="$1" run_id="$RUN_PREFIX-$1"
  local status_file="$OUT_DIR/$run_id.status" code=0 started
  if [ -f "$status_file" ]; then
    return "$(cat "$status_file")"
  fi
  info "running $scenario on Airflow (run id $run_id)"
  rm -rf "${RUN_ROOT:?}/$run_id"
  started="$(utc_now)"
  "$KIT_ROOT/scripts/airflow-run.sh" "$scenario" --run-id "$run_id" \
    >"$OUT_DIR/$run_id.manifest.json" 2>"$OUT_DIR/$run_id.log" || code=$?
  printf '%s %s\n' "$started" "$(utc_now)" >>"$WINDOWS"
  if [ "$code" -ne 0 ]; then
    err "$scenario: airflow-run.sh exited $code"
    tail -n 15 "$OUT_DIR/$run_id.log" | sed 's/^/    /' >&2
  fi
  if ! verify_run "$RUN_ROOT/$run_id" >"$OUT_DIR/$run_id.verify.txt" 2>&1; then
    err "$scenario: the Airflow run did not pass verify_run.py"
    sed 's/^/    /' "$OUT_DIR/$run_id.verify.txt" | head -n 40 >&2
    code=1
  fi
  printf '%s' "$code" >"$status_file"
  return "$code"
}

# Sets OWED to every directory under fixtures/scenarios, the set the scenario
# checks owe. A directory without an oracle stays owed and fails verify_run.py.
load_owed_scenarios() {
  local scenario
  OWED=()
  while IFS= read -r scenario; do
    [ -n "$scenario" ] && OWED+=("$scenario")
  done < <(checks_py owed-scenarios)
  [ "${#OWED[@]}" -gt 0 ] || { err "found no scenarios under fixtures/scenarios"; return 1; }
}

# -- the checks -------------------------------------------------------------

check_structure() {
  local ok=0
  airflow_unittests "DAG structure" test_airflow_dag || ok=1
  airflow_unittests "task commands and outcomes" test_airflow_task || ok=1
  stdlib_unittests "runner" test_airflow_runner || ok=1
  stdlib_unittests "suite assertions" test_equivalence_checks || ok=1
  return "$ok"
}

check_schedule() {
  airflow_unittests "schedule" test_airflow_schedule
}

check_oracle() {
  local scenario passed=0 failed=0
  load_owed_scenarios || return 1
  info "owed scenarios: ${OWED[*]}"
  for scenario in "${OWED[@]}"; do
    if airflow_run "$scenario" \
        && checks_py labels --run-dir "$RUN_ROOT/$RUN_PREFIX-$scenario" >"$OUT_DIR/labels-$scenario.txt"; then
      info "$(printf '%-24s %s' "$scenario" "$(sed -n '2p' "$OUT_DIR/$RUN_PREFIX-$scenario.verify.txt" | sed 's/^ *//')")"
      passed=$((passed + 1))
    else
      sed 's/^/    /' "$OUT_DIR/labels-$scenario.txt" >&2 2>/dev/null || true
      err "$scenario: failed on Airflow"
      failed=$((failed + 1))
    fi
  done
  info "oracle: $passed/${#OWED[@]} scenarios passed"
  [ "$failed" -eq 0 ]
}

check_equivalence() {
  local scenario matched=0 harness_id airflow_dir harness_dir
  load_owed_scenarios || return 1
  for scenario in "${OWED[@]}"; do
    harness_id="$HARNESS_PREFIX-$scenario"
    airflow_dir="$RUN_ROOT/$RUN_PREFIX-$scenario"
    harness_dir="$RUN_ROOT/$harness_id"
    if ! airflow_run "$scenario"; then
      err "$scenario: no passing Airflow run to compare"
      continue
    fi
    if ! python3 "$HARNESS" --scenario "$scenario" --run-id "$harness_id" \
          --kit-root "$KIT_ROOT" --repos-root "$(resolve_repos_root)" --run-root "$RUN_ROOT" \
          >/dev/null 2>"$OUT_DIR/$harness_id.log"; then
      err "$scenario: the compatibility harness run did not complete"
      continue
    fi
    if ! python3 "$VERIFY" --run-dir "$airflow_dir" --kit-root "$KIT_ROOT" --normalize \
          >"$OUT_DIR/$scenario.airflow.normalized.json" \
        || ! python3 "$VERIFY" --run-dir "$harness_dir" --kit-root "$KIT_ROOT" --normalize \
          >"$OUT_DIR/$scenario.harness.normalized.json"; then
      err "$scenario: could not normalize both runs"
      continue
    fi
    if cmp -s "$OUT_DIR/$scenario.harness.normalized.json" "$OUT_DIR/$scenario.airflow.normalized.json"; then
      info "$(printf '%-24s normalized Airflow output identical to the harness' "$scenario")"
      matched=$((matched + 1))
    else
      diff -u "$OUT_DIR/$scenario.harness.normalized.json" "$OUT_DIR/$scenario.airflow.normalized.json" \
        >"$OUT_DIR/$scenario.normalized.diff" || true
      err "$scenario: normalized output differs from the harness"
      head -n 40 "$OUT_DIR/$scenario.normalized.diff" | sed 's/^/    /' >&2
    fi
  done
  info "equivalence: $matched/${#OWED[@]} scenarios matched"
  [ "$matched" -eq "${#OWED[@]}" ]
}

check_retry() {
  local scenario others=() ok=0
  load_owed_scenarios || return 1
  for scenario in "${OWED[@]}"; do
    airflow_run "$scenario" || ok=1
    [ "$scenario" = "$RETRY_SCENARIO" ] && continue
    others+=(--other-run-dir "$RUN_ROOT/$RUN_PREFIX-$scenario")
  done
  checks_py retry --run-dir "$RUN_ROOT/$RUN_PREFIX-$RETRY_SCENARIO" ${others[@]+"${others[@]}"} || ok=1
  return "$ok"
}

check_rerun() {
  local ok=0
  airflow_run "$RERUN_SCENARIO" || ok=1
  checks_py rerun --run-dir "$RUN_ROOT/$RUN_PREFIX-$RERUN_SCENARIO" || ok=1
  return "$ok"
}

seed_corrupt_ledger() {
  mkdir -p "$1/ledger"
  printf 'not a SQLite database: seeded by the no-retry check\n' >"$1/ledger/ledger.sqlite3"
}

# Trigger one DAG run that must fail at <failed task> after exactly one try.
# Optional: the exit code that try must record, and a function that seeds the
# run directory first.
no_retry_case() {
  local label="$1" scenario="$2" failed_task="$3" exit_code="${4:-}" seed="${5:-}"
  local run_id="$RUN_PREFIX-$label" dag_run_id code=0 ok=0
  dag_run_id="${run_id}__$(date -u +%Y%m%dT%H%M%SZ)"
  rm -rf "${RUN_ROOT:?}/$run_id"
  [ -z "$seed" ] || "$seed" "$RUN_ROOT/$run_id"
  info "triggering DAG run $dag_run_id (scenario $scenario); $failed_task must fail once"
  "$KIT_ROOT/scripts/airflow-run.sh" --trigger \
      "{\"scenario\": \"$scenario\", \"run_id\": \"$run_id\"}" --dag-run-id "$dag_run_id" \
      >"$OUT_DIR/$label.json" 2>"$OUT_DIR/$label.log" || code=$?
  if [ "$code" -ne 1 ]; then
    err "airflow-run.sh --trigger exited $code, want 1 (the DAG run failed)"
    tail -n 15 "$OUT_DIR/$label.log" | sed 's/^/    /' >&2
    ok=1
  fi
  checks_py no-retry --dag-run-id "$dag_run_id" --failed-task "$failed_task" \
    --run-id "$run_id" --scenario "$scenario" ${exit_code:+--exit-code "$exit_code"} || ok=1
  return "$ok"
}

check_no_retry() {
  local ok=0
  no_retry_case no-retry "$NO_RETRY_SCENARIO" "$NO_RETRY_FAILED_TASK" || ok=1
  no_retry_case no-retry-ledger "$NO_RETRY_LEDGER_SCENARIO" "$NO_RETRY_LEDGER_TASK" \
    "$NO_RETRY_LEDGER_EXIT" seed_corrupt_ledger || ok=1
  airflow_unittests "exit code classification" test_airflow_task.TestClassify || ok=1
  airflow_unittests "task outcomes" test_airflow_task.TestRunPayopsTask || ok=1
  return "$ok"
}

check_determinism() {
  local scenario run_id staging ok=0
  for scenario in "${DETERMINISM_SCENARIOS[@]}"; do
    run_id="$RUN_PREFIX-$scenario"
    staging="$OUT_DIR/determinism/$scenario"
    mkdir -p "$staging"
    if ! airflow_run "$scenario"; then
      err "$scenario: first run failed"
      ok=1
      continue
    fi
    cp -R "$RUN_ROOT/$run_id/stages" "$staging/a-stages"
    cp -R "$RUN_ROOT/$run_id/output" "$staging/a-output"
    info "running $scenario on Airflow again with run id $run_id"
    if ! "$KIT_ROOT/scripts/airflow-run.sh" "$scenario" --run-id "$run_id" \
          >"$staging/b.manifest.json" 2>"$staging/b.log" \
        || ! verify_run "$RUN_ROOT/$run_id" >"$staging/b.verify.txt" 2>&1; then
      err "$scenario: second run failed or did not pass verify_run.py"
      ok=1
      continue
    fi
    if diff -r "$staging/a-stages" "$RUN_ROOT/$run_id/stages" >"$staging/stages.diff" 2>&1 \
        && diff -r "$staging/a-output" "$RUN_ROOT/$run_id/output" >"$staging/output.diff" 2>&1; then
      info "$(printf '%-24s stages/ and output/ byte-identical across two Airflow runs' "$scenario")"
    else
      err "$scenario: Airflow reruns are not byte-identical"
      cat "$staging/stages.diff" "$staging/output.diff" | head -n 30 | sed 's/^/    /' >&2
      ok=1
    fi
  done
  return "$ok"
}

check_listeners() {
  # The sampler has run since Airflow started; make sure a scenario ran under it.
  if ! compgen -G "$OUT_DIR/*.status" >/dev/null; then
    airflow_run happy-path || true
  fi
  checks_py listeners-sample --out "$SNAPSHOTS"
  checks_py listeners --snapshots "$SNAPSHOTS" --windows "$WINDOWS"
}

check_pin() {
  checks_py pin
}

# -- main -------------------------------------------------------------------

start_listener_sampler() {
  (
    while :; do
      checks_py listeners-sample --out "$SNAPSHOTS" >/dev/null 2>&1 || true
      sleep 2
    done
  ) &
  SAMPLER_PID=$!
}

needs_services=0
for check in "${SELECTED[@]}"; do
  [[ "$SERVICE_CHECKS" == *" $check "* ]] && needs_services=1
done
if [ "$needs_services" -eq 1 ]; then
  if ! airflow_running; then
    selected listeners && start_listener_sampler
    STARTED_AIRFLOW=1
    "$KIT_ROOT/scripts/airflow-up.sh" || { err "could not start Airflow"; exit "$EXIT_FAILED"; }
  elif selected listeners; then
    start_listener_sampler
  fi
fi

RESULTS=()
FAILURES=0
for check in "${ALL_CHECKS[@]}"; do
  selected "$check" || continue
  log ""
  log "==> $check"
  started=$SECONDS
  code=0
  "check_${check//-/_}" || code=$?
  if [ "$code" -eq 0 ]; then
    status="pass"
  else
    status="FAIL"
    FAILURES=$((FAILURES + 1))
  fi
  RESULTS+=("$(printf '%-14s %-6s %ss' "$check" "$status" "$((SECONDS - started))")")
done

heading "Airflow equivalence summary (Airflow $AIRFLOW_VERSION, mode airflow, not Control-M)"
for line in "${RESULTS[@]}"; do info "$line"; done
info "details: ${OUT_DIR#"$KIT_ROOT/"}/"
if [ "$FAILURES" -eq 0 ]; then
  info "all ${#RESULTS[@]} selected check(s) passed."
  exit "$EXIT_OK"
fi
err "$FAILURES of ${#RESULTS[@]} check(s) failed."
exit "$EXIT_FAILED"
