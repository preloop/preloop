"""The optional guard against webhook targets in internal address space."""

import pytest

from preloop.services.event_webhooks import targets


@pytest.fixture
def blocking(monkeypatch):
    """Turn the guard on for the duration of a test."""
    monkeypatch.setattr(targets.settings, "webhook_block_private_targets", True)


def test_the_guard_is_off_by_default(monkeypatch):
    """A self-hosted collector on a private network is the normal case."""
    monkeypatch.setattr(targets.settings, "webhook_block_private_targets", False)

    assert targets.blocked_target_reason("https://10.0.0.5/hook") is None


@pytest.mark.parametrize(
    "url,reason",
    [
        ("https://127.0.0.1/hook", "loopback"),
        ("http://[::1]/hook", "loopback"),
        ("https://169.254.169.254/latest/meta-data", "link-local"),
        ("https://10.1.2.3/hook", "private"),
        ("https://192.168.1.10/hook", "private"),
        ("https://172.16.4.4/hook", "private"),
        ("https://224.0.0.1/hook", "reserved"),
    ],
)
def test_literal_addresses_are_refused(blocking, url, reason):
    """Cloud metadata and cluster-internal literals are named, not guessed."""
    assert targets.blocked_target_reason(url) == reason


def test_a_name_that_resolves_into_private_space_is_refused(blocking, monkeypatch):
    """The check resolves, so a friendly hostname cannot smuggle 10/8 in."""
    monkeypatch.setattr(
        targets.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("10.0.0.9", 0))],
    )

    assert targets.blocked_target_reason("https://siem.internal/hook") == "private"


def test_a_public_name_is_allowed(blocking, monkeypatch):
    monkeypatch.setattr(
        targets.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    assert targets.blocked_target_reason("https://siem.example.com/hook") is None


def test_an_unresolvable_name_is_refused_rather_than_registered_blind(
    blocking, monkeypatch
):
    """It could not be delivered to anyway, and it cannot be checked."""

    def _fail(host, port):
        raise targets.socket.gaierror("nope")

    monkeypatch.setattr(targets.socket, "getaddrinfo", _fail)

    assert targets.blocked_target_reason("https://nowhere.invalid/hook") == (
        "unresolvable"
    )


def test_a_url_without_a_host_is_refused(blocking):
    assert targets.blocked_target_reason("https:///hook") == "no host"
