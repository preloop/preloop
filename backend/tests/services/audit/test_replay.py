"""The operator replay command defaults to a scoped, bounded dry run."""

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from preloop.services.audit import replay

ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("apply", [False, True])
def test_replay_requires_explicit_apply_and_preserves_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], apply: bool
) -> None:
    factory = MagicMock()
    monkeypatch.setattr(replay, "get_session_factory", lambda: factory)
    requeue = MagicMock(return_value=2)
    monkeypatch.setattr(replay.crud_audit_outbox, "requeue_failed", requeue)
    argv = ["--account-id", ACCOUNT_ID, "--limit", "5"]
    if apply:
        argv.append("--apply")
    assert replay.main(argv) == 0
    requeue.assert_called_once_with(
        factory.return_value.__enter__.return_value,
        account_id=UUID(ACCOUNT_ID),
        limit=5,
        dry_run=not apply,
    )
    output = capsys.readouterr().out
    assert ("Requeued 2" if apply else "Would requeue 2") in output


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--account-id", "invalid"],
        ["--account-id", ACCOUNT_ID, "--limit", "0"],
        ["--account-id", ACCOUNT_ID, "--limit", "1001"],
    ],
)
def test_invalid_replay_arguments_never_open_a_session(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    factory = MagicMock()
    monkeypatch.setattr(replay, "get_session_factory", factory)
    with pytest.raises(SystemExit) as exc:
        replay.main(argv)
    assert exc.value.code == 2
    factory.assert_not_called()
