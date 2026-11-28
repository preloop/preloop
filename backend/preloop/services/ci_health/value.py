"""The CI health view and the weekly value summary of one flow.

Both are read models over data that already exists: the ledger the
collector keeps in flow state, and the flow's executions (status, tokens,
estimated cost, published pull request). Nothing here calls a provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

from . import ledger as ledger_mod

TREND_POINTS = 60
TOP_FLAKES = 10
SERIES_FIELDS = (
    "success_rate",
    "queue_p95_s",
    "slowest_gate_p95_s",
    "runner_minutes",
    "rerun_rate",
)


def _time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _aware(value: Optional[datetime], like: datetime) -> Optional[datetime]:
    """Compare naive database times with aware ones as UTC."""
    if value is None:
        return None
    if value.tzinfo is None and like.tzinfo is not None:
        return value.replace(tzinfo=like.tzinfo)
    return value


def _in(value: Any, start: datetime, end: datetime) -> bool:
    moment = _aware(_time(value), start)
    return moment is not None and start <= moment < end


def repository_trend(history: list[dict[str, Any]], now: datetime, days: int) -> dict:
    """Latest numbers per repository, the value ``days`` ago, and a series.

    Args:
        history: Ledger history points, oldest first.
        now: Current time.
        days: Comparison distance.

    Returns:
        ``{repo: {"latest": {...}, "prior": {...} | None, "series": [...]}}``.
    """
    horizon = now - timedelta(days=days)
    result: dict[str, Any] = {}
    for point in history[-TREND_POINTS * 4 :]:
        at = _aware(_time(point.get("at")), now)
        for repo, numbers in (point.get("repositories") or {}).items():
            if not isinstance(numbers, Mapping):
                continue
            row = result.setdefault(repo, {"latest": None, "prior": None, "series": []})
            entry = {
                "at": point.get("at"),
                **{k: numbers.get(k) for k in SERIES_FIELDS},
            }
            entry["slowest_gate"] = numbers.get("slowest_gate")
            row["series"].append(entry)
            row["latest"] = entry
            if at is not None and at <= horizon:
                row["prior"] = entry
    for row in result.values():
        row["series"] = row["series"][-TREND_POINTS:]
    return result


def top_flakes(ledger: Mapping[str, Any], limit: int = TOP_FLAKES) -> list[dict]:
    """The signatures most worth a look: regressed, flaky or open, by count.

    Args:
        ledger: The ledger.
        limit: Rows returned.

    Returns:
        Rows without run URLs.
    """
    entries = ledger.get("signatures") or {}
    live = [
        (key, entry)
        for key, entry in entries.items()
        if entry.get("status") in ("open", "regressed")
    ]
    live.sort(
        key=lambda kv: (
            kv[1].get("status") != "regressed",
            not kv[1].get("flaky"),
            -int(kv[1].get("count") or 0),
        )
    )
    keep = (
        "framework",
        "test",
        "error",
        "count",
        "flaky",
        "status",
        "first_seen",
        "last_seen",
        "linked",
        "repositories",
        "jobs",
    )
    return [{"id": key, **{k: e.get(k) for k in keep}} for key, e in live[:limit]]


def weekly_value(
    ledger: Mapping[str, Any],
    outcomes: Iterable[Mapping[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """What the flow cost and delivered between ``start`` and ``end``.

    Args:
        ledger: The ledger (pull requests and signature statuses).
        outcomes: :meth:`crud_flow_execution.list_outcomes` rows.
        start: Window start.
        end: Window end.

    Returns:
        Runs by status, tokens, estimated cost, pull requests opened and
        merged, failures fixed and regressed, the runner minutes per week
        that merged changes claim to save (an estimate, as reported by the
        change), and cost per merged pull request.
    """
    runs = {"total": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    tokens = 0
    cost = 0.0
    for row in outcomes:
        if not _in(row.get("start_time"), start, end):
            continue
        status = str(row.get("status") or "").upper()
        if status == "SKIPPED":
            runs["skipped"] += 1
            continue
        runs["total"] += 1
        if status == "SUCCEEDED":
            runs["succeeded"] += 1
        elif status in ("FAILED", "TIMEOUT", "TIMED_OUT", "ABORTED"):
            runs["failed"] += 1
        tokens += int(row.get("total_tokens") or 0)
        cost += float(row.get("estimated_cost") or 0.0)

    prs = ledger.get("pull_requests") or []
    opened = [pr for pr in prs if _in(pr.get("opened_at"), start, end)]
    merged = [pr for pr in prs if _in(pr.get("merged_at"), start, end)]
    saved = [
        float(pr["estimated_minutes_saved_per_week"])
        for pr in prs
        if pr.get("state") == "merged"
        and isinstance(pr.get("estimated_minutes_saved_per_week"), (int, float))
    ]
    signatures = (ledger.get("signatures") or {}).values()
    fixed = sum(
        1
        for e in signatures
        if e.get("status") == "fixed" and _in(e.get("status_at"), start, end)
    )
    regressed = sum(
        1
        for e in signatures
        if e.get("status") == "regressed" and _in(e.get("status_at"), start, end)
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "runs": runs,
        "tokens": tokens,
        "cost_usd_est": round(cost, 4),
        "pull_requests_opened": len(opened),
        "pull_requests_merged": len(merged),
        "failures_fixed": fixed,
        "failures_regressed": regressed,
        "runner_minutes_saved_per_week_est": round(sum(saved), 1) if saved else None,
        "cost_per_merged_pull_request_usd_est": round(cost / len(merged), 4)
        if merged
        else None,
        "tokens_per_merged_pull_request": tokens // len(merged) if merged else None,
    }


def health_view(
    ledger_value: Any,
    outcomes: list[Mapping[str, Any]],
    *,
    now: datetime,
    days: int = 7,
) -> dict[str, Any]:
    """Everything the console CI health card shows.

    Args:
        ledger_value: Stored ledger.
        outcomes: The flow's recent execution outcome rows.
        now: Current time.
        days: Length of the value window and the trend comparison.

    Returns:
        ``repositories`` (trend), ``top_flakes``, ``ledger`` counts,
        ``week`` and ``prior_week`` value summaries, and ``pull_requests``.
    """
    ledger = ledger_mod.load(ledger_value, now)
    summary = ledger_mod.summary(ledger, top=0)
    week_start = now - timedelta(days=days)
    return {
        "generated_at": now.isoformat(),
        "days": days,
        "first_run_at": ledger.get("first_run_at"),
        "last_run_at": ledger.get("last_run_at"),
        "ledger": {"counts": summary["counts"], "flaky": summary["flaky"]},
        "repositories": repository_trend(ledger["history"], now, days),
        "top_flakes": top_flakes(ledger),
        "week": weekly_value(ledger, outcomes, start=week_start, end=now),
        "prior_week": weekly_value(
            ledger,
            outcomes,
            start=week_start - timedelta(days=days),
            end=week_start,
        ),
        "pull_requests": list(reversed(ledger["pull_requests"][-10:])),
    }
