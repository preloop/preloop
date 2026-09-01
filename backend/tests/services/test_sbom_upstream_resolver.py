"""Tests for the SBOM upstream-repository resolver service.

The resolver maps vendored Arduino/PlatformIO components (generic purls
with no VCS metadata) to their upstream repository URL plus
version-shaped tag candidates, using the public library registries.
All registry traffic is mocked with realistic fixtures (shapes captured
from the live Arduino library index and PlatformIO registry API).

Core honesty invariants under test:
- a resolution requires a repository URL AND a registry entry whose
  version matches the SBOM version — otherwise the component is
  unresolved (never fabricated);
- unreachable registries degrade gracefully: components stay
  unresolved, the registry status is reported, nothing raises.
"""

import json
from typing import Callable

import httpx
import pytest

from preloop.services.sbom_upstream_resolver import (
    ARDUINO_INDEX_URL,
    MAX_COMPONENTS,
    PLATFORMIO_API_BASE,
    ComponentRef,
    SbomUpstreamResolver,
    resolve_components,
)

# ---------------------------------------------------------------------------
# Fixtures: realistic registry payload shapes (public registry data).
# ---------------------------------------------------------------------------

ARDUINO_INDEX = {
    "libraries": [
        {
            "name": "JPEGDEC",
            "version": "1.2.8",
            "author": "Larry Bank",
            "website": "https://github.com/bitbank2/JPEGDEC",
            "category": "Display",
            "architectures": ["*"],
            "types": ["Contributed"],
            "repository": "https://github.com/bitbank2/JPEGDEC.git",
            "url": (
                "https://downloads.arduino.cc/libraries/github.com/"
                "bitbank2/JPEGDEC-1.2.8.zip"
            ),
            "archiveFileName": "JPEGDEC-1.2.8.zip",
        },
        {
            "name": "JPEGDEC",
            "version": "1.8.4",
            "author": "Larry Bank",
            "website": "https://github.com/bitbank2/JPEGDEC",
            "repository": "https://github.com/bitbank2/JPEGDEC.git",
        },
        {
            "name": "Adafruit GFX Library",
            "version": "1.11.9",
            "author": "Adafruit",
            "website": "https://github.com/adafruit/Adafruit-GFX-Library",
            "repository": "https://github.com/adafruit/Adafruit-GFX-Library.git",
        },
        {
            # Entry with no repository URL: must never resolve.
            "name": "OrphanLib",
            "version": "2.0.0",
            "repository": "",
        },
    ]
}

PIO_SEARCH_PUBSUBCLIENT = {
    "page": 1,
    "limit": 10,
    "total": 1,
    "items": [
        {
            "id": 89,
            "type": "library",
            "tier": "community",
            "owner": {"username": "knolleary"},
            "name": "PubSubClient",
            "description": "A client library for MQTT messaging.",
        }
    ],
}

PIO_DETAIL_PUBSUBCLIENT = {
    "id": 89,
    "type": "library",
    "owner": {"username": "knolleary"},
    "name": "PubSubClient",
    "repository_url": "https://github.com/knolleary/pubsubclient.git",
    "version": {"name": "2.8.0", "released_at": "2020-05-20T00:00:00Z"},
    "versions": [
        {"name": "2.8.0", "released_at": "2020-05-20T00:00:00Z"},
        {"name": "2.7.0", "released_at": "2018-12-01T00:00:00Z"},
    ],
}


