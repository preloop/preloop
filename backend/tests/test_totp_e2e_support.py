"""Privacy checks for the standalone browser harness's failure diagnostics."""

import json

from tests.e2e_support.totp_server import failure_details


def test_failure_details_keep_stack_metadata_without_proofs() -> None:
    """SQL parameters, source lines and connection strings stay out of CI."""
    trace = """Traceback (most recent call last):
  File "/scratch/browser-fixture/totp_server.py", line 50, in seed_approval
    submit("synthetic-private-proof")
sqlalchemy.exc.OperationalError: postgresql://user:synthetic-password@localhost
[parameters: {"token": "synthetic-private-proof"}]
"""
    assert failure_details(trace) == (
        "totp_server.py:50 in seed_approval\nOperationalError"
    )


def test_failure_details_decode_json_traces_without_log_messages() -> None:
    """Structured app logs expose only the embedded traceback metadata."""
    log = json.dumps(
        {
            "message": "synthetic-private-proof",
            "exc_info": (
                '  File "/scratch/browser-fixture/app.py", line 10, in lifespan\n'
                "RuntimeError: synthetic-private-proof"
            ),
        }
    )
    assert failure_details(log) == "app.py:10 in lifespan\nRuntimeError"
    assert failure_details("synthetic-private-proof") == (
        "No traceback metadata available"
    )
