"""Parity between the gitleaks baseline and the secrets-history register.

``.gitleaksignore`` records what each baselined history finding is;
``docs/security/secrets-history.md`` records what happened to it (rotated,
never live, or not recorded). The page promises the two files reconcile:
each of its sections names the ignore-file section it pairs with and that
section's fingerprint count, and every fingerprint's commit and path appear
as a row in the paired section. These tests hold both files to that promise
so a finding cannot be baselined without a disposition, and the counts
quoted in either file cannot go stale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
IGNORE_FILE = REPO_ROOT / ".gitleaksignore"
HISTORY_DOC = REPO_ROOT / "docs" / "security" / "secrets-history.md"

_IGNORE_SECTION = re.compile(r"^# --- (?P<name>.+?) -+\s*$")
_FINGERPRINT = re.compile(r"^(?P<sha>[0-9a-f]{40}):(?P<path>[^:]+):[^:]+:\d+$")
_DOC_SECTION = re.compile(r"^## (?P<title>.+)$")
_DOC_PAIRING = re.compile(
    r"^Pairs with `\.gitleaksignore` section: `(?P<name>[^`]+)` "
    r"\((?P<count>\d+) fingerprints?\)\.$"
)
_DOC_ROW = re.compile(r"^\| `(?P<sha>[0-9a-f]{40})` \| `(?P<path>[^`]+)` \|")
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _ignore_sections() -> Dict[str, List[Tuple[str, str]]]:
    """Map each ``.gitleaksignore`` section name to its (sha, path) entries."""
    sections: Dict[str, List[Tuple[str, str]]] = {}
    current = None
    for line in IGNORE_FILE.read_text().splitlines():
        header = _IGNORE_SECTION.match(line)
        if header:
            current = header.group("name")
            sections[current] = []
            continue
        entry = _FINGERPRINT.match(line.strip())
        if entry:
            assert current is not None, f"fingerprint before any section: {line}"
            sections[current].append((entry.group("sha"), entry.group("path")))
    return sections


def _doc_sections() -> List[Dict]:
    """Parse the register into sections with their pairing line and rows."""
    sections: List[Dict] = []
    for line in HISTORY_DOC.read_text().splitlines():
        header = _DOC_SECTION.match(line)
        if header:
            sections.append(
                {"title": header.group("title"), "pairing": None, "rows": set()}
            )
            continue
        if not sections:
            continue
        pairing = _DOC_PAIRING.match(line)
        if pairing:
            sections[-1]["pairing"] = (
                pairing.group("name"),
                int(pairing.group("count")),
            )
        row = _DOC_ROW.match(line)
        if row:
            sections[-1]["rows"].add((row.group("sha"), row.group("path")))
    return sections


def test_every_ignore_section_is_paired_with_the_right_count() -> None:
    ignore = _ignore_sections()
    paired = {
        section["pairing"][0]: section["pairing"][1]
        for section in _doc_sections()
        if section["pairing"]
    }
    assert ignore, ".gitleaksignore has no sections"
    assert set(paired) == set(ignore), (
        "every .gitleaksignore section needs exactly one paired section in "
        "secrets-history.md"
    )
    for name, entries in ignore.items():
        assert paired[name] == len(entries), (
            f"secrets-history.md says section {name!r} has {paired[name]} "
            f"fingerprints; .gitleaksignore has {len(entries)}"
        )


def test_every_fingerprint_has_a_row_in_its_paired_section() -> None:
    ignore = _ignore_sections()
    for section in _doc_sections():
        if not section["pairing"]:
            continue
        name = section["pairing"][0]
        missing = set(ignore.get(name, [])) - section["rows"]
        extra = section["rows"] - set(ignore.get(name, []))
        assert not missing, f"{section['title']!r} lacks rows for {sorted(missing)}"
        assert not extra, (
            f"{section['title']!r} lists rows with no fingerprint in "
            f"{name!r}: {sorted(extra)}"
        )


def test_ignore_header_count_matches_the_assume_compromised_section() -> None:
    raw_header = IGNORE_FILE.read_text().split("# --- ", 1)[0]
    header = " ".join(
        line.lstrip("#").strip() for line in raw_header.splitlines()
    ).lower()
    claim = re.search(
        r"the (\w+) fingerprints\s+under\s+\"assume compromised\"", header
    )
    assert claim, ".gitleaksignore header no longer states the fingerprint count"
    compromised = [
        entries
        for name, entries in _ignore_sections().items()
        if name.startswith("assume compromised")
    ]
    assert len(compromised) == 1
    assert _NUMBER_WORDS[claim.group(1)] == len(compromised[0])
