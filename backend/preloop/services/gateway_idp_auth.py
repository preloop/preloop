"""Customer IdP tokens as bearer credentials on the Anthropic gateway (#1414).

Claude Desktop with ``inferenceCredentialKind: external-idp`` signs the user
in with the organization's OpenID Connect provider and sends the IdP token as
``Authorization: Bearer``. This module decides whether such a token is
trusted, using only the account's configured
:class:`~preloop.models.models.gateway_identity_provider.GatewayIdentityProvider`
rows.

Trust model, in order:

1. Only a bearer that looks like a JWT (three base64url segments) and whose
   unverified ``iss`` equals an enabled provider's issuer is a candidate.
   Everything else falls through to the existing credential checks
   unchanged.
2. From that point the request fails closed: any problem, including an
   unreachable JWKS, is a 401. A matched token never falls through to API
   key authentication.
3. Trust comes from the signature, checked against keys fetched from the
   configured issuer's discovery document over https through an outbound
   address guard. ``alg: none`` and HMAC are never accepted.

Tokens are never logged; log lines carry a short fingerprint only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence
from urllib.parse import urljoin, urlparse

import httpx
import jwt

from preloop.api.auth.jwt import _token_log_fingerprint
from preloop.services.model_gateway_errors import ModelGatewayAPIError

logger = logging.getLogger(__name__)

#: ``auth_method`` recorded on usage rows for IdP-authenticated requests.
AUTH_METHOD_IDP = "idp"
#: Audit action written the first time a provider sees a subject.
AUDIT_SUBJECT_FIRST_SEEN = "gateway_idp_subject_first_seen"

#: Bearer size cap before any parsing (DoS bound).
MAX_TOKEN_BYTES = 16 * 1024
MAX_SUBJECT_LENGTH = 255
#: JWKS and discovery response size cap.
MAX_DOCUMENT_BYTES = 256 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 3
#: JWKS cache TTL bounds; the IdP's ``Cache-Control: max-age`` is clamped.
MIN_JWKS_TTL_SECONDS = 5 * 60
MAX_JWKS_TTL_SECONDS = 60 * 60
#: At most one forced refresh (unknown ``kid`` or failed fetch) per issuer
#: in this window.
REFRESH_INTERVAL_SECONDS = 60.0
#: Warning logs for an unavailable issuer are rate limited per issuer.
WARN_INTERVAL_SECONDS = 60.0

#: Asymmetric JWS algorithms an admin may allow. HMAC and ``none`` are not
#: in this set and can never be configured.
SUPPORTED_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)

_B64URL_SEGMENT = r"[A-Za-z0-9_-]+"
_JWT_SHAPE = re.compile(rf"^{_B64URL_SEGMENT}\.{_B64URL_SEGMENT}\.[A-Za-z0-9_-]*$")
_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*(\d+)", re.IGNORECASE)


class IdpTokenRejectedError(Exception):
    """A candidate IdP token failed validation; ``reason`` is a short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IssuerUnavailableError(IdpTokenRejectedError):
    """Discovery or JWKS could not be fetched or parsed (fails closed)."""

    def __init__(self, detail: str) -> None:
        super().__init__("issuer_unavailable")
        self.detail = detail


def idp_auth_error(reason: str) -> ModelGatewayAPIError:
    """The Anthropic-shaped 401 for a rejected IdP token.

    The body carries only the reason code; ``WWW-Authenticate`` tells the
    client to re-authenticate instead of retrying.
    """
    error = ModelGatewayAPIError(
        provider="anthropic",
        status_code=401,
        message=f"Invalid identity provider token ({reason})",
        error_type="authentication_error",
        code=reason,
    )
    error.extra_response_headers = {  # type: ignore[attr-defined]
        "WWW-Authenticate": f'Bearer error="invalid_token", error_description="{reason}"'
    }
    return error


# --- Token shape ------------------------------------------------------------


def looks_like_jwt(token: str) -> bool:
    """Three base64url segments (the last may be empty, so an unsigned
    ``alg: none`` token is caught and rejected rather than ignored)."""
    return bool(token) and _JWT_SHAPE.match(token) is not None


