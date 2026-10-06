#!/usr/bin/env bash
#
# Start the local Apache Airflow runtime for PAYOPS_CROSS_BORDER_RECONCILIATION.
#
# On first use, installs the exact pin from runtimes/airflow/airflow-version.env
# into a virtual environment under workspace/airflow/ (git-ignored), using the
# official constraints file. Then starts the four components one by one, each
# in its own process group, with every listener on 127.0.0.1 (design
# Decision 17):
#
#   airflow scheduler --skip-serve-logs
#   airflow dag-processor
#   airflow api-server --host 127.0.0.1 --port $PAYOPS_AIRFLOW_PORT
#   airflow triggerer --skip-serve-logs
#
# Waits until the API server is healthy and the DAG is registered with no
# import errors. Does nothing if Airflow is already running.
#
# Usage: airflow-up.sh [--install-only]
#   --install-only   install the pin if needed, but start nothing
#
# Exit codes:
#   0   Airflow is installed (and running, unless --install-only)
#   1   Airflow failed to start, or the DAG did not load
#   2   usage error
#   78  Airflow is not installed and cannot be installed here

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/airflow.sh
source "$KIT_ROOT/scripts/lib/airflow.sh"

INSTALL_ONLY=0
case "${1:-}" in
  "") ;;
  --install-only) INSTALL_ONLY=1 ;;
  -h|--help) sed -n '3,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "$EXIT_OK" ;;
  *) err "unexpected argument '$1'"; exit "$EXIT_USAGE" ;;
esac

START_TIMEOUT_SECONDS=180

install_airflow() {
  local python="python$AIRFLOW_PYTHON_VERSION"
  heading "Installing apache-airflow==$AIRFLOW_VERSION (Python $AIRFLOW_PYTHON_VERSION)"
  if ! have "$python"; then
    err "$python is required to install Airflow $AIRFLOW_VERSION"
    return "$EXIT_UNAVAILABLE"
  fi
  mkdir -p "$PAYOPS_AIRFLOW_HOME"
  info "venv:        ${AIRFLOW_VENV#"$KIT_ROOT/"}"
  info "constraints: $AIRFLOW_CONSTRAINTS_URL"
  if ! curl -fsSL --retry 3 -o "$AIRFLOW_CONSTRAINTS_FILE.tmp" "$AIRFLOW_CONSTRAINTS_URL"; then
    rm -f "$AIRFLOW_CONSTRAINTS_FILE.tmp"
    err "could not download the constraints file"
    return "$EXIT_UNAVAILABLE"
  fi
  mv "$AIRFLOW_CONSTRAINTS_FILE.tmp" "$AIRFLOW_CONSTRAINTS_FILE"
  if [ ! -x "$AIRFLOW_PYTHON" ] && ! "$python" -m venv "$AIRFLOW_VENV"; then
    err "could not create $AIRFLOW_VENV"
    return "$EXIT_UNAVAILABLE"
  fi
  if ! "$AIRFLOW_PYTHON" -m pip install --quiet --disable-pip-version-check \
        "apache-airflow==$AIRFLOW_VERSION" --constraint "$AIRFLOW_CONSTRAINTS_FILE"; then
    err "pip could not install apache-airflow==$AIRFLOW_VERSION"
    return "$EXIT_UNAVAILABLE"
  fi
  printf 'apache-airflow==%s python%s\n' "$AIRFLOW_VERSION" "$AIRFLOW_PYTHON_VERSION" \
    >"$AIRFLOW_INSTALLED_MARKER"
  info "installed $("$AIRFLOW_BIN" version 2>/dev/null)"
}

start_component() {
  local name="$1"; shift
  # setsid gives each component its own process group, so airflow-down.sh can
  # stop the component together with every worker it forked.
  setsid "$AIRFLOW_BIN" "$@" >"$AIRFLOW_PID_DIR/$name.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"$AIRFLOW_PID_DIR/$name.pid"
  info "$(printf '%-14s pid %s, log %s' "$name" "$!" "${AIRFLOW_PID_DIR#"$KIT_ROOT/"}/$name.log")"
}

wait_until() {
  local description="$1"; shift
  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
  until "$@"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      err "timed out after ${START_TIMEOUT_SECONDS}s waiting for $description"
      return 1
    fi
    if ! airflow_running; then
      err "an Airflow component exited while waiting for $description"
      return 1
    fi
    sleep 2
  done
}

dag_ready() {
  airflow_state dag-ready "$PAYOPS_DAG_ID" >/dev/null 2>&1
}

report_start_failure() {
  local component
  for component in "${AIRFLOW_COMPONENTS[@]}"; do
    [ -f "$AIRFLOW_PID_DIR/$component.log" ] || continue
    err "last lines of $component.log:"
    tail -n 15 "$AIRFLOW_PID_DIR/$component.log" | sed 's/^/    /' >&2
  done
  airflow_state dag-ready "$PAYOPS_DAG_ID" >&2 || true
}

if ! airflow_installed; then
  install_airflow || exit $?
fi
[ "$INSTALL_ONLY" -eq 1 ] && exit "$EXIT_OK"

if airflow_running; then
  info "Airflow is already running at $AIRFLOW_URL"
  exit "$EXIT_OK"
fi
if airflow_partly_running; then
  warn "some Airflow components are running without the others; stopping them first"
  "$KIT_ROOT/scripts/airflow-down.sh" || exit "$EXIT_FAILED"
fi
if port_in_use; then
  err "port $PAYOPS_AIRFLOW_PORT on 127.0.0.1 is already in use; set PAYOPS_AIRFLOW_PORT"
  exit "$EXIT_FAILED"
fi

heading "Starting Airflow $AIRFLOW_VERSION"
mkdir -p "$AIRFLOW_PID_DIR"
if ! "$AIRFLOW_BIN" db migrate >"$AIRFLOW_PID_DIR/db-migrate.log" 2>&1; then
  err "airflow db migrate failed; see ${AIRFLOW_PID_DIR#"$KIT_ROOT/"}/db-migrate.log"
  exit "$EXIT_FAILED"
fi

start_component scheduler scheduler --skip-serve-logs
start_component dag-processor dag-processor
start_component api-server api-server --host 127.0.0.1 --port "$PAYOPS_AIRFLOW_PORT"
start_component triggerer triggerer --skip-serve-logs

if ! wait_until "the API server health check" airflow_healthy \
    || ! wait_until "DAG $PAYOPS_DAG_ID to load" dag_ready; then
  report_start_failure
  "$KIT_ROOT/scripts/airflow-down.sh" || true
  exit "$EXIT_FAILED"
fi

info "Airflow is running."
info "UI and API:     $AIRFLOW_URL (bound to 127.0.0.1 only)"
info "login:          user admin, password in ${AIRFLOW_PASSWORD_FILE#"$KIT_ROOT/"}"
info "DAG:            $PAYOPS_DAG_ID (paused; scripts/airflow-run.sh unpauses it only while it runs)"
info "stop with:      scripts/airflow-down.sh"
exit "$EXIT_OK"
