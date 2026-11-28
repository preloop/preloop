"""The CI health ledger a flow keeps across runs.

Stored as flow state under :data:`LEDGER_KEY`. The collector updates it on
every call; the agent changes only an entry's ``status``, ``linked`` and
``note`` through the tool's ``ledger_updates`` argument.

Shape (version 1)::

    {
      "version": 1,
      "first_run_at": ISO, "last_run_at": ISO,
      "cursors": {"github:owner/name": ISO of the newest run already counted},
      "signatures": {id: {
          "framework", "test", "error", "repositories": [...],
          "first_seen", "last_seen", "count", "flaky": bool,
          "commits": [last 5 shas], "runs": [last 5 run urls],
          "jobs": [job names], "status": open|fixed|ignored|regressed|quiet,
          "linked": [urls], "note": str}},
      "history": [{"at", "execution_id", per-repo totals and gates}],
      "pull_requests": [{"url", "execution_id", "opened_at", "state",
                         "merged_at", "estimated_minutes_saved_per_week"}]
    }

Counting is idempotent: a run counts once, when it is newer than the
repository's cursor, so overlapping windows never double-count.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

LEDGER_KEY = "preloop.ci_health"
VERSION = 1
MAX_SIGNATURES = 150
MAX_HISTORY = 100
MAX_PULL_REQUESTS = 60
RECENT = 5
#: An open signature not seen for this long becomes ``quiet``.
QUIET_AFTER = timedelta(days=7)
STATUSES = ("open", "fixed", "ignored", "regressed", "quiet")
#: Statuses an agent (or person) may set through ``ledger_updates``.
SETTABLE_STATUSES = ("open", "fixed", "ignored")
NOTE_CHARS = 280


def empty(now: datetime) -> dict[str, Any]:
    """A fresh ledger.

    Args:
        now: Creation time.

    Returns:
        The ledger document.
    """
    stamp = now.isoformat()
    return {
        "version": VERSION,
        "first_run_at": stamp,
        "last_run_at": None,
        "cursors": {},
        "counted_runs": {},
        "signatures": {},
        "history": [],
        "pull_requests": [],
    }


def load(value: Any, now: datetime) -> dict[str, Any]:
    """Return a usable ledger from a stored value.

    Args:
        value: The stored flow-state value (None when absent).
        now: Current time, used for a fresh ledger.

    Returns:
        The ledger; an unknown or damaged shape starts over.
    """
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return empty(now)
    ledger = empty(now)
    ledger.update({k: value[k] for k in ledger if k in value})
    for key, kind in (
        ("cursors", dict),
        ("counted_runs", dict),
        ("signatures", dict),
        ("history", list),
        ("pull_requests", list),
    ):
        if not isinstance(ledger[key], kind):
            ledger[key] = kind()
    return ledger


def _push(items: list[Any], value: Any) -> list[Any]:
    if value and value not in items:
        items = [*items, value]
    return items[-RECENT:]


def record_failure(
    ledger: dict[str, Any],
    *,
    signature: Mapping[str, str],
    repository: str,
    job: str,
    head_sha: str,
    run_url: str,
    seen_at: datetime,
) -> dict[str, Any]:
    """Count one occurrence of a signature.

    Args:
        ledger: Ledger to update in place.
        signature: ``{"id", "framework", "test", "error"}``.
        repository: ``provider:path`` of the repository.
        job: Base job name.
        head_sha: Commit the run tested.
        run_url: Link to the run.
        seen_at: When the run was created.

    Returns:
        The updated entry.
    """
    entries = ledger["signatures"]
    stamp = seen_at.isoformat()
    entry = entries.get(signature["id"])
    if entry is None:
        entry = {
            "framework": signature["framework"],
            "test": signature["test"],
            "error": signature.get("error", ""),
            "repositories": [],
            "first_seen": stamp,
            "last_seen": stamp,
            "count": 0,
            "flaky": False,
            "commits": [],
            "runs": [],
            "jobs": [],
            "status": "open",
            "linked": [],
            "note": "",
        }
        entries[signature["id"]] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last_seen"] = max(str(entry.get("last_seen") or ""), stamp)
    entry["first_seen"] = min(str(entry.get("first_seen") or stamp), stamp)
    entry["repositories"] = sorted(set(entry.get("repositories") or []) | {repository})
    entry["commits"] = _push(entry.get("commits") or [], head_sha)
    entry["runs"] = _push(entry.get("runs") or [], run_url)
    entry["jobs"] = sorted(set(entry.get("jobs") or []) | {job})[:10]
    if entry.get("status") in ("fixed", "quiet"):
        entry["status"] = "regressed" if entry["status"] == "fixed" else "open"
    return entry


def mark_flaky(
    ledger: dict[str, Any], flaky: Mapping[tuple[str, str], Iterable[str]]
) -> None:
    """Flag signatures whose job both failed and passed on one commit.

    Args:
        ledger: Ledger to update.
        flaky: ``{(workflow, base job): shas}`` from the aggregator.
    """
    flaky_jobs = {job: set(shas) for (_wf, job), shas in flaky.items()}
    for entry in ledger["signatures"].values():
        for job in entry.get("jobs") or []:
            if flaky_jobs.get(job, set()) & set(entry.get("commits") or []):
                entry["flaky"] = True


def age(ledger: dict[str, Any], now: datetime) -> None:
    """Mark long-silent open signatures ``quiet`` and trim to the cap.

    Args:
        ledger: Ledger to update.
        now: Current time.
    """
    entries = ledger["signatures"]
    for entry in entries.values():
        last = _time(entry.get("last_seen"))
        if (
            entry.get("status") in ("open", "regressed")
            and last
            and (now - last > QUIET_AFTER)
        ):
            entry["status"] = "quiet"
    if len(entries) > MAX_SIGNATURES:
        order = sorted(
            entries,
            key=lambda k: (
                entries[k].get("status") in ("open", "regressed"),
                str(entries[k].get("last_seen") or ""),
            ),
        )
        for key in order[: len(entries) - MAX_SIGNATURES]:
            del entries[key]


def apply_updates(
    ledger: dict[str, Any], updates: Iterable[Mapping[str, Any]]
) -> list[dict[str, str]]:
    """Apply an agent's status, link and note changes.

    Args:
        ledger: Ledger to update.
        updates: ``{"signature_id", "status"?, "linked"?, "note"?}`` items.

    Returns:
        Refusals as ``{"signature_id", "reason"}``.
    """
    refused = []
    for update in updates:
        key = str(update.get("signature_id") or "")
        entry = ledger["signatures"].get(key)
        if entry is None:
            refused.append({"signature_id": key, "reason": "unknown signature"})
            continue
        status = update.get("status")
        if status is not None:
            if status not in SETTABLE_STATUSES:
                refused.append({"signature_id": key, "reason": "invalid status"})
                continue
            entry["status"] = status
        linked = update.get("linked")
        if isinstance(linked, str) and linked.startswith(("https://", "http://")):
            entry["linked"] = _push(entry.get("linked") or [], linked[:300])
        note = update.get("note")
        if isinstance(note, str):
            entry["note"] = note[:NOTE_CHARS]
    return refused


def record_pull_requests(
    ledger: dict[str, Any], published: Iterable[Mapping[str, Any]]
) -> None:
    """Remember pull requests the flow's earlier runs published.

    Args:
        ledger: Ledger to update.
        published: ``{"url", "execution_id", "opened_at",
            "estimated_minutes_saved_per_week"?, "signatures"?}`` items.
    """
    known = {pr.get("url") for pr in ledger["pull_requests"]}
    for item in published:
        url = item.get("url")
        if not url or url in known:
            continue
        ledger["pull_requests"].append(
            {
                "url": url,
                "execution_id": item.get("execution_id"),
                "opened_at": item.get("opened_at"),
                "state": "open",
                "merged_at": None,
                "estimated_minutes_saved_per_week": item.get(
                    "estimated_minutes_saved_per_week"
                ),
                "signatures": list(item.get("signatures") or [])[:10],
            }
        )
        known.add(url)
    ledger["pull_requests"] = ledger["pull_requests"][-MAX_PULL_REQUESTS:]


def append_history(ledger: dict[str, Any], point: dict[str, Any]) -> None:
    """Add one run's headline numbers.

    Args:
        ledger: Ledger to update.
        point: ``{"at", "execution_id", "repositories": {...}}``.
    """
    ledger["history"] = [*ledger["history"], point][-MAX_HISTORY:]


def summary(ledger: dict[str, Any], *, top: int = 15) -> dict[str, Any]:
    """The part of the ledger worth showing an agent.

    Args:
        ledger: The ledger.
        top: Entries listed.

    Returns:
        Counts per status plus the most relevant entries (regressed and flaky
        first, then by count).
    """
    entries = ledger["signatures"]
    counts = {status: 0 for status in STATUSES}
    for entry in entries.values():
        counts[entry.get("status", "open")] = counts.get(entry.get("status"), 0) + 1
    ranked = sorted(
        entries.items(),
        key=lambda kv: (
            kv[1].get("status") not in ("regressed", "open"),
            kv[1].get("status") != "regressed",
            not kv[1].get("flaky"),
            -int(kv[1].get("count") or 0),
        ),
    )
    return {
        "counts": counts,
        "flaky": sum(1 for e in entries.values() if e.get("flaky")),
        "top": [
            {"id": key, **{k: v for k, v in entry.items() if k != "runs"}}
            for key, entry in ranked[:top]
        ],
        "pull_requests": ledger["pull_requests"][-10:],
        "first_run_at": ledger.get("first_run_at"),
    }


def _time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def cursor(ledger: dict[str, Any], repository: str) -> Optional[datetime]:
    """The newest run already counted for ``repository``.

    Args:
        ledger: The ledger.
        repository: ``provider:path``.

    Returns:
        The cursor time or None.
    """
    return _time(ledger["cursors"].get(repository))


def fit(ledger: dict[str, Any], max_bytes: int, size: Any) -> dict[str, Any]:
    """Trim the ledger until ``size(ledger) <= max_bytes``.

    Drops, in order: half of the remembered run ids, half of the history,
    then the least relevant signatures. Counts of kept signatures are never
    changed.

    Args:
        ledger: Ledger to trim in place.
        max_bytes: Size cap.
        size: Callable returning the encoded size of a value.

    Returns:
        The ledger.
    """
    for _ in range(12):
        if size(ledger) <= max_bytes:
            break
        counted = ledger.get("counted_runs") or {}
        if any(len(v) > 200 for v in counted.values()):
            for key, ids in counted.items():
                counted[key] = ids[len(ids) // 2 :]
            continue
        if len(ledger["history"]) > 20:
            ledger["history"] = ledger["history"][len(ledger["history"]) // 2 :]
            continue
        entries = ledger["signatures"]
        keep = max(10, len(entries) // 2)
        newest = sorted(
            entries, key=lambda k: str(entries[k].get("last_seen") or ""), reverse=True
        )
        ranked = sorted(
            newest,
            key=lambda k: entries[k].get("status") not in ("open", "regressed"),
        )
        ledger["signatures"] = {k: entries[k] for k in ranked[:keep]}
    return ledger
