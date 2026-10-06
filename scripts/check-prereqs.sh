#!/usr/bin/env bash
#
# Check the prerequisites the legacy estate actually needs, and report clearly on the
# ones it merely prefers.
#
# Required:   git, python3 (3.11 or newer), bash, sqlite3 module
# Preferred:  docker + a compose command (only needed for Control-M Workbench)
# Not needed: Java. The implementation deliberately avoids it.
#
# Exit codes:
#   0   all required prerequisites are present
#   1   a required prerequisite is missing
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

[ $# -eq 0 ] || { err "unexpected argument '$1'"; exit "$EXIT_USAGE"; }

MISSING=0
require() {
  local label="$1" command="$2" hint="$3"
  if have "$command"; then
    info "$(printf '%-22s %s' "$label" "present ($(command -v "$command"))")"
  else
    err "$(printf '%-22s %s' "$label" "MISSING")"
    info "  $hint"
    MISSING=$((MISSING + 1))
  fi
}

heading "Required"
require "git"      git      "Install Git, then re-run."
require "python3"  python3  "Install Python 3.11 or newer, then re-run."
require "bash"     bash     "These scripts need bash."

if have python3; then
  PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info[:2] >= (3, 11) else 0)')"
  PY_VERSION="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  if [ "$PY_OK" = "1" ]; then
    info "$(printf '%-22s %s' "python3 version" "$PY_VERSION")"
  else
    err "$(printf '%-22s %s' "python3 version" "$PY_VERSION is too old; 3.11 or newer is required")"
    MISSING=$((MISSING + 1))
  fi
  if python3 -c 'import sqlite3' 2>/dev/null; then
    info "$(printf '%-22s %s' "python3 sqlite3" "present")"
  else
    err "$(printf '%-22s %s' "python3 sqlite3" "MISSING (the ledger needs it)")"
    MISSING=$((MISSING + 1))
  fi
  # The estate is standard-library only by design. Say so, so nobody hunts for
  # a requirements file that does not exist.
  info "$(printf '%-22s %s' "python packages" "none required; the estate is standard-library only")"
fi

heading "Preferred (Control-M Workbench only)"
if docker_ready; then
  DOCKER_SERVER="$(docker info --format '{{.ServerVersion}}' 2>/dev/null || echo unknown)"
  DOCKER_ARCH="$(docker info --format '{{.Architecture}}' 2>/dev/null || echo unknown)"
  info "$(printf '%-22s %s' "docker daemon" "reachable (server $DOCKER_SERVER, $DOCKER_ARCH)")"
  if COMPOSE="$(compose_command)"; then
    info "$(printf '%-22s %s' "compose" "$COMPOSE")"
  else
    warn "no docker compose plugin and no docker-compose binary found."
    info "  Not required: the legacy runtime does not use Compose."
  fi
else
  warn "docker is not available."
  info "  Only needed to attempt BMC Control-M Workbench. The compatibility"
  info "  harness runs without it, and scripts/controlm-up.sh will say so."
fi

heading "Not required"
if have java; then
  info "$(printf '%-22s %s' "java" "present, but unused by this estate")"
else
  info "$(printf '%-22s %s' "java" "absent, which is fine; this estate does not use Java")"
fi

heading "Result"
if [ "$MISSING" -eq 0 ]; then
  info "all required prerequisites are present."
  exit "$EXIT_OK"
fi
err "$MISSING required prerequisite(s) missing."
exit "$EXIT_FAILED"
