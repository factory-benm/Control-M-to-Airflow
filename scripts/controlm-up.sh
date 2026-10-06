#!/usr/bin/env bash
#
# Select and prepare the Control-M runtime.
#
# Preference order is BMC Control-M Workbench first, compatibility harness
# second. The selection is always printed and always persisted. This script
# never falls back silently.
#
# Modes:
#   auto        try Workbench, fall back to the harness with a recorded reason
#   workbench   require Workbench; fail if it is unavailable
#   harness     use the compatibility harness explicitly
#
# Exit codes:
#   0   a runtime is selected and ready
#   2   usage error
#   78  the runtime required by --mode is unavailable

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=../runtimes/control-m/workbench/workbench.env
source "$KIT_ROOT/runtimes/control-m/workbench/workbench.env"

MODE="auto"

usage() {
  sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --mode=*) MODE="${1#*=}"; shift ;;
    -h|--help) usage; exit "$EXIT_OK" ;;
    *) err "unrecognized argument '$1'"; usage; exit "$EXIT_USAGE" ;;
  esac
done

case "$MODE" in
  auto|workbench|harness) ;;
  *) err "--mode must be one of auto, workbench, harness (got '$MODE')"; exit "$EXIT_USAGE" ;;
esac

mkdir -p "$RUNTIME_STATE_DIR"

HOST_ARCH="$(uname -m)"
HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

# ---------------------------------------------------------------------------
# Probe for the real Workbench, recording only observed facts.
# ---------------------------------------------------------------------------

probe_workbench() {
  # Sets: WB_AVAILABLE, WB_REASON, WB_DETAIL, WB_CREDS, WB_DIGEST,
  #       WB_IMAGE_ARCH, WB_EMULATION
  WB_AVAILABLE="false"
  WB_REASON=""
  WB_DETAIL=""
  WB_CREDS="none-detected"
  WB_DIGEST=""
  WB_IMAGE_ARCH=""
  WB_EMULATION="not-required"

  if ! have docker; then
    WB_REASON="docker-cli-missing"
    WB_DETAIL="The docker CLI is not on PATH."
    return
  fi

  if ! docker info >/dev/null 2>&1; then
    WB_REASON="docker-daemon-unreachable"
    WB_DETAIL="The docker CLI is installed but no daemon is reachable. Start Docker Desktop or 'colima start'."
    return
  fi

  # Credentials: environment pair, or an existing docker login for the BMC
  # registry. We record only whether credentials were detected, never a value.
  if [ -n "${BMC_EPD_USER:-}" ] && [ -n "${BMC_EPD_TOKEN:-}" ]; then
    WB_CREDS="environment"
  elif [ -f "$HOME/.docker/config.json" ] &&
       grep -q "$WORKBENCH_REGISTRY" "$HOME/.docker/config.json" 2>/dev/null; then
    WB_CREDS="existing-docker-login"
  fi

  if [ "$WB_CREDS" = "environment" ]; then
    info "authenticating to $WORKBENCH_REGISTRY as the supplied EPD user"
    if ! printf '%s' "$BMC_EPD_TOKEN" |
         docker login "https://$WORKBENCH_REGISTRY" -u "$BMC_EPD_USER" --password-stdin >/dev/null 2>&1; then
      WB_REASON="bmc-authentication-failed"
      WB_DETAIL="docker login to $WORKBENCH_REGISTRY was rejected with the supplied BMC_EPD_USER and BMC_EPD_TOKEN."
      return
    fi
  fi

  local manifest
  if ! manifest="$(docker manifest inspect "$WORKBENCH_IMAGE:$WORKBENCH_TAG" 2>&1)"; then
    if printf '%s' "$manifest" | grep -qiE 'authentication is required|unauthorized|denied'; then
      WB_REASON="bmc-entitlement-required"
      WB_DETAIL="$WORKBENCH_IMAGE:$WORKBENCH_TAG is gated behind BMC EPD entitlement. The registry answered with an authentication challenge and no usable credentials are configured on this machine. Workbench itself is no-cost, but it is not published to a public registry."
    else
      WB_REASON="workbench-image-unreachable"
      WB_DETAIL="Could not read the Workbench manifest: $(printf '%s' "$manifest" | head -1)"
    fi
    return
  fi

  WB_DIGEST="$(printf '%s' "$manifest" |
    python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("config",{}).get("digest",""))' 2>/dev/null || true)"
  WB_IMAGE_ARCH="$(printf '%s' "$manifest" |
    python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("manifests") or [{}])[0].get("platform",{}).get("architecture","amd64"))' 2>/dev/null || echo amd64)"

  if [ "$HOST_ARCH" = "arm64" ] || [ "$HOST_ARCH" = "aarch64" ]; then
    if [ "$WB_IMAGE_ARCH" = "amd64" ]; then
      WB_EMULATION="required-amd64-on-arm64"
    fi
  fi

  info "pulling $WORKBENCH_IMAGE:$WORKBENCH_TAG (this is a large image)"
  local pull_args=()
  [ "$WB_EMULATION" = "required-amd64-on-arm64" ] && pull_args+=(--platform linux/amd64)
  if ! docker pull "${pull_args[@]}" "$WORKBENCH_IMAGE:$WORKBENCH_TAG" >&2; then
    WB_REASON="workbench-pull-failed"
    WB_DETAIL="Authenticated but the image pull did not complete."
    return
  fi

  info "starting container $WORKBENCH_CONTAINER"
  docker rm -f "$WORKBENCH_CONTAINER" >/dev/null 2>&1 || true
  local run_args=(-dt --name "$WORKBENCH_CONTAINER"
                  --cpus="$WORKBENCH_CPUS" -m "$WORKBENCH_MEMORY"
                  -p "$WORKBENCH_PORT:$WORKBENCH_PORT"
                  --hostname="$WORKBENCH_HOSTNAME")
  [ "$WB_EMULATION" = "required-amd64-on-arm64" ] && run_args+=(--platform linux/amd64)
  if ! docker run "${run_args[@]}" "$WORKBENCH_IMAGE:$WORKBENCH_TAG" >/dev/null 2>&1; then
    WB_REASON="workbench-start-failed"
    WB_DETAIL="The image is present but the container did not start. On Apple silicon this is usually AMD64 emulation failing."
    return
  fi

  WB_AVAILABLE="true"
  WB_REASON="workbench-started"
  WB_DETAIL="Container $WORKBENCH_CONTAINER is running and listening on port $WORKBENCH_PORT."
}

