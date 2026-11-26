#!/usr/bin/env bash
# Assign CI jobs to runner classes from one snapshot of the runner inventory.
#
# Usage: pick-runners.sh early|late
#
# Runner classes:
#   hosted   ubuntu-24.04, the default and the fallback for every failure.
#   general  self-hosted + Linux + X64 + preloop-ci-general (opt-in with
#            CI_SELF_HOSTED_TESTS=true). Never the gce-tests-only pool.
#   local    self-hosted + Linux + X64 + preloop-ci-local (on unless
#            CI_LOCAL_RUNNERS=false). One registration is one job slot.
#
# early: Detect changed paths, Lint & Format and CLI Fast. They run in two
#   stages (changes first, then lint and CLI Fast), so a slot can serve both.
# late:  backend shards and the fuzz and browser-fixture leaves. The
#   backend set goes to exactly one class: general when it has one idle runner
#   per group, else local when it does, else hosted. Leaves then take the idle
#   local slots that are left, in priority order; the rest stay hosted.
#
# Untrusted sources (forks, Dependabot) and reruns get hosted only: a job on a
# local slot runs as a docker-group user, which is root on that VM. The
# snapshot is not a reservation; another run can take a slot afterwards.
#
# Environment: RUNNER_READ_TOKEN, REPO, TRUSTED_SOURCE, RUN_ATTEMPT,
# SELF_HOSTED_TESTS, LOCAL_RUNNERS, and for late mode BACKEND_REQUIRED,
# BACKEND_SHARDS, LEAF_FUZZ, LEAF_BROWSER_FIXTURE. Writes
# GITHUB_OUTPUT and GITHUB_STEP_SUMMARY. Always exits 0.
set -uo pipefail

MODE="${1:-}"
HOSTED='"ubuntu-24.04"'
GENERAL='["self-hosted","Linux","X64","preloop-ci-general"]'
LOCAL='["self-hosted","Linux","X64","preloop-ci-local"]'
# Concurrent legs of fuzz.yml (its matrix max-parallel).
FUZZ_SLOTS=2

out() { echo "$1" >> "$GITHUB_OUTPUT"; }
note() { echo "$1"; echo "- $1" >> "$GITHUB_STEP_SUMMARY"; }
is_count() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; }

general_idle=0
local_idle=0
general_reason=""
local_reason=""

local_enabled=true
general_enabled=true
[ "${LOCAL_RUNNERS:-}" = "false" ] && local_enabled=false \
  && local_reason="local runners disabled by CI_LOCAL_RUNNERS=false"
[ "${SELF_HOSTED_TESTS:-false}" != "true" ] && general_enabled=false \
  && general_reason="self-hosted routing requires CI_SELF_HOSTED_TESTS=true"
if [ "${TRUSTED_SOURCE:-false}" != "true" ]; then
  local_enabled=false; general_enabled=false
  local_reason="untrusted source uses hosted runners"; general_reason="$local_reason"
elif [ "${RUN_ATTEMPT:-1}" != "1" ]; then
  local_enabled=false; general_enabled=false
  local_reason="reruns use hosted runners"; general_reason="$local_reason"
fi

# Count online, idle runners carrying every label in $1, never gce-tests-only.
count_idle() {
  printf '%s' "$runners" | jq -s --arg class "$1" '
    [ .[].runners[]?
      | select(.status == "online" and .busy == false)
      | [.labels[].name] as $labels
      | select(($labels | index("self-hosted")) != null
           and ($labels | index("Linux")) != null
           and ($labels | index("X64")) != null
           and ($labels | index($class)) != null
           and ($labels | index("gce-tests-only")) == null)
    ] | length' 2>/dev/null
}

if [ "$local_enabled" = true ] || [ "$general_enabled" = true ]; then
  if [ -z "${RUNNER_READ_TOKEN:-}" ]; then
    reason="secret CI_RUNNER_READ_TOKEN is absent or empty"
    local_enabled=false; general_enabled=false
    local_reason="$reason"; general_reason="$reason"
  elif ! runners=$(GH_TOKEN="$RUNNER_READ_TOKEN" timeout 15s gh api \
      "repos/${REPO}/actions/runners" --paginate 2>/dev/null); then
    reason="could not list repository runners (token scope or API error)"
    local_enabled=false; general_enabled=false
    local_reason="$reason"; general_reason="$reason"
  else
    g=$(count_idle preloop-ci-general) || g=""
    l=$(count_idle preloop-ci-local) || l=""
    if ! is_count "$g" || ! is_count "$l"; then
      reason="could not parse the runner list"
      local_enabled=false; general_enabled=false
      local_reason="$reason"; general_reason="$reason"
    else
      [ "$general_enabled" = true ] && general_idle=$g
      [ "$local_enabled" = true ] && local_idle=$l
    fi
  fi
fi

note "idle general runners: \`$general_idle\`; idle local slots: \`$local_idle\`"
[ -n "$local_reason" ] && note "local: $local_reason"
out "local_idle=$local_idle"

# take VAR NEED: set VAR to local when NEED slots are free, else hosted.
# Not a subshell, so the claimed slots stay claimed.
take() {
  if [ "$free" -ge "$2" ]; then free=$((free - $2)); printf -v "$1" '%s' "$LOCAL"
  else printf -v "$1" '%s' "$HOSTED"; fi
}

if [ "$MODE" = "early" ]; then
  # Stage 1 is changes alone; stage 2 (lint, CLI Fast) starts after it ends.
  free=$local_idle; take changes 1
  free=$local_idle; take lint 1; take cli_fast 1
  out "changes_runner=$changes"
  out "lint_runner=$lint"
  out "cli_fast_runner=$cli_fast"
  note "changes: \`$changes\`, lint: \`$lint\`, CLI Fast: \`$cli_fast\`"
  exit 0
fi

if [ "$MODE" != "late" ]; then
  echo "usage: pick-runners.sh early|late" >&2
  exit 0
fi

splits="${BACKEND_SHARDS:-}"
backend="$HOSTED"
class=hosted
groups=0
free=$local_idle
if ! is_count "$splits" || [ "$splits" -eq 0 ]; then
  reason="missing or invalid matrix size"
elif [ "${BACKEND_REQUIRED:-true}" != "true" ]; then
  reason="backend paths are irrelevant; no optional capacity to recover"
elif [ "$general_idle" -ge "$splits" ]; then
  # One runner class per run: the whole set or none of it.
  backend="$GENERAL"; class=general; groups=$splits
  reason="one idle general CI runner per shard takes every shard"
elif [ "$local_idle" -ge "$splits" ]; then
  backend="$LOCAL"; class=local; groups=$splits; free=$((local_idle - splits))
  reason="one idle local slot per shard takes every shard"
else
  reason="${general_reason:-fewer idle general runners than shards}; ${local_reason:-fewer idle local slots than shards}; all hosted"
fi

browser_fixture="$HOSTED"; fuzz="$HOSTED"
[ "${LEAF_BROWSER_FIXTURE:-false}" = "true" ] && take browser_fixture 1
[ "${LEAF_FUZZ:-false}" = "true" ] && take fuzz "$FUZZ_SLOTS"

out "backend_runner=$backend"
out "runner_class=$class"
out "self_hosted=$([ "$class" != hosted ] && echo true || echo false)"
out "observed_idle=$groups"
out "routing_reason=$reason"
out "fuzz_runner=$fuzz"
out "browser_fixture_runner=$browser_fixture"
note "backend groups on $class: \`$groups / ${splits:-0}\` ($reason)"
note "fuzz: \`$fuzz\`, browser fixture: \`$browser_fixture\`"
exit 0
