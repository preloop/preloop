"""Apply bounded issue assessment to fresh provider content and triage labels."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from preloop.schemas.issue_triage import (
    ComplexityScheme,
    IssueTriageApply,
    IssueTriageContext,
    IssueTriageResult,
    TriageIssue,
    provider_revision,
)
from preloop.services.issue_triage_provider import IssueTriageProvider
from preloop.sync.exceptions import TrackerError

START = "<!-- preloop-issue-triage:start -->"
END = "<!-- preloop-issue-triage:end -->"
STANDARD = ["complexity:low", "complexity:medium", "complexity:high"]
STANDARD_RISK = ["risk:low", "risk:medium", "risk:high"]
STANDARD_READINESS = [
    "readiness:ready",
    "readiness:needs-spec",
    "readiness:blocked",
    "readiness:in-progress",
    "readiness:needs-verification",
]
SECTION_HEADING = "## Ready for development"


def family_scheme(
    catalogue: list[dict[str, str]], family: str, standard: list[str]
) -> ComplexityScheme:
    """Reuse a project's ``<family>:*`` labels, or offer the standard family.

    Risk and readiness have one obvious prefix each, so unlike complexity
    there is no competing vocabulary to disambiguate. A family the tool
    created earlier (every name standard, managed descriptions) stays
    creatable, so a partly created standard family can be completed.
    """
    names = [
        row["name"]
        for row in catalogue
        if re.match(rf"^{family}(?::+|[-_/ ]+).+", row["name"], re.I)
    ]
    if not names:
        return ComplexityScheme(name="standard", labels=standard, create_missing=True)
    managed = set(names) <= set(standard) and all(
        row.get("description")
        == f"Preloop issue {family}: " + row["name"].split(":", 1)[-1]
        for row in catalogue
        if row["name"] in names
    )
    return ComplexityScheme(
        name=family,
        labels=standard if managed else sorted(names),
        create_missing=managed,
    )


def risk_scheme(catalogue: list[dict[str, str]]) -> ComplexityScheme:
    """The project's risk family, or the standard ``risk:*`` family."""
    return family_scheme(catalogue, "risk", STANDARD_RISK)


def readiness_scheme(catalogue: list[dict[str, str]]) -> ComplexityScheme:
    """The project's readiness family, or the standard ``readiness:*`` family."""
    return family_scheme(catalogue, "readiness", STANDARD_READINESS)


def complexity_scheme(catalogue: list[dict[str, str]]) -> ComplexityScheme:
    """Reuse one explicit family; refuse ambiguous project vocabulary."""
    groups: dict[str, list[str]] = {}
    for label in catalogue:
        name = label["name"]
        match = re.match(
            r"^(complexity|effort|size|difficulty)(?::+|[-_/ ]+).+", name, re.I
        )
        if match:
            groups.setdefault(match[1].lower(), []).append(name)
    candidates = []
    for family, names in groups.items():
        managed = set(names) <= set(STANDARD) and all(
            row.get("description")
            == "Preloop issue complexity: " + row["name"].split(":")[-1]
            for row in catalogue
            if row["name"] in names
        )
        candidates.append(
            ComplexityScheme(
                name=family,
                labels=STANDARD if managed else sorted(names),
                create_missing=managed,
            )
        )
    names = {row["name"] for row in catalogue}
    shirts = names & {"XS", "S", "M", "L", "XL", "XXL"}
    if {"S", "M", "L"} <= shirts:
        candidates.append(ComplexityScheme(name="t-shirt", labels=sorted(shirts)))
    ambiguous = False
    for family in ({"low", "medium", "high"}, {"easy", "medium", "hard"}):
        matching = [row for row in catalogue if row["name"].lower() in family]
        if {row["name"].lower() for row in matching} == family:
            if family == {"low", "medium", "high"} and not all(
                re.search(
                    r"complexity|effort|difficulty|size",
                    row.get("description", ""),
                    re.I,
                )
                for row in matching
            ):
                ambiguous = True
            else:
                candidates.append(
                    ComplexityScheme(
                        name="standalone",
                        labels=sorted(row["name"] for row in matching),
                    )
                )
    if len(candidates) > 1:
        raise ValueError("multiple_complexity_schemes")
    if candidates:
        return candidates[0]
    if ambiguous:
        raise ValueError("ambiguous_complexity_vocabulary")
    return ComplexityScheme(name="standard", labels=STANDARD, create_missing=True)


