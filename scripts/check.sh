#!/usr/bin/env bash
#
# Run every check for the estate, in order:
#
#   1. prerequisite check         scripts/check-prereqs.sh
#   2. file size limits           scripts/check-file-sizes.sh
#   3. lint, format, types        scripts/lint.sh (skipped if dev tools are unavailable)
#   4. Control-M validation       scripts/controlm-validate.sh
#   5. every repository's tests   repos/*/scripts/test.sh
#   6. coverage thresholds        scripts/coverage.sh
#   7. compatibility test         scripts/test-controlm-compatibility.sh
#   8. Airflow equivalence        scripts/test-airflow-equivalence.sh (skipped if
#                                 Airflow is not installed; see runtimes/airflow/README.md)
#
# If the prerequisite check fails, nothing else runs. Otherwise every step runs
# even when an earlier one fails, so one invocation reports every failure.
#
# Exit codes:
#   0   every check passed (a skipped lint or Airflow step still counts as passing)
#   1   one or more checks failed
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

if [ $# -gt 0 ]; then
  case "$1" in
    -h|--help) sed -n '3,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "$EXIT_OK" ;;
    *) err "unexpected argument '$1'"; exit "$EXIT_USAGE" ;;
  esac
fi

REPOS_ROOT="$(resolve_repos_root)"
RESULTS=()
FAILURES=0

run_step() {
  local label="$1"; shift
  local started status
  started=$SECONDS
  log ""
  log "==> $label"
  local code=0
  "$@" || code=$?
  if [ "$code" -eq 0 ]; then
    status="pass"
  elif [ "$code" -eq "$EXIT_UNAVAILABLE" ] && [ "${ALLOW_SKIP:-0}" = "1" ]; then
    status="skipped"
  else
    status="FAIL (exit $code)"
    FAILURES=$((FAILURES + 1))
  fi
  RESULTS+=("$(printf '%-42s %-14s %ss' "$label" "$status" "$((SECONDS - started))")")
  [ "$status" != "FAIL (exit $code)" ]
}

summary() {
  heading "Summary"
  local line
  for line in "${RESULTS[@]}"; do info "$line"; done
}

if ! run_step "prerequisites" "$KIT_ROOT/scripts/check-prereqs.sh"; then
  summary
  err "required prerequisites are missing; skipped the remaining checks."
  exit "$EXIT_FAILED"
fi

run_step "file size limits" "$KIT_ROOT/scripts/check-file-sizes.sh" || true
ALLOW_SKIP=1 run_step "lint, format, types" "$KIT_ROOT/scripts/lint.sh" || true
run_step "Control-M validation" "$KIT_ROOT/scripts/controlm-validate.sh" || true

found_tests=0
for test_script in "$REPOS_ROOT"/*/scripts/test.sh; do
  [ -f "$test_script" ] || continue
  found_tests=$((found_tests + 1))
  repo="$(basename "$(dirname "$(dirname "$test_script")")")"
  run_step "tests: $repo" bash "$test_script" || true
done
if [ "$found_tests" -eq 0 ]; then
  err "no repository test scripts found under $REPOS_ROOT"
  RESULTS+=("$(printf '%-42s %s' "repository tests" "FAIL (none found)")")
  FAILURES=$((FAILURES + 1))
fi

run_step "coverage thresholds" "$KIT_ROOT/scripts/coverage.sh" || true
run_step "Control-M compatibility test" "$KIT_ROOT/scripts/test-controlm-compatibility.sh" || true
ALLOW_SKIP=1 run_step "Airflow equivalence" "$KIT_ROOT/scripts/test-airflow-equivalence.sh" || true

summary
if [ "$FAILURES" -eq 0 ]; then
  info "all checks passed."
  exit "$EXIT_OK"
fi
err "$FAILURES check(s) failed."
exit "$EXIT_FAILED"