def _routes(
    *,
    arduino_status: int = 200,
    arduino_body: dict | None = None,
    pio_status: int = 200,
    counters: dict | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler for both registries.

    Args:
        arduino_status: HTTP status for the Arduino index download.
        arduino_body: Arduino index payload (defaults to ARDUINO_INDEX).
        pio_status: HTTP status for all PlatformIO endpoints.
        counters: Optional dict collecting per-URL request counts.

    Returns:
        A handler suitable for ``httpx.MockTransport``.
    """
    index = arduino_body if arduino_body is not None else ARDUINO_INDEX

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if counters is not None:
            counters[url] = counters.get(url, 0) + 1
        if url.startswith(ARDUINO_INDEX_URL):
            if arduino_status != 200:
                return httpx.Response(arduino_status, text="registry down")
            return httpx.Response(200, json=index)
        if url.startswith(f"{PLATFORMIO_API_BASE}/v3/search"):
            if pio_status != 200:
                return httpx.Response(pio_status, text="registry down")
            query = request.url.params.get("query", "")
            if "pubsubclient" in query.lower():
                return httpx.Response(200, json=PIO_SEARCH_PUBSUBCLIENT)
            return httpx.Response(
                200, json={"page": 1, "limit": 10, "total": 0, "items": []}
            )
        if url.startswith(
            f"{PLATFORMIO_API_BASE}/v3/packages/knolleary/library/PubSubClient"
        ):
            if pio_status != 200:
                return httpx.Response(pio_status, text="registry down")
            return httpx.Response(200, json=PIO_DETAIL_PUBSUBCLIENT)
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def make_resolver(
    handler: Callable[[httpx.Request], httpx.Response],
) -> SbomUpstreamResolver:
    """Create a resolver wired to a mock transport with no retry delay."""
    return SbomUpstreamResolver(
        transport=httpx.MockTransport(handler), retry_delay_seconds=0.0
    )


# ---------------------------------------------------------------------------
# Arduino library index resolution
# ---------------------------------------------------------------------------


class TestArduinoResolution:
    @pytest.mark.asyncio
    async def test_jpegdec_resolves_with_repo_and_ref_candidates(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [
                ComponentRef(
                    name="JPEGDEC",
                    version="1.2.8",
                    purl="pkg:generic/JPEGDEC@1.2.8",
                )
            ]
        )
        assert len(report.resolved) == 1
        assert not report.unresolved
        res = report.resolved[0]
        assert res.source == "arduino"
        assert res.repository_url == "https://github.com/bitbank2/JPEGDEC.git"
        assert res.registry_version == "1.2.8"
        assert "1.2.8" in res.ref_candidates
        assert "v1.2.8" in res.ref_candidates
        assert report.registry_status["arduino_index"] == "ok"

    @pytest.mark.asyncio
    async def test_enriched_purl_carries_vcs_url_qualifier(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [
                ComponentRef(
                    name="JPEGDEC",
                    version="1.2.8",
                    purl="pkg:generic/JPEGDEC@1.2.8",
                )
            ]
        )
        enriched = report.resolved[0].enriched_purl
        assert enriched is not None
        assert enriched.startswith("pkg:generic/JPEGDEC@1.2.8?vcs_url=")
        # The qualifier is percent-encoded and carries the git+https URL.
        assert "git%2Bhttps" in enriched

    @pytest.mark.asyncio
    async def test_v_prefixed_sbom_version_matches_registry_version(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [ComponentRef(name="JPEGDEC", version="v1.2.8")]
        )
        assert len(report.resolved) == 1
        res = report.resolved[0]
        assert res.registry_version == "1.2.8"
        assert "v1.2.8" in res.ref_candidates

    @pytest.mark.asyncio
    async def test_separator_insensitive_name_match(self):
        """purl names use dashes/underscores; the index uses spaces."""
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [ComponentRef(name="adafruit-gfx-library", version="1.11.9")]
        )
        assert len(report.resolved) == 1
        assert report.resolved[0].repository_url == (
            "https://github.com/adafruit/Adafruit-GFX-Library.git"
        )

    @pytest.mark.asyncio
    async def test_version_not_in_any_registry_is_unresolved(self):
        """Never fabricate: a repo-known name with an unknown version
        stays unresolved."""
        resolver = make_resolver(_routes())
        report = await resolver.resolve([ComponentRef(name="JPEGDEC", version="9.9.9")])
        assert not report.resolved
        assert len(report.unresolved) == 1
        assert "9.9.9" in report.unresolved[0].reason

    @pytest.mark.asyncio
    async def test_entry_without_repository_url_is_unresolved(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [ComponentRef(name="OrphanLib", version="2.0.0")]
        )
        assert not report.resolved
        assert len(report.unresolved) == 1

    @pytest.mark.asyncio
    async def test_index_downloaded_once_across_resolve_calls(self):
        counters: dict = {}
        resolver = make_resolver(_routes(counters=counters))
        await resolver.resolve([ComponentRef(name="JPEGDEC", version="1.2.8")])
        await resolver.resolve([ComponentRef(name="JPEGDEC", version="1.8.4")])
        assert counters[ARDUINO_INDEX_URL] == 1


# ---------------------------------------------------------------------------
# PlatformIO registry fallback
# ---------------------------------------------------------------------------


class TestPlatformIOResolution:
    @pytest.mark.asyncio
    async def test_resolves_via_platformio_when_not_in_arduino_index(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [ComponentRef(name="PubSubClient", version="2.8.0")]
        )
        assert len(report.resolved) == 1
        res = report.resolved[0]
        assert res.source == "platformio"
        assert res.repository_url == ("https://github.com/knolleary/pubsubclient.git")
        assert "2.8.0" in res.ref_candidates
        assert "v2.8.0" in res.ref_candidates
        assert report.registry_status["platformio"] == "ok"

    @pytest.mark.asyncio
    async def test_platformio_version_mismatch_is_unresolved(self):
        """The PIO detail versions list is authoritative and may be
        truncated upstream: a version it does not carry never resolves."""
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [ComponentRef(name="PubSubClient", version="1.0.0")]
        )
        assert not report.resolved
        assert len(report.unresolved) == 1

    @pytest.mark.asyncio
    async def test_unknown_component_is_unresolved(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [ComponentRef(name="totally-private-lib", version="0.0.1")]
        )
        assert not report.resolved
        assert len(report.unresolved) == 1


# ---------------------------------------------------------------------------
# Degradation: unreachable registries never crash, never fabricate
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_both_registries_down_all_unresolved(self):
        resolver = make_resolver(_routes(arduino_status=503, pio_status=503))
        report = await resolver.resolve(
            [
                ComponentRef(name="JPEGDEC", version="1.2.8"),
                ComponentRef(name="PubSubClient", version="2.8.0"),
            ]
        )
        assert not report.resolved
        assert len(report.unresolved) == 2
        assert report.registry_status["arduino_index"].startswith("unreachable")
        assert report.registry_status["platformio"].startswith("unreachable")

    @pytest.mark.asyncio
    async def test_network_errors_do_not_raise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        resolver = make_resolver(handler)
        report = await resolver.resolve([ComponentRef(name="JPEGDEC", version="1.2.8")])
        assert not report.resolved
        assert len(report.unresolved) == 1

    @pytest.mark.asyncio
    async def test_platformio_lookups_stop_after_repeated_failures(self):
        """Bounded degradation: after the failure threshold, remaining
        components are marked unresolved without further requests."""
        counters: dict = {}
        resolver = make_resolver(
            _routes(arduino_status=503, pio_status=503, counters=counters)
        )
        components = [ComponentRef(name=f"lib-{i}", version="1.0.0") for i in range(10)]
        report = await resolver.resolve(components)
        assert len(report.unresolved) == 10
        search_requests = sum(
            count
            for url, count in counters.items()
            if url.startswith(f"{PLATFORMIO_API_BASE}/v3/search")
        )
        # Each search retries a bounded number of times; the circuit
        # breaker must stop far before one search per component.
        assert search_requests < 10


# ---------------------------------------------------------------------------
# Report shape and the resolve_components() tool entry point
# ---------------------------------------------------------------------------


class TestReportContract:
    @pytest.mark.asyncio
    async def test_report_dict_shape_and_stats(self):
        resolver = make_resolver(_routes())
        report = await resolver.resolve(
            [
                ComponentRef(name="JPEGDEC", version="1.2.8"),
                ComponentRef(name="PubSubClient", version="2.8.0"),
                ComponentRef(name="mystery", version="0.1.0"),
            ]
        )
        data = report.to_dict()
        assert data["stats"] == {
            "requested": 3,
            "resolved": 2,
            "unresolved": 1,
            "by_source": {"arduino": 1, "platformio": 1},
        }
        assert {r["name"] for r in data["resolved"]} == {
            "JPEGDEC",
            "PubSubClient",
        }
        assert data["unresolved"][0]["name"] == "mystery"
        assert data["unresolved"][0]["reason"]
        assert set(data["registry_status"]) == {"arduino_index", "platformio"}
        # The whole report must be JSON-serializable (MCP tool output).
        json.dumps(data)

    @pytest.mark.asyncio
    async def test_resolve_components_validates_input(self):
        with pytest.raises(ValueError):
            await resolve_components([{"name": "JPEGDEC"}])  # missing version
        with pytest.raises(ValueError):
            await resolve_components([{"version": "1.0.0"}])  # missing name
        with pytest.raises(ValueError):
            await resolve_components(
                [{"name": "x", "version": "1"}] * (MAX_COMPONENTS + 1)
            )

    @pytest.mark.asyncio
    async def test_resolve_components_uses_injected_resolver(self):
        resolver = make_resolver(_routes())
        data = await resolve_components(
            [{"name": "JPEGDEC", "version": "1.2.8"}], resolver=resolver
        )
        assert data["stats"]["resolved"] == 1
        assert data["resolved"][0]["source"] == "arduino"
