"""Daily import of Claude Code Analytics from the Anthropic Admin API (#1413).

Claude Code traffic that does not pass through the Preloop gateway (people
signed in with a subscription seat, keys used outside Preloop, sessions from
before rollout) is invisible to gateway metering. This module reads the
organization-level Claude Code Analytics report with an Admin API key and
writes one row per (actor, day, model) to ``provider_billing_snapshot`` with
``provider='anthropic_cc'``, ``usage_source='imported'``,
``line_item='claude_code_analytics'`` and ``cost_basis='estimated'``.

Contract:

* Rows are daily aggregates. They create no sessions and no ``api_usage``
  rows, and never feed budgets, ingestion quota or provider reconciliation.
* Never double count: an ``api_actor`` whose key name is a Preloop upstream
  credential (matched through ``GET /v1/organizations/api_keys``
  ``partial_key_hint``, or listed on the connection) is stored with
  ``raw.metered_by_gateway = true`` and excluded from totals. ``user_actor``
  rows are subscription or console OAuth usage the gateway never metered.
* ``raw`` is an allowlist (counters, tool actions, terminal, customer and
  subscription type, report date, provenance). The API carries no prompt
  text and nothing outside the allowlist is stored.
* The Admin key is resolved through the secret service, sent only in the
  ``x-api-key`` header and never logged or returned.

Only these Anthropic routes are called:

* ``GET /v1/organizations/usage_report/claude_code`` (one UTC day per call)
* ``GET /v1/organizations/api_keys`` (key names and hints for the overlap
  rule, and the cheap read the connection test uses)
"""

from __future__ import annotations

import logging
import time as time_module
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_anthropic_import_connection,
    crud_anthropic_usage,
    crud_anthropic_user_mapping,
    crud_gateway_subject,
    crud_secret_reference,
)
from preloop.models.crud.anthropic_import import (
    ANTHROPIC_CC_PROVIDER,
    API_KEY_ACTOR_PREFIX,
    LINE_ITEM_CLAUDE_CODE_ANALYTICS,
)

from preloop.models.crud.provider_billing import IMPORTED_USAGE_SOURCE
from preloop.services.copilot_usage_import import day_start, days_to_sync

logger = logging.getLogger(__name__)

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
CLAUDE_CODE_REPORT_PATH = "/v1/organizations/usage_report/claude_code"
API_KEYS_PATH = "/v1/organizations/api_keys"
#: Secret kind for the Admin API key stored by this import.
ANTHROPIC_IMPORT_SECRET_KIND = "anthropic_admin_import_key"
#: Provenance written into every row's ``raw``.
PROVENANCE = "anthropic_cc_analytics"
REPORT_PAGE_SIZE = 1000
MAX_REPORT_PAGES = 200
API_KEYS_PAGE_SIZE = 100
MAX_API_KEY_PAGES = 50
REQUEST_TIMEOUT_SECONDS = 30.0
#: Retries for a rate-limited request (429).
RATE_LIMIT_RETRIES = 3
#: Longest single wait honoured from ``Retry-After``.
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0

#: Marker attached to every figure that was not metered by the gateway.
NOT_METERED_MARKER = "Not metered by the gateway"

#: Shown in the API and the console panel; also in the docs.
NOT_ATTRIBUTABLE = [
    "Claude Enterprise and Team seats on claude.ai (web, Desktop and Cowork "
    "chat usage) are reported by a separate Enterprise Analytics API with a "
    "different key type and are not imported.",
    "The Claude Code Analytics API covers Claude Code on the Claude API only; "
    "usage through cloud provider platforms is not included.",
    "Imported rows are daily aggregates: no per-request tokens, sessions or "
    "transcripts are created, and they never feed budgets or ingestion quota.",
    "Priority Tier and code execution costs are not represented.",
]

HttpClientFactory = Callable[[], httpx.Client]


class AnthropicImportError(Exception):
    """A sync step failed; the message is safe to show on the Cost page."""


@dataclass
class AnthropicResponse:
    """Status and decoded JSON body of one Admin API call."""

    status: int
    body: Any


