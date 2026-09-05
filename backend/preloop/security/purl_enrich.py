"""Small generic PURL enrichment hook for SBOM components.

Builds a ``pkg:generic`` PURL from a repository URL plus commit so OSV can
be queried for components that shipped without an identifier. This is a
hook, not a full SBOM rewrite pipeline.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote, urlparse


def normalize_vcs_url(repo_url: str) -> str:
    """Return a credential-free git URL suitable for a PURL qualifier."""
    raw = (repo_url or "").strip()
    if not raw:
        raise ValueError("repo_url is required")
    if raw.startswith("git@"):
        # git@host:path.git -> git+ssh://host/path.git
        host_path = raw[4:]
        if ":" in host_path:
            host, path = host_path.split(":", 1)
            return f"git+ssh://{host}/{path.lstrip('/')}"
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https", "git", "ssh"}:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        path = parsed.path or ""
        return f"git+{parsed.scheme}://{netloc}{path}"
    return raw


def generic_vcs_purl(
    name: str,
    repo_url: str,
    commit: str,
    version: Optional[str] = None,
) -> str:
    """Build ``pkg:generic/<name>@<version>?vcs_url=<url@commit>``.

    Args:
        name: Component name (no ecosystem prefix).
        repo_url: Origin URL of the source repository.
        commit: Git commit SHA the component was built from.
        version: Optional version; defaults to the commit prefix.

    Returns:
        A Package URL string.

    Raises:
        ValueError: If required fields are missing.
    """
    if not (name or "").strip():
        raise ValueError("name is required")
    if not (commit or "").strip():
        raise ValueError("commit is required")
    ver = (version or commit[:12]).strip()
    vcs = normalize_vcs_url(repo_url)
    # PURL spec: vcs_url may include @<commit>
    if "@" not in vcs.rsplit("/", 1)[-1]:
        qualified = f"{vcs}@{commit}"
    else:
        qualified = vcs
    encoded_name = quote(name.strip(), safe="-._")
    encoded_vcs = quote(qualified, safe=":/@+.-_")
    return f"pkg:generic/{encoded_name}@{ver}?vcs_url={encoded_vcs}"


def enrich_components_with_generic_purl(
    components: List[Mapping[str, Any]],
    repo_url: str,
    commit: str,
) -> List[Dict[str, Any]]:
    """Attach a generic VCS PURL to components that lack one.

    Existing purl/CPE identifiers are left untouched. The function does not
    call OSV; callers POST the returned purls to ``api.osv.dev`` themselves.

    Args:
        components: SBOM component dicts (name, optional version/purl).
        repo_url: Repository URL shared by the generic components.
        commit: Commit SHA to pin in the PURL.

    Returns:
        New list of component dicts; enriched ones gain ``purl`` and
        ``purl_enriched`` = True.
    """
    enriched: List[Dict[str, Any]] = []
    for raw in components:
        item = dict(raw)
        if item.get("purl") or item.get("cpe"):
            item.setdefault("purl_enriched", False)
            enriched.append(item)
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            item.setdefault("purl_enriched", False)
            enriched.append(item)
            continue
        item["purl"] = generic_vcs_purl(
            name,
            repo_url,
            commit,
            version=str(item.get("version") or "") or None,
        )
        item["purl_enriched"] = True
        enriched.append(item)
    return enriched