def scope_revision(issue: TriageIssue, *schemes: ComplexityScheme | None) -> str:
    """Bind scope and managed labels while allowing unrelated label edits.

    With only a complexity scheme this is byte-identical to the original
    single-family revision, so stored packets keep matching.
    """
    family = sorted({name for scheme in schemes if scheme for name in scheme.labels})
    return sha256(
        json.dumps(
            [
                issue.title,
                issue.body,
                issue.state,
                family,
                sorted(set(issue.labels) & set(family)),
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def merge_assessment(body: str, assessment: str) -> str:
    """Replace one managed section, preserving every byte outside its bounds."""
    assessment = assessment.strip()
    if not assessment or START in assessment or END in assessment:
        raise ValueError("invalid_assessment")
    section = START + "\n" + SECTION_HEADING + "\n\n" + assessment + "\n" + END
    if START not in body and END not in body:
        return body + ("\n\n" if body else "") + section
    if body.count(START) != 1 or body.count(END) != 1:
        raise ValueError("ambiguous_managed_section")
    start, end = body.index(START), body.index(END)
    if end < start:
        raise ValueError("ambiguous_managed_section")
    return body[:start] + section + body[end + len(END) :]


async def get_context(provider: IssueTriageProvider) -> IssueTriageContext:
    """Fetch current issue and complete project vocabulary, never cached data."""
    issue = await provider.read_issue()
    catalogue = await provider.catalogue()
    limitations = []
    try:
        scheme = complexity_scheme(catalogue)
    except ValueError as exc:
        scheme = None
        limitations.append(str(exc))
    risk, readiness = risk_scheme(catalogue), readiness_scheme(catalogue)
    return IssueTriageContext(
        issue=issue,
        expected_revision=scope_revision(issue, scheme, risk, readiness),
        catalogue=catalogue,
        complexity_scheme=scheme,
        risk_scheme=risk,
        readiness_scheme=readiness,
        limitations=limitations,
    )


SCHEME_READERS: dict[str, Callable[[list[dict[str, str]]], ComplexityScheme]] = {
    "complexity": complexity_scheme,
    "risk": risk_scheme,
    "readiness": readiness_scheme,
}


def _changed_families(
    catalogue: list[dict[str, str]], expected: list[tuple[str, ComplexityScheme]]
) -> str | None:
    """Name the first family whose fresh scheme no longer matches, if any."""
    for name, scheme in expected:
        try:
            current = SCHEME_READERS[name](catalogue)
        except ValueError:
            return name
        if set(current.labels) != set(scheme.labels) or (
            current.create_missing != scheme.create_missing
        ):
            return name
    return None


def _intent(
    issue: TriageIssue,
    title: str,
    body: str,
    add: list[str],
    remove: list[str],
    *,
    atomic_labels: bool = False,
) -> dict[str, Any]:
    labels = set(issue.labels)
    revisions = []
    if title != issue.title or body != issue.body:
        revisions.append(provider_revision(title, body, sorted(labels), issue.state))
    if atomic_labels and (add or remove):
        labels.update(add)
        labels.difference_update(remove)
        revisions.append(provider_revision(title, body, sorted(labels), issue.state))
        add, remove = [], []
    labels.update(add)
    if add:
        revisions.append(provider_revision(title, body, sorted(labels), issue.state))
    for name in remove:
        labels.discard(name)
        revisions.append(provider_revision(title, body, sorted(labels), issue.state))
    return {
        "expected_revisions": list(
            dict.fromkeys(r for r in revisions if r != issue.revision)
        ),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }


async def apply_triage(
    provider: IssueTriageProvider,
    request: IssueTriageApply,
    record_intent: Callable[[dict[str, Any]], None],
    *,
    dispatch_label: str | None = None,
) -> IssueTriageResult:
    """Apply with explicit stale checks, deltas and honest partial receipts.

    ``dispatch_label`` comes from the controller's operator policy, never from
    the model. It is added in the same label delta as the assessed labels and
    only when complexity is estimated; triage never removes it.

    The provider has no issue-body CAS contract. Preflight rejects already stale
    input; final verification detects observable races, not every intervening edit.
    """
    operations: list[dict[str, str]] = []
    latest: TriageIssue | None = None
    receipt: dict[str, Any] = {}

    def retain_intent(intent: dict[str, Any]) -> None:
        nonlocal receipt
        combined = list(
            dict.fromkeys(
                receipt.get("expected_revisions", [])
                + intent.get("expected_revisions", [])
            )
        )
        if len(combined) > 8:
            raise ValueError("too_many_triage_intent_revisions")
        receipt = {**receipt, **intent, "expected_revisions": combined}
        record_intent(receipt)

    try:
        context = await get_context(provider)
        latest, scheme = context.issue, context.complexity_scheme
        schemes = (scheme, context.risk_scheme, context.readiness_scheme)
        if latest.state != "open":
            return IssueTriageResult(
                status="conflict", reason="issue_not_open", issue=latest
            )
        if context.expected_revision != request.expected_revision:
            return IssueTriageResult(
                status="conflict",
                reason="stale_issue",
                issue=latest,
                next_action="Refresh triage context and re-evaluate; no mutation was made.",
            )
        label = request.complexity_label
        selected: dict[str, tuple[str, ComplexityScheme]] = {}
        for name, chosen, family_of in (
            ("complexity", label, scheme),
            ("risk", request.risk_label, context.risk_scheme),
            ("readiness", request.readiness_label, context.readiness_scheme),
        ):
            if chosen is None:
                continue
            if family_of is None or chosen not in family_of.labels:
                raise ValueError(f"{name}_label_not_in_current_scheme")
            selected[name] = (chosen, family_of)
        title = request.title.strip() if request.title is not None else latest.title
        if not title:
            raise ValueError("empty_title")
        body = merge_assessment(latest.body, request.assessment)
        if len(body.encode()) > 60000:
            raise ValueError("issue_body_too_large")
        family = {name for _, sch in selected.values() for name in sch.labels}
        chosen_labels = {chosen for chosen, _ in selected.values()}
        remove = sorted((set(latest.labels) & family) - chosen_labels)
        add = [chosen for chosen, _ in selected.values() if chosen not in latest.labels]
        if label is None or dispatch_label in family:
            dispatch_label = None
        if dispatch_label is not None and dispatch_label not in latest.labels:
            add.append(dispatch_label)
        if len(remove) > 6:
            raise ValueError("too_many_conflicting_complexity_labels")
        content_changed = title != latest.title or body != latest.body
        if not content_changed and not add and not remove:
            retain_intent(
                {
                    "provider_revision": latest.revision,
                    "provider_updated_at": latest.updated_at,
                }
            )
            return IssueTriageResult(
                status="unchanged" if label is not None else "partial",
                reason=None if label is not None else "complexity_not_estimated",
                issue=latest,
            )
        # Recheck after catalogue I/O, before even creating project labels.
        fresh = await provider.read_issue()
        if scope_revision(fresh, *schemes) != request.expected_revision:
            return IssueTriageResult(
                status="conflict", reason="stale_issue", issue=fresh
            )
        latest = fresh
        creatable = [
            (name, sch) for name, (_, sch) in selected.items() if sch.create_missing
        ]
        if creatable:
            catalogue = await provider.catalogue()
            changed = _changed_families(catalogue, creatable)
            if changed:
                raise ValueError(f"{changed}_catalogue_changed")
            # Check scope once more after the catalogue refresh.
            fresh = await provider.read_issue()
            if scope_revision(fresh, *schemes) != request.expected_revision:
                return IssueTriageResult(
                    status="conflict", reason="stale_issue", issue=fresh
                )
            latest = fresh
            present = {row["name"] for row in catalogue}
            for name in [n for _, sch in creatable for n in sch.labels]:
                if name in present:
                    continue
                operation = {
                    "operation": "create_label",
                    "label": name,
                    "state": "requested",
                }
                operations.append(operation)
                try:
                    await provider.create_label(name)
                except (TrackerError, ValueError, TimeoutError):
                    # A concurrent creator or uncertain response may have succeeded.
                    if name not in {row["name"] for row in await provider.catalogue()}:
                        raise
                operation["state"] = "confirmed"
            refreshed = await provider.catalogue()
            changed = _changed_families(refreshed, creatable)
            if changed:
                raise ValueError(f"{changed}_catalogue_changed")
        fresh = await provider.read_issue()
        if scope_revision(fresh, *schemes) != request.expected_revision:
            raise ValueError("stale_issue")
        latest = fresh
        if content_changed:
            retain_intent(_intent(latest, title, body, [], []))
        if content_changed:
            operation = {"operation": "update_content", "state": "requested"}
            operations.append(operation)
            await provider.write_content(title, body)
            operation["state"] = "confirmed"
        # Never remove a human's intervening complexity classification.
        fresh = await provider.read_issue()
        if (fresh.title, fresh.body, fresh.state) != (title, body, "open") or (
            set(fresh.labels) & family
        ) != (set(latest.labels) & family):
            raise ValueError("issue_changed_during_apply")
        latest = fresh
        if add or remove:
            retain_intent(
                _intent(
                    latest,
                    title,
                    body,
                    add,
                    remove,
                    atomic_labels=getattr(provider, "kind", "github") == "gitlab",
                )
            )
            operation = {"operation": "update_complexity_labels", "state": "requested"}
            operations.append(operation)
            dispatch_op: dict[str, str] | None = None
            if dispatch_label is not None and dispatch_label in add:
                dispatch_op = {
                    "operation": "apply_dispatch_label",
                    "label": dispatch_label,
                    "state": "requested",
                }
                operations.append(dispatch_op)
            await provider.update_labels(add, remove)
            operation["state"] = "confirmed"
            if dispatch_op is not None:
                dispatch_op["state"] = "confirmed"
        latest = await provider.read_issue()
        if (
            (latest.title, latest.body, latest.state) != (title, body, "open")
            or any(
                set(latest.labels) & set(sch.labels) != {chosen}
                for chosen, sch in selected.values()
            )
            or (dispatch_label is not None and dispatch_label not in latest.labels)
        ):
            raise ValueError("final_verification_failed")
        retain_intent(
            {
                "provider_revision": latest.revision,
                "provider_updated_at": latest.updated_at,
            }
        )
        return IssueTriageResult(
            status="updated" if label is not None else "partial",
            reason=None if label is not None else "complexity_not_estimated",
            issue=latest,
            operations=operations,
            next_action=None
            if label is not None
            else "The issue assessment is updated. Resolve missing context before assigning complexity.",
        )
    except (ValueError, TrackerError, TimeoutError, SQLAlchemyError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]+", str(exc))
            else (
                "triage_receipt_failed"
                if isinstance(exc, SQLAlchemyError)
                else "provider_request_failed"
            )
        )
        try:
            latest = await provider.read_issue()
        except (ValueError, TrackerError, TimeoutError):
            # Keep the last snapshot; a second provider error must not replace
            # the original write-failure reason.
            pass
        return IssueTriageResult(
            status="partial" if operations else "failed",
            reason=reason,
            issue=latest,
            operations=operations,
            next_action="Inspect the updated issue and receipt, then fetch fresh context; do not blindly repeat the write.",
        )
