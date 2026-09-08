"""Signature construction and the verification a receiver should perform."""

import hashlib
import hmac
import json

import pytest

from preloop.services.event_webhooks.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    compute_signature,
    generate_secret,
    parse_signature_header,
    secret_hint,
    signature_header,
    verify_signature,
)

BODY = json.dumps({"id": "abc", "type": "approval.created"}).encode("utf-8")
SECRET = "whsec_test_secret_value"


def test_signature_is_hmac_over_timestamp_dot_body():
    expected = hmac.new(
        SECRET.encode(), b"1757328000." + BODY, hashlib.sha256
    ).hexdigest()
    assert compute_signature(SECRET, 1757328000, BODY) == expected


def test_header_carries_timestamp_and_v1():
    header = signature_header(SECRET, BODY, 1757328000)
    assert header.startswith("t=1757328000,v1=")
    timestamp, signatures = parse_signature_header(header)
    assert timestamp == 1757328000
    assert signatures == [compute_signature(SECRET, 1757328000, BODY)]


def test_verify_accepts_a_fresh_signature():
    header = signature_header(SECRET, BODY, 1757328000)
    assert verify_signature(SECRET, header, BODY, now=1757328000) is True


def test_verify_accepts_inside_tolerance_and_rejects_outside():
    header = signature_header(SECRET, BODY, 1757328000)
    inside = 1757328000 + DEFAULT_TOLERANCE_SECONDS
    outside = inside + 1
    assert verify_signature(SECRET, header, BODY, now=inside) is True
    assert verify_signature(SECRET, header, BODY, now=outside) is False


def test_verify_rejects_a_tampered_body():
    header = signature_header(SECRET, BODY, 1757328000)
    assert verify_signature(SECRET, header, BODY + b" ", now=1757328000) is False


def test_verify_rejects_a_wrong_secret():
    header = signature_header(SECRET, BODY, 1757328000)
    assert verify_signature("whsec_other", header, BODY, now=1757328000) is False


@pytest.mark.parametrize("header", ["", "v1=abc", "t=notanumber,v1=abc", "t=1"])
def test_verify_rejects_malformed_headers(header):
    assert verify_signature(SECRET, header, BODY, now=1757328000) is False


def test_parse_ignores_unknown_scheme_parts():
    timestamp, signatures = parse_signature_header("t=5,v0=old,v1=new,x=y")
    assert timestamp == 5
    assert signatures == ["new"]


def test_generated_secrets_are_prefixed_and_unique():
    first = generate_secret()
    second = generate_secret()
    assert first.startswith("whsec_")
    assert first != second
    assert secret_hint(first) == first[-4:]
