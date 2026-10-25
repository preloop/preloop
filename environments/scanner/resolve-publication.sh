#!/usr/bin/env bash
# Select the scanner image tag without changing any git tags.
set -euo pipefail

publish=false
publish_latest=false
release_tag=''
if [[ "${EVENT_NAME:-}" == push && "${REF_TYPE:-}" == tag && "${REF_NAME:-}" == v* ]]; then
    publish=true
    release_tag="$REF_NAME"
    if [[ "$release_tag" != *-* ]]; then
        publish_latest=true
    fi
elif [[ "${EVENT_NAME:-}" == workflow_dispatch && "${MANUAL_PUBLISH:-}" == true ]]; then
    release_tag="${RELEASE_TAG:-}"
    # Restrict input to version tags safe for both OCI tags and the GitHub API.
    if [[ ! "$release_tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]] || (( ${#release_tag} > 128 )); then
        echo 'Manual publishing requires a release_tag such as v0.17.0 or v0.17.0-rc.1.' >&2
        exit 1
    fi
    gh api "repos/${REPOSITORY:?}/git/ref/tags/$release_tag" --silent
    publish=true
fi
{
    echo "publish=$publish"
    echo "release_tag=$release_tag"
    echo "publish_latest=$publish_latest"
} >> "${GITHUB_OUTPUT:?}"
