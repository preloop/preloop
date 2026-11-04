"""ARCHITECTURE.md stays a short map; chapters live under docs/architecture/.

Agents and contributors load one chapter for the subsystem they change, so the
root file must not regrow into a monolith, and every chapter must be reachable
from both the map and the docs.preloop.ai navigation. Run this file directly
for the documentation-only CI check, without pytest or backend dependencies.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
CHAPTER_DIR = REPO_ROOT / "docs" / "architecture"
MAX_MAP_LINES = 150


def _chapters() -> list[Path]:
    return sorted(CHAPTER_DIR.glob("*.md"))


def test_architecture_map_stays_short() -> None:
    lines = ARCHITECTURE.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_MAP_LINES, (
        f"ARCHITECTURE.md has {len(lines)} lines; move subsystem detail into a "
        "chapter under docs/architecture/ and link it from the map"
    )


def test_architecture_map_has_only_index_headings() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    headings = re.findall(r"^#{2,} (.+)$", text, flags=re.MULTILINE)
    assert headings == ["High-Level Architecture", "Chapters"]


def test_architecture_map_links_every_chapter() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    missing = [
        chapter.name
        for chapter in _chapters()
        if f"(docs/architecture/{chapter.name})" not in text
    ]
    assert missing == []


def test_docs_nav_includes_every_chapter() -> None:
    config = yaml.load(
        (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    nav = str(config["nav"])
    missing = [
        chapter.name
        for chapter in _chapters()
        if f"architecture/{chapter.name}" not in nav
    ]
    assert missing == []


if __name__ == "__main__":
    test_architecture_map_stays_short()
    test_architecture_map_has_only_index_headings()
    test_architecture_map_links_every_chapter()
    test_docs_nav_includes_every_chapter()
