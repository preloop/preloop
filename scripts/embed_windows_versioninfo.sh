#!/usr/bin/env sh
# Embed Windows PE version resources into the CLI main package before go build.
#
# Usage: embed_windows_versioninfo.sh <version> [arch]
#   version: semver without leading v (e.g. 1.2.3 or 1.2.3-beta.1)
#   arch:    amd64|arm64|all (default: all)
#
# Requires: go, and installs github.com/tc-hib/go-winres if missing.

set -eu

VERSION="${1:-}"
ARCH="${2:-all}"

if [ -z "$VERSION" ]; then
  echo "usage: $0 <version> [amd64|arm64|all]" >&2
  exit 2
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CLI_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/../cli" && pwd)"
TEMPLATE="${CLI_DIR}/winres/winres.json"
OUT_DIR="${CLI_DIR}/cmd/preloop"
GEN_JSON="${CLI_DIR}/winres/winres.generated.json"

# FileVersion / ProductVersion fixed fields need four numeric components.
VERSION_QUAD="$(
  printf '%s' "$VERSION" | awk -F'[.+-]' '{
    a=$1+0; b=$2+0; c=$3+0;
    printf "%d.%d.%d.0", a, b, c
  }'
)"

if [ ! -f "$TEMPLATE" ]; then
  echo "missing winres template: $TEMPLATE" >&2
  exit 1
fi

# Portable JSON rewrite without jq.
VERSION_ESC="$(printf '%s' "$VERSION" | sed 's/[\\/&]/\\&/g')"
QUAD_ESC="$(printf '%s' "$VERSION_QUAD" | sed 's/[\\/&]/\\&/g')"
sed \
  -e "s/\"file_version\": \"[^\"]*\"/\"file_version\": \"${QUAD_ESC}\"/" \
  -e "s/\"product_version\": \"[^\"]*\"/\"product_version\": \"${QUAD_ESC}\"/" \
  -e "s/\"FileVersion\": \"[^\"]*\"/\"FileVersion\": \"${VERSION_ESC}\"/" \
  -e "s/\"ProductVersion\": \"[^\"]*\"/\"ProductVersion\": \"${VERSION_ESC}\"/" \
  -e "/\"identity\": {/,/^[[:space:]]*}/ s/\"version\": \"[^\"]*\"/\"version\": \"${QUAD_ESC}\"/" \
  "$TEMPLATE" > "$GEN_JSON"

if ! command -v go-winres >/dev/null 2>&1; then
  go install github.com/tc-hib/go-winres@v0.3.3
  export PATH="$(go env GOPATH)/bin:${PATH}"
fi

case "$ARCH" in
  amd64|arm64) arch_args="$ARCH" ;;
  all) arch_args="amd64,arm64" ;;
  *)
    echo "unsupported arch: $ARCH" >&2
    exit 2
    ;;
esac

# go-winres writes <out>_windows_<arch>.syso next to the main package.
go-winres make --in "$GEN_JSON" --out "${OUT_DIR}/rsrc" --arch "$arch_args"
echo "Embedded Windows version info ${VERSION} (${VERSION_QUAD}) for ${arch_args}"
