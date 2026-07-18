#!/usr/bin/env bash
# Recorded lifecycle e2e rig — OSS flow entrypoint (Phase 1).
#
# Runs the full offboard -> teardown -> reinstall -> onboard -> verify ->
# offboard lifecycle against a real VM, recording browser video (Playwright)
# and terminal casts (pexpect + agg). See README.md in this directory.
#
# Usage:
#   scripts/e2e-rig/run-oss.sh \
#     --host root@<vm> --url https://<name>.nip.io \
#     --release 0.11.1 --cli-version 0.11.1 \
#     [--headless] [--run-dir DIR] [--from NN] [--until NN] [--only NN,NN]
#     [--keep-going] [--purge-certs] [--no-video]
#
# Module exit codes: 0 = pass, 3 = skip (recorded), other = fail.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"

RIG_HOST=""
RIG_URL=""
RIG_RELEASE=""
RIG_CLI_VERSION=""
RIG_HEADLESS="${RIG_HEADLESS:-0}"
RIG_PURGE_CERTS="${RIG_PURGE_CERTS:-0}"
RUN_DIR=""
FROM="01"
UNTIL="14"
ONLY=""
KEEP_GOING=0
NO_VIDEO=0

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) RIG_HOST="$2"; shift 2 ;;
    --url) RIG_URL="${2%/}"; shift 2 ;;
    --release) RIG_RELEASE="$2"; shift 2 ;;
    --cli-version) RIG_CLI_VERSION="$2"; shift 2 ;;
    --headless) RIG_HEADLESS=1; shift ;;
    --run-dir) RUN_DIR="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --until) UNTIL="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --keep-going) KEEP_GOING=1; shift ;;
    --purge-certs) RIG_PURGE_CERTS=1; shift ;;
    --no-video) NO_VIDEO=1; shift ;;
    -h|--help) usage ;;
    *) rig_die "unknown argument: $1" ;;
  esac
done

[ -n "$RIG_HOST" ] || rig_die "--host is required (e.g. root@vm.example)"
[ -n "$RIG_URL" ] || rig_die "--url is required (e.g. https://x.y.z.w.nip.io)"
[ -n "$RIG_RELEASE" ] || rig_die "--release is required (e.g. 0.11.1)"
[ -n "$RIG_CLI_VERSION" ] || RIG_CLI_VERSION="$RIG_RELEASE"

# Python with pexpect + playwright. Override with RIG_PYTHON.
if [ -z "${RIG_PYTHON:-}" ]; then
  for cand in "$HERE/../../.venv/bin/python" "$HERE/../../../.venv/bin/python" \
              "$HERE/../../../../.venv/bin/python" python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import pexpect, playwright' >/dev/null 2>&1; then
      RIG_PYTHON="$(command -v "$cand")"
      break
    fi
  done
fi
[ -n "${RIG_PYTHON:-}" ] || rig_die "no python with pexpect+playwright found; set RIG_PYTHON"

command -v agg >/dev/null 2>&1 || rig_die "agg is required (brew install agg)"
command -v ffmpeg >/dev/null 2>&1 || rig_die "ffmpeg is required (brew install ffmpeg)"

# Fail early if ssh needs a password/interactive touch.
ssh -o BatchMode=yes -o ConnectTimeout=15 "$RIG_HOST" true \
  || rig_die "cannot ssh to $RIG_HOST non-interactively"

if [ -z "$RUN_DIR" ]; then
  RUN_DIR="$HERE/artifacts/run-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$RUN_DIR"/{logs,snapshots,casts,browser-videos,screenshots,diffs,state,instance-archive}
RIG_RUN_DIR="$(cd "$RUN_DIR" && pwd)"

export RIG_HOST RIG_URL RIG_RELEASE RIG_CLI_VERSION RIG_RUN_DIR RIG_PYTHON \
       RIG_HEADLESS RIG_PURGE_CERTS

# Per-run first-user credentials. Generated once, reused on --run-dir resume.
CREDS="$RIG_RUN_DIR/state/creds.json"
if [ ! -f "$CREDS" ]; then
  "$RIG_PYTHON" - "$CREDS" <<'PY'
import json, secrets, sys
suffix = secrets.token_hex(3)
creds = {
    # Username must be alphanumeric (registration validation).
    "username": f"e2e{suffix}",
    "email": f"e2e{suffix}@example.com",
    "password": secrets.token_hex(10) + "aA1",
}
with open(sys.argv[1], "w") as fh:
    json.dump(creds, fh, indent=2)
PY
  chmod 600 "$CREDS"
fi

