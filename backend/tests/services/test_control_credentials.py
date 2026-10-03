"""Scoped employee credentials stay out of durable plaintext and projections."""

from preloop.utils.control_credentials import (
    hydrate_control_credentials,
    protect_control_credentials,
)
from preloop.utils.redaction import redact_dict


def test_gateway_credential_encrypted_in_storage_redacted_in_history():
    original = {
        "payload": {
            "metadata": {
                "gateway": {
                    "api_key": "synthetic-execution-token",
                    "model": "example",
                }
            }
        }
    }
    stored = protect_control_credentials(original)
    assert "synthetic-execution-token" not in str(stored)
    assert "api_key" not in stored["payload"]["metadata"]["gateway"]
    assert hydrate_control_credentials(stored) == original
    assert "synthetic-execution-token" not in str(redact_dict(original))
    assert stored["payload"]["metadata"]["gateway"]["encrypted_api_key"] not in str(
        redact_dict(stored)
    )
    assert (
        original["payload"]["metadata"]["gateway"]["api_key"]
        == "synthetic-execution-token"
    )
