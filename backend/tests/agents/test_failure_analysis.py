"""Tests for agent-log failure analysis.

The log fixtures here reproduce the shape an agent CLI produces when the model
gateway in front of it returns a proxy timeout: an internal retry loop, an HTML
error page from the proxy, a long stack trace, and a final line where the JS
runtime stringified an error object instead of serialising it.
"""

from preloop.agents.container import ContainerAgentExecutor
from preloop.agents.failure_analysis import analyze_agent_failure
from preloop.services.upstream_errors import (
    ERROR_CLASS_UPSTREAM_AUTH,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
)


def _gateway_timeout_logs(status: int = 504) -> str:
    """Agent logs for an upstream proxy timeout, in the real observed shape."""
    return "\n".join(
        ["Starting agent run", "Reading changed files", "Building review prompt"]
        + [f"processing hunk {index}" for index in range(300)]
        + [
            f"Attempt 1 failed with status {status}. Retrying with backoff... <html>",
            f"<head><title>{status} Gateway Time-out</title></head>",
            f"<body><center><h1>{status} Gateway Time-out</h1></center>",
            "<hr><center>nginx</center>",
            "</body></html>",
            f"Attempt 2 failed with status {status}. Retrying with backoff...",
            f"Attempt 3 failed with status {status}. Max attempts reached.",
        ]
        + [
            f"    at frame{index} (/usr/lib/agent-cli/index.js:1:1)"
            for index in range(200)
        ]
        + [
            "  at makeRequest (/usr/lib/agent-cli/index.js:1:1)",
            f"  status: {status}",
            "}",
            "An unexpected critical error occurred:[object Object]",
        ]
    )


class TestUpstreamTimeoutExtraction:
    """The incident case: a proxy timeout must be named, not swallowed."""

    def test_names_status_and_retry_exhaustion(self):
        analysis = analyze_agent_failure(_gateway_timeout_logs())

        assert analysis.upstream_status == 504
        assert analysis.attempts == 3
        assert analysis.retry_exhausted is True
        assert "504" in analysis.message
        assert "3 attempts" in analysis.message
        assert analysis.message.lower().startswith("upstream model provider")

    def test_does_not_surface_the_useless_object_dump(self):
        """The pre-fix output was exactly this 69-char string."""
        analysis = analyze_agent_failure(_gateway_timeout_logs())

        assert "[object Object]" not in analysis.message
        assert analysis.message.strip() != (
            "status: 504\n}\nAn unexpected critical error occurred:[object Object]"
        )
        # Nor may it be a bare stack-trace tail.
        assert "at makeRequest" not in analysis.message

    def test_executor_extraction_uses_the_analysis(self):
        """The executor's extraction hook must produce the same answer."""
        executor = ContainerAgentExecutor(
            agent_type="codex", config={}, image="test:latest", use_kubernetes=True
        )

        message = executor._extract_error_from_logs(_gateway_timeout_logs())

        assert "504" in message
        assert "[object Object]" not in message
        assert len(message) > 0


