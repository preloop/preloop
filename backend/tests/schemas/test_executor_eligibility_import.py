"""Model schemas must load before the API package, including in CLI helpers."""

import os
import subprocess
import sys


def test_model_schema_first_import_has_no_api_cycle() -> None:
    """A fresh process exercises the import order hidden by the test app."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from preloop.models.schemas import FlowResponse; "
            "from preloop.schemas.ai_model import AIModelRead; "
            "assert 'executor_eligibility' in FlowResponse.model_fields; "
            "assert 'executor_eligibility' in AIModelRead.model_fields",
        ],
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
