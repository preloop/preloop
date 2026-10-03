"""Shared Bitbucket Data Center helpers.

Pure functions and constants for the ``bitbucket_dc`` tracker: deployment
policy (feature flag, approved instances, private-network allowlist, CA
bundle), canonical instance URL handling, destination pinning, pagination
cursors and payload normalisation. Nothing here performs HTTP I/O; the only
network call is the DNS resolution in :func:`resolve_pinned_address`, which
the tracker performs at connection time.

Baseline: **Bitbucket Data Center 10.2 LTS**, REST API ``/rest/api/1.0``.
Shapes were verified against the official versioned reference
(``https://developer.atlassian.com/server/bitbucket/rest/v1002/``, OpenAPI
``info.version = "10.2"``, document ``10.2.swagger.v3.json``). Other releases
are reported as unvalidated, never silently treated as Bitbucket Cloud.

Deployment configuration (environment):

* ``PRELOOP_BITBUCKET_DC_ENABLED`` - opt-in flag, off by default.
* ``PRELOOP_BITBUCKET_DC_INSTANCES`` - JSON array of approved canonical HTTPS
  instance URLs (origin plus optional context path), for example
  ``["https://bitbucket.example.com", "https://scm.example.com:8443/bitbucket"]``.
* ``PRELOOP_BITBUCKET_DC_PRIVATE_NETWORKS`` - comma separated CIDRs an
  approved instance may resolve to inside private address space. Without it,
  an instance that resolves to a private address is refused.
* ``PRELOOP_BITBUCKET_DC_CA_BUNDLE`` - optional PEM bundle for a private CA.
  TLS verification stays on either way.

Link-local, loopback, multicast, reserved and cloud metadata destinations are
refused even when allowlisted.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import unquote, urlsplit

ENV_ENABLED = "PRELOOP_BITBUCKET_DC_ENABLED"
ENV_INSTANCES = "PRELOOP_BITBUCKET_DC_INSTANCES"
ENV_PRIVATE_NETWORKS = "PRELOOP_BITBUCKET_DC_PRIVATE_NETWORKS"
ENV_CA_BUNDLE = "PRELOOP_BITBUCKET_DC_CA_BUNDLE"

BITBUCKET_DC_TRACKER_TYPE = "bitbucket_dc"
BITBUCKET_DC_REST_PREFIX = "rest/api/1.0"
BITBUCKET_DC_DEFAULT_VERSION = "10.2"
# Releases whose REST contract has been fixture-tested. Anything else is
# "unsupported/unvalidated", not an error at runtime for read paths but
# rejected at configuration time.
BITBUCKET_DC_SUPPORTED_VERSIONS: Tuple[str, ...] = ("10.2",)

# Authentication modes stored on ``Tracker.auth_type``. Only a user personal
# access token sent as ``Authorization: Bearer`` is supported.
BITBUCKET_DC_AUTH_API_TOKEN = "api_token"
BITBUCKET_DC_AUTH_TYPES = (BITBUCKET_DC_AUTH_API_TOKEN,)

# Response header Bitbucket Data Center adds to authenticated responses with
# the current user's slug. Not part of the OpenAPI document; used only as an
# optimisation to learn the reviewer slug when ``username`` is not configured.
CURRENT_USER_HEADER = "X-AUSERNAME"

PAT_MESSAGE = (
    "Bitbucket Data Center trackers authenticate with a user personal access "
    "token (Bitbucket profile, Manage account, HTTP access tokens) with "
    "repository read and write permission."
)

# Addresses that are refused regardless of any allowlist.
_METADATA_NETWORKS: Tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local, incl. metadata
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS over IPv6
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba metadata
    ipaddress.ip_network("168.63.129.16/32"),  # Azure platform virtual IP
    # Transition routes can reach embedded IPv4 destinations with different
    # network classification; do not permit them as repository destinations.
    ipaddress.ip_network("2002::/16"),  # 6to4
    ipaddress.ip_network("2001::/32"),  # Teredo
    ipaddress.ip_network("::ffff:169.254.0.0/112"),  # mapped IPv4 link-local
)

_PROJECT_KEY_RE = re.compile(r"^~?[A-Za-z][A-Za-z0-9_\-.]{0,127}$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
_USER_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@\-]{0,254}$")
_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_CONTEXT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


class BitbucketDCConfigError(ValueError):
    """Raised when Bitbucket Data Center tracker configuration is invalid."""


class BitbucketDCIdentityError(ValueError):
    """Raised when a payload belongs to another instance, project or repository."""


class BitbucketDCPaginationError(ValueError):
    """Raised when a paged response is malformed or its cursor does not progress."""


@dataclass(frozen=True)
class InstanceIdentity:
    """Canonical identity of one Bitbucket Data Center instance.

    Attributes:
        host: Lower-case DNS name or IP literal.
        port: Effective TCP port (443 when the URL carried none).
        context_path: Context path without trailing slash (``""`` or ``/bitbucket``).
    """

    host: str
    port: int
    context_path: str

    @property
    def origin(self) -> str:
        """``https://host[:port]`` with the default port omitted."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == 443:
            return f"https://{host}"
        return f"https://{host}:{self.port}"

    @property
    def base_url(self) -> str:
        """Canonical instance URL: origin plus context path."""
        return f"{self.origin}{self.context_path}"

    @property
    def rest_base_url(self) -> str:
        """Base of the REST 1.0 API on this instance."""
        return f"{self.base_url}/{BITBUCKET_DC_REST_PREFIX}"

    def owns_url(self, url: Optional[str]) -> bool:
        """Whether ``url`` is on this origin and under this context path.

        Args:
            url: Absolute URL from a payload or link.

        Returns:
            True only for the same scheme, host, port and context path.
        """
        if not url:
            return False
        try:
            other = parse_instance_url(url, allow_path=True)
        except BitbucketDCConfigError:
            return False
        if (other.host, other.port) != (self.host, self.port):
            return False
        context = self.context_path
        return other.context_path == context or other.context_path.startswith(
            f"{context}/"
        )


