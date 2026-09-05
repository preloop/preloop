"""Flow preset configurations loaded from layered YAML preset directories.

``PRELOOP_PRESETS_PATH`` accepts an ``os.pathsep``-separated list of
directories (e.g. ``/app/backend/presets:/app/presets`` on Linux). Directories
are loaded in order and merged with union semantics:

* Each preset has a stable identity: the explicit ``slug`` key in its YAML,
  falling back to the filename stem minus the numeric order prefix
  (``004-documentation-generator.yaml`` -> ``documentation-generator``).
* On slug collision, the preset from the LATER directory fully replaces the
  earlier one (by-identity override); presets without a collision all appear.
* A preset with ``disabled: true`` in a later directory is a tombstone: it
  suppresses the same-slug preset from earlier directories and is itself not
  included in the catalog.
* The catalog is stably sorted by (numeric filename prefix, slug); on
  override, the overriding file's numeric prefix determines the position.

The ``slug`` is loader-internal identity: it is stripped from the returned
catalog dicts and never reaches downstream consumers.

A single-directory value (the default) matches the historical loader except
that presets are now de-duplicated by slug even within one directory: two
files in the same directory resolving to the same slug (e.g. ``001-triage.yaml``
and ``002-triage.yaml``) collide, the later file wins, and a warning is logged.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PRESETS_DIR = BASE_DIR / "presets"

# Allow overriding preset directories via environment variable. This is used
# by EE to layer enterprise presets on top of the open-source ones (see module
# docstring for merge semantics).
PRESETS_DIRS: List[Path] = [
    Path(part)
    for part in os.environ.get("PRELOOP_PRESETS_PATH", str(DEFAULT_PRESETS_DIR)).split(
        os.pathsep
    )
    if part
]


def _extract_order(filename: str) -> int:
    """Extract numeric order prefix from a preset filename."""

    prefix = filename.split("-", 1)[0]
    try:
        return int(prefix)
    except ValueError:
        return 9999


def _derive_slug(stem: str) -> str:
    """Derive a fallback slug from a filename stem, minus the numeric prefix."""

    prefix, sep, rest = stem.partition("-")
    if sep and prefix.isdigit() and rest:
        return rest
    return stem


def _load_yaml_file(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # pragma: no cover - YAML errors rare
        raise ValueError(f"Failed to parse preset file {path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Preset file {path} must define a mapping")

    slug = data.get("slug")
    if slug is not None and (not isinstance(slug, str) or not slug.strip()):
        raise ValueError(f"Preset file {path} has an invalid 'slug' value")

    # Ensure presets default to being marked as presets unless explicitly overridden
    data.setdefault("is_preset", True)
    return data


_PRESET_SLUGS: Dict[str, str] = {}


@lru_cache()
def load_flow_presets() -> List[Dict[str, Any]]:
    """Load flow preset configurations from the layered preset directories.

    Directories in ``PRESETS_DIRS`` are merged in order: presets are keyed by
    slug (explicit ``slug`` key, or filename-derived fallback); a same-slug
    preset in a later directory overrides the earlier one, and ``disabled:
    true`` tombstones suppress the preset entirely. A same-slug collision
    within a single directory is almost certainly a mistake: the later file
    wins and a warning is logged. The result is stably sorted by (numeric
    filename prefix, slug); the loader-internal ``slug`` and ``disabled``
    keys are stripped from the returned dicts. Missing directories are
    skipped; an empty catalog is the open-source default.
    """

    # slug -> (numeric order, slug, source path, config)
    merged: Dict[str, Tuple[int, str, Path, Dict[str, Any]]] = {}
    for directory in PRESETS_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml")):
            config = _load_yaml_file(path)
            # The slug is loader-internal identity; pop it so it never leaks
            # into the catalog dicts handed to downstream consumers.
            slug = config.pop("slug", None) or _derive_slug(path.stem)
            previous = merged.get(slug)
            if previous is not None and previous[2].parent == directory:
                # Cross-directory collisions are the override feature; a
                # collision within one directory silently drops a preset,
                # so surface it.
                logger.warning(
                    "Preset slug collision within %s: %s overrides %s (slug %r)",
                    directory,
                    path.name,
                    previous[2].name,
                    slug,
                )
            # Later directories override earlier ones on slug collision.
            merged[slug] = (_extract_order(path.stem), slug, path, config)

    catalog: List[Dict[str, Any]] = []
    slugs: Dict[str, str] = {}
    for _, slug, _, config in sorted(merged.values(), key=lambda e: (e[0], e[1])):
        if config.pop("disabled", False):
            continue  # Tombstone: suppress this slug entirely.
        name = config.get("name")
        if isinstance(name, str) and name:
            slugs[slug] = name
        catalog.append(config)
    _PRESET_SLUGS.clear()
    _PRESET_SLUGS.update(slugs)
    return catalog


FLOW_PRESETS: List[Dict[str, Any]] = load_flow_presets()
# slug -> catalog name. The YAML ``slug`` is stripped from FLOW_PRESETS
# (loader-internal); this map is how API callers resolve a slug to the
# global preset row (looked up by name).
PRESET_SLUGS: Dict[str, str] = _PRESET_SLUGS
