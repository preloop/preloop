#!/usr/bin/env bash
# Generate CycloneDX SBOMs for the three shipped Preloop components.
#
# Why this exists: Preloop sells SBOM verification, so it has to ship one.
# CRA Annex I Part II requires the manufacturer to identify and document the
# components of the product, and the standard artefact for that is a
# build-time SBOM.
#
# The SBOMs are derived from BUILT artefacts, not from manifests:
#   backend   installed distributions in a venv built from the hash-pinned
#             runtime lock plus the local package, so licence and version
#             metadata comes from wheel METADATA rather than a requirements
#             file
#   frontend  the resolved npm install tree (node_modules), not package.json
#   cli       the Go build graph for ./cmd/preloop under an explicit
#             GOOS/GOARCH, resolved through the module cache
#
# Usage:
#   scripts/generate_sbom.sh                  # all three components
#   scripts/generate_sbom.sh cli              # one or more of: backend frontend cli
#
# Environment:
#   SBOM_OUT_DIR   output directory            (default: <repo>/sbom)
#   SBOM_VERSION   version stamped into names  (default: contents of VERSION)
#   GOOS / GOARCH  CLI build constraints       (default: linux/amd64)
#
# Tool versions are pinned here and in .github/requirements/sbom.txt so a
# regenerated SBOM is comparable with the one attached to a release.

set -euo pipefail

CYCLONEDX_GOMOD_VERSION="v1.9.0"
CYCLONEDX_NPM_VERSION="4.0.2"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${SBOM_OUT_DIR:-${REPO_ROOT}/sbom}"
VERSION="${SBOM_VERSION:-$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")}"

COMPONENTS=("$@")
if [ ${#COMPONENTS[@]} -eq 0 ]; then
  COMPONENTS=(backend frontend cli)
fi

mkdir -p "${OUT_DIR}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

log() { printf '\n==> %s\n' "$*" >&2; }

# cyclonedx-py and cyclonedx-python-lib live in their own venv so that the
# generator never appears in the SBOM it generates, and so the schema
# validator is available whichever components were asked for.
TOOL_VENV="${WORK_DIR}/tool"
ensure_tool_venv() {
  [ -x "${TOOL_VENV}/bin/python" ] && return 0
  log "installing cyclonedx-py from .github/requirements/sbom.txt"
  python3 -m venv "${TOOL_VENV}"
  "${TOOL_VENV}/bin/pip" install --quiet --disable-pip-version-check \
    --require-hashes -r "${REPO_ROOT}/.github/requirements/sbom.txt"
}

generate_backend() {
  local target="${OUT_DIR}/preloop-sbom-backend-${VERSION}.cdx.json"
  log "backend: building an analysis venv from requirements/runtime.txt"
  # The analysis venv holds only what ships. cyclonedx-py lives in its own
  # venv and is pointed at the analysis interpreter, so the SBOM does not
  # end up describing the SBOM generator.
  python3 -m venv "${WORK_DIR}/app"
  "${WORK_DIR}/app/bin/pip" install --quiet --disable-pip-version-check \
    --require-hashes -r "${REPO_ROOT}/requirements/runtime.txt"
  "${WORK_DIR}/app/bin/pip" install --quiet --disable-pip-version-check \
    --no-deps "${REPO_ROOT}"

  ensure_tool_venv

  log "backend: generating ${target}"
  # --pyproject carries the PEP 621 root component metadata (name, version,
  # licence, authors) into metadata.component, which is what fills the
  # NTIA author/supplier fields that a manifest scan leaves null.
  "${TOOL_VENV}/bin/cyclonedx-py" environment "${WORK_DIR}/app/bin/python" \
    --pyproject "${REPO_ROOT}/pyproject.toml" \
    --mc-type application \
    --spec-version 1.6 \
    --output-format JSON \
    --output-file "${target}"
}

generate_frontend() {
  local target="${OUT_DIR}/preloop-sbom-frontend-${VERSION}.cdx.json"
  log "frontend: installing dependencies"
  # The full install tree, build tooling included. The console image ships a
  # bundle produced by that tooling, so a dev-omitted SBOM would understate
  # the supply chain that produced the shipped asset.
  (cd "${REPO_ROOT}/frontend" && npm ci --no-audit --no-fund)

  log "frontend: generating ${target}"
  (cd "${REPO_ROOT}/frontend" && npx --yes "@cyclonedx/cyclonedx-npm@${CYCLONEDX_NPM_VERSION}" \
    --spec-version 1.6 \
    --output-format JSON \
    --output-file "${target}" \
    --mc-type application)
}

generate_cli() {
  local target="${OUT_DIR}/preloop-sbom-cli-${VERSION}.cdx.json"
  local goos="${GOOS:-linux}"
  local goarch="${GOARCH:-amd64}"

  if ! command -v cyclonedx-gomod >/dev/null 2>&1; then
    log "cli: installing cyclonedx-gomod ${CYCLONEDX_GOMOD_VERSION}"
    GOBIN="${WORK_DIR}/bin" go install \
      "github.com/CycloneDX/cyclonedx-gomod/cmd/cyclonedx-gomod@${CYCLONEDX_GOMOD_VERSION}"
    PATH="${WORK_DIR}/bin:${PATH}"
    export PATH
  fi

  log "cli: generating ${target} for ${goos}/${goarch}"
  # Build constraints select modules, so the SBOM is only true for one
  # target. linux/amd64 is the default because that is what the container
  # images and the primary release binary use; the constraints are recorded
  # as properties on the main component.
  (cd "${REPO_ROOT}/cli" && GOOS="${goos}" GOARCH="${goarch}" cyclonedx-gomod app \
    -json \
    -licenses \
    -std \
    -main ./cmd/preloop \
    -output "${target}" \
    .)
}

for component in "${COMPONENTS[@]}"; do
  case "${component}" in
    backend) generate_backend ;;
    frontend) generate_frontend ;;
    cli) generate_cli ;;
    *)
      echo "unknown component: ${component}" >&2
      echo "expected one or more of: backend frontend cli" >&2
      exit 2
      ;;
  esac
done

ensure_tool_venv
log "stamping supplier, manufacturer and authors, validating, measuring quality"
"${TOOL_VENV}/bin/python" "${REPO_ROOT}/scripts/sbom_metadata.py" --validate \
  "${OUT_DIR}"/*.cdx.json

log "digests"
(
  cd "${OUT_DIR}"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum ./*.cdx.json
  else
    shasum -a 256 ./*.cdx.json
  fi
)
