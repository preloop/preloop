"""Deterministic discovery from visible tracker projects and stored session facts."""

import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import project_discovery as facts
from preloop.models.crud.delivery_report_policy import repository_push_time
from preloop.schemas.project_discovery import (
    ProjectDiscoveryCandidate,
    ProjectDiscoveryCoverage,
    ProjectDiscoveryEvidence,
    ProjectDiscovery,
)
from preloop.services.delivery_report_facts import parse_pr_key
from preloop.services.fleet_work_refs import parse_reference

_DEFAULT_HOST = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
}
# The merge ledger preserves the web host spelling. Private forge hosts are exact.
_PUBLIC_WEB_ALIASES = {
    "github.com": "www.github.com",
    "bitbucket.org": "www.bitbucket.org",
}
_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+", re.ASCII)
_HOST = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?", re.ASCII)


def _path(value: object) -> str | None:
    """Normalize only namespaced forge paths."""
    if not isinstance(value, str):
        return None
    path = value.rstrip("/").removesuffix(".git").lower()
    if (
        len(path) > 512
        or not _PATH.fullmatch(path)
        or {".", ".."}.intersection(path.split("/"))
    ):
        return None
    return path


def _host(value: object) -> str | None:
    """Read a host or tracker URL without leaking URL credentials or guessing ports."""
    if not isinstance(value, str) or not value:
        return None
    try:
        url = urlsplit(value if "://" in value else "//" + value)
        host = url.hostname
        if not host or url.username or url.password or not _HOST.fullmatch(host):
            return None
        if url.port is not None and (url.scheme, url.port) not in {
            ("https", 443),
            ("http", 80),
        }:
            return f"{host}:{url.port}"
    except ValueError:
        return None
    for canonical, web_alias in _PUBLIC_WEB_ALIASES.items():
        if host in (web_alias, f"api.{canonical}"):
            return canonical
    return host


class _Catalog:
    """Index current and historical paths without choosing among collisions."""

    def __init__(self, projects: list[models.Project]) -> None:
        self.projects: dict[UUID, ProjectDiscoveryCandidate] = {}
        self.ledger_repositories: set[str] = set()
        self.paths: dict[str, set[UUID]] = defaultdict(set)
        self.directories: dict[str, set[UUID]] = defaultdict(set)
        self.hosts: dict[UUID, str | None] = {}
        self.trackers: dict[UUID, str] = {}
        for p in projects:
            tracker = p.organization.tracker
            path = _path(p.slug) if tracker.tracker_type in _DEFAULT_HOST else None
            host = _host(tracker.url or _DEFAULT_HOST.get(tracker.tracker_type))
            self.hosts[p.id] = host
            self.trackers[p.id] = str(tracker.id)
            self.projects[p.id] = ProjectDiscoveryCandidate(
                id=f"project:{p.id}",
                label=p.name,
                project_id=p.id,
                repository=f"{host}/{path}" if host and path else None,
                repository_id=f"{tracker.tracker_type}:{host}:{p.identifier}"
                if host and tracker.tracker_type in _DEFAULT_HOST and p.identifier
                else None,
            )
            if path:
                aliases = (p.meta_data or {}).get("former_slugs")
                for name in [path, *(aliases if isinstance(aliases, list) else [])]:
                    if alias := _path(name):
                        self.paths[alias].add(p.id)
                        if host:
                            self.ledger_repositories.add(f"{host}/{alias}")
                            if host in _PUBLIC_WEB_ALIASES:
                                self.ledger_repositories.add(
                                    f"{_PUBLIC_WEB_ALIASES[host]}/{alias}"
                                )
                        self.directories[alias.rsplit("/", 1)[-1]].add(p.id)

    def repository(
        self,
        path: str,
        host: str | None,
        *,
        tracker_id: str | None = None,
        session_id: UUID | None = None,
    ) -> ProjectDiscoveryCandidate:
        """Resolve an explicit checkout or host-qualified ref, leaving choices visible."""
        ids = sorted(
            i
            for i in self.paths[path]
            if (not host or self.hosts[i] == host)
            and (not tracker_id or self.trackers[i] == tracker_id)
        )
        if len(ids) == 1 and (host or tracker_id):
            return self.projects[ids[0]]
        repository = f"{host}/{path}" if host else None
        return ProjectDiscoveryCandidate(
            id=f"repository:{host or tracker_id or session_id}/{path}",
            repository=repository,
            label=path,
            possible_project_ids=ids[:20],
            possible_project_count=len(ids),
        )

    def directory(
        self, session_id: UUID, directory: str | None, endpoint: str | None
    ) -> ProjectDiscoveryCandidate:
        """A directory is a hint, including when it names part of a monorepo."""
        parts = PurePosixPath((directory or "").replace("\\", "/")).parts
        ids = sorted(
            {i for part in parts for i in self.directories.get(part.lower(), ())}
        )
        # Only a machine-qualified directory can link otherwise unrelated sessions.
        identity = f"session:{session_id}"
        if endpoint and directory:
            workspace = uuid5(NAMESPACE_URL, f"{len(endpoint)}:{endpoint}{directory}")
            identity = f"workspace:{workspace}"
        return ProjectDiscoveryCandidate(
            id=identity,
            label=parts[-1] if parts else "Unknown workspace",
            possible_project_ids=ids[:20],
            possible_project_count=len(ids),
        )


