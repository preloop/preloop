#!/usr/bin/env bash
# Prefer a matching system interpreter when it can create a venv.
#
# actions/setup-python downloads from actions/python-versions, whose
# manifest has Ubuntu and RHEL builds only. Debian 12 therefore errors
# with "not found for debian 12" even when python3.11 is already on the
# machine. Distro Python is also PEP 668 managed, so CI cannot pip-install
# into it: we only use the system interpreter through a venv. If none
# matches, or venv is missing (Debian without python3.11-venv), the
# caller runs setup-python.

set -euo pipefail

WANT="${PYTHON_VERSION:?PYTHON_VERSION is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${GITHUB_PATH:?GITHUB_PATH is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

use_setup=true

finish() {
  echo "use_setup=${use_setup}" >> "${GITHUB_OUTPUT}"
}
trap finish EXIT

# "3.11" or "3.11.16" -> "3.11"
major="${WANT%%.*}"
rest="${WANT#*.}"
minor="${rest%%.*}"
MM="${major}.${minor}"

matches_want() {
  local py="$1"
  local reported
  reported="$("${py}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 1
  [ "${reported}" = "${MM}" ]
}

find_system_python() {
  local candidate py
  for candidate in "python${MM}" python3 python; do
    py="$(command -v "${candidate}" 2>/dev/null)" || continue
    if matches_want "${py}"; then
      printf '%s\n' "${py}"
      return 0
    fi
  done
  return 1
}

py=""
if ! py="$(find_system_python)"; then
  echo "No system Python ${MM} on PATH; will use actions/setup-python"
  exit 0
fi

venv_dir="${RUNNER_TEMP}/preloop-python"
rm -rf "${venv_dir}"
if ! "${py}" -m venv "${venv_dir}"; then
  echo "::warning::System Python ${py} cannot create a venv; install python${MM}-venv on Debian. Falling back to actions/setup-python, which has no ${MM} build for this distro."
  rm -rf "${venv_dir}"
  exit 0
fi

if [ ! -x "${venv_dir}/bin/python" ] || [ ! -x "${venv_dir}/bin/pip" ]; then
  echo "venv at ${venv_dir} is missing python or pip; will use actions/setup-python"
  rm -rf "${venv_dir}"
  exit 0
fi

# Debian 12's venv bundles pip 23.0.1, which rejects a transitive extra
# (fastmcp-slim -> "pydantic[email]>=...") under --require-hashes even
# though app-dev.txt pins pydantic. setup-python ships a current pip, so
# hosted shards never saw it. Match that before the hashed install.
if ! "${venv_dir}/bin/python" -m pip install --quiet --disable-pip-version-check "pip==25.2"; then
  echo "venv at ${venv_dir} could not upgrade pip; will use actions/setup-python"
  rm -rf "${venv_dir}"
  exit 0
fi

echo "${venv_dir}/bin" >> "${GITHUB_PATH}"
use_setup=false
echo "Using system Python ${py} (${MM}) via venv ${venv_dir}"
"${venv_dir}/bin/python" -V
