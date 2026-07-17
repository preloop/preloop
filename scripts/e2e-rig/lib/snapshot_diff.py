"""Snapshot comparison for the offboarding assertion.

Compares two snapshot directories (as written by rig_snapshot) per agent:
byte-identical first, then semantic equality (parsed config compare with a
small per-file volatility allowlist). The verdict is per agent, over every
config file that agent owns.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

# path prefix (relative to $HOME) -> agent display name; first match wins,
# so more specific prefixes come first.
AGENT_PATH_MAP: list[tuple[str, str]] = [
    (".claude/claude_desktop_config.json", "Claude Desktop"),
    (".config/claude/claude_desktop_config.json", "Claude Desktop"),
    (".claude.json", "Claude Code"),
    (".claude/", "Claude Code"),
    (".cursor/", "Cursor"),
    (".codeium/", "Windsurf"),
    (".vscode/", "VSCode / Copilot"),
    (".gemini/settings.json", "Gemini CLI"),
    (".gemini/config/", "Antigravity"),
    (".gemini/antigravity/", "Antigravity"),
    (".config/opencode/", "OpenCode"),
    (".codex/", "Codex CLI"),
    (".openclaw/", "OpenClaw"),
    (".openclaw-dev/", "OpenClaw"),
    (".hermes/", "Hermes"),
    (".config/devin/", "Devin"),
    (".devin/", "Devin"),
]

# Keys that legitimately change without meaning a broken restore.
# ~/.claude.json is Claude Code's kitchen-sink state file: only the managed
# surface (MCP servers + model) is asserted; everything else churns on its
# own (caches, project history, tips state).
CLAUDE_JSON_MANAGED_KEYS = ("mcpServers", "model")
VOLATILE_KEYS: dict[str, set[str]] = {
    ".claude/settings.json": {"feedbackSurveyState"},
}


def agent_for(path: str) -> str:
    for prefix, name in AGENT_PATH_MAP:
        if path == prefix or path.startswith(prefix):
            return name
    return "unknown"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_config(rel: str, path: Path) -> Any:
    text = path.read_text()
    if rel.endswith(".toml"):
        import tomllib

        return tomllib.loads(text)
    if rel.endswith((".yaml", ".yml")):
        try:
            import yaml

            return yaml.safe_load(text)
        except ImportError:
            return None
    if rel.endswith((".json", ".json5")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def semantic_view(rel: str, doc: Any) -> Any:
    if doc is None:
        return None
    if rel == ".claude.json" and isinstance(doc, dict):
        return {k: doc.get(k) for k in CLAUDE_JSON_MANAGED_KEYS}
    if isinstance(doc, dict):
        drop = VOLATILE_KEYS.get(rel, set())
        return {k: v for k, v in doc.items() if k not in drop}
    return doc


def compare_file(rel: str, a: Path | None, b: Path | None) -> dict[str, Any]:
    """Compare one config file across two snapshots."""
    if a is None and b is None:
        return {"path": rel, "verdict": "absent-both"}
    if a is None or b is None:
        # A file that only exists on one side. For ~/.claude.json (state file
        # that Claude Code creates on any run) require the managed keys to be
        # effectively empty on the side that has it.
        present = a or b
        side = "baseline" if b is None else "final"
        if rel == ".claude.json":
            doc = parse_config(rel, present)
            view = semantic_view(rel, doc)
            if isinstance(view, dict) and not any(view.values()):
                return {
                    "path": rel,
                    "verdict": "semantic-equal",
                    "detail": f"only in {side}, managed keys empty",
                }
        return {"path": rel, "verdict": "FAIL", "detail": f"only in {side}"}

    if sha256(a) == sha256(b):
        return {"path": rel, "verdict": "byte-identical"}

    doc_a = parse_config(rel, a)
    doc_b = parse_config(rel, b)
    if doc_a is not None and doc_b is not None:
        if semantic_view(rel, doc_a) == semantic_view(rel, doc_b):
            return {"path": rel, "verdict": "semantic-equal"}

    diff = "\n".join(
        list(
            difflib.unified_diff(
                a.read_text().splitlines(),
                b.read_text().splitlines(),
                fromfile=f"baseline/{rel}",
                tofile=f"final/{rel}",
                lineterm="",
            )
        )[:400]
    )
    return {"path": rel, "verdict": "FAIL", "diff": diff}


def compare_snapshots(baseline: Path, final: Path, diffs_dir: Path) -> dict:
    """Returns {agents: {name: {verdict, files: [...]}}, verdict}."""
    files_a = {
        str(p.relative_to(baseline / "home")): p
        for p in (baseline / "home").rglob("*")
        if p.is_file()
    }
    files_b = {
        str(p.relative_to(final / "home")): p
        for p in (final / "home").rglob("*")
        if p.is_file()
    }

    agents: dict[str, dict] = {}
    for rel in sorted(set(files_a) | set(files_b)):
        result = compare_file(rel, files_a.get(rel), files_b.get(rel))
        entry = agents.setdefault(agent_for(rel), {"verdict": "PASS", "files": []})
        entry["files"].append(result)
        if result["verdict"] == "FAIL":
            entry["verdict"] = "FAIL"
            diffs_dir.mkdir(parents=True, exist_ok=True)
            safe = rel.replace("/", "__")
            (diffs_dir / f"{safe}.diff").write_text(
                result.get("diff", result.get("detail", ""))
            )

    overall = "PASS" if all(a["verdict"] == "PASS" for a in agents.values()) else "FAIL"
    return {"verdict": overall, "agents": agents}