def _b64_json(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("not json") from exc
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def unverified_parts(token: str) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    """Return ``(header, claims)`` without verifying, or ``None`` if unparsable.

    Only used to pick a candidate provider by ``iss``. Callers must cap the
    token size first.
    """
    try:
        header_segment, claims_segment, _ = token.split(".")
        return _b64_json(header_segment), _b64_json(claims_segment)
    except ValueError:
        return None


def candidate_issuer(token: str) -> Optional[str]:
    """The unverified ``iss`` of a JWT-shaped, size-capped, https token."""
    if not looks_like_jwt(token) or len(token) > MAX_TOKEN_BYTES:
        return None
    parts = unverified_parts(token)
    if parts is None:
        return None
    issuer = parts[1].get("iss")
    if not isinstance(issuer, str) or not issuer.startswith("https://"):
        return None
    return issuer


# --- Outbound address guard -------------------------------------------------


_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("169.254.0.0/16"),  # link-local and cloud metadata
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IPv6 metadata
)


def blocked_address_reason(raw: str) -> Optional[str]:
    """Why one resolved address is off limits for an issuer fetch, or None."""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    for network in _EXTRA_BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return "metadata-or-shared"
    if address.is_private:
        return "private"
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return "reserved"
    return None


Resolver = Callable[[str], Sequence[str]]


def _system_resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def guard_url(url: str, *, allow_private: bool, resolver: Resolver) -> None:
    """Refuse non-https URLs and hosts resolving to internal address space.

    Raises:
        IssuerUnavailableError: The URL may not be fetched.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise IssuerUnavailableError("not https")
    if parsed.username or parsed.password:
        raise IssuerUnavailableError("credentials in url")
    if allow_private:
        return
    host = parsed.hostname
    direct = blocked_address_reason(host)
    if direct:
        raise IssuerUnavailableError(f"blocked address ({direct})")
    try:
        addresses = list(resolver(host))
    except (OSError, UnicodeError) as exc:
        raise IssuerUnavailableError("unresolvable") from exc
    if not addresses:
        raise IssuerUnavailableError("unresolvable")
    for raw in addresses:
        reason = blocked_address_reason(raw)
        if reason:
            raise IssuerUnavailableError(f"blocked address ({reason})")


def private_issuers_allowed(provider: Any) -> bool:
    """Both the provider flag and the self-hosted instance setting are on."""
    from preloop.config import settings

    return bool(
        getattr(provider, "allow_private_network_issuer", False)
        and settings.gateway_idp_allow_private_issuers
    )


def jwks_host_allowed(issuer: str, jwks_uri: str, extra_hosts: Sequence[str]) -> bool:
    """``jwks_uri`` host equals the issuer host, is under it, or is listed.

    Stricter than "same registrable domain": without a public suffix list a
    shared parent such as ``co.uk`` cannot be told apart from a company
    domain, so a sibling host must be listed by the admin.
    """
    issuer_host = (urlparse(issuer).hostname or "").lower()
    jwks_host = (urlparse(jwks_uri).hostname or "").lower()
    if not issuer_host or not jwks_host:
        return False
    if jwks_host == issuer_host or jwks_host.endswith("." + issuer_host):
        return True
    return jwks_host in {h.strip().lower() for h in extra_hosts if h}


@dataclass(frozen=True)
class FetchedDocument:
    """A JSON document and the TTL its ``Cache-Control`` asked for."""

    data: dict[str, Any]
    max_age: Optional[int]


Fetcher = Callable[[str, bool], Awaitable[FetchedDocument]]


async def guarded_fetch_json(
    url: str,
    allow_private: bool,
    *,
    resolver: Resolver = _system_resolve,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FetchedDocument:
    """GET a JSON document through the address guard with time and size caps.

    Redirects are followed manually, only to https on the same host, at most
    :data:`MAX_REDIRECTS` times, and each hop is re-checked by the guard.

    Raises:
        IssuerUnavailableError: Any failure; the caller fails closed.
    """
    origin_host = (urlparse(url).hostname or "").lower()
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
            # No proxy or CA from the environment: a proxy would bypass the
            # address guard. A private CA comes only from the setting.
            trust_env=False,
            verify=_tls_verify(),
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await asyncio.to_thread(
                    guard_url, current, allow_private=allow_private, resolver=resolver
                )
                async with client.stream(
                    "GET", current, headers={"Accept": "application/json"}
                ) as response:
                    if response.is_redirect:
                        target = urljoin(current, response.headers.get("location", ""))
                        if (urlparse(target).hostname or "").lower() != origin_host:
                            raise IssuerUnavailableError("cross-host redirect")
                        current = target
                        continue
                    if response.status_code != 200:
                        raise IssuerUnavailableError(f"status {response.status_code}")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit():
                        if int(declared) > MAX_DOCUMENT_BYTES:
                            raise IssuerUnavailableError("document too large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DOCUMENT_BYTES:
                            raise IssuerUnavailableError("document too large")
                    try:
                        data = json.loads(bytes(body))
                    except ValueError as exc:
                        raise IssuerUnavailableError("invalid json") from exc
                    if not isinstance(data, dict):
                        raise IssuerUnavailableError("invalid json")
                    return FetchedDocument(
                        data=data,
                        max_age=_max_age(response.headers.get("cache-control")),
                    )
            raise IssuerUnavailableError("too many redirects")
    except httpx.HTTPError as exc:
        raise IssuerUnavailableError(type(exc).__name__) from exc


def _tls_verify() -> Any:
    """Public roots, or the operator's CA bundle (``gateway_idp_ca_bundle``)."""
    from preloop.config import settings

    bundle = (settings.gateway_idp_ca_bundle or "").strip()
    if not bundle:
        return True
    import ssl

    return ssl.create_default_context(cafile=bundle)


