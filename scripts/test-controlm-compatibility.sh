#!/usr/bin/env bash
#
# Control-M compatibility contract.
#
# Three things must hold:
#
#   1. The three Control-M definition artifacts describe the same workflow.
#      The Automation API JSON, the legacy XML export, and the task-command
#      mapping must agree on tasks, dependencies, and retry limits. If they can
#      drift, the runtime is executing something the scheduler does not declare.
#
#   2. Every scenario reproduces its fixture oracle. The harness records what
#      happened; the fixture decides whether that was correct.
#
#   3. Re-running a scenario produces equivalent normalized output. Volatile
#      fields are replaced with placeholders rather than deleted, so structural
#      drift is still detected.
#
# Exit codes:
#   0   the compatibility contract holds
#   1   one or more checks failed
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

[ $# -eq 0 ] || { err "unexpected argument '$1'"; exit "$EXIT_USAGE"; }

REPOS_ROOT="$(resolve_repos_root)"
HARNESS="$KIT_ROOT/runtimes/control-m/harness/harness.py"
VERIFY="$KIT_ROOT/runtimes/control-m/harness/verify_run.py"
RUN_ROOT="$KIT_ROOT/workspace/runtime/runs"
STDERR_DIR="$KIT_ROOT/workspace/runtime/contract"
SCENARIOS=(happy-path duplicate-retry business-cutoff partial-ledger-write reconciliation-breaks)

# These directories may not exist on a clean checkout, and a redirection into a
# missing directory fails before the harness is ever invoked.
mkdir -p "$RUN_ROOT" "$STDERR_DIR"

FAILURES=0
note_failure() { FAILURES=$((FAILURES + 1)); err "$1"; }

heading "1. Control-M definition agreement"
info "repositories: ${REPOS_ROOT#"$KIT_ROOT/"}"
if PYTHONPATH="$REPOS_ROOT/payments-orchestrator/src" python3 - "$REPOS_ROOT/payments-orchestrator/controlm" <<'PY'
import sys
from pathlib import Path
from orchestrator.graph import all_graphs, agreement_problems

controlm = Path(sys.argv[1])
graphs = all_graphs(controlm)
for graph in graphs:
    print(f"  {graph.source:<40} tasks={len(graph.tasks):>3} edges={len(graph.edges):>3}")

problems = agreement_problems(controlm)
if len(graphs) < 3:
    problems.append(f"expected 3 definition artifacts, parsed {len(graphs)}")
for graph in graphs:
    if len(graph.tasks) != 12:
        problems.append(f"{graph.source}: expected 12 tasks, found {len(graph.tasks)}")
    graph.topological_order()

if problems:
    for problem in problems:
        print(f"  problem: {problem}")
    sys.exit(1)
print("  all artifacts agree on tasks, dependencies, and retry limits.")
PY
then
  info "definition agreement holds."
else
  note_failure "the Control-M definition artifacts do not agree"
fi

heading "2. Scenario reproduction against fixture oracles"
for scenario in "${SCENARIOS[@]}"; do
  run_id="contract-$scenario"
  if ! python3 "$HARNESS" \
        --scenario "$scenario" \
        --run-id "$run_id" \
        --kit-root "$KIT_ROOT" \
        --repos-root "$REPOS_ROOT" \
        --run-root "$RUN_ROOT" \
        --workbench-note "$WORKBENCH_PROBE_FILE" >/dev/null 2>"$STDERR_DIR/$scenario.harness.err"; then
    note_failure "$scenario: the harness did not complete"
    sed 's/^/    /' "$STDERR_DIR/$scenario.harness.err" >&2 || true
    continue
  fi
  if output="$(python3 "$VERIFY" --run-dir "$RUN_ROOT/$run_id" --kit-root "$KIT_ROOT" 2>&1)"; then
    info "$(printf '%-24s %s' "$scenario" "$(printf '%s' "$output" | sed -n '2p' | sed 's/^ *//')")"
  else
    note_failure "$scenario: did not reproduce its fixture oracle"
    printf '%s\n' "$output" | sed 's/^/    /' >&2
  fi
done

heading "3a. Byte-identical reruns with the same run identifier"
# The same scenario, the same fixed clock, and the same run
# identifier must reproduce stages/*.jsonl and output/* byte for byte. This is
# the strongest determinism claim and needs no normalization at all.
STAGING="$KIT_ROOT/workspace/runtime/determinism"
for scenario in happy-path reconciliation-breaks partial-ledger-write; do
  rm -rf "$STAGING"; mkdir -p "$STAGING"
  ok=true
  for copy in a b; do
    if ! python3 "$HARNESS" \
          --scenario "$scenario" \
          --run-id "byte-$scenario" \
          --kit-root "$KIT_ROOT" \
          --repos-root "$REPOS_ROOT" \
          --run-root "$RUN_ROOT" >/dev/null 2>&1; then
      note_failure "$scenario: byte-identity run $copy did not complete"
      ok=false
      break
    fi
    mkdir -p "$STAGING/$copy"
    cp -R "$RUN_ROOT/byte-$scenario/stages" "$STAGING/$copy/stages"
    cp -R "$RUN_ROOT/byte-$scenario/output" "$STAGING/$copy/output"
  done
  [ "$ok" = true ] || continue

  if diff -r "$STAGING/a" "$STAGING/b" >"$STAGING/$scenario.diff" 2>&1; then
    info "$(printf '%-24s stages/ and output/ byte-identical across two runs' "$scenario")"
  else
    note_failure "$scenario: reruns are not byte-identical"
    head -30 "$STAGING/$scenario.diff" | sed 's/^/    /' >&2
  fi
done
rm -rf "$STAGING"

heading "3b. Equivalence across different run identifiers"
# Different run identifiers legitimately change file bytes, because every record
# carries its run identifier. After normalizing the volatile fields with
# verify_run.py --normalize, the two runs must still be identical.
for scenario in happy-path reconciliation-breaks; do
  first="$RUN_ROOT/determinism-$scenario-a"
  second="$RUN_ROOT/determinism-$scenario-b"
  ok=true
  for run_dir in "$first" "$second"; do
    if ! python3 "$HARNESS" \
          --scenario "$scenario" \
          --run-id "$(basename "$run_dir")" \
          --kit-root "$KIT_ROOT" \
          --repos-root "$REPOS_ROOT" \
          --run-root "$RUN_ROOT" >/dev/null 2>&1; then
      note_failure "$scenario: normalized run $(basename "$run_dir") did not complete"
      ok=false
    fi
  done
  [ "$ok" = true ] || continue

  python3 "$VERIFY" --run-dir "$first"  --kit-root "$KIT_ROOT" --normalize > "$first.normalized.json"
  python3 "$VERIFY" --run-dir "$second" --kit-root "$KIT_ROOT" --normalize > "$second.normalized.json"
  if diff -u "$first.normalized.json" "$second.normalized.json" >"$RUN_ROOT/$scenario.normalized.diff"; then
    info "$(printf '%-24s normalized output identical across run identifiers' "$scenario")"
  else
    note_failure "$scenario: normalized output differs between run identifiers"
    head -40 "$RUN_ROOT/$scenario.normalized.diff" | sed 's/^/    /' >&2
  fi
done

heading "Result"
if [ "$FAILURES" -eq 0 ]; then
  info "the Control-M compatibility contract holds."
  exit "$EXIT_OK"
fi
err "$FAILURES check(s) failed."
exit "$EXIT_FAILED"
