#!/usr/bin/env bash
#
# Validate the Control-M workflow definitions.
#
# When Workbench is the selected runtime, this runs BMC's own 'ctm build', which
# is real Control-M validation. Otherwise it runs structural validation: the
# three definition artifacts (Automation API JSON, legacy XML export, and the
# task-command mapping) must describe exactly the same twelve-task graph.
#
# Structural validation is not equivalent to 'ctm build' and is labelled as
# such in the output and in the recorded result.
#
# Exit codes:
#   0   definitions validated under the reported method
#   1   validation failed
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
source "$KIT_ROOT/runtimes/control-m/workbench/workbench.env"

[ $# -eq 0 ] || { err "unexpected argument '$1'"; exit "$EXIT_USAGE"; }

REPOS_ROOT="$(resolve_repos_root)"
CONTROLM_DIR="$REPOS_ROOT/payments-orchestrator/controlm"

if [ ! -d "$CONTROLM_DIR" ]; then
  err "no Control-M definitions found at $CONTROLM_DIR"
  exit "$EXIT_FAILED"
fi

MODE="compatibility-harness"
if [ -f "$SELECTED_RUNTIME_FILE" ]; then
  MODE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["mode"])' "$SELECTED_RUNTIME_FILE")"
fi

heading "Validating Control-M definitions (runtime: $MODE)"
info "definitions: ${CONTROLM_DIR#"$KIT_ROOT/"}"

METHOD=""
RESULT=""

if [ "$MODE" = "control-m-workbench" ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$WORKBENCH_CONTAINER"; then
  METHOD="ctm-build"
  info "running 'ctm build' inside $WORKBENCH_CONTAINER"
  if docker exec "$WORKBENCH_CONTAINER" ctm build /definitions/payments_reconciliation.json >&2; then
    RESULT="pass"
  else
    RESULT="fail"
  fi
else
  METHOD="structural-agreement"
  info "Workbench is not the active runtime, so 'ctm build' is unavailable."
  info "Running structural validation instead."
  if PYTHONPATH="$REPOS_ROOT/payments-orchestrator/src" python3 - "$CONTROLM_DIR" <<'PY'
import sys
from pathlib import Path
from orchestrator.graph import agreement_problems, canonical_graph

controlm = Path(sys.argv[1])
problems = agreement_problems(controlm)
graph = canonical_graph(controlm)

print(f"  tasks: {len(graph.tasks)} (expected 12)")
print(f"  edges: {len(graph.edges)} (expected 11)")
order = graph.topological_order()
print(f"  acyclic: yes, topological order resolved ({len(order)} tasks)")

if len(graph.tasks) != 12:
    problems.append(f"expected 12 tasks, found {len(graph.tasks)}")
if problems:
    print("\n  validation problems:")
    for problem in problems:
        print(f"    - {problem}")
    sys.exit(1)
print("  all three definition artifacts agree.")
PY
  then
    RESULT="pass"
  else
    RESULT="fail"
  fi
fi

mkdir -p "$RUNTIME_STATE_DIR"
python3 - "$RUNTIME_STATE_DIR/validation.json" <<PY
import json, sys
payload = {
    "method": "$METHOD",
    "methodLabel": (
        "BMC Control-M 'ctm build' executed inside Workbench"
        if "$METHOD" == "ctm-build"
        else "Structural agreement check across the JSON, XML, and mapping artifacts. "
             "This is not Control-M validation."
    ),
    "isControlMValidation": "$METHOD" == "ctm-build",
    "runtimeMode": "$MODE",
    "result": "$RESULT",
    "validatedAt": "$(utc_now)",
    "provenance": "observed-run",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

if [ "$RESULT" = "pass" ]; then
  heading "Validation passed (method: $METHOD)"
  [ "$METHOD" = "structural-agreement" ] &&
    info "Reminder: this did not run Control-M's own parser."
  exit "$EXIT_OK"
fi

err "validation failed (method: $METHOD)"
exit "$EXIT_FAILED"