def _max_age(cache_control: Optional[str]) -> Optional[int]:
    # ``no-store`` and ``no-cache`` get the 5 minute floor like any short
    # TTL: refetching keys on every request would be the DoS the floor
    # exists to prevent.
    if not cache_control:
        return None
    match = _MAX_AGE.search(cache_control)
    return int(match.group(1)) if match else None


def clamp_ttl(max_age: Optional[int]) -> float:
    """Clamp an IdP-supplied TTL to 5 to 60 minutes."""
    if max_age is None:
        return float(MIN_JWKS_TTL_SECONDS)
    return float(min(max(max_age, MIN_JWKS_TTL_SECONDS), MAX_JWKS_TTL_SECONDS))


# --- JWKS cache -------------------------------------------------------------


@dataclass
class _IssuerKeys:
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    expires_at: float = 0.0
    last_fetch_attempt: float = float("-inf")
    last_warning: float = float("-inf")
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class JwksCache:
    """Per-issuer JWKS cache with a forced-refresh rate limit.

    A key set is fetched when the cache is empty or expired, and refetched
    early only for an unknown ``kid``, at most once per
    :data:`REFRESH_INTERVAL_SECONDS` per issuer. A failed fetch counts
    against the same budget, so a dead issuer is not hammered.
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher = guarded_fetch_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock
        self._issuers: dict[str, _IssuerKeys] = {}
        self._guard = threading.Lock()
        self.fetch_count = 0

    @staticmethod
    def _key(issuer: str, allow_private: bool, extra_jwks_hosts: Sequence[str]) -> str:
        # Providers sharing an issuer may differ in guard settings; a key set
        # fetched under one provider's settings is never served to another.
        hosts = ",".join(sorted({h.strip().lower() for h in extra_jwks_hosts if h}))
        return f"{issuer}\n{int(allow_private)}\n{hosts}"

    def _entry(self, key: str) -> _IssuerKeys:
        with self._guard:
            entry = self._issuers.get(key)
            if entry is None:
                entry = _IssuerKeys()
                self._issuers[key] = entry
            return entry

    def clear(self) -> None:
        """Drop every cached key set (tests, provider edits)."""
        with self._guard:
            self._issuers.clear()

    def forget(self, issuer: str) -> None:
        """Drop every cached key set for ``issuer``."""
        with self._guard:
            for key in [k for k in self._issuers if k.split("\n", 1)[0] == issuer]:
                self._issuers.pop(key, None)

    async def get_key(
        self,
        issuer: str,
        kid: Optional[str],
        *,
        allow_private: bool,
        extra_jwks_hosts: Sequence[str],
    ) -> dict[str, Any]:
        """Return the JWK for ``kid`` from the issuer's key set.

        Raises:
            IssuerUnavailableError: The key set could not be fetched.
            IdpTokenRejectedError: ``unknown_kid`` after the allowed refresh.
        """
        entry = self._entry(self._key(issuer, allow_private, extra_jwks_hosts))
        async with entry.lock:
            now = self._clock()
            if not entry.keys or now >= entry.expires_at:
                await self._refresh(
                    issuer, entry, allow_private, extra_jwks_hosts, force=False
                )
            key = _pick_key(entry.keys, kid)
            if key is not None:
                return key
            await self._refresh(
                issuer, entry, allow_private, extra_jwks_hosts, force=True
            )
            key = _pick_key(entry.keys, kid)
            if key is None:
                raise IdpTokenRejectedError("unknown_kid")
            return key

    async def _refresh(
        self,
        issuer: str,
        entry: _IssuerKeys,
        allow_private: bool,
        extra_jwks_hosts: Sequence[str],
        *,
        force: bool,
    ) -> None:
        now = self._clock()
        if now - entry.last_fetch_attempt < REFRESH_INTERVAL_SECONDS:
            if not entry.keys:
                raise IssuerUnavailableError("refresh rate limited")
            if force:
                return
            # Expired but recently attempted: keep serving the stale set.
            return
        entry.last_fetch_attempt = now
        self.fetch_count += 1
        try:
            keys, max_age = await fetch_issuer_keys(
                issuer,
                allow_private=allow_private,
                extra_jwks_hosts=extra_jwks_hosts,
                fetcher=self._fetcher,
            )
        except IssuerUnavailableError as exc:
            if now - entry.last_warning >= WARN_INTERVAL_SECONDS:
                entry.last_warning = now
                logger.warning(
                    "Gateway IdP issuer unavailable (issuer=%s): %s", issuer, exc.detail
                )
            raise
        entry.keys = keys
        entry.expires_at = now + clamp_ttl(max_age)


def _pick_key(
    keys: dict[str, dict[str, Any]], kid: Optional[str]
) -> Optional[dict[str, Any]]:
    if kid is None:
        # A token without ``kid`` is accepted only against a single-key set.
        if len(keys) == 1:
            return next(iter(keys.values()))
        return None
    return keys.get(kid)


async def discover_jwks_uri(
    issuer: str,
    *,
    allow_private: bool,
    extra_jwks_hosts: Sequence[str],
    fetcher: Fetcher = guarded_fetch_json,
) -> str:
    """Read ``jwks_uri`` from the issuer's discovery document and vet it."""
    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    document = await fetcher(discovery_url, allow_private)
    if document.data.get("issuer") != issuer:
        raise IssuerUnavailableError("discovery issuer mismatch")
    jwks_uri = document.data.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
        raise IssuerUnavailableError("jwks_uri not https")
    if not jwks_host_allowed(issuer, jwks_uri, extra_jwks_hosts):
        raise IssuerUnavailableError("jwks_uri host not allowed")
    return jwks_uri