def _retry_after(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("retry-after")
    wait: Optional[float] = None
    if value is not None:
        try:
            wait = float(value)
        except ValueError:
            wait = None
    if wait is None:
        wait = float(2**attempt)
    return min(max(wait, 1.0), MAX_RATE_LIMIT_WAIT_SECONDS)


class AnthropicAdminClient:
    """Thin Admin API client with bounded ``Retry-After`` retries."""

    def __init__(
        self,
        http: httpx.Client,
        api_key: str,
        *,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        """Bind an HTTP client and the Admin key (never logged)."""
        self._http = http
        self._api_key = api_key
        self._sleep = sleep

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET one Admin API route and return the decoded body.

        Raises:
            AnthropicImportError: On a transport failure, a 401 or 403, a rate
                limit that outlasts the retries, or any other non-200.
        """
        attempt = 0
        while True:
            try:
                response = self._http.get(
                    f"{ANTHROPIC_API_BASE}{path}",
                    params=params,
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise AnthropicImportError(
                    f"Anthropic request to {path} failed: {type(exc).__name__}"
                ) from exc
            if response.status_code != 429:
                break
            if attempt >= RATE_LIMIT_RETRIES:
                raise AnthropicImportError(
                    f"Anthropic rate limited {path} after {RATE_LIMIT_RETRIES} "
                    "retries. The next run resumes from the last imported day."
                )
            wait = _retry_after(response, attempt)
            attempt += 1
            logger.info("Anthropic rate limited %s; retrying in %.0fs", path, wait)
            self._sleep(wait)
        if response.status_code == 401:
            raise AnthropicImportError(
                "Anthropic rejected the Admin API key (401). Check that it is an "
                "Admin key and has not been revoked."
            )
        if response.status_code == 403:
            raise AnthropicImportError(
                f"The Admin API key cannot read {path} (403). Use an Admin API "
                "key of the organization."
            )
        if response.status_code != 200:
            raise AnthropicImportError(
                f"Anthropic returned {response.status_code} for {path}."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AnthropicImportError(
                f"Anthropic returned a non-JSON body for {path}."
            ) from exc


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def latest_available_day(now: datetime) -> date:
    """Yesterday in UTC: the newest complete day (data lags about an hour)."""
    return now.astimezone(UTC).date() - timedelta(days=1)


def fetch_claude_code_day(client: AnthropicAdminClient, day: date) -> List[dict]:
    """Every Claude Code Analytics record of one UTC day, following pages."""
    records: List[dict] = []
    page: Optional[str] = None
    for _ in range(MAX_REPORT_PAGES):
        params: Dict[str, Any] = {
            "starting_at": day.isoformat(),
            "limit": REPORT_PAGE_SIZE,
        }
        if page:
            params["page"] = page
        body = client.get(CLAUDE_CODE_REPORT_PATH, params=params) or {}
        records.extend(
            item for item in body.get("data") or [] if isinstance(item, dict)
        )
        page = body.get("next_page")
        if not body.get("has_more") or not page:
            return records
    raise AnthropicImportError(
        f"The Claude Code report for {day.isoformat()} had more than "
        f"{MAX_REPORT_PAGES} pages."
    )


def fetch_api_keys(client: AnthropicAdminClient) -> List[dict]:
    """The organization's API keys (name and partial hint only are used)."""
    keys: List[dict] = []
    after: Optional[str] = None
    for _ in range(MAX_API_KEY_PAGES):
        params: Dict[str, Any] = {"limit": API_KEYS_PAGE_SIZE}
        if after:
            params["after_id"] = after
        body = client.get(API_KEYS_PATH, params=params) or {}
        keys.extend(item for item in body.get("data") or [] if isinstance(item, dict))
        after = body.get("last_id")
        if not body.get("has_more") or not after:
            break
    return keys


def test_connection(client: AnthropicAdminClient) -> None:
    """Cheapest read that proves the key works: one API key, one page."""
    client.get(API_KEYS_PATH, params={"limit": 1})


test_connection.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


def hint_matches(hint: Optional[str], key: Optional[str]) -> bool:
    """Whether a ``partial_key_hint`` such as ``sk-ant-api03-R2D...igAA`` fits.

    Both the visible prefix and suffix must match, and each must be present.
    """
    if not hint or not key or "..." not in hint:
        return False
    prefix, _, suffix = hint.partition("...")
    if not prefix or not suffix:
        return False
    return key.startswith(prefix) and key.endswith(suffix)


def upstream_key_values(db: Session, account_id: Any) -> List[str]:
    """Plain values of the account's Anthropic upstream keys (kept in memory)."""
    from preloop.services.secret_service import get_secret_service

    service = get_secret_service()
    values: List[str] = []
    for ai_model in crud_anthropic_import_connection.list_anthropic_upstream_models(
        db, account_id=account_id
    ):
        try:
            resolved = service.resolve_ai_model_api_key(ai_model)
        except Exception:  # noqa: BLE001 - one bad model must not stop the sync
            logger.warning("Could not resolve an Anthropic upstream key for matching")
            continue
        value = getattr(resolved, "value", None) if resolved else None
        if value:
            values.append(value)
    return values


def gateway_key_names(
    api_keys: Iterable[dict], upstream_keys: Iterable[str], listed: Iterable[str]
) -> Set[str]:
    """Anthropic key names that belong to Preloop upstream credentials."""
    names = {name.strip() for name in listed if isinstance(name, str) and name.strip()}
    upstream = list(upstream_keys)
    for key in api_keys:
        name = key.get("name")
        if isinstance(name, str) and any(
            hint_matches(key.get("partial_key_hint"), value) for value in upstream
        ):
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Record to rows
# ---------------------------------------------------------------------------


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _cents(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def actor_login(actor: Any) -> Optional[Tuple[str, str]]:
    """``(user_login, actor_type)`` for one record's actor, or None."""
    if not isinstance(actor, dict):
        return None
    kind = actor.get("type")
    if kind == "user_actor" and isinstance(actor.get("email_address"), str):
        email = actor["email_address"].strip().lower()
        return (email, kind) if email else None
    if kind == "api_actor" and isinstance(actor.get("api_key_name"), str):
        name = actor["api_key_name"].strip()
        return (f"{API_KEY_ACTOR_PREFIX}{name}", kind) if name else None
    return None


def _counters(record: dict) -> Dict[str, int]:
    core = record.get("core_metrics") or {}
    lines = core.get("lines_of_code") or {}
    return {
        "num_sessions": _int(core.get("num_sessions")),
        "lines_added": _int(lines.get("added")),
        "lines_removed": _int(lines.get("removed")),
        "commits_by_claude_code": _int(core.get("commits_by_claude_code")),
        "pull_requests_by_claude_code": _int(core.get("pull_requests_by_claude_code")),
    }


_TOOLS = ("edit_tool", "multi_edit_tool", "write_tool", "notebook_edit_tool")


def _tool_actions(record: dict) -> Dict[str, Dict[str, int]]:
    actions = record.get("tool_actions") or {}
    result: Dict[str, Dict[str, int]] = {}
    for tool in _TOOLS:
        entry = actions.get(tool) or {}
        result[tool] = {
            "accepted": _int(entry.get("accepted")),
            "rejected": _int(entry.get("rejected")),
        }
    return result


def _short(value: Any) -> Optional[str]:
    return value[:64] if isinstance(value, str) and value else None


def build_day_rows(
    records: Iterable[dict],
    *,
    day: date,
    gateway_names: Set[str],
    fetched_at: datetime,
) -> List[Dict[str, Any]]:
    """Turn one day's records into snapshot rows, one per (actor, model).

    An actor can appear in several records of a day (one per terminal), so
    tokens and cost are summed per (actor, model) and the actor's counters
    are summed per actor. The counters are stored on every model row of the
    actor; readers take them once per actor and day.
    """
    bucket_start = day_start(day)
    per_actor: Dict[str, Dict[str, Any]] = {}
    for record in records:
        identity = actor_login(record.get("actor"))
        if identity is None:
            continue
        login, kind = identity
        entry = per_actor.setdefault(
            login,
            {
                "actor_type": kind,
                "counters": {},
                "tool_actions": {t: {"accepted": 0, "rejected": 0} for t in _TOOLS},
                "terminal_types": set(),
                "customer_type": None,
                "subscription_type": None,
                "models": {},
            },
        )
        for name, value in _counters(record).items():
            entry["counters"][name] = entry["counters"].get(name, 0) + value
        for tool, counts in _tool_actions(record).items():
            for outcome, value in counts.items():
                entry["tool_actions"][tool][outcome] += value
        terminal = _short(record.get("terminal_type"))
        if terminal:
            entry["terminal_types"].add(terminal)
        entry["customer_type"] = (
            _short(record.get("customer_type")) or entry["customer_type"]
        )
        entry["subscription_type"] = (
            _short(record.get("subscription_type")) or entry["subscription_type"]
        )
        for breakdown in record.get("model_breakdown") or []:
            if not isinstance(breakdown, dict):
                continue
            model = _short(breakdown.get("model")) or "unknown"
            tokens = breakdown.get("tokens") or {}
            cost = breakdown.get("estimated_cost") or {}
            bucket = entry["models"].setdefault(
                model,
                {
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_creation": 0,
                    "cents": 0.0,
                },
            )
            for token_kind in ("input", "output", "cache_read", "cache_creation"):
                bucket[token_kind] += _int(tokens.get(token_kind))
            bucket["cents"] += _cents(cost.get("amount"))

    rows: List[Dict[str, Any]] = []
    for login, entry in per_actor.items():
        metered = entry["actor_type"] == "api_actor" and (
            login[len(API_KEY_ACTOR_PREFIX) :] in gateway_names
        )
        raw_common = {
            "source": PROVENANCE,
            "report_date": day.isoformat(),
            "actor_type": entry["actor_type"],
            "core_metrics": entry["counters"],
            "tool_actions": entry["tool_actions"],
            "terminal_types": sorted(entry["terminal_types"]),
            "customer_type": entry["customer_type"],
            "subscription_type": entry["subscription_type"],
            "metered_by_gateway": metered,
        }
        model_items = list(entry["models"].items()) or [(None, None)]
        for model, bucket in model_items:
            rows.append(
                {
                    "provider": ANTHROPIC_CC_PROVIDER,
                    "granularity": "1d",
                    "bucket_start": bucket_start,
                    "bucket_end": bucket_start + timedelta(days=1),
                    "line_item": LINE_ITEM_CLAUDE_CODE_ANALYTICS,
                    "user_login": login,
                    "model": model,
                    "usage_source": IMPORTED_USAGE_SOURCE,
                    "cost_basis": "estimated" if bucket else None,
                    "cost_amount": (bucket["cents"] / 100.0) if bucket else None,
                    "currency": "USD",
                    "uncached_input_tokens": bucket["input"] if bucket else None,
                    "cached_input_tokens": bucket["cache_read"] if bucket else None,
                    "cache_creation_tokens": (
                        bucket["cache_creation"] if bucket else None
                    ),
                    "output_tokens": bucket["output"] if bucket else None,
                    "raw": dict(raw_common),
                    "fetched_at": fetched_at,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def resolve_admin_key(
    db: Session, connection: models.AnthropicImportConnection
) -> Optional[str]:
    """The stored Admin key, resolved through the secret service."""
    from preloop.services.secret_service import get_secret_service

    secret = crud_secret_reference.get_for_account(
        db,
        secret_id=str(connection.secret_reference_id),
        account_id=str(connection.account_id),
    )
    if secret is None:
        return None
    return get_secret_service().resolve_secret_reference(secret).value


def _default_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "preloop-anthropic-usage-import"},
    )


def sync_connection(
    db: Session,
    connection: models.AnthropicImportConnection,
    *,
    now: Optional[datetime] = None,
    http_client_factory: HttpClientFactory = _default_http_client,
    sleep: Callable[[float], None] = time_module.sleep,
) -> Dict[str, Any]:
    """Run one sync for a connection and record the outcome on it.

    Never raises for an Anthropic failure: the message goes to
    ``last_error`` and the next run resumes after the last imported day.

    Returns:
        A summary with ``days``, ``rows``, ``error`` and ``warning``.
    """
    now = now or datetime.now(UTC)
    days = days_to_sync(connection.last_synced_day, latest_available_day(now))
    synced: List[date] = []
    written = 0
    error: Optional[str] = None
    warnings: List[str] = []
    try:
        admin_key = resolve_admin_key(db, connection)
        if not admin_key:
            raise AnthropicImportError("The Admin API key is missing.")
        with http_client_factory() as http:
            client = AnthropicAdminClient(http, admin_key, sleep=sleep)
            listed = connection.gateway_key_names or []
            try:
                api_keys = fetch_api_keys(client)
            except AnthropicImportError as exc:
                api_keys = []
                warnings.append(
                    "Could not list the organization's API keys, so only the key "
                    f"names listed on the connection count as gateway keys ({exc})"
                )
            names = gateway_key_names(
                api_keys, upstream_key_values(db, connection.account_id), listed
            )
            for day in days:
                records = fetch_claude_code_day(client, day)
                rows = build_day_rows(
                    records, day=day, gateway_names=names, fetched_at=now
                )
                written += crud_anthropic_usage.replace_day_rows(
                    db,
                    account_id=connection.account_id,
                    provider=ANTHROPIC_CC_PROVIDER,
                    bucket_start=day_start(day),
                    line_items=(LINE_ITEM_CLAUDE_CODE_ANALYTICS,),
                    rows=rows,
                )
                synced.append(day)
    except AnthropicImportError as exc:
        db.rollback()
        error = str(exc)
        logger.warning(
            "Anthropic usage import failed for account %s: %s",
            connection.account_id,
            error,
        )
    crud_anthropic_import_connection.record_sync(
        db,
        connection=connection,
        synced_at=now,
        synced_day=synced[-1] if synced else None,
        error=error,
        warning=" ".join(warnings) or None,
    )
    return {
        "days": [day.isoformat() for day in synced],
        "rows": written,
        "error": error,
        "warning": " ".join(warnings) or None,
    }


def ingest_anthropic_usage(
    db: Session,
    *,
    account_id: Optional[str] = None,
    now: Optional[datetime] = None,
    http_client_factory: HttpClientFactory = _default_http_client,
    sleep: Callable[[float], None] = time_module.sleep,
) -> Dict[str, Dict[str, Any]]:
    """Sync every active connection, or one account's. No-op without one."""
    if account_id:
        connection = crud_anthropic_import_connection.get_for_account(
            db, account_id=account_id
        )
        connections = [connection] if connection and connection.is_active else []
    else:
        connections = crud_anthropic_import_connection.list_active(db)
    results: Dict[str, Dict[str, Any]] = {}
    for connection in connections:
        results[str(connection.account_id)] = sync_connection(
            db,
            connection,
            now=now,
            http_client_factory=http_client_factory,
            sleep=sleep,
        )
    return results


# ---------------------------------------------------------------------------
# Cost page summary
# ---------------------------------------------------------------------------


def _tokens(row: models.ProviderBillingSnapshot) -> int:
    return sum(
        int(value or 0)
        for value in (
            row.uncached_input_tokens,
            row.cached_input_tokens,
            row.cache_creation_tokens,
            row.output_tokens,
        )
    )


def build_anthropic_summary(
    db: Session, *, account_id: str, start: datetime, end: datetime
) -> Dict[str, Any]:
    """Assemble the Anthropic section of the Cost page for one window.

    Rows flagged ``metered_by_gateway`` are reported separately and never
    enter the totals, so gateway-metered usage is not counted twice.
    Identity mapping is read only: an explicit mapping wins, then an
    account member with the actor's email; a gateway subject with that email
    is shown when one exists. Nothing is created.
    """
    connection = crud_anthropic_import_connection.get_for_account(
        db, account_id=account_id
    )
    rows = crud_anthropic_usage.list_rows(
        db,
        account_id=account_id,
        provider=ANTHROPIC_CC_PROVIDER,
        start=start,
        end=end,
    )
    explicit = (
        crud_anthropic_user_mapping.resolve_user_ids(db, connection=connection)
        if connection
        else {}
    )
    emails = {
        row.user_login
        for row in rows
        if row.user_login and not row.user_login.startswith(API_KEY_ACTOR_PREFIX)
    }
    subjects = crud_anthropic_usage.subjects_by_email(
        db, account_id=account_id, emails=emails
    )

    total_cost = 0.0
    total_tokens = 0
    excluded_cost = 0.0
    excluded_tokens = 0
    excluded_actors: Set[str] = set()
    by_actor: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    counted_actor_days: Set[Tuple[str, datetime]] = set()
    for row in rows:
        raw = row.raw or {}
        login = row.user_login or "unknown"
        cost = float(row.cost_amount or 0.0)
        tokens = _tokens(row)
        if raw.get("metered_by_gateway"):
            excluded_cost += cost
            excluded_tokens += tokens
            excluded_actors.add(login)
            continue
        total_cost += cost
        total_tokens += tokens
        actor = by_actor.setdefault(
            login,
            {
                "actor": login,
                "actor_type": raw.get("actor_type"),
                "estimated_cost": 0.0,
                "tokens": 0,
                "num_sessions": 0,
                "lines_added": 0,
                "lines_removed": 0,
                "commits": 0,
                "pull_requests": 0,
                "days": 0,
            },
        )
        actor["estimated_cost"] += cost
        actor["tokens"] += tokens
        if (login, row.bucket_start) not in counted_actor_days:
            counted_actor_days.add((login, row.bucket_start))
            core = raw.get("core_metrics") or {}
            actor["num_sessions"] += _int(core.get("num_sessions"))
            actor["lines_added"] += _int(core.get("lines_added"))
            actor["lines_removed"] += _int(core.get("lines_removed"))
            actor["commits"] += _int(core.get("commits_by_claude_code"))
            actor["pull_requests"] += _int(core.get("pull_requests_by_claude_code"))
            actor["days"] += 1
        if row.model:
            model = by_model.setdefault(
                row.model, {"model": row.model, "estimated_cost": 0.0, "tokens": 0}
            )
            model["estimated_cost"] += cost
            model["tokens"] += tokens

    for login, actor in by_actor.items():
        user_id = explicit.get(login)
        source = "mapping" if user_id else None
        if user_id is None and not login.startswith(API_KEY_ACTOR_PREFIX):
            user_id = crud_gateway_subject.find_member_by_email(
                db, account_id=account_id, email=login
            )
            source = "member_email" if user_id else None
        subject = subjects.get(login)
        actor["user_id"] = user_id
        actor["mapping_source"] = source
        actor["gateway_subject_id"] = subject.id if subject else None

    return {
        "metered_by_gateway": False,
        "marker": NOT_METERED_MARKER,
        "period_start": start,
        "period_end": end,
        "connection": connection_payload(connection) if connection else None,
        "total_estimated_cost": total_cost if rows else None,
        "total_tokens": total_tokens,
        "currency": "USD",
        "excluded_metered_by_gateway": {
            "estimated_cost": excluded_cost,
            "tokens": excluded_tokens,
            "actors": sorted(excluded_actors),
        },
        "by_actor": sorted(
            by_actor.values(), key=lambda a: (-a["estimated_cost"], a["actor"])
        ),
        "by_model": sorted(
            by_model.values(), key=lambda m: (-m["estimated_cost"], m["model"])
        ),
        "not_attributable": list(NOT_ATTRIBUTABLE),
    }


def connection_payload(connection: models.AnthropicImportConnection) -> Dict[str, Any]:
    """Serialize a connection without any key material (a 4-char hint only)."""
    return {
        "id": connection.id,
        "has_key": connection.secret_reference_id is not None,
        "key_hint": connection.key_hint,
        "gateway_key_names": list(connection.gateway_key_names or []),
        "is_active": connection.is_active,
        "last_synced_at": connection.last_synced_at,
        "last_synced_day": connection.last_synced_day,
        "last_error": connection.last_error,
        "last_warning": connection.last_warning,
    }
