"""Upstream MCP OAuth discovery and HTTP boundaries (independent of inbound OAuth).

Only metadata reads are retried. POSTs never follow redirects or retry: a code
or rotating refresh token might have been consumed even when no reply arrives.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import math
import os
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx


class OAuthError(ValueError):
    """A fixed, credential-free error safe for API responses and logs."""


class OAuthTokenError(OAuthError):
    """Token failure; ambiguous responses require a fresh authorization."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def resolve_addresses(host: str, port: int) -> list[str]:
    """Resolve off the event loop; the transport dials only a checked address."""
    entries = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(str(entry[4][0]) for entry in entries))


class OAuthHTTP:
    """Bounded, DNS-pinned HTTP with no ambient credentials or proxy settings."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str, int], Awaitable[list[str]]] = resolve_addresses,
    ) -> None:
        self.transport = transport
        self.resolver = resolver
        self.deadline = time.monotonic() + 45
        self.retries = 0

    async def validate(self, url: str) -> tuple[httpx.URL, str]:
        """Validate all DNS answers and return one pinned address with TLS SNI."""
        try:
            if not isinstance(url, str):
                raise ValueError
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or "\\" in url
                or any(ord(char) <= 32 for char in url)
            ):
                raise ValueError
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            answers = await self.resolver(parsed.hostname, port)
            networks = [
                ipaddress.ip_network(part.strip())
                for part in os.getenv("MCP_OAUTH_ALLOWED_CIDRS", "").split(",")
                if part.strip()
            ]
            if not answers:
                raise ValueError
            for answer in answers:
                address = ipaddress.ip_address(answer)
                address = getattr(address, "ipv4_mapped", None) or address
                allowed = any(address in network for network in networks)
                if (
                    address.is_link_local
                    or address.is_multicast
                    or address.is_unspecified
                    or address.is_reserved
                    or (not address.is_global and not allowed)
                    or (parsed.scheme != "https" and not allowed)
                ):
                    raise ValueError
            return httpx.URL(url), answers[0]
        except (ValueError, TypeError, OSError):
            raise OAuthError(
                "OAuth URL is invalid or targets a blocked network."
            ) from None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float = 15,
        probe: bool = False,
        data: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async with asyncio.timeout(timeout):
            original, address = await self.validate(url)
            # Pin DNS without changing certificate validation or HTTP authority.
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                request = client.build_request(
                    method,
                    original.copy_with(host=address),
                    data=data,
                    json=body,
                    headers={"Host": original.netloc.decode(), **(headers or {})},
                    extensions={"sni_hostname": original.host},
                )
                response = await client.send(request, stream=True)
                try:
                    if probe:
                        return httpx.Response(
                            response.status_code, headers=response.headers
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 262144:
                            raise OAuthError("OAuth response is too large.")
                    return httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=bytes(chunks),
                    )
                finally:
                    await response.aclose()

    async def get(self, url: str, *, probe: bool = False) -> httpx.Response:
        """Read metadata with a shared 45s budget, two retries, three redirects."""
        seen: set[str] = set()
        redirects = 0
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise OAuthError("OAuth discovery deadline exceeded.")
            try:
                response = await self._request(
                    "GET", url, timeout=min(15, remaining), probe=probe
                )
            except (httpx.TransportError, TimeoutError, OSError):
                response = None
            if response is None or response.status_code in {429, 500, 502, 503, 504}:
                delay = retry_delay(response) if response is not None else 0.2
                if self.retries >= 2 or delay >= self.deadline - time.monotonic():
                    raise OAuthError("OAuth discovery is temporarily unavailable.")
                self.retries += 1
                await asyncio.sleep(delay)
                continue
            if response.is_redirect:
                location = response.headers.get("location")
                if not location or url in seen or redirects >= 3:
                    raise OAuthError("OAuth metadata redirect loop or limit exceeded.")
                seen.add(url)
                url = urljoin(url, location)
                redirects += 1
                continue
            return response

    async def post(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send once. Never include provider error bodies in exceptions."""
        try:
            response = await self._request(
                "POST", url, data=data, body=body, headers=headers
            )
        except (httpx.TransportError, TimeoutError, OSError):
            raise OAuthTokenError(
                "OAuth response was not received. Reconnect the account."
            ) from None
        if response.status_code == 429:
            raise OAuthTokenError(
                "OAuth provider rate limited the request. Try again later.",
                retry_after=max(1, retry_delay(response)),
            )
        if response.status_code not in {200, 201}:
            raise OAuthTokenError(
                "OAuth authorization was rejected or could not be confirmed. Reconnect the account."
            )
        return metadata_object(response)