async def fetch_issuer_keys(
    issuer: str,
    *,
    allow_private: bool,
    extra_jwks_hosts: Sequence[str],
    fetcher: Fetcher = guarded_fetch_json,
) -> tuple[dict[str, dict[str, Any]], Optional[int]]:
    """Fetch discovery then JWKS; return ``{kid: jwk}`` and the JWKS max-age.

    Symmetric (``kty: oct``) keys are dropped so an HMAC key can never be
    used, even if an IdP publishes one.
    """
    jwks_uri = await discover_jwks_uri(
        issuer,
        allow_private=allow_private,
        extra_jwks_hosts=extra_jwks_hosts,
        fetcher=fetcher,
    )
    document = await fetcher(jwks_uri, allow_private)
    raw_keys = document.data.get("keys")
    if not isinstance(raw_keys, list):
        raise IssuerUnavailableError("jwks has no keys")
    keys: dict[str, dict[str, Any]] = {}
    for index, jwk in enumerate(raw_keys):
        if not isinstance(jwk, dict) or jwk.get("kty") not in {"RSA", "EC", "OKP"}:
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        kid = jwk.get("kid")
        keys[str(kid) if kid is not None else f"__index_{index}"] = jwk
    if not keys:
        raise IssuerUnavailableError("jwks has no usable keys")
    return keys, document.max_age