SUMMARY="$RIG_RUN_DIR/run-summary.json"
if [ ! -f "$SUMMARY" ]; then
  "$RIG_PYTHON" - "$SUMMARY" "$RIG_HOST" "$RIG_URL" "$RIG_RELEASE" "$RIG_CLI_VERSION" <<'PY'
import json, sys, datetime, os
path, host, url, release, cli = sys.argv[1:6]
json.dump({
    "run_id": os.path.basename(os.path.dirname(os.path.abspath(path))),
    "host": host, "url": url, "release": release, "cli_version": cli,
    "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "steps": [], "result": "running",
}, open(path, "w"), indent=2)
PY
fi

record_step() {
  # record_step <id> <name> <status> <duration_s> <notes-file>
  "$RIG_PYTHON" - "$SUMMARY" "$@" <<'PY'
import json, sys, datetime, pathlib
path, sid, name, status, duration, notes_file = sys.argv[1:7]
doc = json.load(open(path))
notes = ""
p = pathlib.Path(notes_file)
if p.exists():
    notes = p.read_text().strip()
doc["steps"] = [s for s in doc["steps"] if s["id"] != sid]
doc["steps"].append({
    "id": sid, "name": name, "status": status,
    "duration_s": round(float(duration), 1),
    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "notes": notes,
})
doc["steps"].sort(key=lambda s: s["id"])
json.dump(doc, open(path, "w"), indent=2)
PY
}

finalize() {
  "$RIG_PYTHON" - "$SUMMARY" "$1" <<'PY'
import json, sys, datetime
path, result = sys.argv[1:3]
doc = json.load(open(path))
doc["result"] = result
doc["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
json.dump(doc, open(path, "w"), indent=2)
PY
}

MODULES=(
  "01:snapshot"
  "02:offboard"
  "03:teardown"
  "04:install-agents"
  "05:install-preloop"
  "06:wait-healthy"
  "07:browser-register"
  "08:cli-onboard"
  "09:browser-agents"
  "10:custom-agent"
  "11:wasteful-session"
  "12:optimize-validate"
  "13:console-tour"
  "14:offboard-verify"
)

should_run() {
  local id="$1"
  if [ -n "$ONLY" ]; then
    case ",$ONLY," in *",$id,"*) return 0 ;; *) return 1 ;; esac
  fi
  [ "$id" \< "$FROM" ] && return 1
  [ "$id" \> "$UNTIL" ] && return 1
  return 0
}

overall="pass"
for entry in "${MODULES[@]}"; do
  id="${entry%%:*}"
  name="${entry#*:}"
  should_run "$id" || continue

  script=""
  for ext in sh py; do
    if [ -f "$HERE/modules/$id-$name.$ext" ]; then
      script="$HERE/modules/$id-$name.$ext"
    fi
  done
  [ -n "$script" ] || rig_die "module $id-$name not found"

  export RIG_STEP_NOTES_FILE="$RIG_RUN_DIR/state/notes-$id.txt"
  : > "$RIG_STEP_NOTES_FILE"
  log="$RIG_RUN_DIR/logs/$id-$name.log"
  rig_log "=== [$id] $name ==="
  start=$SECONDS
  status=0
  # pipefail makes the pipeline carry the module's exit code (tee is always
  # 0); the || stops set -e from killing the orchestrator on skip/fail.
  case "$script" in
    *.py) "$RIG_PYTHON" "$script" 2>&1 | tee "$log" || status=$? ;;
    *.sh) bash "$script" 2>&1 | tee "$log" || status=$? ;;
  esac
  dur=$((SECONDS - start))

  case "$status" in
    0) record_step "$id" "$name" "pass" "$dur" "$RIG_STEP_NOTES_FILE" ;;
    3) record_step "$id" "$name" "skip" "$dur" "$RIG_STEP_NOTES_FILE" ;;
    *)
      record_step "$id" "$name" "fail" "$dur" "$RIG_STEP_NOTES_FILE"
      overall="fail"
      rig_log "=== [$id] $name FAILED (exit $status, ${dur}s) — log: $log ==="
      if [ "$KEEP_GOING" -ne 1 ]; then
        finalize "fail"
        exit "$status"
      fi
      ;;
  esac
  rig_log "=== [$id] $name done (${dur}s) ==="
done

finalize "$overall"

# One continuous documentation video for the whole run (skip with --no-video).
if [ "$NO_VIDEO" -ne 1 ]; then
  rig_log "compositing the full-run video ..."
  export RIG_STEP_NOTES_FILE="$RIG_RUN_DIR/state/notes-video.txt"
  "$RIG_PYTHON" "$HERE/lib/compose_video.py" \
    || rig_log "WARNING: full-run video compositing failed (artifacts intact)"
fi

rig_log "run complete: $overall — summary: $SUMMARY"
[ "$overall" = "pass" ]
