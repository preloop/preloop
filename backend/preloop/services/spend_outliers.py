"""Spend outlier alerts: per developer daily spend, model mix and session cost.

Three rules, evaluated from gateway usage (``api_usage.estimated_cost``) and,
when a source is registered, from imported per-user spend that the gateway did
not meter (for example Copilot premium requests, #788):

``daily_spend``
    Yesterday's spend for one user is at least ``daily_multiple`` times the
    median of the days with spend among the previous 28 UTC days. Needs at
    least ``min_history_days`` such days, and never fires on a median of 0.
``model_mix``
    One model whose name starts with an operator-marked top-tier prefix took
    more than ``top_tier_share`` of the user's spend on two consecutive UTC
    days. One expensive day does not fire; two in a row does.
``session_cost``
    One runtime session's total cost exceeds ``session_cost_threshold_usd``.
    Off until the operator sets a threshold.

Each finding is stored once per fingerprint (see
:func:`finding_fingerprint`), so re-running an evaluation is harmless. The
console shows the findings as Attention cards and dismisses them through the
existing ``attention_dismissal`` table:

* ``item_id`` is ``spend:<rule>:<user id>`` (``spend:session_cost:<session
  id>`` for the session rule), stable across days.
* ``fingerprint`` is ``<rule>|<user id>|<UTC day>`` (``session_cost|<user
  id>|<session id>``). A dismissal hides the card while the fingerprint is
  unchanged. A new UTC day that still matches is a new fingerprint, so a
  standing problem comes back the day after it was dismissed. A snooze is the
  exception: it keeps the card hidden until ``snooze_until`` even when the
  fingerprint moves on to a new day.

A card stays open for :data:`OPEN_WINDOW_DAYS` after the finding was detected,
or until a newer finding for the same item replaces it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import logging
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_attention_dismissal,
    crud_spend_outlier_finding,
    crud_spend_outlier_settings,
)
from preloop.models.crud.spend_outlier import SessionSpend
from preloop.models.models.spend_outlier import (
    DEFAULT_DAILY_MULTIPLE,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_TOP_TIER_SHARE,
    SPEND_OUTLIER_RULE_DAILY,
    SPEND_OUTLIER_RULE_MODEL_MIX,
    SPEND_OUTLIER_RULE_SESSION,
)
from preloop.utils.reporting_window import resolve_reporting_window

logger = logging.getLogger(__name__)

#: Attention kind, and the prefix of every spend outlier item id.
SPEND_ATTENTION_KIND = "spend"
#: Trailing window the daily rule takes its median from.
HISTORY_WINDOW_DAYS = 28
#: How long a finding stays on the Attention page after it was detected.
OPEN_WINDOW_DAYS = 7
#: The digest covers findings detected in this many trailing days.
DIGEST_WINDOW_DAYS = 7
#: How far back the periodic session check looks for session activity.
SESSION_ACTIVITY_WINDOW = timedelta(hours=2)

RULE_LABELS: Dict[str, str] = {
    SPEND_OUTLIER_RULE_DAILY: "Daily spend spike",
    SPEND_OUTLIER_RULE_MODEL_MIX: "Top-tier model mix",
    SPEND_OUTLIER_RULE_SESSION: "Expensive session",
}


@dataclass(frozen=True)
class SpendOutlierConfig:
    """The thresholds one evaluation uses."""

    daily_multiple: float = DEFAULT_DAILY_MULTIPLE
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS
    top_tier_model_prefixes: tuple[str, ...] = ()
    top_tier_share: float = DEFAULT_TOP_TIER_SHARE
    session_cost_threshold_usd: Optional[float] = None

    @classmethod
    def from_row(
        cls, row: Optional[models.SpendOutlierSettings]
    ) -> "SpendOutlierConfig":
        """Settings row to config; None means every default."""
        if row is None:
            return cls()
        prefixes = tuple(
            prefix.strip().lower()
            for prefix in (row.top_tier_model_prefixes or [])
            if isinstance(prefix, str) and prefix.strip()
        )
        return cls(
            daily_multiple=float(
                row.daily_multiple
                if row.daily_multiple is not None
                else DEFAULT_DAILY_MULTIPLE
            ),
            min_history_days=int(
                row.min_history_days
                if row.min_history_days is not None
                else DEFAULT_MIN_HISTORY_DAYS
            ),
            top_tier_model_prefixes=prefixes,
            top_tier_share=float(
                row.top_tier_share
                if row.top_tier_share is not None
                else DEFAULT_TOP_TIER_SHARE
            ),
            session_cost_threshold_usd=row.session_cost_threshold_usd,
        )


@dataclass(frozen=True)
class ImportedSpendRow:
    """Per-user spend imported from outside the gateway.

    Attributes:
        user_id: Preloop user the spend belongs to.
        day: UTC day of the spend.
        model: Model name as the source reported it.
        cost_usd: Amount in USD.
        source: Short source label shown on the card, e.g. ``copilot``.
    """

    user_id: UUID
    day: date
    model: str
    cost_usd: float
    source: str


#: ``(db, account_id, start_day, end_day) -> rows`` for ``[start, end]``.
ImportedSpendSource = Callable[[Session, UUID, date, date], Iterable[ImportedSpendRow]]

_imported_spend_sources: List[ImportedSpendSource] = []


def register_imported_spend_source(source: ImportedSpendSource) -> None:
    """Add a source of per-user spend the gateway did not meter.

    The Copilot import (#788) registers here once it stores per user, per
    model, per day amounts. With no source registered the rules read gateway
    spend only.

    Args:
        source: Callable returning imported rows for an account and day range.
    """
    if source not in _imported_spend_sources:
        _imported_spend_sources.append(source)


def unregister_imported_spend_source(source: ImportedSpendSource) -> None:
    """Remove a previously registered source (tests, plugin unload)."""
    if source in _imported_spend_sources:
        _imported_spend_sources.remove(source)


def _imported_rows(
    db: Session, account_id: UUID, start_day: date, end_day: date
) -> List[ImportedSpendRow]:
    rows: List[ImportedSpendRow] = []
    for source in list(_imported_spend_sources):
        try:
            rows.extend(source(db, account_id, start_day, end_day))
        except Exception:
            # One broken import must not stop gateway alerts.
            logger.exception("Imported spend source failed; skipping it")
    return rows


@dataclass
class _UserSpend:
    """One user's spend by day and model, split by where it came from."""

    gateway: Dict[date, Dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    imported: Dict[date, Dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    imported_sources: set[str] = field(default_factory=set)

    def day_total(self, day: date) -> float:
        return sum(self.gateway.get(day, {}).values()) + sum(
            self.imported.get(day, {}).values()
        )

    def day_by_model(self, day: date) -> Dict[str, float]:
        merged: Dict[str, float] = defaultdict(float)
        for bucket in (self.gateway.get(day, {}), self.imported.get(day, {})):
            for model, cost in bucket.items():
                merged[model] += cost
        return merged


@dataclass(frozen=True)
class OutlierCandidate:
    """A rule that matched, before it is stored."""

    rule: str
    user_id: Optional[UUID]
    day: date
    details: Dict[str, Any]
    runtime_session_id: Optional[UUID] = None


def finding_item_id(
    rule: str, user_id: Optional[UUID], runtime_session_id: Optional[UUID] = None
) -> str:
    """Attention item id: stable per rule and user (or session)."""
    if rule == SPEND_OUTLIER_RULE_SESSION:
        return f"{SPEND_ATTENTION_KIND}:{rule}:{runtime_session_id}"
    return f"{SPEND_ATTENTION_KIND}:{rule}:{user_id}"


def finding_fingerprint(
    rule: str,
    user_id: Optional[UUID],
    day: date,
    runtime_session_id: Optional[UUID] = None,
) -> str:
    """Rule id, user id, and the UTC day or the session id."""
    subject = (
        str(runtime_session_id)
        if rule == SPEND_OUTLIER_RULE_SESSION
        else day.isoformat()
    )
    return f"{rule}|{user_id or ''}|{subject}"


def is_top_tier(model: str, prefixes: Sequence[str]) -> bool:
    """Whether a model name starts with one of the operator's prefixes.

    Matches with or without a provider prefix, so ``anthropic/model-x`` is
    caught by a ``model-`` prefix as well as by ``anthropic/model-``.
    """
    name = (model or "").strip().lower()
    if not name or not prefixes:
        return False
    keys = [name]
    slash = name.rfind("/")
    if slash > 0:
        keys.append(name[slash + 1 :])
    return any(key.startswith(prefix) for key in keys for prefix in prefixes)


def _money(value: float) -> float:
    return round(float(value), 4)


def daily_spend_candidate(
    user_id: UUID,
    spend: _UserSpend,
    day: date,
    config: SpendOutlierConfig,
) -> Optional[OutlierCandidate]:
    """Rule 1: yesterday against the trailing median of days with spend.

    Args:
        user_id: The user evaluated.
        spend: The user's spend by day.
        day: The UTC day being judged (normally yesterday).
        config: Thresholds.

    Returns:
        A candidate when the rule fires, otherwise None.
    """
    history = [
        total
        for offset in range(1, HISTORY_WINDOW_DAYS + 1)
        if (total := spend.day_total(day - timedelta(days=offset))) > 0
    ]
    if len(history) < max(1, config.min_history_days):
        return None
    trailing_median = float(median(history))
    if trailing_median <= 0:
        return None
    today_total = spend.day_total(day)
    multiple = today_total / trailing_median
    if multiple < config.daily_multiple:
        return None
    imported = sum(spend.imported.get(day, {}).values())
    return OutlierCandidate(
        rule=SPEND_OUTLIER_RULE_DAILY,
        user_id=user_id,
        day=day,
        details={
            "spend_usd": _money(today_total),
            "median_usd": _money(trailing_median),
            "multiple": round(multiple, 2),
            "threshold_multiple": config.daily_multiple,
            "history_days": len(history),
            "gateway_usd": _money(today_total - imported),
            "imported_usd": _money(imported),
            "imported_sources": sorted(spend.imported_sources) if imported else [],
        },
    )


def model_family(model: str) -> str:
    """The key the model mix rule groups by: lowercase, provider dropped.

    ``anthropic/Model-X`` and ``model-x`` are the same model reached two
    ways, so their spend is one share. Mirrors how :func:`is_top_tier`
    matches a prefix with or without the provider segment.
    """
    name = (model or "").strip().lower()
    slash = name.rfind("/")
    return name[slash + 1 :] if slash >= 0 else name


def _spend_by_family(
    by_model: Mapping[str, float],
) -> Dict[str, tuple[float, str]]:
    """Group a day's spend by model family: family -> (cost, display name).

    The display name is the spelling that carried the most spend that day.
    """
    grouped: Dict[str, tuple[float, str, float]] = {}
    for model, cost in by_model.items():
        family = model_family(model)
        total, display, display_cost = grouped.get(family, (0.0, model, -1.0))
        if cost > display_cost:
            display, display_cost = model, cost
        grouped[family] = (total + cost, display, display_cost)
    return {family: (total, display) for family, (total, display, _) in grouped.items()}


def _top_tier_leader(
    by_family: Mapping[str, tuple[float, str]], prefixes: Sequence[str]
) -> Optional[tuple[str, str, float, float]]:
    """The top-tier family with the largest share.

    Returns:
        ``(family, display name, share, cost)``, or None.
    """
    total = sum(cost for cost, _ in by_family.values())
    if total <= 0:
        return None
    best: Optional[tuple[str, str, float, float]] = None
    for family, (cost, display) in by_family.items():
        if not is_top_tier(display, prefixes):
            continue
        share = cost / total
        if best is None or share > best[2]:
            best = (family, display, share, cost)
    return best


def model_mix_candidate(
    user_id: UUID,
    spend: _UserSpend,
    day: date,
    config: SpendOutlierConfig,
) -> Optional[OutlierCandidate]:
    """Rule 2: one top-tier model over the share threshold two days running.

    Shares are per model family (see :func:`model_family`), so spend on
    ``provider/model-x`` and on ``model-x`` counts as one model, on the same
    day and across the two days.

    Args:
        user_id: The user evaluated.
        spend: The user's spend by day and model.
        day: The second of the two UTC days (normally yesterday).
        config: Thresholds and the top-tier prefix list.

    Returns:
        A candidate when the rule fires, otherwise None.
    """
    prefixes = config.top_tier_model_prefixes
    if not prefixes:
        return None
    today = _spend_by_family(spend.day_by_model(day))
    previous_day = day - timedelta(days=1)
    previous = _spend_by_family(spend.day_by_model(previous_day))
    leader = _top_tier_leader(today, prefixes)
    if leader is None or leader[2] <= config.top_tier_share:
        return None
    family, model, share, model_cost = leader
    previous_total = sum(cost for cost, _ in previous.values())
    if previous_total <= 0:
        return None
    previous_share = previous.get(family, (0.0, model))[0] / previous_total
    if previous_share <= config.top_tier_share:
        return None
    total = sum(cost for cost, _ in today.values())
    imported = sum(spend.imported.get(day, {}).values())
    return OutlierCandidate(
        rule=SPEND_OUTLIER_RULE_MODEL_MIX,
        user_id=user_id,
        day=day,
        details={
            "model": model,
            "share": round(share, 4),
            "previous_share": round(previous_share, 4),
            "previous_day": previous_day.isoformat(),
            "threshold_share": config.top_tier_share,
            "model_usd": _money(model_cost),
            "spend_usd": _money(total),
            "gateway_usd": _money(total - imported),
            "imported_usd": _money(imported),
            "imported_sources": sorted(spend.imported_sources) if imported else [],
        },
    )


def session_candidate(
    session: SessionSpend, config: SpendOutlierConfig
) -> Optional[OutlierCandidate]:
    """Rule 3: one session over the operator's cost threshold."""
    threshold = config.session_cost_threshold_usd
    if threshold is None or session.cost_usd <= threshold:
        return None
    return OutlierCandidate(
        rule=SPEND_OUTLIER_RULE_SESSION,
        user_id=session.user_id,
        day=session.last_request_at.date(),
        runtime_session_id=session.runtime_session_id,
        details={
            "session_id": str(session.runtime_session_id),
            "spend_usd": _money(session.cost_usd),
            "threshold_usd": threshold,
        },
    )


def load_config(db: Session, account_id: UUID) -> SpendOutlierConfig:
    """The account's thresholds, defaults where unset."""
    return SpendOutlierConfig.from_row(
        crud_spend_outlier_settings.get_for_account(db, account_id=account_id)
    )


def _collect_user_spend(
    db: Session, account_id: UUID, start_day: date, end_day: date
) -> Dict[UUID, _UserSpend]:
    by_user: Dict[UUID, _UserSpend] = defaultdict(_UserSpend)
    for row in crud_spend_outlier_finding.gateway_spend_by_user_model_day(
        db, account_id=account_id, start_day=start_day, end_day=end_day
    ):
        by_user[row.user_id].gateway[row.day][row.model] += row.cost_usd
    for imported in _imported_rows(db, account_id, start_day, end_day):
        if imported.cost_usd <= 0:
            continue
        spend = by_user[imported.user_id]
        spend.imported[imported.day][imported.model] += imported.cost_usd
        spend.imported_sources.add(imported.source)
    return by_user


def _store(
    db: Session,
    account_id: UUID,
    candidates: Iterable[OutlierCandidate],
    now: datetime,
) -> List[models.SpendOutlierFinding]:
    stored: List[models.SpendOutlierFinding] = []
    for candidate in candidates:
        finding = crud_spend_outlier_finding.record(
            db,
            account_id=account_id,
            rule=candidate.rule,
            user_id=candidate.user_id,
            runtime_session_id=candidate.runtime_session_id,
            day=candidate.day,
            item_id=finding_item_id(
                candidate.rule, candidate.user_id, candidate.runtime_session_id
            ),
            fingerprint=finding_fingerprint(
                candidate.rule,
                candidate.user_id,
                candidate.day,
                candidate.runtime_session_id,
            ),
            details=candidate.details,
            detected_at=now,
            commit=False,
        )
        if finding is not None:
            stored.append(finding)
    db.commit()
    return stored


def _utc(now: Optional[datetime]) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def evaluate_daily_rules(
    db: Session, account_id: UUID, now: Optional[datetime] = None
) -> List[models.SpendOutlierFinding]:
    """Run the daily spend and model mix rules for yesterday (UTC).

    Args:
        db: Database session.
        account_id: Account to evaluate.
        now: Evaluation time; yesterday is the UTC day before it.

    Returns:
        Findings that were new. A repeat run on the same day returns [].
    """
    moment = _utc(now)
    day = moment.date() - timedelta(days=1)
    config = load_config(db, account_id)
    by_user = _collect_user_spend(
        db, account_id, day - timedelta(days=HISTORY_WINDOW_DAYS), day
    )
    candidates: List[OutlierCandidate] = []
    for user_id, spend in by_user.items():
        if spend.day_total(day) <= 0:
            continue
        for rule in (daily_spend_candidate, model_mix_candidate):
            candidate = rule(user_id, spend, day, config)
            if candidate is not None:
                candidates.append(candidate)
    return _store(db, account_id, candidates, moment)


def evaluate_session_rule(
    db: Session,
    account_id: UUID,
    now: Optional[datetime] = None,
    active_within: timedelta = SESSION_ACTIVITY_WINDOW,
) -> List[models.SpendOutlierFinding]:
    """Record sessions that crossed the account's session cost threshold.

    Only sessions with a gateway request inside ``active_within`` are summed,
    so the periodic check stays cheap. Nothing runs while the threshold is
    unset.

    Returns:
        Findings that were new.
    """
    moment = _utc(now)
    config = load_config(db, account_id)
    if config.session_cost_threshold_usd is None:
        return []
    sessions = crud_spend_outlier_finding.session_spend_over(
        db,
        account_id=account_id,
        threshold_usd=config.session_cost_threshold_usd,
        active_since=moment - active_within,
    )
    candidates = [
        candidate
        for session in sessions
        if (candidate := session_candidate(session, config)) is not None
    ]
    return _store(db, account_id, candidates, moment)


def run_daily_pass(db: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """The scheduled daily pass over every account with spend yesterday.

    Also runs the session rule with a one-day activity window, so a session
    that crossed its threshold between periodic checks is still caught.

    Returns:
        Counts of accounts evaluated and findings recorded.
    """
    moment = _utc(now)
    day = moment.date() - timedelta(days=1)
    account_ids = set(
        crud_spend_outlier_finding.list_account_ids_with_gateway_spend(db, day=day)
    )
    recorded = 0
    for account_id in sorted(account_ids, key=str):
        try:
            recorded += len(evaluate_daily_rules(db, account_id, moment))
        except Exception:
            db.rollback()
            logger.exception("Spend outlier daily rules failed for %s", account_id)
    for (
        account_id
    ) in crud_spend_outlier_settings.list_account_ids_with_session_threshold(db):
        try:
            recorded += len(
                evaluate_session_rule(
                    db, account_id, moment, active_within=timedelta(days=1)
                )
            )
        except Exception:
            db.rollback()
            logger.exception("Spend outlier session rule failed for %s", account_id)
    return {"accounts": len(account_ids), "findings": recorded}


def run_session_pass(db: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """The periodic session check for accounts with a threshold set."""
    moment = _utc(now)
    account_ids = crud_spend_outlier_settings.list_account_ids_with_session_threshold(
        db
    )
    recorded = 0
    for account_id in account_ids:
        try:
            recorded += len(evaluate_session_rule(db, account_id, moment))
        except Exception:
            db.rollback()
            logger.exception("Spend outlier session rule failed for %s", account_id)
    return {"accounts": len(account_ids), "findings": recorded}


def _display_context(
    db: Session, account_id: UUID, findings: Sequence[models.SpendOutlierFinding]
) -> tuple[Dict[UUID, str], Dict[UUID, str]]:
    names = crud_spend_outlier_finding.user_display_names(
        db,
        account_id=account_id,
        user_ids=[finding.user_id for finding in findings if finding.user_id],
    )
    titles = crud_spend_outlier_finding.session_titles(
        db,
        account_id=account_id,
        session_ids=[
            finding.runtime_session_id
            for finding in findings
            if finding.runtime_session_id
        ],
    )
    return names, titles


def finding_summary(finding: models.SpendOutlierFinding, user_name: str) -> str:
    """One line a person can read: who, which rule, the numbers, the day."""
    details = finding.details or {}
    day = finding.day.isoformat()
    if finding.rule == SPEND_OUTLIER_RULE_DAILY:
        text = (
            f"{user_name} spent ${details.get('spend_usd', 0):.2f} on {day}, "
            f"{details.get('multiple', 0):.1f}x the 28-day median of "
            f"${details.get('median_usd', 0):.2f}"
        )
    elif finding.rule == SPEND_OUTLIER_RULE_MODEL_MIX:
        text = (
            f"{details.get('model', 'A top-tier model')} was "
            f"{float(details.get('share', 0)) * 100:.0f}% of {user_name}'s spend "
            f"on {day} and {float(details.get('previous_share', 0)) * 100:.0f}% "
            "the day before"
        )
    else:
        text = (
            f"A session by {user_name} cost ${details.get('spend_usd', 0):.2f}, "
            f"over the ${details.get('threshold_usd', 0):.2f} threshold ({day})"
        )
    imported = float(details.get("imported_usd") or 0)
    if imported > 0:
        sources = ", ".join(details.get("imported_sources") or []) or "imported"
        text += (
            f". Includes ${imported:.2f} of imported {sources} spend, "
            "not metered by the gateway"
        )
    return text


def serialize_finding(
    finding: models.SpendOutlierFinding,
    user_names: Mapping[UUID, str],
    session_titles: Mapping[UUID, str],
) -> Dict[str, Any]:
    """The shape the API and the digest share."""
    user_name = (
        user_names.get(finding.user_id, str(finding.user_id))
        if finding.user_id
        else "Unknown user"
    )
    return {
        "id": str(finding.id),
        "item_id": finding.item_id,
        "fingerprint": finding.fingerprint,
        "rule": finding.rule,
        "rule_label": RULE_LABELS.get(finding.rule, finding.rule),
        "user_id": str(finding.user_id) if finding.user_id else None,
        "user_name": user_name,
        "runtime_session_id": (
            str(finding.runtime_session_id) if finding.runtime_session_id else None
        ),
        "session_title": (
            session_titles.get(finding.runtime_session_id)
            if finding.runtime_session_id
            else None
        ),
        "day": finding.day.isoformat(),
        "detected_at": finding.detected_at.isoformat(),
        "details": dict(finding.details or {}),
        "summary": finding_summary(finding, user_name),
    }


def list_open_findings(
    db: Session, account_id: UUID, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """The findings the Attention page shows, newest first.

    One per item id: the latest finding for that rule and user (or session)
    detected within :data:`OPEN_WINDOW_DAYS`. Dismissal is applied by the
    console against ``attention_dismissal``, exactly as for other kinds.
    """
    moment = _utc(now)
    findings = crud_spend_outlier_finding.list_detected_since(
        db, account_id=account_id, since=moment - timedelta(days=OPEN_WINDOW_DAYS)
    )
    latest: Dict[str, models.SpendOutlierFinding] = {}
    for finding in findings:
        current = latest.get(finding.item_id)
        if current is None or (finding.day, finding.detected_at) >= (
            current.day,
            current.detected_at,
        ):
            latest[finding.item_id] = finding
    ordered = sorted(
        latest.values(),
        key=lambda item: (item.detected_at, item.day),
        reverse=True,
    )
    names, titles = _display_context(db, account_id, ordered)
    return [serialize_finding(finding, names, titles) for finding in ordered]


def _dismissed_in_digest(
    finding: models.SpendOutlierFinding,
    dismissal: Optional[models.AttentionDismissal],
) -> bool:
    if finding.dismissed_at is not None:
        return True
    if dismissal is None:
        return False
    if dismissal.fingerprint == finding.fingerprint:
        return True
    # A snooze hides later fingerprints of the same item until it runs out.
    snooze_until = dismissal.snooze_until
    if snooze_until is None:
        return False
    if snooze_until.tzinfo is None:
        snooze_until = snooze_until.replace(tzinfo=timezone.utc)
    detected_at = finding.detected_at
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)
    return detected_at < snooze_until


def build_spend_outlier_digest_section(
    db: Session,
    account_id: UUID,
    now: Optional[datetime] = None,
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The weekly digest's "Spend outliers" section for one account.

    Called by the digest plugin, which renders it. Lists each finding
    detected in the section's window once, including findings that were
    dismissed afterwards (``dismissed`` is True for those).

    Without explicit bounds the window is the :data:`DIGEST_WINDOW_DAYS`
    ending at ``now``. With ``start`` and ``end`` it is exactly that window.
    Both bounds are always applied, so a finding recorded at or after the
    end of the window is not in the section whatever the caller passed. The
    section covers one account: display names, session titles and
    dismissals are all resolved within ``account_id``, and a finding that
    points at another account's user or session shows no name at all rather
    than that account's label.

    Args:
        db: Database session.
        account_id: Account the digest is for.
        now: End of the default window; defaults to the current time.
        start: Inclusive window start, or None for the default window.
        end: Exclusive window end, or None for the default window.

    Returns:
        ``{"title", "window_start", "window_end", "items"}``. ``items`` is
        empty when nothing fired.

    Raises:
        ValueError: The bounds cannot describe one window; see
            :func:`preloop.utils.reporting_window.resolve_reporting_window`.
    """
    window_start, window_end = resolve_reporting_window(
        start=start,
        end=end,
        now=now,
        default_window=timedelta(days=DIGEST_WINDOW_DAYS),
        what="spend outlier digest window",
    )
    findings = crud_spend_outlier_finding.list_detected_since(
        db, account_id=account_id, since=window_start, until=window_end
    )
    unique: Dict[str, models.SpendOutlierFinding] = {}
    for finding in findings:
        unique.setdefault(finding.fingerprint, finding)
    dismissals: Dict[str, models.AttentionDismissal] = {
        dismissal.item_id: dismissal
        for dismissal in crud_attention_dismissal.get_active_for_account(
            db, account_id=account_id, now=window_end
        )
        if dismissal.item_id.startswith(f"{SPEND_ATTENTION_KIND}:")
    }
    ordered = list(unique.values())
    names, titles = _display_context(db, account_id, ordered)
    items: List[Dict[str, Any]] = []
    for finding in ordered:
        payload = serialize_finding(finding, names, titles)
        payload["dismissed"] = _dismissed_in_digest(
            finding, dismissals.get(finding.item_id)
        )
        items.append(payload)
    return {
        "title": "Spend outliers",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "items": items,
    }


def is_spend_item(item_id: str) -> bool:
    """Whether an attention item id belongs to a spend outlier card."""
    return item_id.startswith(f"{SPEND_ATTENTION_KIND}:")


def record_dismissal(
    db: Session,
    *,
    account_id: UUID,
    item_id: str,
    fingerprint: str,
    dismissed_at: Optional[datetime],
) -> None:
    """Keep a finding's ``dismissed_at`` in step with the dismissal API.

    Called by ``PUT``/``DELETE /attention/dismissals`` for spend items, so
    the digest can still say a finding was dismissed after the dismissal row
    has moved on to a later day's fingerprint.
    """
    if not is_spend_item(item_id):
        return
    crud_spend_outlier_finding.set_dismissed(
        db,
        account_id=account_id,
        item_id=item_id,
        fingerprint=fingerprint,
        dismissed_at=dismissed_at,
    )