# --- Validation -------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """Detached copy of a provider row, safe to use off the DB session."""

    id: Any
    account_id: Any
    api_key_id: Any
    issuer: str
    audiences: tuple[str, ...]
    allowed_email_domains: tuple[str, ...]
    email_claim: str
    require_email_verified: bool
    groups_claim: Optional[str]
    allowed_groups: tuple[str, ...]
    required_claims: dict[str, Any]
    clock_skew_seconds: int
    max_token_lifetime_seconds: int
    allowed_algorithms: tuple[str, ...]
    allowed_jwks_hosts: tuple[str, ...]
    allow_private: bool

    @classmethod
    def from_row(cls, row: Any) -> "ProviderConfig":
        """Copy the fields validation needs from an ORM row."""
        return cls(
            id=row.id,
            account_id=row.account_id,
            api_key_id=row.api_key_id,
            issuer=row.issuer,
            audiences=tuple(row.audiences),
            allowed_email_domains=tuple(
                d.lower() for d in (row.allowed_email_domains or [])
            ),
            email_claim=row.email_claim or "email",
            require_email_verified=bool(row.require_email_verified),
            groups_claim=row.groups_claim,
            allowed_groups=tuple(row.allowed_groups or ()),
            required_claims=dict(row.required_claims or {}),
            clock_skew_seconds=int(row.clock_skew_seconds),
            max_token_lifetime_seconds=int(row.max_token_lifetime_seconds),
            allowed_algorithms=tuple(
                a for a in (row.allowed_algorithms or ()) if a in SUPPORTED_ALGORITHMS
            ),
            allowed_jwks_hosts=tuple(row.allowed_jwks_hosts or ()),
            allow_private=private_issuers_allowed(row),
        )


@dataclass(frozen=True)
class IdpIdentity:
    """A verified IdP identity, ready to resolve a gateway subject."""

    provider: ProviderConfig
    subject: str
    email: Optional[str]
    groups: tuple[str, ...]


def _token_audiences(claims: dict[str, Any]) -> list[str]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        return list(aud)
    return []


def select_provider(
    claims: dict[str, Any], providers: Sequence[ProviderConfig]
) -> tuple[ProviderConfig, str]:
    """Pick the one provider whose audience the token names.

    Raises:
        IdpTokenRejectedError: ``bad_audience`` when no provider or more than
            one provider matches, or when several audiences of the provider
            match and ``azp`` names none of them.
    """
    token_audiences = set(_token_audiences(claims))
    matched = {
        provider.id: (provider, [a for a in provider.audiences if a in token_audiences])
        for provider in providers
        if token_audiences & set(provider.audiences)
    }
    if len(matched) != 1:
        # None, or audiences of two providers (two accounts): never guess.
        raise IdpTokenRejectedError("bad_audience")
    provider, audiences = next(iter(matched.values()))
    if len(audiences) == 1:
        return provider, audiences[0]
    # Several of this provider's audiences: ``azp`` names the one in use.
    azp = claims.get("azp")
    if isinstance(azp, str) and azp in audiences:
        return provider, azp
    raise IdpTokenRejectedError("bad_audience")


