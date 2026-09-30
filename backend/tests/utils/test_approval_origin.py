"""Origin labels must distinguish native processes sharing a credential."""

from preloop.utils.approval_origin import approval_origin_text
from preloop.services.push_notifications.notification_payloads import (
    NotificationPayloadBuilder,
)


def test_parallel_native_origin_labels() -> None:
    for session_id, model in [
        ("11111111-first", "gpt-alpha"),
        ("22222222-second", "gpt-beta"),
    ]:
        args = {
            "_preloop_source": "codex_cli",
            "_preloop_origin": {"session_id": session_id, "model": model},
        }
        text = f"Session: {session_id} · Model: {model}"
        assert approval_origin_text(args, "shared-credential") == text
        payload = NotificationPayloadBuilder.new_approval_request(
            "request-one", "Bash", tool_args=args, agent_name="Codex"
        )
        assert text in payload["aps"]["alert"]["subtitle"]


def test_unknown_native_origin_does_not_borrow_credential_session() -> None:
    assert (
        approval_origin_text({"_preloop_source": "codex_cli"}, "shared-session")
        == "Session: Unknown · Model: Unknown"
    )
    assert (
        approval_origin_text({}, "recorded-session")
        == "Session: recorded-session · Model: Unknown"
    )
    assert approval_origin_text({}) is None