def retry_delay(response: httpx.Response) -> float:
    """Honor Retry-After only when it fits the caller's deadline."""
    value = response.headers.get("retry-after", "0.2")
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 0.2
    return max(0.0, min(delay, 86400)) if math.isfinite(delay) else 86400


def metadata_object(response: httpx.Response) -> dict[str, Any]:
    """Reject non-object or malformed JSON with a redacted message."""
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, UnicodeError):
        raise OAuthError("OAuth metadata or response is malformed.") from None


def well_known(url: str, name: str) -> list[str]:
    """RFC 8414/9728 path-aware location followed by origin fallback."""
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return list(
        dict.fromkeys(
            [
                f"{origin}/.well-known/{name}{parsed.path.rstrip('/')}",
                f"{origin}/.well-known/{name}",
            ]
        )
    )


async def _first_metadata(urls: list[str], http: OAuthHTTP) -> dict[str, Any] | None:
    for url in urls:
        response = await http.get(url)
        if response.status_code == 200:
            return metadata_object(response)
        if response.status_code not in {404, 405}:
            raise OAuthError("OAuth metadata could not be read.")
    return None


async def discover(
    resource: str, config: dict[str, Any], http: OAuthHTTP
) -> dict[str, Any]:
    """Bound the entire discovery, including DNS and endpoint validation."""
    try:
        async with asyncio.timeout(max(0, http.deadline - time.monotonic())):
            return await _discover(resource, config, http)
    except (TimeoutError, OSError):
        raise OAuthError(
            "OAuth discovery deadline exceeded or DNS is unavailable."
        ) from None


async def _discover(
    resource: str, config: dict[str, Any], http: OAuthHTTP
) -> dict[str, Any]:
    """Discover the protected resource, then its explicitly selected issuer."""
    # No bearer credentials, even on the initial challenge probe.
    challenge = await http.get(resource, probe=True)
    header = challenge.headers.get("www-authenticate", "")
    match = re.search(
        r'\bresource_metadata\s*=\s*(?:"([^"\\]+)"|([^\s,]+))', header, re.I
    )
    explicit = "resource_metadata" in header.lower()
    if explicit and not match:
        raise OAuthError("OAuth protected resource challenge is malformed.")
    protected: dict[str, Any] | None
    if match:
        response = await http.get(match.group(1) or match.group(2))
        if response.status_code != 200:
            raise OAuthError("Advertised OAuth resource metadata is unavailable.")
        protected = metadata_object(response)
    else:
        protected = await _first_metadata(
            well_known(resource, "oauth-protected-resource"), http
        )
    if protected is not None:
        if protected.get("resource") != resource:
            raise OAuthError(
                "OAuth resource metadata does not match the configured resource."
            )
        issuers = protected.get("authorization_servers")
        if (
            not isinstance(issuers, list)
            or not issuers
            or not all(isinstance(item, str) for item in issuers)
        ):
            raise OAuthError(
                "OAuth resource metadata has no valid authorization servers."
            )
        issuer = config.get("issuer")
        if issuer and issuer not in issuers:
            raise OAuthError("Configured OAuth issuer contradicts resource metadata.")
        if not issuer and len(issuers) != 1:
            raise OAuthError(
                "Several OAuth authorization servers are advertised. Configure an issuer explicitly."
            )
        issuer = issuer or issuers[0]
        parsed = urlsplit(issuer)
        if parsed.query or parsed.fragment:
            raise OAuthError("OAuth issuer must not contain a query or fragment.")
        await http.validate(issuer)
        # RFC 8414 followed by OIDC discovery, including issuer-path placement.
        urls = well_known(issuer, "oauth-authorization-server")[:1]
        urls += [issuer.rstrip("/") + "/.well-known/openid-configuration"]
        urls += well_known(issuer, "openid-configuration")[:1]
        metadata = await _first_metadata(list(dict.fromkeys(urls)), http)
        if not metadata or metadata.get("issuer") != issuer:
            raise OAuthError("OAuth authorization metadata issuer mismatch.")
    else:
        # Legacy MCP servers publish AS metadata directly; never use this branch
        # after explicit resource metadata has contradicted the configuration.
        metadata = await _first_metadata(
            well_known(resource, "oauth-authorization-server"), http
        )
        if not metadata or not isinstance(metadata.get("issuer"), str):
            raise OAuthError("No compatible OAuth metadata was discovered.")
        issuer = metadata["issuer"]
        origin = urlunsplit((*urlsplit(resource)[:2], "", "", ""))
        if issuer not in {resource, origin} or (
            config.get("issuer") and config["issuer"] != issuer
        ):
            raise OAuthError("Legacy OAuth metadata issuer mismatch.")
    for field in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(metadata.get(field), str):
            raise OAuthError("OAuth metadata is missing required endpoints.")
        await http.validate(metadata[field])
    for field in ("registration_endpoint", "revocation_endpoint"):
        if metadata.get(field):
            await http.validate(metadata[field])
    if not _supports(metadata, "code_challenge_methods_supported", "S256"):
        raise OAuthError("OAuth provider does not support S256 PKCE.")
    if not _supports(metadata, "response_types_supported", "code"):
        raise OAuthError("OAuth provider does not support authorization code flow.")
    if not _supports(metadata, "grant_types_supported", "authorization_code"):
        raise OAuthError("OAuth provider does not support authorization code grants.")
    return {**metadata, "issuer": issuer, "resource": resource}


