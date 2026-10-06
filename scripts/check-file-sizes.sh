#!/usr/bin/env bash
#
# Keep tracked files reviewable: fail when any tracked file exceeds the byte
# limit, or any Python or shell source file exceeds the line limit. Large
# modules should be split by responsibility rather than allowed to grow.
#
#   MAX_FILE_KB      512   any tracked file
#   MAX_SOURCE_LINES 800   *.py and *.sh files
#
# Usage: scripts/check-file-sizes.sh [file ...]
#   With no arguments, every file tracked by Git is checked.
#
# Exit codes:
#   0   every file is within the limits
#   1   one or more files exceed a limit
#   2   usage error

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

MAX_FILE_KB=512
MAX_SOURCE_LINES=800

case "${1:-}" in
  -h|--help) sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "$EXIT_OK" ;;
esac

cd "$KIT_ROOT"

if [ $# -gt 0 ]; then
  files=("$@")
else
  files=()
  while IFS= read -r -d '' f; do files+=("$f"); done < <(git ls-files -z)
fi

OVER=0
for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  kb=$(( ($(wc -c < "$f") + 1023) / 1024 ))
  if [ "$kb" -gt "$MAX_FILE_KB" ]; then
    err "$f is ${kb} KB (limit ${MAX_FILE_KB} KB)"
    OVER=$((OVER + 1))
  fi
  case "$f" in
    *.py|*.sh)
      lines=$(wc -l < "$f" | tr -d ' ')
      if [ "$lines" -gt "$MAX_SOURCE_LINES" ]; then
        err "$f has $lines lines (limit $MAX_SOURCE_LINES)"
        OVER=$((OVER + 1))
      fi
      ;;
  esac
done

if [ "$OVER" -eq 0 ]; then
  info "file sizes: ${#files[@]} files within limits."
  exit "$EXIT_OK"
fi
err "file sizes: $OVER file(s) over the limit."
exit "$EXIT_FAILED"
