#!/bin/bash
# Run the universal image smoke as the agent user, with networking disabled.
#
# The container command is the Codex entrypoint, which selects the Preloop
# CODEX_ENV_* runtimes before the smoke script. --negative-tool removes one
# required tool in a throwaway layer and passes only if the smoke then fails
# and names that tool.
#
# Usage:
#   environments/universal/run-smoke.sh IMAGE [--user UID:GID] [--negative-tool TOOL]
set -euo pipefail

usage() {
    echo "usage: $0 IMAGE [--user UID:GID] [--negative-tool TOOL]" >&2
    exit 2
}

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$dir/tools.sh"

[ $# -ge 1 ] || usage
image="$1"
shift
user_args=()
negative_tool=""
while [ $# -gt 0 ]; do
    case "$1" in
        --user)
            user_args=(--user "${2:?}")
            shift 2
            ;;
        --negative-tool)
            negative_tool="${2:?}"
            shift 2
            ;;
        *) usage ;;
    esac
done

known_tool() {
    local name="$1"
    local tool
    for tool in "${UNIVERSAL_TOOLS[@]}"; do
        if [ "$tool" = "$name" ]; then
            return 0
        fi
    done
    return 1
}

env_args=()
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        "" | \#*) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
        CODEX_ENV_*) env_args+=(-e "${key}=${value}") ;;
        *) echo "unexpected codex-env.sh line: $line" >&2; exit 1 ;;
    esac
done < "$dir/codex-env.sh"

base="$(
    docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.base.name"}}' \
        "$image"
)"
if [ -z "$base" ]; then
    echo "image has no org.opencontainers.image.base.name label" >&2
    exit 1
fi
env_args+=(-e "CODEX_BASE_IMAGE=${base}")

run_smoke() {
    # Check the final image, including binaries copied after its build smoke.
    # Supply the guard from this checkout rather than trusting an older image.
    docker run --rm -i --network none \
        --entrypoint /opt/entrypoint.sh \
        ${env_args[@]+"${env_args[@]}"} \
        ${user_args[@]+"${user_args[@]}"} \
        "$1" \
        -s < "$dir/check-preflight.sh" || return $?
    # ${user_args[@]+...} stays safe when the array is empty under set -u.
    docker run --rm --network none \
        --entrypoint /opt/entrypoint.sh \
        ${env_args[@]+"${env_args[@]}"} \
        ${user_args[@]+"${user_args[@]}"} \
        "$1" \
        /opt/preloop-universal/smoke.sh
}

expect_pass_lines() {
    local output="$1"
    local lang
    local count
    count="$(grep -c '^PASS ' <<<"$output" || true)"
    if [ "$count" -ne 8 ]; then
        echo "FAIL: expected 8 PASS lines, found ${count}" >&2
        return 1
    fi
    for lang in preloop-preflight python java go rust typescript perl shell; do
        if ! grep -qx "PASS ${lang}" <<<"$output"; then
            echo "FAIL: missing PASS ${lang}" >&2
            return 1
        fi
    done
}

if [ -n "$negative_tool" ]; then
    if ! known_tool "$negative_tool"; then
        echo "unknown tool: $negative_tool" >&2
        exit 2
    fi
    broken="preloop-universal-smoke-negative:${negative_tool}"
    default_user="$(docker image inspect --format '{{.Config.User}}' "$image")"
    empty="$(mktemp -d)"
    cp "$dir/remove-tool.sh" "$empty/remove-tool.sh"
    docker build -q -t "$broken" -f - "$empty" >/dev/null <<EOF
FROM ${image}
USER root
COPY remove-tool.sh /tmp/remove-tool.sh
RUN bash --login /tmp/remove-tool.sh ${negative_tool}
USER ${default_user:-root}
EOF
    rm -rf "$empty"
    set +e
    output="$(run_smoke "$broken" 2>&1)"
    status=$?
    set -e
    docker rmi -f "$broken" >/dev/null
    printf '%s\n' "$output"
    echo "negative smoke (${negative_tool} removed) exit status: ${status}"
    if [ "$status" -eq 0 ]; then
        echo "FAIL: smoke passed without ${negative_tool}" >&2
        exit 1
    fi
    if ! grep -q "missing required tool: ${negative_tool}" <<<"$output"; then
        echo "FAIL: smoke failed but did not name ${negative_tool}" >&2
        exit 1
    fi
    echo "OK: smoke fails and names the missing tool"
    exit 0
fi

echo "image: $image"
echo "image id: $(docker image inspect --format '{{.Id}}' "$image")"
echo "repo digests: $(docker image inspect --format '{{join .RepoDigests " "}}' "$image")"
echo "size bytes: $(docker image inspect --format '{{.Size}}' "$image")"
echo "base image: ${base}"
echo "entrypoint: /opt/entrypoint.sh"
echo "network: none"

log="$(mktemp)"
trap 'rm -f "$log"' EXIT
set +e
run_smoke "$image" 2>&1 | tee "$log"
status="${PIPESTATUS[0]}"
set -e
echo "smoke exit status: ${status}"
if [ "$status" -ne 0 ]; then
    exit "$status"
fi
expect_pass_lines "$(cat "$log")"

# The guard comes from this checkout, like check-preflight.sh. It runs as
# root and checks the unprivileged agent user hosted Codex runs as.
if [ "${#user_args[@]}" -eq 0 ]; then
    agent_out="$(docker run --rm -i --network none \
        --entrypoint /opt/entrypoint.sh \
        ${env_args[@]+"${env_args[@]}"} \
        "$image" \
        -s < "$dir/check-agent-user.sh")"
    grep -v '^PASS ' <<<"$agent_out"
    if ! grep -q '^agent-user-check: OK' <<<"$agent_out"; then
        echo "FAIL: the agent user check did not finish" >&2
        exit 1
    fi
fi
