"""Registry-based upstream-repository resolution for vendored components.

Real embedded-firmware SBOMs are full of vendored Arduino/PlatformIO
libraries carried as generic purls with no VCS metadata. The osv_git
screening source (presets 005/006) can only evaluate a component when
its purl carries a ``vcs_url`` qualifier or a resolvable tag, so those
components stay blind to the one source that catches CVEs in vendored C
code.

This service resolves such components to their upstream repository URL
plus version-shaped tag candidates via the public library registries:

- the Arduino library index (one JSON document, one entry per library
  version, carrying a ``repository`` URL) — primary source, cached
  in-memory with a TTL;
- the PlatformIO registry API (name search + package detail carrying
  ``repository_url`` and a versions list) — fallback. The detail
  endpoint may truncate its versions list to recent releases, so a
  version it does not carry stays unresolved rather than being guessed.

Honesty contract: a resolution requires a repository URL AND a registry
entry whose version matches the SBOM version. Anything else is reported
as unresolved with a reason — unresolved is a first-class outcome, and
a resolution is never fabricated. Unreachable registries degrade
gracefully: affected components stay unresolved, the registry status is
reported, and nothing raises.

The service performs no DB access; caching is in-memory only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

ARDUINO_INDEX_URL = "https://downloads.arduino.cc/libraries/library_index.json"
PLATFORMIO_API_BASE = "https://api.registry.platformio.org"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 0.5
ARDUINO_INDEX_TTL_SECONDS = 24 * 60 * 60
# Consecutive hard PlatformIO failures after which remaining lookups are
# skipped for the run (bounded degradation instead of N timeouts).
PLATFORMIO_FAILURE_THRESHOLD = 3
# Candidate package pages inspected per PlatformIO name search.
PLATFORMIO_MAX_CANDIDATES = 3
MAX_COMPONENTS = 500

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ComponentRef:
    """One SBOM component to resolve.

    Attributes:
        name: Component name as carried by the SBOM/purl.
        version: Component version string from the SBOM.
        purl: Optional original purl; when present, the resolution
            carries an enriched purl with a ``vcs_url`` qualifier.
    """

    name: str
    version: str
    purl: Optional[str] = None


@dataclass(frozen=True)
class UpstreamResolution:
    """A conservative, registry-confirmed upstream resolution.

    Attributes:
        name: Component name as requested.
        version: Component version as requested.
        source: Registry that confirmed the resolution (``arduino`` or
            ``platformio``).
        repository_url: Upstream repository URL from the registry.
        registry_version: The registry's own version string that matched
            the SBOM version.
        ref_candidates: Version-shaped tag names to resolve with
            ``git ls-remote`` (the resolver never guesses a commit).
        enriched_purl: Original purl with a ``vcs_url`` qualifier
            appended, or ``None`` when no purl was supplied.
    """

    name: str
    version: str
    source: str
    repository_url: str
    registry_version: str
    ref_candidates: Tuple[str, ...]
    enriched_purl: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe representation of this resolution."""
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "repository_url": self.repository_url,
            "registry_version": self.registry_version,
            "ref_candidates": list(self.ref_candidates),
            "enriched_purl": self.enriched_purl,
        }


@dataclass(frozen=True)
class UnresolvedComponent:
    """A component no registry could conservatively resolve.

    Attributes:
        name: Component name as requested.
        version: Component version as requested.
        reason: Why the component stayed unresolved.
    """

    name: str
    version: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe representation of this outcome."""
        return {"name": self.name, "version": self.version, "reason": self.reason}


@dataclass(frozen=True)
class UpstreamResolutionReport:
    """Outcome of one resolution run.

    Attributes:
        resolved: Registry-confirmed resolutions, in request order.
        unresolved: Components that stayed unresolved, in request order.
        registry_status: Per-registry health for the run (``ok``,
            ``not queried``, or ``unreachable: <detail>``).
    """

    resolved: Tuple[UpstreamResolution, ...]
    unresolved: Tuple[UnresolvedComponent, ...]
    registry_status: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe report shape used as MCP tool output."""
        by_source: Dict[str, int] = {}
        for res in self.resolved:
            by_source[res.source] = by_source.get(res.source, 0) + 1
        return {
            "resolved": [r.to_dict() for r in self.resolved],
            "unresolved": [u.to_dict() for u in self.unresolved],
            "stats": {
                "requested": len(self.resolved) + len(self.unresolved),
                "resolved": len(self.resolved),
                "unresolved": len(self.unresolved),
                "by_source": by_source,
            },
            "registry_status": dict(self.registry_status),
        }


