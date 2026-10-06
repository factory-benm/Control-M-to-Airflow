# shellcheck shell=bash
# Shared settings and helpers for the local Apache Airflow runtime. Source this
# after lib/common.sh, do not execute it. See runtimes/airflow/README.md.
#
# Overridable:
#   PAYOPS_AIRFLOW_HOME   AIRFLOW_HOME, venv, metadata DB, logs, generated
#                         admin password (default workspace/airflow, git-ignored)
#   PAYOPS_AIRFLOW_PORT   API server port on 127.0.0.1 (default 8080)

# shellcheck source=../../runtimes/airflow/airflow-version.env
source "$KIT_ROOT/runtimes/airflow/airflow-version.env"

readonly PAYOPS_DAG_ID="PAYOPS_CROSS_BORDER_RECONCILIATION"
readonly AIRFLOW_COMPONENTS=(scheduler dag-processor api-server triggerer)

PAYOPS_AIRFLOW_HOME="${PAYOPS_AIRFLOW_HOME:-$KIT_ROOT/workspace/airflow}"
PAYOPS_AIRFLOW_PORT="${PAYOPS_AIRFLOW_PORT:-8080}"
AIRFLOW_VENV="$PAYOPS_AIRFLOW_HOME/venv"
AIRFLOW_BIN="$AIRFLOW_VENV/bin/airflow"
AIRFLOW_PYTHON="$AIRFLOW_VENV/bin/python"
AIRFLOW_INSTALLED_MARKER="$AIRFLOW_VENV/.payops-installed"
AIRFLOW_CONSTRAINTS_FILE="$PAYOPS_AIRFLOW_HOME/constraints-$AIRFLOW_VERSION-$AIRFLOW_PYTHON_VERSION.txt"
AIRFLOW_PID_DIR="$PAYOPS_AIRFLOW_HOME/run"
AIRFLOW_URL="http://127.0.0.1:$PAYOPS_AIRFLOW_PORT"
AIRFLOW_PASSWORD_FILE="$PAYOPS_AIRFLOW_HOME/simple_auth_manager_passwords.json.generated"
export PAYOPS_AIRFLOW_HOME PAYOPS_AIRFLOW_PORT PAYOPS_DAG_ID

# Everything Airflow reads comes from here, so every component, CLI call, and
# helper sees the same configuration. Every listener stays on 127.0.0.1
# (design Decision 17): the API server is bound explicitly, the scheduler and
# triggerer start with --skip-serve-logs, and the health check server is off.
export AIRFLOW_HOME="$PAYOPS_AIRFLOW_HOME"
export AIRFLOW__CORE__DAGS_FOLDER="$KIT_ROOT/runtimes/airflow/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
export AIRFLOW__CORE__AUTH_MANAGER=airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=admin:admin
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE="$AIRFLOW_PASSWORD_FILE"
export AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=True
export AIRFLOW__API__HOST=127.0.0.1
export AIRFLOW__API__PORT="$PAYOPS_AIRFLOW_PORT"
export AIRFLOW__API__BASE_URL="$AIRFLOW_URL"
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="$AIRFLOW_URL/execution/"
export AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK=False
# Re-parse the DAG file within seconds of an edit instead of the defaults.
export AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL=10
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=30

airflow_installed() {
  [ -x "$AIRFLOW_BIN" ] && [ -f "$AIRFLOW_INSTALLED_MARKER" ]
}

# Print the pid recorded for a component if that process is still alive.
component_pid() {
  local pid_file="$AIRFLOW_PID_DIR/$1.pid" pid
  [ -f "$pid_file" ] || return 1
  pid="$(cat "$pid_file")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  printf '%s' "$pid"
}

# 0 when every component is alive.
airflow_running() {
  local component
  for component in "${AIRFLOW_COMPONENTS[@]}"; do
    component_pid "$component" >/dev/null || return 1
  done
}

# 0 when any component is alive.
airflow_partly_running() {
  local component
  for component in "${AIRFLOW_COMPONENTS[@]}"; do
    component_pid "$component" >/dev/null && return 0
  done
  return 1
}

airflow_healthy() {
  python3 - "$AIRFLOW_URL/api/v2/monitor/health" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    sys.exit(0 if response.status == 200 else 1)
PY
}

port_in_use() {
  python3 - "$PAYOPS_AIRFLOW_PORT" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.socket() as probe:
    probe.settimeout(1)
    sys.exit(0 if probe.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

# Read-only queries against the Airflow metadata database.
airflow_state() {
  "$AIRFLOW_PYTHON" "$KIT_ROOT/runtimes/airflow/airflow_state.py" "$@"
}
