# shellcheck shell=bash
# Shared helpers for the estate scripts. Source this, do not execute it.

set -euo pipefail

# Stable exit codes used across the scripts.
readonly EXIT_OK=0
readonly EXIT_FAILED=1
readonly EXIT_USAGE=2
readonly EXIT_UNAVAILABLE=78   # a required runtime or prerequisite is unavailable

kit_root() {
  # Resolve the estate root from this library's location, so scripts work from
  # any working directory.
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$lib_dir/../.." && pwd
}

# The location of these scripts is authoritative. KIT_ROOT is exported so that
# scripts calling each other agree, but an inherited value from a different tree
# must never win: that would make a script silently operate on another checkout.
_computed_kit_root="$(kit_root)"
if [ -n "${KIT_ROOT:-}" ] && [ "$KIT_ROOT" != "$_computed_kit_root" ]; then
  printf 'warning: ignoring inherited KIT_ROOT=%s; these scripts belong to %s\n' \
    "$KIT_ROOT" "$_computed_kit_root" >&2
fi
KIT_ROOT="$_computed_kit_root"
unset _computed_kit_root
export KIT_ROOT

log()  { printf '%s\n' "$*" >&2; }
info() { printf '  %s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
err()  { printf 'error: %s\n' "$*" >&2; }

heading() {
  printf '\n%s\n' "$*" >&2
  printf '%s\n' "$(printf '%*s' "${#1}" '' | tr ' ' '-')" >&2
}

have() { command -v "$1" >/dev/null 2>&1; }

# Resolve a working Docker Compose invocation. The plugin form and the
# standalone binary are both acceptable; nothing else is.
compose_command() {
  if docker compose version >/dev/null 2>&1; then
    printf 'docker compose'
    return 0
  fi
  if have docker-compose; then
    printf 'docker-compose'
    return 0
  fi
  return 1
}

docker_ready() {
  have docker && docker info >/dev/null 2>&1
}

# Where the service repositories live. PAYOPS_REPOS_ROOT overrides it.
resolve_repos_root() {
  if [ -n "${PAYOPS_REPOS_ROOT:-}" ]; then
    printf '%s' "$PAYOPS_REPOS_ROOT"
  else
    printf '%s' "$KIT_ROOT/repos"
  fi
}

RUNTIME_STATE_DIR="$KIT_ROOT/workspace/runtime/control-m"
SELECTED_RUNTIME_FILE="$RUNTIME_STATE_DIR/selected-runtime.json"
WORKBENCH_PROBE_FILE="$RUNTIME_STATE_DIR/workbench-probe.json"

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

require_scenario() {
  local scenario="$1"
  if [ ! -d "$KIT_ROOT/fixtures/scenarios/$scenario" ]; then
    err "unknown scenario '$scenario'"
    info "available scenarios:"
    local dir
    for dir in "$KIT_ROOT"/fixtures/scenarios/*/; do
      [ -d "$dir" ] && info "  - $(basename "$dir")"
    done
    return "$EXIT_USAGE"
  fi
}
