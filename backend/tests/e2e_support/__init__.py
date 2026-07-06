"""Test-support helpers for the T10 custom-agent / per-run-session E2E.

These modules exist to stand up a deterministic CI harness for the Playwright
E2E in ``preloop/frontend/tests/e2e``. They are NOT imported by product code and
are not part of the pytest suite; they are standalone scripts the E2E harness
invokes:

* :mod:`fake_upstream` -- a tiny OpenAI-compatible model server so the gateway's
  ``litellm`` call resolves without a real provider key.
* :mod:`seed_gateway_model` -- seeds a gateway-enabled ``AIModel`` for the
  ``INIT_TEST_DATA`` admin account whose ``api_endpoint`` points at the fake
  upstream.
"""