def _normalize_name(name: str) -> str:
    """Normalize a library name for conservative matching.

    Lowercases and removes space/dash/underscore separators so purl
    names such as ``adafruit-gfx-library`` match index names such as
    ``Adafruit GFX Library`` without admitting fuzzy matches.

    Args:
        name: Raw component or registry library name.

    Returns:
        The normalized matching key.
    """
    return "".join(ch for ch in name.lower() if ch not in " -_")


def _normalize_version(version: str) -> str:
    """Normalize a version string for exact comparison.

    Strips surrounding whitespace and a single leading ``v``/``V``
    prefix when followed by a digit; no other transformation is applied.

    Args:
        version: Raw version string.

    Returns:
        The normalized version string.
    """
    cleaned = version.strip()
    if len(cleaned) > 1 and cleaned[0] in "vV" and cleaned[1].isdigit():
        return cleaned[1:]
    return cleaned


def _usable_repository_url(url: Optional[str]) -> Optional[str]:
    """Validate a registry-carried repository URL.

    Args:
        url: Repository URL as published by the registry.

    Returns:
        The URL when it is a plausible public http(s) repository URL,
        otherwise ``None``.
    """
    if not url:
        return None
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    # Reject embedded credentials outright rather than trying to launder
    # them: registries publish anonymous clone URLs.
    if "@" in candidate.split("://", 1)[1].split("/", 1)[0]:
        return None
    return candidate


def _ref_candidates(registry_version: str, sbom_version: str) -> Tuple[str, ...]:
    """Build version-shaped tag candidates for ``git ls-remote``.

    Args:
        registry_version: The registry's version string that matched.
        sbom_version: The version string carried by the SBOM.

    Returns:
        Ordered, de-duplicated tag candidates.
    """
    candidates = [
        registry_version,
        f"v{registry_version}",
        sbom_version.strip(),
        f"v{_normalize_version(sbom_version)}",
    ]
    seen: List[str] = []
    for cand in candidates:
        if cand and cand not in seen:
            seen.append(cand)
    return tuple(seen)


def _enrich_purl(purl: Optional[str], repository_url: str) -> Optional[str]:
    """Append a ``vcs_url`` qualifier to a purl.

    Args:
        purl: Original component purl, if any.
        repository_url: Registry-confirmed upstream repository URL.

    Returns:
        The enriched purl, the unchanged purl when it already carries a
        ``vcs_url`` qualifier, or ``None`` when no purl was supplied.
    """
    if not purl:
        return None
    if "vcs_url=" in purl:
        return purl
    qualifier = "vcs_url=" + quote(f"git+{repository_url}", safe="")
    separator = "&" if "?" in purl else "?"
    return f"{purl}{separator}{qualifier}"


@dataclass
class _ArduinoIndexCache:
    """In-memory cache of the parsed Arduino library index."""

    fetched_at: float = 0.0
    # (normalized name, normalized version) -> (repository URL, version)
    entries: Dict[Tuple[str, str], Tuple[Optional[str], str]] = field(
        default_factory=dict
    )
    known_names: Dict[str, bool] = field(default_factory=dict)
    loaded: bool = False


