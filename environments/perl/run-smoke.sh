#!/bin/bash
# Run the Perl toolchain smoke inside a built image, as the agent user, with
# networking disabled, and print the evidence fields.
#
# Usage:
#   environments/perl/run-smoke.sh IMAGE [--user UID:GID] [--negative-tool TOOL]
#
# --user defaults to 10000:10000, the user the hosted Docker executor runs
# agent containers as. --negative-tool removes one required tool in a
# throwaway layer and succeeds only if the smoke then fails and names it.
set -euo pipefail

usage() {
    echo "usage: $0 IMAGE [--user UID:GID] [--negative-tool TOOL]" >&2
    exit 2
}

[ $# -ge 1 ] || usage
image="$1"
shift
user="10000:10000"
negative_tool=""
while [ $# -gt 0 ]; do
    case "$1" in
        --user) user="${2:?}"; shift 2 ;;
        --negative-tool) negative_tool="${2:?}"; shift 2 ;;
        *) usage ;;
    esac
done

smoke=/opt/preloop-perl/perl-toolchain-smoke.sh

if [ -n "$negative_tool" ]; then
    case "$negative_tool" in
        perl | cpanm | perlver | perlcritic | prove) ;;
        *) echo "unknown tool: $negative_tool" >&2; exit 2 ;;
    esac
    broken="preloop-perl-smoke-negative:$negative_tool"
    # Shadow the tool on PATH with nothing: remove every copy found.
    printf 'FROM %s\nUSER root\nRUN for p in $(which -a %s); do rm -f "$p"; done; ! command -v %s\n' \
        "$image" "$negative_tool" "$negative_tool" |
        docker build -q -t "$broken" - >/dev/null
    set +e
    output="$(docker run --rm --network none --user "$user" --entrypoint bash "$broken" "$smoke" 2>&1)"
    status=$?
    set -e
    docker rmi -f "$broken" >/dev/null
    echo "$output"
    echo "negative smoke ($negative_tool removed) exit status: $status"
    if [ "$status" -eq 0 ]; then
        echo "FAIL: smoke passed without $negative_tool" >&2
        exit 1
    fi
    if ! grep -q "missing required tool: $negative_tool" <<<"$output"; then
        echo "FAIL: smoke failed but did not name $negative_tool" >&2
        exit 1
    fi
    echo "OK: smoke fails and names the missing tool"
    exit 0
fi

echo "image: $image"
echo "image id: $(docker image inspect --format '{{.Id}}' "$image")"
echo "repo digests: $(docker image inspect --format '{{join .RepoDigests " "}}' "$image")"
echo "base image: $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.base.name"}}' "$image")"
echo "entrypoint: $(docker image inspect --format '{{json .Config.Entrypoint}}' "$image")"
echo "network: none"
set +e
docker run --rm --network none --user "$user" --entrypoint bash "$image" "$smoke"
status=$?
set -e
echo "smoke exit status: $status"
exit "$status"