heading "Control-M runtime selection (requested mode: $MODE)"
info "host: $HOST_OS/$HOST_ARCH"

if [ "$MODE" = "harness" ]; then
  WB_AVAILABLE="false"
  WB_REASON="not-attempted"
  WB_DETAIL="Mode 'harness' was requested explicitly, so Workbench was not probed."
  WB_CREDS="not-checked"
  WB_DIGEST=""; WB_IMAGE_ARCH=""; WB_EMULATION="not-applicable"
else
  probe_workbench
fi

python3 - "$WORKBENCH_PROBE_FILE" <<PY
import json, sys
payload = {
    "attempted": "$MODE" != "harness",
    "available": "$WB_AVAILABLE" == "true",
    "reason": "$WB_REASON",
    "detail": """$WB_DETAIL""".strip(),
    "image": "$WORKBENCH_IMAGE:$WORKBENCH_TAG",
    "registry": "$WORKBENCH_REGISTRY",
    "imageDigest": "$WB_DIGEST",
    "imageArchitecture": "$WB_IMAGE_ARCH",
    "emulation": "$WB_EMULATION",
    "credentials": "$WB_CREDS",
    "host": {"os": "$HOST_OS", "architecture": "$HOST_ARCH"},
    "probedAt": "$(utc_now)",
    "provenance": "observed-run",
    "note": (
        "Control-M Workbench is a no-cost BMC development environment, but it is "
        "distributed through BMC's entitled registry rather than a public one."
    ),
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

if [ "$WB_AVAILABLE" = "true" ]; then
  SELECTED="control-m-workbench"
else
  if [ "$MODE" = "workbench" ]; then
    err "Workbench was required but is unavailable: $WB_REASON"
    info "$WB_DETAIL"
    log ""
    log "Not falling back. Re-run with --mode auto to use the compatibility harness."
    exit "$EXIT_UNAVAILABLE"
  fi
  SELECTED="compatibility-harness"
fi

python3 - "$SELECTED_RUNTIME_FILE" <<PY
import json, sys
payload = {
    "mode": "$SELECTED",
    "isControlM": "$SELECTED" == "control-m-workbench",
    "requestedMode": "$MODE",
    "fallbackOccurred": "$MODE" == "auto" and "$SELECTED" == "compatibility-harness",
    "fallbackReason": "" if "$SELECTED" == "control-m-workbench" else "$WB_REASON",
    "selectedAt": "$(utc_now)",
    "workbenchProbe": "workspace/runtime/control-m/workbench-probe.json",
    "provenance": "observed-run",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

heading "Selected runtime: $SELECTED"
if [ "$SELECTED" = "control-m-workbench" ]; then
  info "BMC Control-M Workbench $WORKBENCH_TAG is running."
  [ "$WB_EMULATION" != "not-required" ] && info "emulation: $WB_EMULATION"
else
  info "Reason Workbench was not used: $WB_REASON"
  info "$WB_DETAIL"
  log ""
  info "This is a COMPATIBILITY EXECUTION of the Control-M-defined workflow."
  info "It executes the same task graph and the same repository commands, and"
  info "it is recorded as 'compatibility-harness'. It is not Control-M."
fi
info "recorded: ${SELECTED_RUNTIME_FILE#"$KIT_ROOT/"}"
info "recorded: ${WORKBENCH_PROBE_FILE#"$KIT_ROOT/"}"
exit "$EXIT_OK"