class SbomUpstreamResolver:
    """Resolve vendored components to upstream repositories via registries.

    The resolver is safe to reuse across runs: the Arduino index is
    cached in-memory with a TTL, and per-run PlatformIO lookups are
    memoized by normalized name. All network failures degrade to
    unresolved outcomes; ``resolve`` never raises for registry errors.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        arduino_index_ttl_seconds: float = ARDUINO_INDEX_TTL_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            timeout_seconds: Per-request timeout.
            retries: Bounded retry count for retryable failures.
            retry_delay_seconds: Base delay between retries.
            arduino_index_ttl_seconds: How long the parsed Arduino index
                stays cached.
            transport: Optional httpx transport (tests inject a
                ``httpx.MockTransport``).
        """
        self._timeout = timeout_seconds
        self._retries = max(0, retries)
        self._retry_delay = max(0.0, retry_delay_seconds)
        self._arduino_ttl = arduino_index_ttl_seconds
        self._transport = transport
        self._arduino_cache = _ArduinoIndexCache()
        self._arduino_lock = asyncio.Lock()

    async def resolve(
        self, components: Sequence[ComponentRef]
    ) -> UpstreamResolutionReport:
        """Resolve components to upstream repository URLs and tag refs.

        Args:
            components: SBOM components to resolve.

        Returns:
            A report with registry-confirmed resolutions, unresolved
            components (with reasons), and per-registry status.
        """
        resolved: List[UpstreamResolution] = []
        unresolved: List[UnresolvedComponent] = []
        status: Dict[str, str] = {
            "arduino_index": "not queried",
            "platformio": "not queried",
        }
        pio_cache: Dict[str, Optional[Tuple[str, List[str]]]] = {}
        pio_failures = 0

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": "preloop-sbom-upstream-resolver"},
        ) as client:
            arduino_ok = await self._ensure_arduino_index(client, status)

            for comp in components:
                nname = _normalize_name(comp.name)
                nversion = _normalize_version(comp.version)

                if arduino_ok:
                    outcome = self._resolve_from_arduino(comp, nname, nversion)
                    if isinstance(outcome, UpstreamResolution):
                        resolved.append(outcome)
                        continue
                    if isinstance(outcome, UnresolvedComponent):
                        unresolved.append(outcome)
                        continue

                if pio_failures >= PLATFORMIO_FAILURE_THRESHOLD:
                    unresolved.append(
                        UnresolvedComponent(
                            name=comp.name,
                            version=comp.version,
                            reason=(
                                "PlatformIO registry unreachable "
                                "(lookups suspended for this run); "
                                "no other registry matched"
                            ),
                        )
                    )
                    continue

                pio_outcome, hard_failure = await self._resolve_from_platformio(
                    client, comp, nname, nversion, pio_cache, status
                )
                if hard_failure:
                    pio_failures += 1
                else:
                    pio_failures = 0
                if isinstance(pio_outcome, UpstreamResolution):
                    resolved.append(pio_outcome)
                else:
                    unresolved.append(pio_outcome)

        return UpstreamResolutionReport(
            resolved=tuple(resolved),
            unresolved=tuple(unresolved),
            registry_status=status,
        )

    # -- Arduino library index ------------------------------------------

    async def _ensure_arduino_index(
        self, client: httpx.AsyncClient, status: Dict[str, str]
    ) -> bool:
        """Load (or reuse) the parsed Arduino library index.

        Args:
            client: HTTP client for the run.
            status: Mutable per-registry status map to update.

        Returns:
            True when an index (fresh or cached) is available.
        """
        async with self._arduino_lock:
            cache = self._arduino_cache
            fresh = (
                cache.loaded
                and (time.monotonic() - cache.fetched_at) < self._arduino_ttl
            )
            if fresh:
                status["arduino_index"] = "ok"
                return True

            payload, error = await self._get_json(client, ARDUINO_INDEX_URL)
            if payload is None:
                if cache.loaded:
                    # Keep serving the stale index rather than going blind.
                    status["arduino_index"] = f"stale cache ({error})"
                    return True
                status["arduino_index"] = f"unreachable: {error}"
                return False

            entries: Dict[Tuple[str, str], Tuple[Optional[str], str]] = {}
            known: Dict[str, bool] = {}
            libraries = payload.get("libraries")
            if not isinstance(libraries, list):
                status["arduino_index"] = "unreachable: malformed index document"
                return cache.loaded
            for lib in libraries:
                if not isinstance(lib, dict):
                    continue
                name = lib.get("name")
                version = lib.get("version")
                if not isinstance(name, str) or not isinstance(version, str):
                    continue
                repo = _usable_repository_url(lib.get("repository"))
                key = (_normalize_name(name), _normalize_version(version))
                entries[key] = (repo, version)
                known[_normalize_name(name)] = True

            cache.entries = entries
            cache.known_names = known
            cache.fetched_at = time.monotonic()
            cache.loaded = True
            status["arduino_index"] = "ok"
            return True

    def _resolve_from_arduino(
        self, comp: ComponentRef, nname: str, nversion: str
    ) -> Optional[UpstreamResolution | UnresolvedComponent]:
        """Match one component against the cached Arduino index.

        Args:
            comp: The component to resolve.
            nname: Normalized component name.
            nversion: Normalized component version.

        Returns:
            A resolution, a terminal unresolved outcome (name+version
            matched but the entry is unusable), or ``None`` when the
            index has no match and the PlatformIO fallback should run.
        """
        entry = self._arduino_cache.entries.get((nname, nversion))
        if entry is None:
            return None
        repo, registry_version = entry
        if repo is None:
            return UnresolvedComponent(
                name=comp.name,
                version=comp.version,
                reason=(
                    "Arduino index entry matches name and version but "
                    "carries no usable repository URL"
                ),
            )
        return UpstreamResolution(
            name=comp.name,
            version=comp.version,
            source="arduino",
            repository_url=repo,
            registry_version=registry_version,
            ref_candidates=_ref_candidates(registry_version, comp.version),
            enriched_purl=_enrich_purl(comp.purl, repo),
        )

    # -- PlatformIO registry --------------------------------------------

    async def _resolve_from_platformio(
        self,
        client: httpx.AsyncClient,
        comp: ComponentRef,
        nname: str,
        nversion: str,
        cache: Dict[str, Optional[Tuple[str, List[str]]]],
        status: Dict[str, str],
    ) -> Tuple[UpstreamResolution | UnresolvedComponent, bool]:
        """Resolve one component via the PlatformIO registry.

        Args:
            client: HTTP client for the run.
            comp: The component to resolve.
            nname: Normalized component name.
            nversion: Normalized component version.
            cache: Per-run memo of normalized name -> (repository URL,
                listed version names), or ``None`` for a confirmed miss.
            status: Mutable per-registry status map to update.

        Returns:
            A pair of (outcome, hard_failure). ``hard_failure`` is True
            when the registry itself was unreachable (feeds the
            circuit breaker), False for honest misses.
        """
        arduino_known = nname in self._arduino_cache.known_names

        if nname in cache:
            hit = cache[nname]
        else:
            hit, error = await self._platformio_lookup(client, comp.name, nname)
            if error is not None:
                status["platformio"] = f"unreachable: {error}"
                return (
                    UnresolvedComponent(
                        name=comp.name,
                        version=comp.version,
                        reason=f"PlatformIO registry unreachable ({error}); "
                        "no other registry matched",
                    ),
                    True,
                )
            cache[nname] = hit
            status["platformio"] = "ok"

        if hit is None:
            if arduino_known:
                reason = (
                    f"registries know the library but no entry matches "
                    f"version {comp.version!r}"
                )
            else:
                reason = (
                    "not found in the Arduino library index or the PlatformIO registry"
                )
            return (
                UnresolvedComponent(
                    name=comp.name, version=comp.version, reason=reason
                ),
                False,
            )

        repo, version_names = hit
        registry_version = next(
            (v for v in version_names if _normalize_version(v) == nversion),
            None,
        )
        if registry_version is None:
            return (
                UnresolvedComponent(
                    name=comp.name,
                    version=comp.version,
                    reason=(
                        f"PlatformIO registry knows the library but no "
                        f"listed version matches {comp.version!r}"
                    ),
                ),
                False,
            )
        return (
            UpstreamResolution(
                name=comp.name,
                version=comp.version,
                source="platformio",
                repository_url=repo,
                registry_version=registry_version,
                ref_candidates=_ref_candidates(registry_version, comp.version),
                enriched_purl=_enrich_purl(comp.purl, repo),
            ),
            False,
        )

    async def _platformio_lookup(
        self, client: httpx.AsyncClient, raw_name: str, nname: str
    ) -> Tuple[Optional[Tuple[str, List[str]]], Optional[str]]:
        """Search PlatformIO for a library and read its repository detail.

        Args:
            client: HTTP client for the run.
            raw_name: Library name as carried by the SBOM.
            nname: Normalized name used for exact matching.

        Returns:
            A pair of (match, error). ``match`` is (repository URL,
            listed version names) for the first exactly-named library
            package whose detail page carries a usable repository URL;
            ``None`` when nothing matched. ``error`` is set only for
            registry-unreachable failures. The caller matches the SBOM
            version against the listed versions — a version the registry
            does not list never resolves.
        """
        search_url = f"{PLATFORMIO_API_BASE}/v3/search"
        payload, error = await self._get_json(
            client, search_url, params={"query": raw_name}
        )
        if payload is None:
            return None, error

        items = payload.get("items")
        if not isinstance(items, list):
            return None, None
        candidates = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "library"
            and isinstance(item.get("name"), str)
            and _normalize_name(item["name"]) == nname
            and isinstance(item.get("owner"), dict)
            and isinstance(item["owner"].get("username"), str)
        ][:PLATFORMIO_MAX_CANDIDATES]

        for item in candidates:
            owner = item["owner"]["username"]
            pkg_name = item["name"]
            detail_url = (
                f"{PLATFORMIO_API_BASE}/v3/packages/"
                f"{quote(owner, safe='')}/library/{quote(pkg_name, safe='')}"
            )
            detail, error = await self._get_json(client, detail_url)
            if detail is None:
                if error is not None:
                    return None, error
                continue
            repo = _usable_repository_url(detail.get("repository_url"))
            if repo is None:
                continue
            version_names: List[str] = []
            versions = detail.get("versions")
            if isinstance(versions, list):
                version_names.extend(
                    v["name"]
                    for v in versions
                    if isinstance(v, dict) and isinstance(v.get("name"), str)
                )
            latest = detail.get("version")
            if isinstance(latest, dict) and isinstance(latest.get("name"), str):
                version_names.append(latest["name"])
            if version_names:
                return (repo, version_names), None

        return None, None

    # -- HTTP helpers -----------------------------------------------------

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """GET a JSON document with bounded retries.

        Args:
            client: HTTP client for the run.
            url: Absolute request URL.
            params: Optional query parameters.

        Returns:
            A pair of (payload, error). ``payload`` is the parsed JSON
            object on success. ``error`` describes the terminal failure
            when the endpoint was unreachable. Non-retryable 4xx client
            errors (400/401/403/404) return ``(None, None)`` — an honest
            miss, not an outage — so they do not trip the PlatformIO
            circuit breaker. Transport failures and exhausted 5xx/429
            retries return ``(None, error)``.
        """
        last_error = "unknown error"
        for attempt in range(self._retries + 1):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        return None, "invalid JSON in registry response"
                    if isinstance(payload, dict):
                        return payload, None
                    return None, "unexpected JSON shape in registry response"
                last_error = f"HTTP {response.status_code}"
                if (
                    400 <= response.status_code < 500
                    and response.status_code not in _RETRYABLE_STATUS
                ):
                    # Client error: honest miss, not a registry outage.
                    return None, None
                if response.status_code not in _RETRYABLE_STATUS:
                    return None, last_error
            if attempt < self._retries and self._retry_delay > 0:
                await asyncio.sleep(self._retry_delay * (attempt + 1))
        logger.warning("Registry request failed for %s: %s", url, last_error)
        return None, last_error


