"""Embed the repo-audit package into an agent sandbox startup script.

Agent images do not ship the Preloop package. When a flow opts into
``repo-audit``, the agent launcher writes these sources under
``/tmp/preloop_repo_audit`` and puts that directory on PYTHONPATH so
``python3 -m preloop.security.mcp_server`` works without a runner rebuild.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

BOOTSTRAP_ROOT = "/tmp/preloop_repo_audit"
PACKAGE_RELATIVE = (
    "preloop/__init__.py",
    "preloop/security/__init__.py",
    "preloop/security/opt_in.py",
    "preloop/security/defaults.py",
    "preloop/security/git_guard.py",
    "preloop/security/secret_history.py",
    "preloop/security/hygiene.py",
    "preloop/security/ci_workflow.py",
    "preloop/security/upstream.py",
    "preloop/security/gap_register.py",
    "preloop/security/purl_enrich.py",
    "preloop/security/mcp_server.py",
)


def _security_dir() -> Path:
    return Path(__file__).resolve().parent


def package_sources() -> Dict[str, str]:
    """Return relative-path → source text for the embeddable package."""
    security_dir = _security_dir()
    preloop_dir = security_dir.parent
    mapping: Dict[str, str] = {}
    mapping["preloop/__init__.py"] = (
        '"""Minimal preloop package stub for the repo-audit sandbox."""\n'
    )
    mapping["preloop/security/__init__.py"] = (security_dir / "__init__.py").read_text()
    for rel in PACKAGE_RELATIVE:
        if rel in mapping:
            continue
        if rel.startswith("preloop/security/"):
            src = security_dir / rel.split("/", 2)[2]
            mapping[rel] = src.read_text()
        else:
            src = preloop_dir / rel.split("/", 1)[1]
            mapping[rel] = src.read_text()
    return mapping


def _delimiter_for(rel: str) -> str:
    slug = rel.replace("/", "_").replace(".", "_")
    return f"PRELOOP_REPO_AUDIT_{slug.upper()}"


def bootstrap_shell_script() -> str:
    """Return a POSIX shell snippet that writes the package and sets PATH."""
    sources = package_sources()
    lines = [
        "echo 'Bootstrapping repo-audit MCP (opt-in)'",
        f"rm -rf {BOOTSTRAP_ROOT}",
        f"mkdir -p {BOOTSTRAP_ROOT}/preloop/security",
    ]
    for rel, text in sources.items():
        dest = f"{BOOTSTRAP_ROOT}/{rel}"
        delimiter = _delimiter_for(rel)
        # Quoted heredoc so the source is copied byte-for-byte.
        lines.append(f"cat > {dest} << '{delimiter}'")
        lines.append(text.rstrip("\n"))
        lines.append(delimiter)
    lines.append(f'export PYTHONPATH="{BOOTSTRAP_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}"')
    lines.append(
        "python3 -c 'import preloop.security.mcp_server' "
        "|| echo 'WARNING: repo-audit MCP failed to import'"
    )
    return "\n".join(lines) + "\n"


def stdio_server_config() -> Dict[str, object]:
    """MCP stdio config consumed by Codex / MCPConfigService."""
    return {
        "command": "python3",
        "args": ["-m", "preloop.security.mcp_server"],
        "transport": "stdio",
        "env": {"PYTHONPATH": BOOTSTRAP_ROOT},
    }
