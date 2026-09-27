#!/usr/bin/env bash
# Smoke test for the backend venv and frontend tree baked into the image.
#
# With no argument (the image build) it checks the venv imports the
# backend's runtime and test dependencies. With a checkout path it copies
# the checkout to /tmp and runs a database-backed backend test file and
# one frontend test file, with no network:
#   docker run --rm --network none --user 10000:10000 \
#     -v "$PWD:/src:ro" <image> /opt/preloop-pip/python-venv-smoke.sh /src
set -euo pipefail

python -c 'import fastapi, sqlalchemy, alembic, psycopg, pgvector, pytest, pytest_asyncio'
pytest --version
node --version

if [[ $# -eq 0 ]]; then
  exit 0
fi

src=$1
work=${TMPDIR:-/tmp}/preloop-smoke-checkout
rm -rf "$work"
mkdir -p "$work"
git -c safe.directory='*' -C "$src" ls-files -z --cached --others --exclude-standard \
  | tar -C "$src" --null --ignore-failed-read -T - -cf - \
  | tar -C "$work" -xf -
cd "$work"

export PYTHONDONTWRITEBYTECODE=1
# The runner must hand tests mock provider keys and drop agent tokens.
cat >backend/tests/test_zz_venv_smoke_env.py <<'PY'
import os


def test_agent_tokens_do_not_reach_tests():
    assert "PRELOOP_API_TOKEN" not in os.environ
    assert os.environ["OPENAI_API_KEY"] == "mock_key"
PY
PRELOOP_API_TOKEN=smoke-secret OPENAI_API_KEY=smoke-secret \
  preloop-pytest -q -p no:cacheprovider \
  backend/tests/services/test_model_gateway_account_binding.py \
  backend/tests/test_zz_venv_smoke_env.py

preloop-frontend-deps
(cd frontend && npx --no-install web-test-runner src/utils/agent-kinds.test.ts)
echo 'python-venv-smoke: backend and frontend tests passed offline'
