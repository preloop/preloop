"""Locally generated RSA and EC keys plus a stub IdP for #1414 tests.

No network: discovery and JWKS are served by an in-memory fetcher.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from preloop.services.gateway_idp_auth import (
    FetchedDocument,
    IssuerUnavailableError,
    ProviderConfig,
)

ISSUER = "https://idp.corp.example"
AUDIENCE = "desktop-client"

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(private_key: Any, kid: str, alg: str) -> dict[str, Any]:
    algorithm = jwt.get_algorithm_by_name(alg)
    jwk = json.loads(algorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": alg})
    return jwk


def public_pem(private_key: Any) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "email": "dev@corp.example",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def sign(
    payload: Optional[dict[str, Any]] = None,
    *,
    key: Any = RSA_KEY,
    alg: str = "RS256",
    kid: Optional[str] = "rsa-1",
) -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(payload or claims(), key, algorithm=alg, headers=headers)


def unsigned(payload: Optional[dict[str, Any]] = None) -> str:
    import base64

    def b64(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'kid': 'rsa-1'})}.{b64(payload or claims())}."


@dataclass
class StubIdp:
    """In-memory discovery and JWKS documents, with a call log."""

    issuer: str = ISSUER
    keys: list[dict[str, Any]] = field(
        default_factory=lambda: [
            public_jwk(RSA_KEY, "rsa-1", "RS256"),
            public_jwk(EC_KEY, "ec-1", "ES256"),
        ]
    )
    jwks_uri: Optional[str] = None
    max_age: Optional[int] = 600
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    async def __call__(self, url: str, allow_private: bool) -> FetchedDocument:
        self.calls.append(url)
        if self.fail:
            raise IssuerUnavailableError("stub down")
        if url == self.issuer + "/.well-known/openid-configuration":
            return FetchedDocument(
                data={
                    "issuer": self.issuer,
                    "jwks_uri": self.jwks_uri or self.issuer + "/keys",
                },
                max_age=None,
            )
        if url == (self.jwks_uri or self.issuer + "/keys"):
            return FetchedDocument(data={"keys": list(self.keys)}, max_age=self.max_age)
        raise IssuerUnavailableError("unknown url")

    @property
    def jwks_fetches(self) -> int:
        return sum(1 for c in self.calls if not c.endswith("openid-configuration"))


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def provider(**overrides: Any) -> ProviderConfig:
    values: dict[str, Any] = dict(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        allowed_email_domains=(),
        email_claim="email",
        require_email_verified=True,
        groups_claim=None,
        allowed_groups=(),
        required_claims={},
        clock_skew_seconds=60,
        max_token_lifetime_seconds=86400,
        allowed_algorithms=("RS256", "ES256"),
        allowed_jwks_hosts=(),
        allow_private=False,
    )
    values.update(overrides)
    return ProviderConfig(**values)
