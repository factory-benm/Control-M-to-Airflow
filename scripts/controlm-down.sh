#!/usr/bin/env bash
#
# Stop the Control-M runtime.
#
# Removes the Workbench container if it is running. The compatibility harness
# has no long-running process, so there is nothing to stop in harness mode.
# Run directories and recorded evidence are never deleted here.
#
# Exit codes:
#   0   the runtime is down
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
source "$KIT_ROOT/runtimes/control-m/workbench/workbench.env"

[ $# -eq 0 ] || { err "unexpected argument '$1'"; exit "$EXIT_USAGE"; }

heading "Stopping the Control-M runtime"

if have docker && docker info >/dev/null 2>&1; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$WORKBENCH_CONTAINER"; then
    info "removing container $WORKBENCH_CONTAINER"
    docker rm -f "$WORKBENCH_CONTAINER" >/dev/null
    info "removed."
  else
    info "no Workbench container named $WORKBENCH_CONTAINER is present."
  fi
else
  info "docker is not available, so no Workbench container can be running."
fi

info "the compatibility harness runs in-process and needs no teardown."
info "run evidence under workspace/runtime/runs is preserved."
exit "$EXIT_OK"
