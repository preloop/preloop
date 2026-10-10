"""Validator for customer IdP tokens on the Anthropic gateway (#1414).

Uses locally generated RSA and EC keys and a stub JWKS; no network.
Each threat model bullet of #1414 maps to tests here or in
``tests/endpoints/test_anthropic_gateway_idp.py`` (see the PR body).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
import pytest

from preloop.services import gateway_idp_auth as idp
from preloop.services.gateway_idp_auth import (
    IdpTokenRejectedError,
    IssuerUnavailableError,
    JwksCache,
    validate_idp_token,
)
from tests.services.idp_test_keys import (
    AUDIENCE,
    EC_KEY,
    ISSUER,
    OTHER_RSA_KEY,
    RSA_KEY,
    FakeClock,
    StubIdp,
    claims,
    provider,
    public_pem,
    sign,
    unsigned,
)


def _validate(token, providers=None, stub=None, cache=None, now=None):
    stub = stub or StubIdp()
    cache = cache or JwksCache(fetcher=stub)
    return asyncio.run(
        validate_idp_token(token, providers or [provider()], cache, now=now)
    )


def _reason(token, **kwargs) -> str:
    with pytest.raises(IdpTokenRejectedError) as info:
        _validate(token, **kwargs)
    return info.value.reason


# --- Accepted tokens ------------------------------------------------------


def test_valid_rsa_token_is_accepted():
    identity = _validate(sign())
    assert identity.subject == "user-123"
    assert identity.email == "dev@corp.example"


def test_valid_ec_token_is_accepted():
    identity = _validate(sign(key=EC_KEY, alg="ES256", kid="ec-1"))
    assert identity.subject == "user-123"


def test_multi_audience_token_with_matching_azp_is_accepted():
    token = sign(claims(aud=[AUDIENCE, "other-api"], azp=AUDIENCE))
    assert _validate(token).subject == "user-123"


# --- Token replay and lifetime ---------------------------------------------


def test_expired_token_is_rejected():
    now = int(time.time())
    assert _reason(sign(claims(iat=now - 7200, exp=now - 120))) == "expired"


def test_expiry_within_clock_skew_is_accepted():
    now = int(time.time())
    assert _validate(sign(claims(iat=now - 600, exp=now - 30))).subject


def test_not_yet_valid_nbf_is_rejected():
    now = int(time.time())
    assert _reason(sign(claims(nbf=now + 600))) == "not_yet_valid"


def test_future_iat_is_rejected():
    now = int(time.time())
    token = sign(claims(iat=now + 600, exp=now + 1200))
    assert _reason(token) == "not_yet_valid"


def test_lifetime_cap_is_enforced():
    now = int(time.time())
    token = sign(claims(iat=now, exp=now + 7200))
    assert (
        _reason(token, providers=[provider(max_token_lifetime_seconds=3600)])
        == "lifetime_exceeded"
    )


def test_missing_exp_is_rejected():
    token = sign(claims(exp=None))
    assert _reason(token) == "missing_claim"


# --- Audience confusion ---------------------------------------------------


def test_wrong_audience_is_rejected():
    assert _reason(sign(claims(aud="another-app"))) == "bad_audience"


def test_multi_audience_without_matching_azp_is_rejected():
    token = sign(claims(aud=[AUDIENCE, "other-api"], azp="other-api"))
    assert _reason(token) == "bad_audience"


def test_token_naming_two_accounts_audiences_is_rejected():
    first = provider(audiences=("aud-a",))
    second = provider(audiences=("aud-b",))
    token = sign(claims(aud=["aud-a", "aud-b"], azp="aud-a"))
    assert _reason(token, providers=[first, second]) == "bad_audience"


def test_same_issuer_different_audiences_pick_the_right_provider():
    first = provider(audiences=("aud-a",))
    second = provider(audiences=("aud-b",))
    identity = _validate(sign(claims(aud="aud-b")), providers=[first, second])
    assert identity.provider.id == second.id


# --- Issuer spoofing ------------------------------------------------------


def test_wrong_issuer_is_rejected():
    # The candidate provider is selected, but the claim must equal it exactly.
    token = sign(claims(iss=ISSUER + "/"))
    assert _reason(token) == "bad_issuer"


def test_token_signed_by_another_key_with_known_kid_is_rejected():
    assert _reason(sign(key=OTHER_RSA_KEY)) == "bad_signature"


def test_discovery_issuer_mismatch_fails_closed():
    stub = StubIdp()

    async def lying(url, allow_private):
        doc = await stub(url, allow_private)
        if url.endswith("openid-configuration"):
            doc.data["issuer"] = "https://evil.example"
        return doc

    assert _reason(sign(), cache=JwksCache(fetcher=lying)) == "issuer_unavailable"


def test_jwks_uri_on_foreign_host_fails_closed():
    stub = StubIdp(jwks_uri="https://evil.example/keys")
    assert _reason(sign(), stub=stub) == "issuer_unavailable"


def test_jwks_uri_on_listed_host_is_accepted():
    stub = StubIdp(jwks_uri="https://keys.other.example/jwks")
    p = provider(allowed_jwks_hosts=("keys.other.example",))
    assert _validate(sign(), providers=[p], stub=stub).subject


def test_jwks_uri_must_be_https():
    stub = StubIdp(jwks_uri="http://idp.corp.example/keys")
    assert _reason(sign(), stub=stub) == "issuer_unavailable"


def test_jwks_host_rules():
    assert idp.jwks_host_allowed(ISSUER, "https://idp.corp.example/k", [])
    assert idp.jwks_host_allowed(ISSUER, "https://keys.idp.corp.example/k", [])
    assert not idp.jwks_host_allowed(ISSUER, "https://corp.example/k", [])
    assert not idp.jwks_host_allowed(ISSUER, "https://idp.corp.example.evil/k", [])


# --- Algorithm attacks ----------------------------------------------------


def test_alg_none_is_rejected():
    assert _reason(unsigned()) == "bad_algorithm"


def test_hs256_with_public_key_as_secret_is_rejected():
    # PyJWT refuses PEM-looking HMAC secrets itself; sign by hand.
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing_input = (
        b64(json.dumps({"alg": "HS256", "kid": "rsa-1"}).encode())
        + "."
        + b64(json.dumps(claims()).encode())
    )
    signature = hmac.new(
        public_pem(RSA_KEY), signing_input.encode(), hashlib.sha256
    ).digest()
    token = signing_input + "." + b64(signature)
    assert _reason(token) == "bad_algorithm"


def test_algorithm_outside_allowlist_is_rejected():
    token = sign(key=EC_KEY, alg="ES256", kid="ec-1")
    assert (
        _reason(token, providers=[provider(allowed_algorithms=("RS256",))])
        == "bad_algorithm"
    )


def test_header_alg_must_match_the_key_alg():
    # RS512 is allowed, but the published key is pinned to RS256.
    token = sign(alg="RS512")
    p = provider(allowed_algorithms=("RS256", "RS512"))
    assert _reason(token, providers=[p]) == "bad_algorithm"


def test_symmetric_keys_in_jwks_are_ignored():
    stub = StubIdp(keys=[{"kty": "oct", "kid": "hmac", "k": "c2VjcmV0"}])
    assert _reason(sign(kid="hmac"), stub=stub) == "issuer_unavailable"


def test_unknown_kid_refreshes_once_then_rejects():
    clock = FakeClock()
    stub = StubIdp()
    cache = JwksCache(fetcher=stub, clock=clock)
    assert _validate(sign(), stub=stub, cache=cache).subject
    assert stub.jwks_fetches == 1
    clock.now += 61
    assert _reason(sign(kid="rotated"), stub=stub, cache=cache) == "unknown_kid"
    assert stub.jwks_fetches == 2
    # Within the rate limit window a second unknown kid causes no fetch.
    assert _reason(sign(kid="rotated-2"), stub=stub, cache=cache) == "unknown_kid"
    assert stub.jwks_fetches == 2


def test_rotated_key_is_picked_up_on_refresh():
    clock = FakeClock()
    stub = StubIdp()
    cache = JwksCache(fetcher=stub, clock=clock)
    _validate(sign(), stub=stub, cache=cache)
    stub.keys.append(idp_test_jwk("rsa-2"))
    clock.now += 61
    assert _validate(sign(kid="rsa-2", key=OTHER_RSA_KEY), stub=stub, cache=cache)


def idp_test_jwk(kid):
    from tests.services.idp_test_keys import public_jwk

    return public_jwk(OTHER_RSA_KEY, kid, "RS256")


# --- Email claim trust ----------------------------------------------------


def test_disallowed_domain_is_rejected():
    p = provider(allowed_email_domains=("corp.example",))
    token = sign(claims(email="dev@evil.example"))
    assert _reason(token, providers=[p]) == "domain_not_allowed"


def test_allowed_domain_is_accepted_case_insensitively():
    p = provider(allowed_email_domains=("corp.example",))
    assert _validate(sign(claims(email="Dev@CORP.example")), providers=[p]).email


def test_unverified_email_is_rejected():
    token = sign(claims(email_verified=False))
    assert _reason(token) == "email_not_verified"


def test_missing_email_verified_drops_email_and_fails_domain_allowlist():
    token = sign(claims(email_verified=None))
    assert _validate(token).email is None
    p = provider(allowed_email_domains=("corp.example",))
    assert _reason(token, providers=[p]) == "domain_not_allowed"


def test_unverified_email_allowed_when_not_required():
    p = provider(require_email_verified=False)
    assert _validate(sign(claims(email_verified=False)), providers=[p]).email


def test_custom_email_claim():
    p = provider(email_claim="upn")
    token = sign(claims(email=None, upn="dev@corp.example"))
    assert _validate(token, providers=[p]).email == "dev@corp.example"


def test_group_allowlist():
    p = provider(groups_claim="groups", allowed_groups=("ai-users",))
    assert _validate(sign(claims(groups=["ai-users", "x"])), providers=[p]).groups
    assert _reason(sign(claims(groups=["x"])), providers=[p]) == "group_not_allowed"


def test_required_claims_must_match_exactly():
    p = provider(required_claims={"tid": "tenant-1"})
    assert _validate(sign(claims(tid="tenant-1")), providers=[p]).subject
    assert _reason(sign(claims(tid="tenant-2")), providers=[p]) == "claim_mismatch"
    assert _reason(sign(claims()), providers=[p]) == "claim_mismatch"


def test_missing_sub_is_rejected():
    assert _reason(sign(claims(sub=None))) == "bad_subject"


def test_overlong_sub_is_rejected():
    assert _reason(sign(claims(sub="x" * 256))) == "bad_subject"


# --- Denial of service ----------------------------------------------------


def test_oversize_token_is_rejected_before_parsing():
    token = sign(claims(pad="x" * (idp.MAX_TOKEN_BYTES + 10)))
    stub = StubIdp()
    assert _reason(token, stub=stub) == "token_too_large"
    assert stub.calls == []


def test_candidate_issuer_ignores_non_jwt_and_http_issuers():
    assert idp.candidate_issuer("pk_live_abc") is None
    assert idp.candidate_issuer(sign(claims(iss="http://idp.corp.example"))) is None
    assert idp.candidate_issuer(sign(claims(iss=None))) is None
    assert idp.candidate_issuer(sign()) == ISSUER


def test_jwks_failure_fails_closed_and_is_rate_limited():
    clock = FakeClock()
    stub = StubIdp(fail=True)
    cache = JwksCache(fetcher=stub, clock=clock)
    assert _reason(sign(), stub=stub, cache=cache) == "issuer_unavailable"
    calls = len(stub.calls)
    assert _reason(sign(), stub=stub, cache=cache) == "issuer_unavailable"
    assert len(stub.calls) == calls  # no refetch within the window
    clock.now += 61
    stub.fail = False
    assert _validate(sign(), stub=stub, cache=cache).subject


def test_unavailable_issuer_warning_is_rate_limited(caplog):
    clock = FakeClock()
    stub = StubIdp(fail=True)
    cache = JwksCache(fetcher=stub, clock=clock)
    with caplog.at_level("WARNING", logger="preloop.services.gateway_idp_auth"):
        for _ in range(3):
            _reason(sign(), stub=stub, cache=cache)
            clock.now += 61
    assert len([r for r in caplog.records if "unavailable" in r.message]) == 3
    caplog.clear()
    with caplog.at_level("WARNING", logger="preloop.services.gateway_idp_auth"):
        _reason(sign(), stub=stub, cache=cache)
        _reason(sign(), stub=stub, cache=cache)
    assert len([r for r in caplog.records if "unavailable" in r.message]) <= 1


def test_cache_ttl_is_clamped():
    assert idp.clamp_ttl(None) == 300
    assert idp.clamp_ttl(1) == 300
    assert idp.clamp_ttl(10_000) == 3600
    assert idp.clamp_ttl(900) == 900


def test_cache_expiry_refetches():
    clock = FakeClock()
    stub = StubIdp(max_age=600)
    cache = JwksCache(fetcher=stub, clock=clock)
    _validate(sign(), stub=stub, cache=cache)
    clock.now += 599
    _validate(sign(), stub=stub, cache=cache)
    assert stub.jwks_fetches == 1
    clock.now += 2
    _validate(sign(), stub=stub, cache=cache)
    assert stub.jwks_fetches == 2


# --- JWKS SSRF: address guard and guarded fetch ----------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
    ],
)
def test_private_addresses_are_refused(address):
    with pytest.raises(IssuerUnavailableError):
        idp.guard_url(
            "https://idp.corp.example/x",
            allow_private=False,
            resolver=lambda host: [address],
        )


def test_public_address_is_allowed():
    idp.guard_url(
        "https://idp.corp.example/x",
        allow_private=False,
        resolver=lambda host: ["93.184.216.34"],
    )


def test_private_address_allowed_only_with_flag():
    idp.guard_url(
        "https://idp.internal/x", allow_private=True, resolver=lambda host: ["10.0.0.1"]
    )


def test_literal_private_ip_and_http_are_refused():
    with pytest.raises(IssuerUnavailableError):
        idp.guard_url(
            "https://10.0.0.1/x", allow_private=False, resolver=lambda h: ["8.8.8.8"]
        )
    with pytest.raises(IssuerUnavailableError):
        idp.guard_url(
            "http://idp.corp.example/x",
            allow_private=False,
            resolver=lambda h: ["8.8.8.8"],
        )


def test_private_issuer_needs_both_provider_flag_and_instance_setting(monkeypatch):
    from types import SimpleNamespace

    from preloop.config import settings

    row = SimpleNamespace(allow_private_network_issuer=True)
    monkeypatch.setattr(settings, "gateway_idp_allow_private_issuers", False)
    assert idp.private_issuers_allowed(row) is False
    monkeypatch.setattr(settings, "gateway_idp_allow_private_issuers", True)
    assert idp.private_issuers_allowed(row) is True
    row.allow_private_network_issuer = False
    assert idp.private_issuers_allowed(row) is False


def _fetch(handler, url="https://idp.corp.example/keys", resolve="93.184.216.34"):
    return asyncio.run(
        idp.guarded_fetch_json(
            url,
            False,
            resolver=lambda host: [resolve],
            transport=httpx.MockTransport(handler),
        )
    )


def test_guarded_fetch_reads_json_and_cache_control():
    doc = _fetch(
        lambda request: httpx.Response(
            200, json={"keys": []}, headers={"cache-control": "public, max-age=900"}
        )
    )
    assert doc.data == {"keys": []} and doc.max_age == 900


def test_guarded_fetch_refuses_private_resolution():
    with pytest.raises(IssuerUnavailableError):
        _fetch(lambda r: httpx.Response(200, json={}), resolve="10.1.2.3")


def test_guarded_fetch_refuses_cross_host_redirect():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/keys"})

    with pytest.raises(IssuerUnavailableError) as info:
        _fetch(handler)
    assert info.value.detail == "cross-host redirect"


def test_guarded_fetch_follows_same_host_redirect():
    def handler(request):
        if request.url.path == "/keys":
            return httpx.Response(302, headers={"location": "/v2/keys"})
        return httpx.Response(200, json={"keys": [1]})

    assert _fetch(handler).data == {"keys": [1]}


def test_guarded_fetch_caps_size():
    big = b'{"keys": "' + b"x" * (idp.MAX_DOCUMENT_BYTES + 1) + b'"}'
    with pytest.raises(IssuerUnavailableError) as info:
        _fetch(lambda r: httpx.Response(200, content=big))
    assert info.value.detail == "document too large"


def test_guarded_fetch_maps_timeouts_to_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(IssuerUnavailableError):
        _fetch(handler)


def test_guarded_fetch_rejects_non_200():
    with pytest.raises(IssuerUnavailableError):
        _fetch(lambda r: httpx.Response(500, json={}))


def test_probe_issuer_reports_key_ids_only():
    result = asyncio.run(
        idp.probe_issuer(
            ISSUER, allow_private=False, extra_jwks_hosts=[], fetcher=StubIdp()
        )
    )
    assert result["ok"] is True
    assert {k["kid"] for k in result["keys"]} == {"rsa-1", "ec-1"}
    failed = asyncio.run(
        idp.probe_issuer(
            ISSUER, allow_private=False, extra_jwks_hosts=[], fetcher=StubIdp(fail=True)
        )
    )
    assert failed["ok"] is False


def test_pyjwt_rejects_hmac_with_rsa_public_key_material():
    # Belt and braces: even if an alg check regressed, PyJWK refuses to
    # build an HMAC key from an RSA JWK.
    from tests.services.idp_test_keys import public_jwk

    with pytest.raises(jwt.PyJWTError):
        jwt.PyJWK(public_jwk(RSA_KEY, "rsa-1", "RS256"), algorithm="HS256")