def discover_projects(
    db: Session,
    *,
    account_id: UUID,
    projects: list[models.Project],
    start: datetime,
    end: datetime,
    limit: int,
) -> ProjectDiscovery:
    """Group distinct work items by project or repository and rank recent work.

    Args:
        db: Request database session.
        account_id: Authenticated account.
        projects: Active projects visible under the caller's tracker scope rules.
        start: Inclusive UTC window start.
        end: Inclusive UTC window end.
        limit: Maximum candidates to return.

    Returns:
        Candidates with original activity dates and source coverage.
    """
    catalog = _Catalog(projects)
    candidates: dict[str, ProjectDiscoveryCandidate] = {}
    items: dict[str, dict[str, ProjectDiscoveryEvidence]] = defaultdict(dict)
    coverage = ProjectDiscoveryCoverage()

    def record(
        candidate: ProjectDiscoveryCandidate,
        key: str,
        evidence: ProjectDiscoveryEvidence,
    ) -> None:
        """Keep one latest reason per work item and preserve corroborating sources."""
        evidence.at = evidence.at.astimezone(UTC)
        candidate = candidates.setdefault(candidate.id, candidate)
        for source in evidence.sources:
            setattr(candidate.coverage, source, "observed")
            setattr(coverage, source, "observed")
        previous = items[candidate.id].get(key)
        if previous:
            sources = sorted(set(previous.sources + evidence.sources))
            # Prefer the forge for equal timestamps; ref breaks all remaining ties.
            if (previous.at, "tracker" in previous.sources, previous.ref) > (
                evidence.at,
                "tracker" in evidence.sources,
                evidence.ref,
            ):
                previous, evidence = evidence, previous
            evidence.sources = sources
            evidence.session_id = evidence.session_id or previous.session_id
        items[candidate.id][key] = evidence

    sessions = facts.sessions(db, account_id=account_id, start=start, end=end)
    work = facts.work_items(db, account_id=account_id, start=start, end=end)
    issues = (
        facts.issues(
            db,
            project_ids=catalog.projects.keys(),
            account_id=account_id,
            start=start,
            end=end,
        )
        if projects
        else []
    )
    merges = (
        facts.merges(
            db,
            account_id=account_id,
            repositories=catalog.ledger_repositories,
            start=start,
            end=end,
        )
        if catalog.ledger_repositories
        else []
    )
    cap = facts.FACT_LIMIT
    truncated = any(len(rows) > cap for rows in (sessions, work, issues, merges))

    for p in projects:
        at = repository_push_time(p.meta_data, p.organization.tracker.tracker_type)
        if at and start <= at <= end:
            record(
                catalog.projects[p.id],
                "push",
                ProjectDiscoveryEvidence(
                    reason="repository_push",
                    at=at,
                    ref=f"project:{p.id}",
                    sources=["tracker"],
                ),
            )
    for issue in issues[:cap]:
        obj = parse_reference(issue.external_url) or parse_reference(
            issue.key, kind="issue"
        )
        key = (
            f"{obj['kind']}:{obj['ref'][1:]}"
            if obj and obj["kind"]
            else f"issue:{issue.id}"
        )
        record(
            catalog.projects[issue.project_id],
            key,
            ProjectDiscoveryEvidence(
                reason="issue_activity",
                at=issue.last_updated_external.replace(tzinfo=UTC),
                ref=f"issue:{issue.id}",
                sources=["tracker"],
            ),
        )
    for merge in merges[:cap]:
        parsed = parse_pr_key(merge.pr_key)
        if parsed and (path := _path(parsed[0])) and (host := _host(merge.pr_key)):
            candidate = catalog.repository(path, host)
            if candidate.project_id or candidate.possible_project_ids:
                record(
                    candidate,
                    f"pull_request:{parsed[1]}",
                    ProjectDiscoveryEvidence(
                        reason="pull_request_merged",
                        at=merge.merged_at,
                        ref=merge.pr_key,
                        sources=["tracker"],
                    ),
                )

    represented = set()
    for call in work[:cap]:
        ref = call.work
        if not isinstance(ref, dict) or not (path := _path(ref.get("repo"))):
            continue
        kind = ref.get("kind")
        if kind not in ("issue", "pull_request"):
            continue
        candidate = catalog.repository(
            path, _host(ref.get("host")), session_id=call.runtime_session_id
        )
        number = ref.get("ref")
        key = f"activity:{call.id}"
        if isinstance(number, str) and re.fullmatch(r"[#!][0-9]{1,19}", number):
            key = f"{kind}:{int(number[1:])}"
        record(
            candidate,
            key,
            ProjectDiscoveryEvidence(
                reason="issue_activity"
                if kind == "issue"
                else "pull_request_merged"
                if ref.get("action") == "merged" and call.status == "succeeded"
                else "pull_request_activity",
                at=call.timestamp,
                ref=f"activity:{call.id}",
                session_id=call.runtime_session_id,
                sources=["sessions"],
            ),
        )
        represented.add(call.runtime_session_id)
    for row in sessions[:cap]:
        if row.id in represented:
            continue
        path = _path(row.repository)
        candidate = (
            catalog.repository(path, None, tracker_id=row.tracker_id, session_id=row.id)
            if path
            else catalog.directory(row.id, row.directory, row.desktop_endpoint_id)
        )
        record(
            candidate,
            f"session:{row.id}",
            ProjectDiscoveryEvidence(
                reason="session_activity",
                at=row.at.replace(tzinfo=UTC),
                ref=f"session:{row.id}",
                session_id=row.id,
                sources=["sessions"],
            ),
        )

    def rank(candidate: ProjectDiscoveryCandidate) -> tuple[int, int, float, str]:
        """Prefer recent active days, then distinct work on those candidates."""
        latest = candidate.evidence[0].at
        return (
            -latest.date().toordinal(),
            -candidate.evidence_count,
            -latest.timestamp(),
            candidate.id,
        )

    for candidate in candidates.values():
        observations = items[candidate.id].values()
        candidate.evidence_count = len(observations)
        candidate.evidence = sorted(
            observations, key=lambda e: (-e.at.timestamp(), e.ref)
        )[:8]
    return ProjectDiscovery(
        start=start,
        end=end,
        coverage=coverage,
        candidates=sorted(candidates.values(), key=rank)[:limit],
        truncated=truncated or len(candidates) > limit,
    )
