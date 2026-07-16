#!/usr/bin/env bash
# Module 12 — final offboard + the offboarding assertion.
#
# Offboards everything (recorded), removes the custom agent server-side,
# snapshots the VM's agent configs one last time, and diffs them per agent
# against the post-module-02 "baseline" snapshot (the pre-onboarding state).
# Verdict per agent: byte-identical or semantically-equal => PASS.
# The "before" snapshot from module 01 is also diffed, informationally: on a
# VM that started with agents already onboarded it is EXPECTED to differ.

set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

# 1. Recorded offboard of every enrolled agent.
"$RIG_PYTHON" "$RIG_DIR/lib/record_offboard.py"

# 2. Server-side cleanup of the custom agent (no local config involved).
"$RIG_PYTHON" - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["RIG_DIR"] + "/lib")
import riglib

url = riglib.env("RIG_URL").rstrip("/")
state = riglib.load_state("custom-agent.json")
token = riglib.user_token(url, riglib.load_creds())
name = (state or {}).get("display_name", "Research Agent (e2e)")
matches = [
    a for a in riglib.list_agents(url, token) if a.get("display_name") == name
]
if not matches:
    riglib.log("no custom agent found; nothing to clean up")
for agent in matches:
    aid = agent["id"]
    s, creds_list = riglib.api_request(
        f"{url}/api/v1/agents/{aid}/credentials", token=token
    )
    items = (
        creds_list.get("items") or creds_list.get("credentials") or []
        if isinstance(creds_list, dict)
        else creds_list or []
    )
    for cred in items:
        s, _ = riglib.api_request(
            f"{url}/api/v1/agents/{aid}/credentials/{cred['id']}",
            "DELETE",
            token,
        )
        riglib.log(f"revoke credential {cred['id']}: {s}")
    s, _ = riglib.api_request(f"{url}/api/v1/agents/{aid}", "DELETE", token)
    riglib.log(f"delete custom agent {aid}: {s}")
PY

# 3. Final snapshot.
rig_snapshot "final"

# 4. Per-agent diff verdicts.
"$RIG_PYTHON" - <<'PY'
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["RIG_DIR"] + "/lib")
import riglib
import snapshot_diff

run = riglib.run_dir()
report = snapshot_diff.compare_snapshots(
    run / "snapshots" / "baseline", run / "snapshots" / "final",
    run / "diffs",
)
info = snapshot_diff.compare_snapshots(
    run / "snapshots" / "before", run / "snapshots" / "final",
    run / "diffs" / "vs-before-informational",
)
report["informational_vs_before"] = {
    "verdict": info["verdict"],
    "note": "differences vs the module-01 before-image are expected when the "
            "VM started with agents already onboarded",
}
out = run / "diffs" / "report.json"
out.write_text(json.dumps(report, indent=2))

for agent, entry in sorted(report["agents"].items()):
    files = ", ".join(f"{f['path']}:{f['verdict']}" for f in entry["files"])
    riglib.log(f"  {entry['verdict']:4}  {agent}  [{files}]")
riglib.note(
    "offboard-diff verdict vs baseline: "
    + ", ".join(f"{a}={e['verdict']}" for a, e in sorted(report["agents"].items()))
)
if report["verdict"] != "PASS":
    sys.exit(f"offboarding assertion FAILED — see {out}")
riglib.log(f"offboarding assertion PASSED — report: {out}")
PY
