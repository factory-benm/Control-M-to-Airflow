#!/usr/bin/env bash
#
# Static checks for every Python file in the estate, configured in pyproject.toml:
#
#   ruff check            lint, naming, complexity, TODO hygiene
#   ruff format --check   formatting
#   mypy                  strict type checking
#   vulture               dead code
#
# Tools come from .venv/bin if present (uv venv && uv pip install -r
# requirements-dev.txt), otherwise from uvx at the versions pinned in
# requirements-dev.txt. Never an unpinned tool from PATH.
#
# Usage: scripts/lint.sh [--fix]
#   --fix   apply ruff fixes and formatting instead of only checking
#
# Exit codes:
#   0   all checks passed
#   1   one or more checks failed
#   2   usage error
#   78  the tools are unavailable (set PAYOPS_REQUIRE_DEV_TOOLS=1 to fail instead)

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

FIX=0
case "${1:-}" in
  "") ;;
  --fix) FIX=1 ;;
  -h|--help) sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "$EXIT_OK" ;;
  *) err "unexpected argument '$1'"; exit "$EXIT_USAGE" ;;
esac

cd "$KIT_ROOT"

pinned_version() {
  sed -n "s/^$1==\([^ ;\\]*\).*/\1/p" requirements-dev.txt | head -n 1
}

tool() {
  local name="$1"; shift
  if [ -x ".venv/bin/$name" ]; then
    ".venv/bin/$name" "$@"
  else
    uvx --quiet --from "$name==$(pinned_version "$name")" "$name" "$@"
  fi
}

if [ ! -x .venv/bin/ruff ] && ! have uv; then
  msg="dev tools unavailable: create .venv (uv venv && uv pip install -r requirements-dev.txt) or install uv"
  if [ "${PAYOPS_REQUIRE_DEV_TOOLS:-0}" = "1" ]; then
    err "$msg"
    exit "$EXIT_FAILED"
  fi
  warn "$msg; skipping lint."
  exit "$EXIT_UNAVAILABLE"
fi

FAILURES=0
step() {
  local label="$1"; shift
  log "--> $label"
  if ! "$@"; then
    err "$label failed"
    FAILURES=$((FAILURES + 1))
  fi
}

if [ "$FIX" -eq 1 ]; then
  step "ruff check --fix" tool ruff check --fix .
  step "ruff format" tool ruff format .
else
  step "ruff check" tool ruff check .
  step "ruff format --check" tool ruff format --check .
fi
step "mypy (strict)" tool mypy
step "vulture" tool vulture

if [ "$FAILURES" -eq 0 ]; then
  info "lint: all checks passed."
  exit "$EXIT_OK"
fi
err "lint: $FAILURES check(s) failed."
exit "$EXIT_FAILED"