_default_resolver: Optional[SbomUpstreamResolver] = None


def _get_default_resolver() -> SbomUpstreamResolver:
    """Return the shared resolver (keeps the Arduino index cache warm)."""
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = SbomUpstreamResolver()
    return _default_resolver


async def resolve_components(
    raw_components: Sequence[Mapping[str, Any]],
    resolver: Optional[SbomUpstreamResolver] = None,
) -> Dict[str, Any]:
    """Validate raw tool input and run one resolution pass.

    Args:
        raw_components: Sequence of mappings each carrying ``name`` and
            ``version`` (strings) and optionally ``purl``.
        resolver: Optional resolver override (tests inject one wired to
            a mock transport).

    Returns:
        The JSON-safe report dict (see
        ``UpstreamResolutionReport.to_dict``).

    Raises:
        ValueError: When the input is not a list of well-formed
            component mappings or exceeds ``MAX_COMPONENTS`` entries.
    """
    if len(raw_components) > MAX_COMPONENTS:
        raise ValueError(
            f"Too many components: {len(raw_components)} > {MAX_COMPONENTS}"
        )
    components: List[ComponentRef] = []
    for idx, raw in enumerate(raw_components):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Component #{idx} is not an object")
        name = raw.get("name")
        version = raw.get("version")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Component #{idx} is missing a 'name' string")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"Component #{idx} is missing a 'version' string")
        purl = raw.get("purl")
        if purl is not None and not isinstance(purl, str):
            raise ValueError(f"Component #{idx} has a non-string 'purl'")
        components.append(ComponentRef(name=name, version=version, purl=purl))

    active = resolver if resolver is not None else _get_default_resolver()
    report = await active.resolve(components)
    return report.to_dict()