def _supports(metadata: dict[str, Any], key: str, value: str) -> bool:
    values = metadata.get(key, [value])
    return (
        isinstance(values, list)
        and all(isinstance(item, str) for item in values)
        and value in values
    )


async def configure_client(
    config: dict[str, Any], metadata: dict[str, Any], redirect_uri: str, http: OAuthHTTP
) -> dict[str, Any]:
    """Use an explicit client, or RFC 7591 public registration when advertised."""
    result = dict(config)
    if result.get("client_id") and not isinstance(result["client_id"], str):
        raise OAuthError("OAuth client_id must be a string.")
    scopes = result.get("scope", result.get("scopes", ""))
    if not isinstance(scopes, str) and not (
        isinstance(scopes, list) and all(isinstance(item, str) for item in scopes)
    ):
        raise OAuthError("OAuth scopes must be a string or a list of strings.")
    method = result.get(
        "token_endpoint_auth_method",
        "client_secret_post" if result.get("client_secret") else "none",
    )
    if not result.get("client_id"):
        endpoint = metadata.get("registration_endpoint")
        if not endpoint or not _supports(
            metadata, "token_endpoint_auth_methods_supported", "none"
        ):
            raise OAuthError(
                "Public client registration is unavailable. Configure a client_id explicitly."
            )
        registered = await http.post(
            endpoint,
            body={
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "client_name": "Preloop MCP Proxy",
            },
        )
        if (
            not isinstance(registered.get("client_id"), str)
            or not registered["client_id"]
            or registered.get("token_endpoint_auth_method", "none") != "none"
            or registered.get("redirect_uris", [redirect_uri]) != [redirect_uri]
        ):
            raise OAuthError(
                "OAuth public client registration returned incompatible metadata."
            )
        result["client_id"] = registered["client_id"]
        method = "none"
    if (
        not isinstance(method, str)
        or method
        not in {
            "none",
            "client_secret_post",
            "client_secret_basic",
        }
        or not _supports(metadata, "token_endpoint_auth_methods_supported", method)
    ):
        raise OAuthError("OAuth client authentication method is unsupported.")
    if method != "none" and not result.get("client_secret"):
        raise OAuthError("The configured OAuth client requires a client secret.")
    result.update(
        {
            key: metadata[key]
            for key in (
                "issuer",
                "resource",
                "authorization_endpoint",
                "token_endpoint",
                "revocation_endpoint",
                "authorization_response_iss_parameter_supported",
            )
            if key in metadata
        }
    )
    result["token_endpoint_auth_method"] = method
    result["redirect_uri"] = redirect_uri
    return result


def authorization_url(config: dict[str, Any], state: str, verifier: str) -> str:
    """Bind S256, exact redirect, scopes and resource to the selected issuer."""
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    scope = config.get("scope", config.get("scopes", ""))
    if isinstance(scope, list):
        scope = " ".join(scope)
    params = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": config["resource"],
    }
    if scope:
        params["scope"] = scope
    endpoint = str(config["authorization_endpoint"])
    if any(key in params for key, _ in parse_qsl(urlsplit(endpoint).query)):
        raise OAuthError(
            "OAuth authorization endpoint contains conflicting parameters."
        )
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)


