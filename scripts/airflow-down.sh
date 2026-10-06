#!/usr/bin/env bash
#
# Stop the local Apache Airflow runtime started by scripts/airflow-up.sh.
#
# Sends TERM to each component's process group, waits up to 30 seconds, then
# sends KILL to anything left. Leaves the venv, metadata DB, and logs in place.
# Safe to run when nothing is running.
#
# Exit codes:
#   0   no Airflow component is running
#   1   a component could not be stopped
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/airflow.sh
source "$KIT_ROOT/scripts/lib/airflow.sh"

case "${1:-}" in
  "") ;;
  -h|--help) sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "$EXIT_OK" ;;
  *) err "unexpected argument '$1'"; exit "$EXIT_USAGE" ;;
esac

STOP_TIMEOUT_SECONDS=30

group_alive() {
  # kill -0 on a negative pid probes the whole process group.
  kill -0 -- "-$1" 2>/dev/null
}

PIDS=()
for component in "${AIRFLOW_COMPONENTS[@]}"; do
  pid_file="$AIRFLOW_PID_DIR/$component.pid"
  [ -f "$pid_file" ] || continue
  pid="$(cat "$pid_file")"
  if [ -n "$pid" ] && group_alive "$pid"; then
    info "stopping $component (process group $pid)"
    kill -TERM -- "-$pid" 2>/dev/null || true
    PIDS+=("$pid")
  fi
done

deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
for pid in "${PIDS[@]}"; do
  while group_alive "$pid" && [ "$SECONDS" -lt "$deadline" ]; do
    sleep 1
  done
  if group_alive "$pid"; then
    warn "process group $pid did not stop within ${STOP_TIMEOUT_SECONDS}s; sending KILL"
    kill -KILL -- "-$pid" 2>/dev/null || true
    sleep 1
  fi
done

FAILED=0
for component in "${AIRFLOW_COMPONENTS[@]}"; do
  pid_file="$AIRFLOW_PID_DIR/$component.pid"
  [ -f "$pid_file" ] || continue
  pid="$(cat "$pid_file")"
  if [ -n "$pid" ] && group_alive "$pid"; then
    err "$component (process group $pid) is still running"
    FAILED=1
  else
    rm -f "$pid_file"
  fi
done

if [ "$FAILED" -ne 0 ]; then
  exit "$EXIT_FAILED"
fi
info "Airflow is stopped."
exit "$EXIT_OK"
