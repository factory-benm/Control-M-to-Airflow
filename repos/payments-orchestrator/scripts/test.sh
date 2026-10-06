#!/usr/bin/env bash
# Unit checks for the scheduler definitions in this repository.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: test.sh

Runs the unit checks for the Control-M definitions in this repository:
the JSON jobs-as-code parses, the legacy XML export parses, and the
task-to-repository mapping is complete and acyclic.
USAGE
  exit 0
fi

cd "$REPO_ROOT"
PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