async def request_tokens(
    config: dict[str, Any],
    http: OAuthHTTP,
    *,
    code: str | None = None,
    verifier: str | None = None,
) -> dict[str, Any]:
    """Exchange once; validate the entire token pair before persisting either."""
    data = {"client_id": config["client_id"], "resource": config["resource"]}
    if code is not None:
        data.update(
            grant_type="authorization_code",
            code=code,
            code_verifier=verifier or "",
            redirect_uri=config["redirect_uri"],
        )
    else:
        data.update(grant_type="refresh_token", refresh_token=config["refresh_token"])
    headers = {}
    method = config.get("token_endpoint_auth_method", "none")
    if method == "client_secret_post":
        data["client_secret"] = config["client_secret"]
    elif method == "client_secret_basic":
        from urllib.parse import quote_plus

        pair = (
            quote_plus(config["client_id"]) + ":" + quote_plus(config["client_secret"])
        )
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    token = await http.post(config["token_endpoint"], data=data, headers=headers)
    if (
        not isinstance(token.get("access_token"), str)
        or not token["access_token"]
        or str(token.get("token_type", "Bearer")).lower() != "bearer"
    ):
        raise OAuthTokenError(
            "OAuth provider returned an invalid access token. Reconnect the account."
        )
    result = {**config, "access_token": token["access_token"], "token_type": "Bearer"}
    if "refresh_token" in token:
        if not isinstance(token["refresh_token"], str) or not token["refresh_token"]:
            raise OAuthTokenError(
                "OAuth provider returned an invalid refresh token. Reconnect the account."
            )
        result["refresh_token"] = token["refresh_token"]
    # An omitted expiry means unknown, not the old (already expired) value.
    result.pop("expires_at", None)
    if "expires_in" in token:
        expiry = token["expires_in"]
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not 0 < expiry < 315360000
        ):
            raise OAuthTokenError(
                "OAuth provider returned an invalid expiry. Reconnect the account."
            )
        result["expires_at"] = time.time() + expiry
    if isinstance(token.get("scope"), str):
        result["scope"] = token["scope"]
    return result


class UpstreamMCPOAuthService:
    """Runtime resolver; each CRUD operation owns and closes its DB session."""

    def __init__(
        self,
        *,
        session_factory: Any = None,
        http_factory: Callable[[], OAuthHTTP] = OAuthHTTP,
    ) -> None:
        self.session_factory = session_factory
        self.http_factory = http_factory

    async def _db(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        from preloop.models.crud.upstream_mcp_oauth import crud_upstream_mcp_oauth
        from preloop.models.db.session import get_session_factory

        def run() -> Any:
            factory = self.session_factory or get_session_factory()
            with factory() as db:
                return getattr(crud_upstream_mcp_oauth, operation)(db, *args, **kwargs)

        return await asyncio.to_thread(run)

    async def credentials(
        self, server_id: Any, *, force_version: int | None = None
    ) -> Any:
        """Read before every session; serialize refresh without caching grants."""
        from preloop.models.crud.upstream_mcp_oauth import OAuthContextError

        deadline = time.monotonic() + 17
        while time.monotonic() < deadline:
            snapshot = await self._db("prepare", server_id, force_version=force_version)
            if snapshot is None:
                await asyncio.sleep(0.05)
                continue
            if snapshot.lease_id is None:
                return snapshot
            try:
                config = await request_tokens(snapshot.config, self.http_factory())
            except (OAuthError, KeyError) as error:
                retry_after = (
                    error.retry_after if isinstance(error, OAuthTokenError) else None
                )
                await self._db("fail", snapshot, retry_after=retry_after)
                raise OAuthError(
                    "OAuth refresh failed. Reconnect the account or retry after the provider rate limit."
                ) from None
            if not await self._db("complete", snapshot, config):
                raise OAuthContextError(
                    "OAuth configuration changed during refresh. Start a new connection."
                )
            # Re-read the winning version, including any concurrent disconnect.
            force_version = None
        raise OAuthError("OAuth refresh is in progress. Try the request again later.")
