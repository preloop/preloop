"""Regression cases for indirect/local DC destinations and malformed origins."""

import ipaddress

import pytest

from preloop.utils.bitbucket_dc import (
    BitbucketDCConfigError,
    check_destination_address,
    parse_instance_url,
    resolve_pinned_address,
)


@pytest.mark.parametrize(
    "address",
    [
        "168.63.129.16",  # Azure platform virtual IP, never a repository instance.
        "2002:7f00:1::",  # 6to4 embedding loopback.
        "2002:a00:1::",  # 6to4 embedding private IPv4.
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo transition address.
    ],
)
def test_platform_and_transition_destinations_never_allowed(address: str) -> None:
    with pytest.raises(BitbucketDCConfigError):
        check_destination_address(
            address,
            allowed_private=[
                ipaddress.ip_network("0.0.0.0/0"),
                ipaddress.ip_network("::/0"),
            ],
        )


def test_shared_address_space_requires_explicit_network_approval() -> None:
    with pytest.raises(BitbucketDCConfigError):
        check_destination_address("100.64.0.1", allowed_private=[])
    assert (
        str(
            check_destination_address(
                "100.64.0.1", allowed_private=[ipaddress.ip_network("100.64.0.0/10")]
            )
        )
        == "100.64.0.1"
    )


@pytest.mark.parametrize(
    "url", ["https://[broken", "https://[example.com]/scm", "https://example.com:abc"]
)
def test_malformed_authority_has_bounded_config_error(url: str) -> None:
    with pytest.raises(BitbucketDCConfigError):
        parse_instance_url(url)


def test_mixed_dns_answers_fail_closed() -> None:
    identity = parse_instance_url("https://bitbucket.example.com")
    with pytest.raises(BitbucketDCConfigError):
        resolve_pinned_address(
            identity,
            resolver=lambda host, port: ["93.184.216.34", "169.254.169.254"],
            allowed_private=[],
        )
