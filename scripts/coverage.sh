#!/usr/bin/env bash
#
# Run each service repository's unit tests under the standard-library coverage
# gate (scripts/lib/coverage_gate.py) and fail any repository whose statement
# coverage is below [tool.payops.coverage] fail_under in pyproject.toml.
#
# Usage: scripts/coverage.sh [repo-name ...]
#   With no arguments, every repository under repos/ is checked.
#
# Reports are written to workspace/coverage/<repo>.json.
#
# Exit codes:
#   0   every repository met the threshold
#   1   tests failed or coverage was below the threshold
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

case "${1:-}" in
  -h|--help) sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "$EXIT_OK" ;;
esac

REPOS_ROOT="$(resolve_repos_root)"
REPORT_DIR="$KIT_ROOT/workspace/coverage"

if [ $# -gt 0 ]; then
  repos=()
  for name in "$@"; do
    if [ ! -d "$REPOS_ROOT/$name/src" ]; then
      err "unknown repository '$name'"
      exit "$EXIT_USAGE"
    fi
    repos+=("$REPOS_ROOT/$name")
  done
else
  repos=()
  for dir in "$REPOS_ROOT"/*/; do
    [ -d "$dir/src" ] && [ -d "$dir/tests" ] && repos+=("${dir%/}")
  done
fi

FAILED=()
for repo in "${repos[@]}"; do
  name="$(basename "$repo")"
  log ""
  log "--> coverage: $name"
  if ! python3 "$KIT_ROOT/scripts/lib/coverage_gate.py" \
      --repo "$repo" --json-out "$REPORT_DIR/$name.json"; then
    FAILED+=("$name")
  fi
done

if [ "${#FAILED[@]}" -eq 0 ]; then
  info "coverage: all ${#repos[@]} repositories meet the threshold."
  exit "$EXIT_OK"
fi
err "coverage below threshold or tests failing: ${FAILED[*]}"
exit "$EXIT_FAILED"