class TestTransience:
    """Transient vs terminal drives whether a retry is allowed at all."""

    def test_gateway_timeout_is_transient(self):
        assert analyze_agent_failure(_gateway_timeout_logs(504)).transient is True

    def test_bad_gateway_is_transient(self):
        assert analyze_agent_failure(_gateway_timeout_logs(502)).transient is True

    def test_auth_failure_is_not_transient(self):
        analysis = analyze_agent_failure(_gateway_timeout_logs(401))

        assert analysis.transient is False
        assert analysis.error_class == ERROR_CLASS_UPSTREAM_AUTH
        assert "401" in analysis.message

    def test_forbidden_is_not_transient(self):
        assert analyze_agent_failure(_gateway_timeout_logs(403)).transient is False

    def test_not_found_model_is_not_transient(self):
        assert analyze_agent_failure(_gateway_timeout_logs(404)).transient is False

    def test_rate_limit_is_transient(self):
        assert analyze_agent_failure(_gateway_timeout_logs(429)).transient is True

    def test_exhausted_quota_is_not_transient(self):
        """A hard quota exhaustion is a 429 that retrying can never fix."""
        logs = "\n".join(
            [
                "Attempt 1 failed with status 429: You exceeded your current quota.",
                "Attempt 2 failed with status 429: You exceeded your current quota.",
                "Max attempts reached.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.error_class == ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED
        assert analysis.transient is False

    def test_connection_reset_without_status_is_transient(self):
        logs = "\n".join(
            [
                "Attempt 1 failed: connection reset by peer",
                "Attempt 2 failed: connection reset by peer",
                "Giving up.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is True
        assert analysis.retry_exhausted is True

    def test_undici_other_side_closed_is_transient(self):
        """A gateway pod cycling mid-stream severs the agent's socket.

        Observed during a rollout: the pod was SIGKILLed while the agent was
        mid-response, and undici surfaced ``SocketError: other side closed``.
        This is exactly as retryable as a connection reset.
        """
        logs = "\n".join(
            [
                "Attempt 1 failed: SocketError: other side closed. Retrying...",
                "Attempt 2 failed: SocketError: other side closed. Retrying...",
                "Attempt 3 failed: SocketError: other side closed.",
                "Max attempts reached.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is True
        assert analysis.retry_exhausted is True

    def test_undici_fetch_terminated_is_transient(self):
        """undici's fetch reports a severed stream as ``TypeError: terminated``."""
        logs = "\n".join(
            [
                "Attempt 1 failed: TypeError: terminated. Retrying with backoff...",
                "Attempt 2 failed: TypeError: terminated. Retrying with backoff...",
                "Giving up.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is True
        assert analysis.retry_exhausted is True

    def test_undici_fetch_failed_is_transient(self):
        """undici wraps most transport failures as ``TypeError: fetch failed``.

        This is the most common shape Node's fetch produces for a connection
        that never completed; it deserves a retry exactly like an explicit
        socket error.
        """
        logs = "\n".join(
            [
                "Attempt 1 failed: TypeError: fetch failed. Retrying...",
                "Attempt 2 failed: TypeError: fetch failed.",
                "Max attempts reached.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is True
        assert analysis.retry_exhausted is True

    def test_raw_severed_stream_stack_is_transient(self):
        """The incident shape: a raw undici stack with no retry-loop phrasing.

        A CLI that crashes outright on a severed stream never prints
        "attempt N failed" — the only signal is the stack itself, on the
        fallback (non-attempt-line) path. It must still classify transient,
        and the matched lines must be carried as evidence.
        """
        logs = "\n".join(
            [
                "Streaming model response...",
                "TypeError: terminated",
                "    at Fetch.onAborted (node:internal/deps/undici/undici:11190:53)",
                "  [cause]: SocketError: other side closed",
                "      at TLSSocket.onSocketEnd (node:internal/deps/undici/undici:8117:26)",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is True
        assert "other side closed" in analysis.evidence

    def test_generic_terminated_wording_is_not_transient(self):
        """Only undici's error shape may match — not any use of 'terminated'.

        An agent killed by policy or by an operator also says "terminated";
        retrying those burns money and can never succeed.

        Contract note: this first-pass ``transient=False`` verdict is what
        production actually enforces — the executor attaches the analysis to
        the agent result and ``_retry_decision`` consults it directly. The
        generated *message* for this shape ("was unreachable after 2
        attempts") re-analyses as transient, which is why the verdict must
        travel with the result; the end-to-end guarantee is pinned by
        ``TestRetryVerdictWiring`` in
        ``tests/services/test_flow_orchestrator_retry.py``.
        """
        logs = "\n".join(
            [
                "Attempt 1 failed: run terminated by policy",
                "Attempt 2 failed: run terminated by policy",
                "Giving up.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is False

    def test_raw_policy_termination_is_not_transient(self):
        """The fallback path must not over-match either: a bare policy kill."""
        logs = "\n".join(
            [
                "Reviewing diff...",
                "ERROR: run terminated by policy",
                "Shutting down.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is False

    def test_plain_agent_error_is_not_transient(self):
        """No upstream signal at all must never be treated as retryable."""
        logs = "\n".join(
            [
                "Starting agent run",
                "ERROR: The requested tool is not permitted by policy.",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.transient is False
        assert analysis.upstream_status is None
        assert "not permitted by policy" in analysis.message


class TestLowValueLines:
    """Information-free lines must never win over a real signal."""

    def test_object_object_tail_does_not_mask_a_real_error(self):
        logs = "\n".join(
            [
                "Starting agent run",
                "ERROR: Repository checkout failed: reference not found.",
                "  at run (/usr/lib/agent-cli/index.js:1:1)",
                "}",
                "An unexpected critical error occurred:[object Object]",
            ]
        )

        message = analyze_agent_failure(logs).message

        assert "Repository checkout failed" in message

    def test_pure_noise_still_returns_something(self):
        logs = "\n".join(["}", "An unexpected critical error occurred:[object Object]"])

        message = analyze_agent_failure(logs).message

        assert message  # never empty when there were logs at all

    def test_empty_logs_return_empty_message(self):
        assert analyze_agent_failure("").message == ""


class TestMessageHygiene:
    """Whatever is surfaced is capped and scrubbed."""

    def test_message_is_length_capped(self):
        logs = "ERROR: " + ("x" * 5000)

        assert len(analyze_agent_failure(logs).message) <= 400

    def test_secrets_are_scrubbed(self):
        logs = "\n".join(
            [
                "ERROR: request rejected",
                "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz0123456789",
            ]
        )

        message = analyze_agent_failure(logs).message

        assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in message


class TestIdempotence:
    """Re-analysing a generated message must reproduce the classification.

    The orchestrator persists only the generated sentence and later re-reads
    it to decide whether a retry is allowed. If analysing our own output lost
    the transient/terminal verdict, retries would silently stop working.
    """

    def test_reanalysis_preserves_transient_verdict(self):
        first = analyze_agent_failure(_gateway_timeout_logs(504))
        second = analyze_agent_failure(first.message)

        assert second.transient is True
        assert second.upstream_status == 504
        assert second.message == first.message

    def test_reanalysis_preserves_terminal_verdict(self):
        first = analyze_agent_failure(_gateway_timeout_logs(401))
        second = analyze_agent_failure(first.message)

        assert second.transient is False
        assert second.upstream_status == 401
