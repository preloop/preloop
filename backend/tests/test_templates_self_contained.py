"""The backend's public HTML pages must load nothing from the network.

The approval page (``/approval/{id}?token=...``), the invitation page
(``/invitations/accept``) and the OAuth consent page are opened from email,
Slack and mobile links, and are served by the backend directly. On air-gapped
or egress-restricted installs a CDN is unreachable, so any external script,
stylesheet, font or image leaves these pages unstyled or broken. These tests
parse each template and fail on any reference that would leave the host.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import preloop

TEMPLATES_DIR = Path(preloop.__file__).parent / "templates"

PUBLIC_TEMPLATES = (
    "approval.html",
    "invitation-accept.html",
    "oauth_authorize.html",
)

# Absolute (http://, https://, any scheme://) or protocol-relative (//host).
_EXTERNAL = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)

# Attributes that make the browser fetch something.
_FETCHING_ATTRS = {
    "script": ("src",),
    "link": ("href",),
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "image": ("href", "xlink:href"),
    "use": ("href", "xlink:href"),
    "input": ("src",),
}

_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]?([^'\");\s]+)", re.IGNORECASE)


class _AssetCollector(HTMLParser):
    """Collect fetching attributes, inline CSS and custom element tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str, str]] = []
        self.css_chunks: list[str] = []
        self.tags: set[str] = set()
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        for name, value in attrs:
            if value is None:
                continue
            if name in _FETCHING_ATTRS.get(tag, ()):
                for candidate in value.split(","):
                    url = candidate.strip().split(" ")[0]
                    self.references.append((tag, name, url))
            if name == "style":
                self.css_chunks.append(value)
        if tag == "style":
            self._in_style = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "style":
            self._in_style = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.css_chunks.append(data)


def _collect(name: str) -> tuple[str, _AssetCollector]:
    source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    collector = _AssetCollector()
    collector.feed(source)
    collector.close()
    return source, collector


@pytest.mark.parametrize("name", PUBLIC_TEMPLATES)
def test_no_external_tag_references(name: str) -> None:
    """No <script src>, <link href>, <img src> etc. points off the host."""
    _source, collector = _collect(name)
    external = [ref for ref in collector.references if _EXTERNAL.match(ref[2])]
    assert not external, f"{name} loads external assets: {external}"


@pytest.mark.parametrize("name", PUBLIC_TEMPLATES)
def test_no_external_css_references(name: str) -> None:
    """No CSS url() or @import points off the host."""
    _source, collector = _collect(name)
    css = "\n".join(collector.css_chunks)
    urls = [match.group(2) for match in _CSS_URL.finditer(css)]
    imports = _CSS_IMPORT.findall(css)
    external = [url for url in urls + imports if _EXTERNAL.match(url)]
    assert not external, f"{name} references external CSS assets: {external}"
    assert not imports, f"{name} uses @import: {imports}"


@pytest.mark.parametrize("name", PUBLIC_TEMPLATES)
def test_no_absolute_urls_anywhere(name: str) -> None:
    """Belt and braces: no http(s) URL appears anywhere in the file.

    Scripts can fetch or inject assets too (``import()``, ``fetch``, a
    dynamically created ``<script>``); the pages talk only to their own
    origin, so any absolute URL is a regression.
    """
    source, _collector = _collect(name)
    found = re.findall(r"https?://[^\s'\"<>)]+", source, flags=re.IGNORECASE)
    assert not found, f"{name} contains absolute URLs: {found}"


@pytest.mark.parametrize("name", PUBLIC_TEMPLATES)
def test_no_shoelace_components(name: str) -> None:
    """No Shoelace custom elements remain, in markup or built in script."""
    source, collector = _collect(name)
    sl_tags = sorted(tag for tag in collector.tags if tag.startswith("sl-"))
    assert not sl_tags, f"{name} still uses Shoelace elements: {sl_tags}"
    assert "<sl-" not in source
    assert not re.search(r"createElement\(\s*['\"]sl-", source), (
        f"{name} still creates Shoelace elements in script"
    )
    # Shoelace-only events never fire on native controls.
    assert not re.search(r"['\"]sl-(input|change)['\"]", source)


@pytest.mark.parametrize("name", ("approval.html", "invitation-accept.html"))
def test_inputs_have_labels(name: str) -> None:
    """Every static form control in the markup has a <label for>."""
    source, _collector = _collect(name)
    ids = re.findall(
        r"<(?:input|textarea|select)\b[^>]*\bid=\"([^\"]+)\"", source, flags=re.S
    )
    assert ids, f"{name} has no static controls to check"
    for control_id in ids:
        assert f'for="{control_id}"' in source, (
            f"{name}: control #{control_id} has no <label for>"
        )


def test_collector_flags_external_assets() -> None:
    """The checks above catch the patterns they claim to catch."""
    collector = _AssetCollector()
    collector.feed(
        '<link rel="stylesheet" href="https://cdn.example.com/a.css">'
        '<script src="//cdn.example.com/a.js"></script>'
        '<img src="http://cdn.example.com/a.png">'
        "<style>@import url('https://cdn.example.com/b.css');"
        ".x{background:url(//cdn.example.com/c.png)}</style>"
        "<sl-button></sl-button>"
    )
    external = [ref for ref in collector.references if _EXTERNAL.match(ref[2])]
    assert len(external) == 3
    css = "\n".join(collector.css_chunks)
    assert any(_EXTERNAL.match(m.group(2)) for m in _CSS_URL.finditer(css))
    assert _CSS_IMPORT.findall(css)
    assert "sl-button" in collector.tags
    assert not _EXTERNAL.match("/vendor/a.js")
    assert not _EXTERNAL.match("#local")