async def validate_idp_token(
    token: str,
    providers: Sequence[ProviderConfig],
    cache: JwksCache,
    *,
    now: Optional[float] = None,
) -> IdpIdentity:
    """Verify an IdP token against the candidate providers for its issuer.

    Args:
        token: The bearer, already known to be JWT-shaped with a matching
            unverified ``iss``.
        providers: Enabled providers for that issuer (one per audience).
        cache: JWKS cache.
        now: Wall clock seconds, for tests.

    Raises:
        IdpTokenRejectedError: With a short reason code; the caller answers 401.
    """
    if len(token) > MAX_TOKEN_BYTES:
        raise IdpTokenRejectedError("token_too_large")
    parts = unverified_parts(token)
    if parts is None:
        raise IdpTokenRejectedError("malformed")
    header, unverified_claims = parts
    provider, audience = select_provider(unverified_claims, providers)

    alg = header.get("alg")
    if (
        not isinstance(alg, str)
        or alg.lower() == "none"
        or alg.upper().startswith("HS")
        or alg not in provider.allowed_algorithms
    ):
        raise IdpTokenRejectedError("bad_algorithm")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise IdpTokenRejectedError("malformed")

    jwk = await cache.get_key(
        provider.issuer,
        kid,
        allow_private=provider.allow_private,
        extra_jwks_hosts=provider.allowed_jwks_hosts,
    )
    if jwk.get("alg") not in (None, alg):
        raise IdpTokenRejectedError("bad_algorithm")
    try:
        key = jwt.PyJWK(jwk, algorithm=alg)
    except jwt.PyJWTError as exc:
        raise IdpTokenRejectedError("bad_algorithm") from exc

    skew = provider.clock_skew_seconds
    current = time.time() if now is None else now
    try:
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=[alg],
            audience=list(provider.audiences),
            issuer=provider.issuer,
            leeway=skew,
            options={
                "require": ["exp", "iat", "iss", "aud"],
                "verify_iat": False,  # checked below with the same leeway
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except jwt.InvalidSignatureError as exc:
        raise IdpTokenRejectedError("bad_signature") from exc
    except jwt.InvalidAudienceError as exc:
        raise IdpTokenRejectedError("bad_audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise IdpTokenRejectedError("bad_issuer") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise IdpTokenRejectedError("missing_claim") from exc
    except jwt.PyJWTError as exc:
        raise IdpTokenRejectedError("invalid_token") from exc

    _check_times(claims, provider, current)

    token_audiences = _token_audiences(claims)
    if len(token_audiences) > 1 and claims.get("azp") != audience:
        raise IdpTokenRejectedError("bad_audience")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
        raise IdpTokenRejectedError("bad_subject")

    for name, expected in provider.required_claims.items():
        if name not in claims or claims[name] != expected:
            raise IdpTokenRejectedError("claim_mismatch")

    email = _verified_email(claims, provider)
    groups = _groups(claims, provider)
    return IdpIdentity(provider=provider, subject=subject, email=email, groups=groups)


def _numeric(claims: dict[str, Any], name: str) -> Optional[float]:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IdpTokenRejectedError("invalid_token")
    return float(value)


def _check_times(claims: dict[str, Any], provider: ProviderConfig, now: float) -> None:
    skew = provider.clock_skew_seconds
    exp = _numeric(claims, "exp")
    iat = _numeric(claims, "iat")
    nbf = _numeric(claims, "nbf")
    if exp is None or iat is None:
        raise IdpTokenRejectedError("missing_claim")
    if now > exp + skew:
        raise IdpTokenRejectedError("expired")
    if nbf is not None and now + skew < nbf:
        raise IdpTokenRejectedError("not_yet_valid")
    if now + skew < iat:
        raise IdpTokenRejectedError("not_yet_valid")
    if exp - iat > provider.max_token_lifetime_seconds:
        raise IdpTokenRejectedError("lifetime_exceeded")


def _verified_email(claims: dict[str, Any], provider: ProviderConfig) -> Optional[str]:
    raw = claims.get(provider.email_claim)
    email = raw.strip() if isinstance(raw, str) and "@" in raw else None
    verified_claim = claims.get("email_verified")
    if provider.require_email_verified and email is not None:
        if verified_claim is not None and verified_claim is not True:
            raise IdpTokenRejectedError("email_not_verified")
        if verified_claim is None:
            # No verification signal: the email is not trusted to link a
            # member or satisfy a domain allowlist.
            email = None
    if provider.allowed_email_domains:
        domain = email.rsplit("@", 1)[1].lower() if email else None
        if domain not in provider.allowed_email_domains:
            raise IdpTokenRejectedError("domain_not_allowed")
    return email


def _groups(claims: dict[str, Any], provider: ProviderConfig) -> tuple[str, ...]:
    if not provider.groups_claim:
        return ()
    raw = claims.get(provider.groups_claim)
    if isinstance(raw, str):
        groups: tuple[str, ...] = (raw,)
    elif isinstance(raw, list):
        groups = tuple(g for g in raw if isinstance(g, str))
    else:
        groups = ()
    if provider.allowed_groups and not set(groups) & set(provider.allowed_groups):
        raise IdpTokenRejectedError("group_not_allowed")
    return groups


# --- Gateway entry point ----------------------------------------------------

#: Process-wide JWKS cache used by the gateway dependency.
default_jwks_cache = JwksCache()


async def authenticate_idp_bearer(
    token: str,
    db: Any,
    *,
    cache: Optional[JwksCache] = None,
) -> Optional[Any]:
    """Authenticate an IdP token, or return ``None`` when it is not one.

    ``None`` means "not an IdP token for any configured provider"; the caller
    continues with the existing credential checks. Once a provider's issuer
    matched, every failure raises the 401 from :func:`idp_auth_error`.

    Returns:
        A ``ModelGatewayAuthContext`` for the binding key with the gateway
        subject attached, or ``None``.

    Raises:
        ModelGatewayAPIError: 401 for a rejected token whose issuer matched.
    """
    if not looks_like_jwt(token):
        return None
    if len(token) > MAX_TOKEN_BYTES:
        # Never decode an oversized JWT-shaped bearer. No Preloop credential
        # is this large, so nothing legitimate is turned away here.
        _log_rejection(token, "token_too_large", None)
        raise idp_auth_error("token_too_large")
    issuer = candidate_issuer(token)
    if issuer is None:
        return None

    from preloop.api.loop_safety import run_db_off_loop

    providers = await run_db_off_loop(lambda: _load_providers(db, issuer))
    if not providers:
        return None

    cache = cache or default_jwks_cache
    try:
        identity = await validate_idp_token(token, providers, cache)
        return await _build_context(identity, db)
    except IdpTokenRejectedError as exc:
        _log_rejection(token, exc.reason, issuer)
        raise idp_auth_error(exc.reason) from None
    except ModelGatewayAPIError:
        raise
    except Exception:
        # Fail closed: a matched issuer never falls through to key auth.
        logger.exception(
            "Gateway IdP validation error (fp=%s issuer=%s)",
            _token_log_fingerprint(token),
            issuer,
        )
        raise idp_auth_error("invalid_token") from None


def _log_rejection(token: str, reason: str, issuer: Optional[str]) -> None:
    logger.info(
        "Rejected gateway IdP token (fp=%s issuer=%s reason=%s)",
        _token_log_fingerprint(token),
        issuer,
        reason,
    )


def _load_providers(db: Any, issuer: str) -> list[ProviderConfig]:
    from sqlalchemy.orm import Session

    from preloop.models.crud import crud_gateway_identity_provider
    from preloop.models.db.gateway_session import release_gateway_session

    with Session(bind=db.get_bind(), expire_on_commit=False) as session:
        rows = crud_gateway_identity_provider.list_enabled_for_issuer(
            session, issuer=issuer
        )
        configs = [ProviderConfig.from_row(row) for row in rows]
        release_gateway_session(session)
        return configs


async def _build_context(identity: IdpIdentity, db: Any) -> Any:
    """Binding key context plus the subject; audit the first sighting."""
    from preloop.api.loop_safety import run_db_off_loop
    from preloop.services.gateway_upstream_identity import attach_gateway_subject

    provider = identity.provider
    base = await run_db_off_loop(lambda: _binding_key_context(db, provider))
    if base is None:
        raise IdpTokenRejectedError("binding_key_invalid")

    def after_resolve(session: Any, subject: Any, created: bool) -> None:
        _check_linked_member(session, subject)
        if created:
            _audit_first_seen(session, provider, subject, identity)

    return await attach_gateway_subject(
        base,
        db,
        external_subject=identity.subject,
        email=identity.email,
        trusted_upstream=False,
        after_resolve=after_resolve,
        auth_method=AUTH_METHOD_IDP,
        idp_provider_id=provider.id,
    )


def _binding_key_context(db: Any, provider: ProviderConfig) -> Optional[Any]:
    from sqlalchemy.orm import Session

    from preloop.models.db.gateway_session import release_gateway_session

    with Session(bind=db.get_bind(), expire_on_commit=False) as session:
        context = binding_key_context(session, provider)
        snapshot = context.snapshot() if context is not None else None
        release_gateway_session(session)
        return snapshot


def binding_key_context(session: Any, provider: ProviderConfig) -> Optional[Any]:
    """Auth context for the provider's binding key, or ``None`` if unusable."""
    from preloop.api.auth.key_scopes import is_device_scoped_api_key
    from preloop.models.crud import crud_api_key, crud_user
    from preloop.services.model_gateway_auth import (
        NO_BEARER_TOKEN,
        ModelGatewayAuthContext,
    )

    api_key = crud_api_key.get(session, id=provider.api_key_id)
    if (
        api_key is None
        or str(api_key.account_id) != str(provider.account_id)
        or not api_key.is_active
        or api_key.is_expired
        or is_device_scoped_api_key(api_key)
    ):
        return None
    user = crud_user.get(session, id=str(api_key.user_id))
    if user is None or not user.is_active:
        return None
    if str(user.account_id) != str(api_key.account_id):
        return None
    return ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=user, api_key=api_key)


def _check_linked_member(session: Any, subject: Any) -> None:
    """A subject linked to a deactivated member is refused."""
    if subject.linked_user_id is None:
        return
    from preloop.models.crud import crud_user

    user = crud_user.get(session, id=str(subject.linked_user_id))
    if user is not None and not user.is_active:
        raise IdpTokenRejectedError("member_inactive")


def _audit_first_seen(
    session: Any, provider: ProviderConfig, subject: Any, identity: IdpIdentity
) -> None:
    from preloop.models.crud import crud_audit_log

    try:
        crud_audit_log.log_action(
            session,
            account_id=provider.account_id,
            action=AUDIT_SUBJECT_FIRST_SEEN,
            resource_type="gateway_subject",
            resource_id=str(subject.id),
            status="success",
            details={
                "gateway_subject_id": str(subject.id),
                "email": subject.email,
                "idp_provider_id": str(provider.id),
                "groups": list(identity.groups),
            },
        )
    except Exception:
        session.rollback()
        logger.warning("Failed to audit first IdP subject sighting", exc_info=True)


# --- Admin test ------------------------------------------------------------


async def probe_issuer(
    issuer: str,
    *,
    allow_private: bool,
    extra_jwks_hosts: Sequence[str],
    fetcher: Fetcher = guarded_fetch_json,
) -> dict[str, Any]:
    """Fetch discovery and JWKS for the admin test; never stores a token."""
    try:
        jwks_uri = await discover_jwks_uri(
            issuer,
            allow_private=allow_private,
            extra_jwks_hosts=extra_jwks_hosts,
            fetcher=fetcher,
        )
        keys, _ = await fetch_issuer_keys(
            issuer,
            allow_private=allow_private,
            extra_jwks_hosts=extra_jwks_hosts,
            fetcher=fetcher,
        )
    except IssuerUnavailableError as exc:
        return {"ok": False, "error": exc.detail, "jwks_uri": None, "keys": []}
    return {
        "ok": True,
        "error": None,
        "jwks_uri": jwks_uri,
        "keys": [
            {"kid": jwk.get("kid"), "kty": jwk.get("kty"), "alg": jwk.get("alg")}
            for jwk in keys.values()
        ],
    }