# ----------------------------------------------------------------------
# Environment policy
# ----------------------------------------------------------------------


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def bitbucket_dc_enabled() -> bool:
    """Whether the ``bitbucket_dc`` capability is switched on for this deployment.

    Returns:
        True when ``PRELOOP_BITBUCKET_DC_ENABLED`` is truthy. Off by default.
    """
    return _truthy(os.getenv(ENV_ENABLED))


def approved_instances() -> List[InstanceIdentity]:
    """Parse ``PRELOOP_BITBUCKET_DC_INSTANCES``.

    Returns:
        The approved instance identities. Empty when unset.

    Raises:
        BitbucketDCConfigError: When the value is not a JSON array of
            canonical HTTPS URL strings.
    """
    raw = (os.getenv(ENV_INSTANCES) or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise BitbucketDCConfigError(
            f"{ENV_INSTANCES} must be a JSON array of HTTPS URL strings."
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        raise BitbucketDCConfigError(
            f"{ENV_INSTANCES} must be a JSON array of HTTPS URL strings."
        )
    identities: List[InstanceIdentity] = []
    for entry in parsed:
        identity = parse_instance_url(entry)
        if identity.base_url != entry.rstrip("/"):
            raise BitbucketDCConfigError(
                f"{ENV_INSTANCES} entry {entry!r} is not canonical; "
                f"use {identity.base_url!r}."
            )
        identities.append(identity)
    return identities


def private_networks() -> List[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse ``PRELOOP_BITBUCKET_DC_PRIVATE_NETWORKS`` (comma separated CIDRs).

    Returns:
        Networks inside which a private-address instance may be contacted.

    Raises:
        BitbucketDCConfigError: When an entry is not a CIDR.
    """
    raw = (os.getenv(ENV_PRIVATE_NETWORKS) or "").strip()
    networks: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise BitbucketDCConfigError(
                f"{ENV_PRIVATE_NETWORKS} entry {part!r} is not a CIDR."
            ) from exc
    return networks


def ca_bundle_path() -> Optional[str]:
    """Return the configured private CA bundle path, if any.

    Raises:
        BitbucketDCConfigError: When the configured file does not exist.
    """
    raw = (os.getenv(ENV_CA_BUNDLE) or "").strip()
    if not raw:
        return None
    if not os.path.isfile(raw):
        raise BitbucketDCConfigError(f"{ENV_CA_BUNDLE} does not point at a file.")
    return raw


# ----------------------------------------------------------------------
# Canonical instance URLs
# ----------------------------------------------------------------------


def _blocked_address_reason(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> Optional[str]:
    """Return why an address is never allowed, or None."""
    for network in _METADATA_NETWORKS:
        if address.version == network.version and address in network:
            return "link-local or metadata"
    if address.is_loopback:
        return "loopback"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    if address.is_reserved:
        return "reserved"
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _blocked_address_reason(mapped)
    return None


def parse_instance_url(url: Any, *, allow_path: bool = False) -> InstanceIdentity:
    """Parse and canonicalise a Bitbucket Data Center instance URL.

    Args:
        url: ``https://host[:port][/context]``.
        allow_path: Accept arbitrary paths (used when checking payload links
            against an instance); the identity's context path then carries
            the whole path.

    Returns:
        The canonical identity.

    Raises:
        BitbucketDCConfigError: For a non-HTTPS scheme, userinfo, query,
            fragment, empty or malformed host, invalid port, path traversal,
            encoded or empty path segments.
    """
    text = str(url or "").strip()
    if not text:
        raise BitbucketDCConfigError("Bitbucket Data Center instance URL is required.")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in text):
        raise BitbucketDCConfigError("Instance URL must not contain whitespace.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise BitbucketDCConfigError("Instance URL authority is invalid.") from exc
    if parts.scheme.lower() != "https":
        raise BitbucketDCConfigError("Instance URL must use https.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BitbucketDCConfigError("Instance URL must not contain credentials.")
    if parts.query or parts.fragment or text.endswith("?") or text.endswith("#"):
        raise BitbucketDCConfigError(
            "Instance URL must not contain a query or fragment."
        )
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise BitbucketDCConfigError("Instance URL port is invalid.") from exc
    if not hostname:
        raise BitbucketDCConfigError("Instance URL has no host.")
    host = hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        reason = _blocked_address_reason(address)
        if reason:
            raise BitbucketDCConfigError(f"Instance address is {reason}; refused.")
    else:
        if not all(_HOST_LABEL_RE.match(label) for label in host.split(".")):
            raise BitbucketDCConfigError("Instance URL host is not a valid DNS name.")
    if port is None:
        port = 443
    if not 1 <= port <= 65535:
        raise BitbucketDCConfigError("Instance URL port is invalid.")

    raw_path = parts.path or ""
    if "%" in raw_path:
        raise BitbucketDCConfigError("Instance URL path must not be percent-encoded.")
    if "\\" in raw_path or "//" in raw_path:
        raise BitbucketDCConfigError("Instance URL path contains empty segments.")
    segments = [s for s in raw_path.split("/") if s != ""]
    if raw_path and raw_path != "/" and not raw_path.startswith("/"):
        raise BitbucketDCConfigError("Instance URL path is malformed.")
    for segment in segments:
        decoded = unquote(segment)
        if decoded in (".", "..") or decoded != segment:
            raise BitbucketDCConfigError("Instance URL path contains traversal.")
        if not allow_path and not _CONTEXT_SEGMENT_RE.match(segment):
            raise BitbucketDCConfigError("Instance context path segment is invalid.")
    context_path = "/" + "/".join(segments) if segments else ""
    return InstanceIdentity(host=host, port=port, context_path=context_path)


def canonical_instance_url(url: Any) -> str:
    """Return the canonical form of an instance URL.

    Args:
        url: The URL as supplied by an administrator.

    Returns:
        ``https://host[:port][/context]`` without a trailing slash.

    Raises:
        BitbucketDCConfigError: When the URL is invalid.
    """
    return parse_instance_url(url).base_url


def approved_instance_for(url: Any) -> InstanceIdentity:
    """Return the approved identity matching ``url``.

    The origin, port and context path all have to match an entry of
    ``PRELOOP_BITBUCKET_DC_INSTANCES`` exactly; a sibling context path on the
    same host is a different instance.

    Raises:
        BitbucketDCConfigError: When the capability is off, the URL is not
            canonical or the instance is not approved.
    """
    if not bitbucket_dc_enabled():
        raise BitbucketDCConfigError(
            f"Bitbucket Data Center trackers are not enabled ({ENV_ENABLED})."
        )
    identity = parse_instance_url(url)
    for approved in approved_instances():
        if approved == identity:
            return identity
    raise BitbucketDCConfigError(
        f"Bitbucket Data Center instance {identity.base_url} is not in the "
        f"administrator-approved list ({ENV_INSTANCES})."
    )


# ----------------------------------------------------------------------
# Destination pinning
# ----------------------------------------------------------------------

Resolver = Callable[[str, int], Sequence[str]]


def default_resolver(host: str, port: int) -> List[str]:
    """Resolve ``host`` to its addresses with the system resolver."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BitbucketDCConfigError(
            f"Could not resolve Bitbucket Data Center host {host}."
        ) from exc
    seen: List[str] = []
    for info in infos:
        candidate = str(info[4][0])
        if candidate not in seen:
            seen.append(candidate)
    return seen


def check_destination_address(
    raw: str,
    *,
    allowed_private: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate one resolved address against the network policy.

    Args:
        raw: Address text as returned by the resolver.
        allowed_private: Private networks approved by the deployment.

    Returns:
        The parsed address.

    Raises:
        BitbucketDCConfigError: For link-local/metadata/loopback/reserved
            addresses, or private addresses outside the allowlist.
    """
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError as exc:
        raise BitbucketDCConfigError(
            f"Resolver returned a non-address {raw!r}."
        ) from exc
    reason = _blocked_address_reason(address)
    if reason:
        raise BitbucketDCConfigError(
            f"Bitbucket Data Center host resolves to a {reason} address; refused."
        )
    effective: ipaddress.IPv4Address | ipaddress.IPv6Address = address
    if address.version == 6 and getattr(address, "ipv4_mapped", None) is not None:
        effective = address.ipv4_mapped  # type: ignore[assignment]
    if not effective.is_global:
        if not any(
            effective.version == net.version and effective in net
            for net in allowed_private
        ):
            raise BitbucketDCConfigError(
                "Bitbucket Data Center host resolves to a private address that is "
                f"not allowlisted in {ENV_PRIVATE_NETWORKS}."
            )
    return address


def resolve_pinned_address(
    identity: InstanceIdentity,
    *,
    resolver: Optional[Resolver] = None,
    allowed_private: Optional[
        Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ] = None,
) -> str:
    """Resolve the instance host now and return the address to connect to.

    Every resolved address is checked, so a name with one public and one
    internal record is refused rather than racing the resolver.

    Args:
        identity: The approved instance.
        resolver: Replacement for the system resolver, for tests.
        allowed_private: Networks from the deployment policy; defaults to
            :func:`private_networks`.

    Returns:
        The first validated address, suitable for the connection target.

    Raises:
        BitbucketDCConfigError: When resolution fails or any address is refused.
    """
    networks = list(private_networks() if allowed_private is None else allowed_private)
    try:
        literal = ipaddress.ip_address(identity.host)
    except ValueError:
        literal = None
    if literal is not None:
        return str(check_destination_address(str(literal), allowed_private=networks))
    addresses = list((resolver or default_resolver)(identity.host, identity.port))
    if not addresses:
        raise BitbucketDCConfigError(
            f"Could not resolve Bitbucket Data Center host {identity.host}."
        )
    validated = [
        check_destination_address(addr, allowed_private=networks) for addr in addresses
    ]
    return str(validated[0])


# ----------------------------------------------------------------------
# Tracker configuration
# ----------------------------------------------------------------------


def validate_project_key(value: Any) -> str:
    """Return a validated Bitbucket project key (``PRJ`` or personal ``~jane``)."""
    key = str(value or "").strip()
    if not _PROJECT_KEY_RE.match(key):
        raise BitbucketDCConfigError(f"Invalid Bitbucket project key {key!r}.")
    return key


def validate_repository_slug(value: Any) -> str:
    """Return a validated repository slug."""
    slug = str(value or "").strip()
    if not _REPO_SLUG_RE.match(slug) or slug in (".", ".."):
        raise BitbucketDCConfigError(f"Invalid Bitbucket repository slug {slug!r}.")
    return slug


def validate_repository_id(value: Any) -> int:
    """Return a validated positive integer repository id."""
    if isinstance(value, bool):
        raise BitbucketDCConfigError("repository_id must be a positive integer.")
    try:
        repo_id = int(value)
    except (TypeError, ValueError) as exc:
        raise BitbucketDCConfigError(
            "repository_id must be a positive integer."
        ) from exc
    if repo_id <= 0 or str(value).strip() != str(repo_id):
        raise BitbucketDCConfigError("repository_id must be a positive integer.")
    return repo_id


def validate_user_slug(value: Any) -> str:
    """Return a validated user slug for ``participants/{userSlug}``."""
    slug = str(value or "").strip()
    if not _USER_SLUG_RE.match(slug) or "/" in slug:
        raise BitbucketDCConfigError(f"Invalid Bitbucket user slug {slug!r}.")
    return slug


def validate_bitbucket_dc_config(
    *,
    api_key: Optional[str],
    auth_type: Optional[str],
    connection_details: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate a Bitbucket Data Center tracker configuration before any I/O.

    Args:
        api_key: The user personal access token.
        auth_type: Must be ``api_token`` (the only supported mode).
        connection_details: ``instance_url`` (required, canonical HTTPS URL
            of an approved instance), optional ``project_key``,
            ``repository_slug`` (requires ``project_key``), integer
            ``repository_id``, ``version`` (default ``10.2``) and
            ``username`` (reviewer user slug).

    Returns:
        The normalised connection details (canonical ``instance_url``,
        integer ``repository_id``, explicit ``version``).

    Raises:
        BitbucketDCConfigError: With a user-facing message when invalid.
    """
    details = dict(connection_details or {})
    mode = (auth_type or BITBUCKET_DC_AUTH_API_TOKEN).strip().lower()
    if mode not in BITBUCKET_DC_AUTH_TYPES:
        raise BitbucketDCConfigError(
            f"Unsupported Bitbucket Data Center auth_type '{auth_type}'. "
            f"Use 'api_token' (a user personal access token). {PAT_MESSAGE}"
        )
    token = str(api_key or "").strip()
    if not token:
        raise BitbucketDCConfigError(
            f"A personal access token is required. {PAT_MESSAGE}"
        )
    if any(ch.isspace() for ch in token) or ":" in token:
        raise BitbucketDCConfigError(
            "The personal access token must be the bare token, not a "
            "'user:token' pair or a header value."
        )
    identity = approved_instance_for(details.get("instance_url"))
    normalised: Dict[str, Any] = {**details, "instance_url": identity.base_url}

    version = str(details.get("version") or BITBUCKET_DC_DEFAULT_VERSION).strip()
    if version not in BITBUCKET_DC_SUPPORTED_VERSIONS:
        raise BitbucketDCConfigError(
            f"Bitbucket Data Center {version} is not validated; supported "
            f"baselines: {', '.join(BITBUCKET_DC_SUPPORTED_VERSIONS)}."
        )
    normalised["version"] = version

    project_key = details.get("project_key")
    if project_key not in (None, ""):
        normalised["project_key"] = validate_project_key(project_key)
    else:
        normalised.pop("project_key", None)

    slug = details.get("repository_slug")
    if slug not in (None, ""):
        if "project_key" not in normalised:
            raise BitbucketDCConfigError(
                "repository_slug requires project_key in connection_details."
            )
        normalised["repository_slug"] = validate_repository_slug(slug)
    else:
        normalised.pop("repository_slug", None)

    repo_id = details.get("repository_id")
    if repo_id not in (None, ""):
        if "project_key" not in normalised:
            raise BitbucketDCConfigError(
                "repository_id requires project_key in connection_details."
            )
        normalised["repository_id"] = validate_repository_id(repo_id)
    else:
        normalised.pop("repository_id", None)

    username = details.get("username")
    if username not in (None, ""):
        normalised["username"] = validate_user_slug(username)
    else:
        normalised.pop("username", None)
    return normalised


# ----------------------------------------------------------------------
# Pagination
# ----------------------------------------------------------------------


def next_page_start(
    page: Any, *, current_start: int, seen_starts: Set[int]
) -> Optional[int]:
    """Validate a 10.2 page envelope and return the next ``start`` cursor.

    Args:
        page: Decoded JSON body of a paged response.
        current_start: The ``start`` that produced this page.
        seen_starts: Cursors already requested; updated in place.

    Returns:
        The next cursor, or None when ``isLastPage`` is true.

    Raises:
        BitbucketDCPaginationError: When the body is not a page object,
            ``values`` is not a list, ``nextPageStart`` is missing or not an
            integer on a non-final page, or the cursor repeats or moves
            backwards.
    """
    if not isinstance(page, Mapping):
        raise BitbucketDCPaginationError("Paged response is not a JSON object.")
    values = page.get("values")
    if not isinstance(values, list):
        raise BitbucketDCPaginationError("Paged response has no 'values' list.")
    if not all(isinstance(value, Mapping) for value in values):
        raise BitbucketDCPaginationError("Paged response contains malformed values.")
    seen_starts.add(current_start)
    is_last = page.get("isLastPage")
    if is_last is True:
        return None
    if is_last is not False:
        raise BitbucketDCPaginationError("Paged response has no boolean 'isLastPage'.")
    nxt = page.get("nextPageStart")
    if isinstance(nxt, bool) or not isinstance(nxt, int):
        raise BitbucketDCPaginationError(
            "Paged response is not the last page but has no integer 'nextPageStart'."
        )
    if nxt <= current_start or nxt in seen_starts:
        raise BitbucketDCPaginationError(
            f"Pagination cursor did not progress (start={current_start}, next={nxt})."
        )
    return nxt


# ----------------------------------------------------------------------
# Payload helpers
# ----------------------------------------------------------------------


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def looks_like_cloud_payload(obj: Any) -> bool:
    """Detect a Bitbucket Cloud object offered where a Data Center one is expected.

    Cloud objects carry braced ``uuid`` values, ``full_name``, ``links.html``
    and ``source``/``destination`` branches; Data Center objects carry integer
    ids, ``slug``, ``links.self`` and ``fromRef``/``toRef``.
    """
    if not isinstance(obj, Mapping):
        return False
    if isinstance(obj.get("uuid"), str) and obj["uuid"].startswith("{"):
        return True
    if "full_name" in obj or "pullrequest" in obj:
        return True
    if "source" in obj and "destination" in obj and "fromRef" not in obj:
        return True
    if isinstance(obj.get("content"), Mapping) and "raw" in obj["content"]:
        return True
    return False


def reject_cloud_payload(obj: Any, what: str = "object") -> None:
    """Raise when ``obj`` is a Bitbucket Cloud payload.

    Raises:
        BitbucketDCIdentityError: With the offending object kind.
    """
    if looks_like_cloud_payload(obj):
        raise BitbucketDCIdentityError(
            f"Received a Bitbucket Cloud {what}; this tracker speaks the Data "
            "Center REST 1.0 contract and does not fall back to Cloud shapes."
        )


def user_name(user: Any) -> Optional[str]:
    """Return a stable handle for a Data Center user object."""
    if not isinstance(user, Mapping):
        return None
    return user.get("slug") or user.get("name") or user.get("displayName")


def ref_branch(ref: Any) -> Optional[str]:
    """Return the short branch name of a ``fromRef``/``toRef`` object."""
    if not isinstance(ref, Mapping):
        return None
    display = ref.get("displayId")
    if display:
        return str(display)
    full = str(ref.get("id") or "")
    if full.startswith("refs/heads/"):
        return full[len("refs/heads/") :]
    return full or None


def full_ref(branch: str) -> str:
    """Return the fully qualified ``refs/heads/`` id for a branch name."""
    name = str(branch or "").strip()
    if name.startswith("refs/"):
        return name
    return f"refs/heads/{name}"


def path_text(path: Any) -> Optional[str]:
    """Return a file path from a ``RestPath`` object or a plain string."""
    if isinstance(path, str):
        return path
    if isinstance(path, Mapping):
        if path.get("toString"):
            return str(path["toString"])
        components = path.get("components")
        if isinstance(components, list) and components:
            return "/".join(str(c) for c in components)
        parent = path.get("parent")
        name = path.get("name")
        if name:
            return f"{parent}/{name}" if parent else str(name)
    return None


def repository_identity(repo: Any) -> Optional[Dict[str, Any]]:
    """Return the stable identity of a Data Center repository object.

    Returns:
        ``{"repository_id": int, "repository_slug": str, "project_key": str,
        "project_id": Optional[int]}`` or None when ``repo`` has no integer id.
    """
    if not isinstance(repo, Mapping):
        return None
    repo_id = repo.get("id")
    if isinstance(repo_id, bool) or not isinstance(repo_id, int):
        return None
    raw_project = repo.get("project")
    project: Mapping[str, Any] = raw_project if isinstance(raw_project, Mapping) else {}
    return {
        "repository_id": repo_id,
        "repository_slug": str(repo.get("slug") or ""),
        "project_key": str(project.get("key") or ""),
        "project_id": project.get("id") if isinstance(project.get("id"), int) else None,
    }


def pull_request_web_url(
    identity: InstanceIdentity, project_key: str, slug: str, pr_id: int
) -> str:
    """Return the web URL of a pull request on the instance."""
    return (
        f"{identity.base_url}/projects/{project_key}/repos/{slug}"
        f"/pull-requests/{int(pr_id)}"
    )


def repository_web_url(identity: InstanceIdentity, project_key: str, slug: str) -> str:
    """Return the web URL of a repository on the instance."""
    return f"{identity.base_url}/projects/{project_key}/repos/{slug}/browse"


def self_link(obj: Any, identity: Optional[InstanceIdentity] = None) -> Optional[str]:
    """Return ``links.self[0].href`` when it belongs to ``identity``.

    Links pointing elsewhere are dropped: they are never followed and never
    surfaced as the object's URL.
    """
    links = _dig(obj, "links", "self")
    if isinstance(links, list):
        for link in links:
            href = link.get("href") if isinstance(link, Mapping) else None
            if href and (identity is None or identity.owns_url(href)):
                return str(href)
    return None


def clone_links(repo: Any) -> List[Dict[str, str]]:
    """Return the repository clone links as inert metadata.

    These are returned for the publication dependency; the tracker never
    contacts them.
    """
    links = _dig(repo, "links", "clone")
    result: List[Dict[str, str]] = []
    if isinstance(links, list):
        for link in links:
            if isinstance(link, Mapping) and link.get("href"):
                result.append(
                    {"name": str(link.get("name") or ""), "href": str(link["href"])}
                )
    return result


def epoch_millis_to_iso(value: Any) -> Optional[str]:
    """Convert a Data Center epoch-milliseconds timestamp to ISO 8601 UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


# Preloop commit status states -> Bitbucket Data Center build states.
BUILD_STATUS_STATES: Dict[str, str] = {
    "pending": "INPROGRESS",
    "success": "SUCCESSFUL",
    "failure": "FAILED",
    "error": "FAILED",
    "cancelled": "CANCELLED",
}

# Data Center build states -> shared check classification outcomes.
BUILD_STATUS_OUTCOMES: Dict[str, str] = {
    "SUCCESSFUL": "success",
    "FAILED": "failure",
    "INPROGRESS": "in_progress",
    "CANCELLED": "cancelled",
    "UNKNOWN": "unknown",
}

# Reviewer verdicts (``participants/{userSlug}`` status values).
PARTICIPANT_APPROVED = "APPROVED"
PARTICIPANT_NEEDS_WORK = "NEEDS_WORK"
PARTICIPANT_UNAPPROVED = "UNAPPROVED"


def build_object_attributes(
    pr: Mapping[str, Any], identity: Optional[InstanceIdentity] = None
) -> Dict[str, Any]:
    """Map a Data Center pull request onto the shared trigger ``object_attributes``.

    Args:
        pr: A ``RestPullRequest`` object.
        identity: The instance, used to build the web URL and to drop links
            to other hosts.

    Returns:
        A dict with title, description, url, branches, state, draft, author,
        number, iid, version and last_commit.

    Raises:
        BitbucketDCIdentityError: When ``pr`` is a Bitbucket Cloud object.
    """
    reject_cloud_payload(pr, "pull request")
    pr_id = pr.get("id")
    raw_to = pr.get("toRef")
    raw_from = pr.get("fromRef")
    to_ref: Mapping[str, Any] = raw_to if isinstance(raw_to, Mapping) else {}
    from_ref: Mapping[str, Any] = raw_from if isinstance(raw_from, Mapping) else {}
    url = self_link(pr, identity)
    repo = repository_identity(to_ref.get("repository"))
    if url is None and identity is not None and repo and isinstance(pr_id, int):
        url = pull_request_web_url(
            identity, repo["project_key"], repo["repository_slug"], pr_id
        )
    return {
        "title": pr.get("title"),
        "description": pr.get("description") or "",
        "url": url,
        "source_branch": ref_branch(from_ref),
        "target_branch": ref_branch(to_ref),
        "state": str(pr.get("state") or "").lower() or None,
        "draft": bool(pr.get("draft", False)),
        "author": user_name(_dig(pr, "author", "user")),
        "number": pr_id,
        "iid": pr_id,
        "version": pr.get("version"),
        "last_commit": {"id": from_ref.get("latestCommit")},
        "repository": repo,
    }


def normalize_comment(
    comment: Mapping[str, Any],
    *,
    anchor: Optional[Mapping[str, Any]] = None,
    parent_id: Optional[int] = None,
    thread_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Map a Data Center comment onto the shared review comment shape.

    Args:
        comment: A ``RestComment`` (possibly nested under an activity).
        anchor: The ``commentAnchor`` from the activity, when the comment
            object itself has no ``anchor``.
        parent_id: Id of the parent comment for replies.
        thread_id: Id of the root comment of the thread.

    Returns:
        A dict with id, author, body, timestamps, type, path, line, side,
        in_reply_to_id, thread_id, resolved, outdated, task and version.

    Raises:
        BitbucketDCIdentityError: When ``comment`` is a Bitbucket Cloud object.
    """
    reject_cloud_payload(comment, "comment")
    anchor_obj = (
        comment.get("anchor") if isinstance(comment.get("anchor"), Mapping) else anchor
    )
    anchor_obj = anchor_obj or {}
    line = anchor_obj.get("line")
    line_type = anchor_obj.get("lineType")
    file_type = anchor_obj.get("fileType")
    is_inline = bool(anchor_obj) and line is not None
    side: Optional[str] = None
    if anchor_obj:
        side = "LEFT" if (line_type == "REMOVED" or file_type == "FROM") else "RIGHT"
    severity = str(comment.get("severity") or "NORMAL").upper()
    state = str(comment.get("state") or "OPEN").upper()
    resolved = bool(comment.get("threadResolved")) or (
        severity == "BLOCKER" and state == "RESOLVED"
    )
    parent = comment.get("parent") if isinstance(comment.get("parent"), Mapping) else {}
    reply_to = parent.get("id") if parent else parent_id
    comment_id = comment.get("id")
    return {
        "id": comment_id,
        "author": user_name(comment.get("author")),
        "body": comment.get("text") or "",
        "created_at": epoch_millis_to_iso(comment.get("createdDate")),
        "updated_at": epoch_millis_to_iso(comment.get("updatedDate")),
        "type": "review_comment" if anchor_obj else "issue_comment",
        "path": path_text(anchor_obj.get("path")) if anchor_obj else None,
        "line": line if is_inline and side == "RIGHT" else None,
        "old_line": line if is_inline and side == "LEFT" else None,
        "side": side,
        "in_reply_to_id": reply_to,
        "html_url": None,
        "thread_id": thread_id or reply_to or comment_id,
        "resolved": resolved,
        "outdated": bool(anchor_obj.get("orphaned")) if anchor_obj else False,
        "task": severity == "BLOCKER",
        "state": state,
        "version": comment.get("version"),
        "deleted": False,
    }


def build_comment_anchor(
    *,
    path: str,
    from_hash: Optional[str] = None,
    to_hash: Optional[str] = None,
    line: Optional[int] = None,
    old_line: Optional[int] = None,
    line_type: Optional[str] = None,
    start_line: Optional[int] = None,
    src_path: Optional[str] = None,
    diff_type: str = "EFFECTIVE",
) -> Dict[str, Any]:
    """Build a ``RestCommentThreadDiffAnchor`` for an inline comment.

    Args:
        path: File path in the destination of the diff.
        from_hash: ``sinceId`` of the diff. Required for ``COMMIT`` and
            ``RANGE``; optional for ``EFFECTIVE`` (the server resolves the
            effective diff itself) but recorded when known.
        to_hash: ``untilId`` of the diff (source latest commit); same rule.
        line: New-file line (``fileType=TO``, ``lineType=ADDED`` by default).
        old_line: Old-file line (``fileType=FROM``, ``lineType=REMOVED``).
        line_type: Override ``ADDED``/``REMOVED``/``CONTEXT``.
        start_line: First line of a multi-line comment range.
        src_path: Previous path for moves and copies.
        diff_type: ``EFFECTIVE`` (default), ``COMMIT`` or ``RANGE``.

    Returns:
        The anchor payload.

    Raises:
        ValueError: When both ``line`` and ``old_line`` are given, the diff
            type is unknown, a ``COMMIT``/``RANGE`` anchor lacks a hash, or
            the path is empty.
    """
    if diff_type not in ("EFFECTIVE", "COMMIT", "RANGE"):
        raise ValueError(f"Unknown diff type {diff_type!r}.")
    if not path or not str(path).strip():
        raise ValueError("Inline comments need a file path.")
    if bool(from_hash) != bool(to_hash) or (
        diff_type != "EFFECTIVE" and not (from_hash and to_hash)
    ):
        raise ValueError(
            f"{diff_type} anchors need both fromHash and toHash (or neither for "
            "EFFECTIVE)."
        )
    anchor: Dict[str, Any] = {
        "diffType": diff_type,
        "path": path,
        "srcPath": src_path or path,
    }
    if from_hash and to_hash:
        anchor["fromHash"] = from_hash
        anchor["toHash"] = to_hash
    if line is not None and old_line is not None:
        raise ValueError("Give either line (new file) or old_line (old file).")
    if line is not None or old_line is not None:
        on_new = line is not None
        anchor["line"] = int(line if on_new else old_line)  # type: ignore[arg-type]
        anchor["fileType"] = "TO" if on_new else "FROM"
        anchor["lineType"] = line_type or ("ADDED" if on_new else "REMOVED")
        if start_line is not None and int(start_line) != anchor["line"]:
            raise NotImplementedError(
                "Ranged comment creation is unsupported: multilineMarker is read-only in the 10.2 contract."
            )
    return anchor


__all__ = [
    "BITBUCKET_DC_AUTH_API_TOKEN",
    "BITBUCKET_DC_DEFAULT_VERSION",
    "BITBUCKET_DC_REST_PREFIX",
    "BITBUCKET_DC_SUPPORTED_VERSIONS",
    "BITBUCKET_DC_TRACKER_TYPE",
    "BUILD_STATUS_OUTCOMES",
    "BUILD_STATUS_STATES",
    "BitbucketDCConfigError",
    "BitbucketDCIdentityError",
    "BitbucketDCPaginationError",
    "CURRENT_USER_HEADER",
    "ENV_CA_BUNDLE",
    "ENV_ENABLED",
    "ENV_INSTANCES",
    "ENV_PRIVATE_NETWORKS",
    "InstanceIdentity",
    "PARTICIPANT_APPROVED",
    "PARTICIPANT_NEEDS_WORK",
    "PARTICIPANT_UNAPPROVED",
    "PAT_MESSAGE",
    "approved_instance_for",
    "approved_instances",
    "bitbucket_dc_enabled",
    "build_comment_anchor",
    "build_object_attributes",
    "ca_bundle_path",
    "canonical_instance_url",
    "check_destination_address",
    "clone_links",
    "default_resolver",
    "epoch_millis_to_iso",
    "full_ref",
    "looks_like_cloud_payload",
    "next_page_start",
    "normalize_comment",
    "parse_instance_url",
    "path_text",
    "private_networks",
    "pull_request_web_url",
    "ref_branch",
    "reject_cloud_payload",
    "repository_identity",
    "repository_web_url",
    "resolve_pinned_address",
    "self_link",
    "user_name",
    "validate_bitbucket_dc_config",
    "validate_project_key",
    "validate_repository_id",
    "validate_repository_slug",
    "validate_user_slug",
]
