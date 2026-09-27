"""Daily import of GitHub Copilot seats, premium-request spend and metrics.

GitHub-hosted Copilot traffic never passes through the Preloop gateway, so
none of these numbers are gateway usage. Everything this module writes goes to
``provider_billing_snapshot`` with ``provider='copilot'`` and
``usage_source='imported'``. Those rows never feed gateway totals, budgets,
ingestion quota or provider reconciliation, and the Cost page labels them
"not metered by the gateway".

Only these GitHub REST routes are called (API version ``2026-03-10``):

* ``GET /orgs/{org}/copilot/billing`` and ``/copilot/billing/seats`` (seats)
* ``GET /organizations/{org}/settings/billing/premium_request/usage``
* ``GET /enterprises/{enterprise}/settings/billing/premium_request/usage``
* ``GET /orgs/{org}/copilot/metrics/reports/users-1-day`` plus the signed
  report download links it returns

The legacy ``/orgs/{org}/copilot/metrics`` route is never called, and no route
here returns prompt text. Usage-metrics records are reduced to an allowlist of
counters, editors, features and models before they are stored.
"""

from __future__ import annotations

import json
import logging
import time as time_module
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_copilot_import_connection,
    crud_copilot_usage,
    crud_secret_reference,
)
from preloop.models.crud.copilot_import import (
    COPILOT_PROVIDER,
    LINE_ITEM_PREMIUM_REQUEST,
    LINE_ITEM_SEAT,
    LINE_ITEM_SEAT_SUMMARY,
    LINE_ITEM_USAGE_METRICS,
)
from preloop.models.crud.provider_billing import IMPORTED_USAGE_SOURCE

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
#: Secret kind for tokens stored by this import.
COPILOT_IMPORT_SECRET_KIND = "copilot_import_token"
#: A day's usage-metrics report is available within two full UTC days after
#: the day closes, so on day T the newest complete day is T - 3.
FRESHNESS_LAG_DAYS = 3
#: How many missed days one run catches up on after downtime.
MAX_CATCHUP_DAYS = 7
SEATS_PAGE_SIZE = 100
MAX_SEAT_PAGES = 100
REQUEST_TIMEOUT_SECONDS = 30.0
#: Retries for a rate-limited request (429, or 403 with rate-limit headers).
RATE_LIMIT_RETRIES = 3
#: Longest single wait honoured from ``Retry-After`` or the reset header.
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0
#: Statuses that mean "this token cannot read this route" rather than an
#: outage. The caller falls back to the next route or to the aggregate.
_NOT_READABLE_STATUSES = frozenset({403, 404})

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"

#: Marker attached to every Copilot figure the API returns.
NOT_METERED_MARKER = "Not metered by the gateway"

HttpClientFactory = Callable[[], httpx.Client]


class CopilotImportError(Exception):
    """A sync step failed; the message is safe to show on the Cost page."""


@dataclass
class GitHubResponse:
    """Status and decoded JSON body of one GitHub API call."""

    status: int
    body: Any


def _header_seconds(value: Optional[str]) -> Optional[float]:
    """Parse a numeric rate-limit header, or None when absent or malformed.

    Args:
        value: Raw header value (``Retry-After`` seconds or
            ``x-ratelimit-reset`` epoch seconds).

    Returns:
        The number, or None so the caller falls back to backoff.
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        logger.debug("Ignoring non-numeric GitHub rate-limit header %r", value)
        return None


class GitHubCopilotClient:
    """Thin GitHub REST client for the Copilot import routes."""

    def __init__(
        self,
        http: httpx.Client,
        token: str,
        *,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        """Bind an HTTP client and a bearer token.

        Args:
            http: Shared HTTP client (its transport is swapped in tests).
            token: GitHub token; never logged.
            sleep: Waits between rate-limit retries (replaced in tests).
        """
        self._http = http
        self._token = token
        self._sleep = sleep

    @staticmethod
    def _rate_limit_wait(response: httpx.Response, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None when not rate limited.

        GitHub signals primary and secondary rate limits with 429, or with a
        403 that carries ``Retry-After`` or ``x-ratelimit-remaining: 0``. A
        plain 403 (missing permission) is not retried.
        """
        headers = response.headers
        limited = response.status_code == 429 or (
            response.status_code == 403
            and (
                "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
            )
        )
        if not limited:
            return None
        wait = _header_seconds(headers.get("retry-after"))
        if wait is None:
            reset = _header_seconds(headers.get("x-ratelimit-reset"))
            if reset is not None:
                wait = reset - datetime.now(UTC).timestamp()
        if wait is None:
            # No usable header: exponential backoff.
            wait = float(2**attempt)
        return min(max(wait, 1.0), MAX_RATE_LIMIT_WAIT_SECONDS)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> GitHubResponse:
        """GET one API route.

        Args:
            path: Route path starting with ``/``.
            params: Query parameters.

        Returns:
            The status and decoded body (``None`` for an empty body).

        Raises:
            CopilotImportError: On a transport failure, a 401, or a rate
                limit that outlasts the retries.
        """
        attempt = 0
        while True:
            try:
                response = self._http.get(
                    f"{GITHUB_API_BASE}{path}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": GITHUB_API_VERSION,
                    },
                )
            except httpx.HTTPError as exc:
                raise CopilotImportError(
                    f"GitHub request to {path} failed: {type(exc).__name__}"
                ) from exc
            wait = self._rate_limit_wait(response, attempt)
            if wait is None:
                break
            if attempt >= RATE_LIMIT_RETRIES:
                raise CopilotImportError(
                    f"GitHub rate limited {path} ({response.status_code}) after "
                    f"{RATE_LIMIT_RETRIES} retries. The next run resumes from "
                    "the last fully imported day."
                )
            attempt += 1
            logger.info("GitHub rate limited %s; retrying in %.0fs", path, wait)
            self._sleep(wait)
        if response.status_code == 401:
            raise CopilotImportError(
                "GitHub rejected the token (401). Check that it has not expired "
                "or been revoked."
            )
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return GitHubResponse(status=response.status_code, body=body)

    def download(self, url: str) -> str:
        """Fetch one signed report download link.

        The link is pre-signed by GitHub and usually points at a storage host,
        so the GitHub token is deliberately NOT sent with it.

        Args:
            url: Signed ``https`` URL from ``download_links``.

        Returns:
            The report text.

        Raises:
            CopilotImportError: For a non-https link or a failed download.
        """
        if urlparse(url).scheme != "https":
            raise CopilotImportError("Refusing a non-https report download link.")
        try:
            response = self._http.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise CopilotImportError(
                f"Usage-metrics report download failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise CopilotImportError(
                f"Usage-metrics report download returned {response.status_code}."
            )
        return response.text


def _unexpected(response: GitHubResponse, what: str) -> CopilotImportError:
    message = ""
    if isinstance(response.body, dict) and isinstance(
        response.body.get("message"), str
    ):
        message = f": {response.body['message'][:200]}"
    return CopilotImportError(f"GitHub returned {response.status} for {what}{message}")


# ---------------------------------------------------------------------------
# Day window
# ---------------------------------------------------------------------------


def latest_available_day(now: datetime) -> date:
    """Return the newest report day GitHub has finished processing.

    Args:
        now: Current time (timezone-aware).

    Returns:
        ``now``'s UTC date minus :data:`FRESHNESS_LAG_DAYS`.
    """
    return now.astimezone(UTC).date() - timedelta(days=FRESHNESS_LAG_DAYS)


def days_to_sync(last_synced_day: Optional[date], available_day: date) -> List[date]:
    """Choose the report days one run imports.

    The first run imports only ``available_day``. Later runs resume after
    ``last_synced_day`` (at most :data:`MAX_CATCHUP_DAYS`). When nothing new
    is available the latest day is imported again, which is idempotent and
    is what a manual re-sync does.

    Args:
        last_synced_day: Last fully imported day, if any.
        available_day: Newest available day.

    Returns:
        Days in ascending order, never empty.
    """
    if last_synced_day is None or last_synced_day >= available_day:
        return [available_day]
    first = max(
        last_synced_day + timedelta(days=1),
        available_day - timedelta(days=MAX_CATCHUP_DAYS - 1),
    )
    count = (available_day - first).days + 1
    return [first + timedelta(days=offset) for offset in range(count)]


def day_start(day: date) -> datetime:
    """UTC midnight of ``day``."""
    return datetime.combine(day, time.min, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------


@dataclass
class SeatInfo:
    """One assigned Copilot seat, reduced to the fields Preloop stores."""

    login: str
    last_activity_at: Optional[str]
    last_activity_editor: Optional[str]
    created_at: Optional[str]
    pending_cancellation_date: Optional[str]
    plan_type: Optional[str]


def fetch_seat_summary(client: GitHubCopilotClient, org: str) -> Dict[str, Any]:
    """Read ``GET /orgs/{org}/copilot/billing``.

    Args:
        client: GitHub client with the organization token.
        org: Organization login.

    Returns:
        ``seat_breakdown`` and ``plan_type`` (no seat price: GitHub does not
        return one).

    Raises:
        CopilotImportError: When the route is not readable.
    """
    response = client.get(f"/orgs/{quote(org, safe='')}/copilot/billing")
    if response.status == 200 and isinstance(response.body, dict):
        breakdown = response.body.get("seat_breakdown") or {}
        return {
            "seat_breakdown": breakdown if isinstance(breakdown, dict) else {},
            "plan_type": response.body.get("plan_type"),
        }
    if response.status in _NOT_READABLE_STATUSES:
        raise CopilotImportError(
            f"GitHub returned {response.status} for Copilot seat information. "
            "Seats need an organization owner token (classic scopes "
            "manage_billing:copilot or read:org) on an organization with "
            "Copilot Business or Copilot Enterprise."
        )
    raise _unexpected(response, "Copilot seat information")


def fetch_seats(client: GitHubCopilotClient, org: str) -> Tuple[List[SeatInfo], int]:
    """List every assigned seat via ``GET /orgs/{org}/copilot/billing/seats``.

    Args:
        client: GitHub client with the organization token.
        org: Organization login.

    Returns:
        The seats (login and activity only; names and emails are dropped)
        and the ``total_seats`` GitHub reported.

    Raises:
        CopilotImportError: When the route is not readable.
    """
    seats: List[SeatInfo] = []
    total_seats = 0
    for page in range(1, MAX_SEAT_PAGES + 1):
        response = client.get(
            f"/orgs/{quote(org, safe='')}/copilot/billing/seats",
            {"per_page": SEATS_PAGE_SIZE, "page": page},
        )
        if response.status != 200 or not isinstance(response.body, dict):
            if response.status in _NOT_READABLE_STATUSES:
                raise CopilotImportError(
                    f"GitHub returned {response.status} listing Copilot seats. "
                    "Seat assignments need an organization owner token."
                )
            raise _unexpected(response, "the Copilot seat list")
        total_seats = int(response.body.get("total_seats") or 0)
        page_seats = response.body.get("seats") or []
        for seat in page_seats:
            assignee = seat.get("assignee") if isinstance(seat, dict) else None
            login = assignee.get("login") if isinstance(assignee, dict) else None
            if not login:
                continue
            seats.append(
                SeatInfo(
                    login=str(login),
                    last_activity_at=seat.get("last_activity_at"),
                    last_activity_editor=seat.get("last_activity_editor"),
                    created_at=seat.get("created_at"),
                    pending_cancellation_date=seat.get("pending_cancellation_date"),
                    plan_type=seat.get("plan_type"),
                )
            )
        if len(page_seats) < SEATS_PAGE_SIZE or len(seats) >= total_seats:
            break
    return seats, total_seats


# ---------------------------------------------------------------------------
# Premium requests
# ---------------------------------------------------------------------------


@dataclass
class PremiumRequestResult:
    """Premium-request rows for one day and how they were obtained."""

    rows: List[Dict[str, Any]]
    per_user: bool
    scope: str
    reason: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class _Route:
    scope: str
    client: GitHubCopilotClient
    path: str
    extra: Dict[str, Any] = field(default_factory=dict)


def _premium_routes(
    org_client: GitHubCopilotClient,
    enterprise_client: Optional[GitHubCopilotClient],
    org: str,
    enterprise: Optional[str],
) -> List[_Route]:
    routes = [
        _Route(
            scope="organization",
            client=org_client,
            path=(
                f"/organizations/{quote(org, safe='')}"
                "/settings/billing/premium_request/usage"
            ),
        )
    ]
    if enterprise:
        routes.append(
            _Route(
                scope="enterprise",
                client=enterprise_client or org_client,
                path=(
                    f"/enterprises/{quote(enterprise, safe='')}"
                    "/settings/billing/premium_request/usage"
                ),
                # Restrict the enterprise report to this organization so
                # spend from sibling organizations is never imported here.
                extra={"organization": org},
            )
        )
    return routes


def _usage_by_model(body: Any) -> Dict[str, Dict[str, Any]]:
    """Group ``usageItems`` by model, summing quantities and amounts."""
    grouped: Dict[str, Dict[str, Any]] = {}
    items = body.get("usageItems") if isinstance(body, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "unknown")
        entry = grouped.setdefault(
            model,
            {
                "netAmount": 0.0,
                "netQuantity": 0.0,
                "grossAmount": 0.0,
                "grossQuantity": 0.0,
                "discountAmount": 0.0,
                "pricePerUnit": None,
                "skus": [],
                "products": [],
            },
        )
        for key in (
            "netAmount",
            "netQuantity",
            "grossAmount",
            "grossQuantity",
            "discountAmount",
        ):
            value = item.get(key)
            if isinstance(value, (int, float)):
                entry[key] += float(value)
        price = item.get("pricePerUnit")
        if isinstance(price, (int, float)):
            current = entry["pricePerUnit"]
            entry["pricePerUnit"] = (
                float(price) if current is None else max(current, float(price))
            )
        for key, target in (("sku", "skus"), ("product", "products")):
            value = item.get(key)
            if value and value not in entry[target]:
                entry[target].append(value)
    return grouped


def _premium_rows(
    body: Any,
    *,
    day: date,
    org: str,
    user_login: Optional[str],
    scope: str,
    fetched_at: datetime,
    per_user_unavailable_reason: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows = []
    for model, usage in _usage_by_model(body).items():
        raw: Dict[str, Any] = {**usage, "scope": scope}
        if per_user_unavailable_reason:
            raw["per_user_unavailable_reason"] = per_user_unavailable_reason
        rows.append(
            {
                "provider": COPILOT_PROVIDER,
                "granularity": "1d",
                "bucket_start": day_start(day),
                "bucket_end": day_start(day + timedelta(days=1)),
                "model": model,
                "line_item": LINE_ITEM_PREMIUM_REQUEST,
                "project_or_workspace_id": org,
                "user_login": user_login,
                "usage_source": IMPORTED_USAGE_SOURCE,
                "cost_basis": "reconciled",
                "cost_amount": usage["netAmount"],
                "currency": "USD",
                "raw": raw,
                "fetched_at": fetched_at,
            }
        )
    return rows


#: Residual spend below this (in dollars) is rounding, not a missing user.
_RESIDUAL_AMOUNT_EPSILON = 0.005
#: Residual request count below this is rounding.
_RESIDUAL_QUANTITY_EPSILON = 0.5


def _residual_rows(
    aggregate_body: Any,
    per_user_rows: List[Dict[str, Any]],
    *,
    day: date,
    org: str,
    scope: str,
    fetched_at: datetime,
) -> List[Dict[str, Any]]:
    """Rows for spend in the organization total that no queried user explains.

    GitHub bills a day's premium requests to whoever used them, including a
    developer whose seat was removed before the import ran. Those developers
    are no longer in the seat list, so their spend is kept as an
    ``unattributed`` organization row and the day still adds up to GitHub's
    bill.
    """
    attributed: Dict[str, Dict[str, float]] = {}
    for row in per_user_rows:
        entry = attributed.setdefault(row["model"], {"amount": 0.0, "quantity": 0.0})
        entry["amount"] += float(row["cost_amount"] or 0.0)
        entry["quantity"] += float(row["raw"].get("netQuantity") or 0.0)
    rows = []
    for model, usage in _usage_by_model(aggregate_body).items():
        seen = attributed.get(model, {"amount": 0.0, "quantity": 0.0})
        amount = usage["netAmount"] - seen["amount"]
        quantity = usage["netQuantity"] - seen["quantity"]
        if amount < _RESIDUAL_AMOUNT_EPSILON and quantity < _RESIDUAL_QUANTITY_EPSILON:
            continue
        rows.append(
            {
                "provider": COPILOT_PROVIDER,
                "granularity": "1d",
                "bucket_start": day_start(day),
                "bucket_end": day_start(day + timedelta(days=1)),
                "model": model,
                "line_item": LINE_ITEM_PREMIUM_REQUEST,
                "project_or_workspace_id": org,
                "user_login": None,
                "usage_source": IMPORTED_USAGE_SOURCE,
                "cost_basis": "reconciled",
                "cost_amount": max(amount, 0.0),
                "currency": "USD",
                "raw": {
                    "netAmount": max(amount, 0.0),
                    "netQuantity": max(quantity, 0.0),
                    "pricePerUnit": usage["pricePerUnit"],
                    "skus": usage["skus"],
                    "products": usage["products"],
                    "scope": scope,
                    "unattributed": True,
                },
                "fetched_at": fetched_at,
            }
        )
    return rows


def active_logins(seats: List[SeatInfo], day: date) -> List[str]:
    """Seated logins that could have used Copilot on ``day``.

    A seat whose ``last_activity_at`` is before ``day`` (or that has never
    been active) cannot have premium requests on that day, so it is not
    queried. Spend the filter misses still shows up as the unattributed
    residual, so the day's total always matches GitHub's.

    Args:
        seats: Assigned seats from the seat list.
        day: Report day.

    Returns:
        Logins to query, in seat-list order.
    """
    logins = []
    for seat in seats:
        if not seat.last_activity_at:
            continue
        try:
            last = datetime.fromisoformat(seat.last_activity_at.replace("Z", "+00:00"))
        except ValueError:
            # Unknown format: query the user rather than risk missing spend.
            logins.append(seat.login)
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last.astimezone(UTC).date() >= day:
            logins.append(seat.login)
    return logins


def fetch_premium_requests(
    *,
    org_client: GitHubCopilotClient,
    enterprise_client: Optional[GitHubCopilotClient],
    org: str,
    enterprise: Optional[str],
    day: date,
    logins: List[str],
    fetched_at: datetime,
    has_seats: bool = True,
) -> PremiumRequestResult:
    """Import one day of premium-request spend.

    Per-user rows come from one call per active seated user on the
    organization route, then the enterprise route (when an enterprise slug is
    set). The same route's organization total is then read once, and any
    spend no queried user explains is stored as an ``unattributed`` row, so
    per-user rows never silently add up to less than GitHub's bill.

    If neither route answers per user, the organization total (a call without
    ``user``) is stored instead with the reason kept beside it. Nothing is
    ever split per user without a per-user answer from GitHub.

    Args:
        org_client: Client with the organization token.
        enterprise_client: Client with the enterprise billing token, if any.
        org: Organization login.
        enterprise: Enterprise slug, if configured.
        day: Report day.
        logins: Seated users active on or after ``day``.
        fetched_at: Timestamp stored on the rows.
        has_seats: Whether GitHub listed any assigned seats at all.

    Returns:
        The rows, whether they are per-user, and any warning.

    Raises:
        CopilotImportError: When not even the aggregate is readable, or on an
            unexpected GitHub error.
    """
    base = {"year": day.year, "month": day.month, "day": day.day}
    routes = _premium_routes(org_client, enterprise_client, org, enterprise)
    refusals: List[str] = []

    for route in routes if logins else []:
        first = route.client.get(route.path, {**base, **route.extra, "user": logins[0]})
        if first.status in _NOT_READABLE_STATUSES:
            refusals.append(f"the {route.scope} route returned {first.status}")
            continue
        if first.status != 200:
            raise _unexpected(first, f"{route.scope} premium-request usage")
        rows = _premium_rows(
            first.body,
            day=day,
            org=org,
            user_login=logins[0],
            scope=route.scope,
            fetched_at=fetched_at,
        )
        for login in logins[1:]:
            response = route.client.get(
                route.path, {**base, **route.extra, "user": login}
            )
            if response.status == 404:
                # The user left the organization after the seat list was
                # read; their spend lands in the unattributed residual.
                continue
            if response.status != 200:
                raise _unexpected(
                    response, f"{route.scope} premium-request usage for a user"
                )
            rows.extend(
                _premium_rows(
                    response.body,
                    day=day,
                    org=org,
                    user_login=login,
                    scope=route.scope,
                    fetched_at=fetched_at,
                )
            )
        warning = None
        total = route.client.get(route.path, {**base, **route.extra})
        if total.status == 200:
            rows.extend(
                _residual_rows(
                    total.body,
                    rows,
                    day=day,
                    org=org,
                    scope=route.scope,
                    fetched_at=fetched_at,
                )
            )
        elif total.status in _NOT_READABLE_STATUSES:
            warning = (
                f"The {route.scope} premium-request total returned "
                f"{total.status}, so spend by developers who no longer hold a "
                "seat could not be checked."
            )
        else:
            raise _unexpected(total, f"{route.scope} premium-request usage")
        return PremiumRequestResult(
            rows=rows, per_user=True, scope=route.scope, warning=warning
        )

    if logins:
        reason: Optional[str] = (
            "Per-user premium-request spend is unavailable: " + "; ".join(refusals)
        )
        if not enterprise:
            reason = (
                f"{reason}. For an organization owned by an enterprise, "
                "configure the enterprise slug and an enterprise billing reader "
                "token."
            )
        else:
            reason = f"{reason}."
    elif has_seats:
        # Seats exist but none was active on the day: any spend GitHub still
        # reports belongs to former seat holders and is kept unattributed.
        reason = None
    else:
        reason = (
            "Per-user premium-request spend is unavailable: GitHub listed no "
            "assigned Copilot seats to query."
        )

    for route in routes:
        response = route.client.get(route.path, {**base, **route.extra})
        if response.status in _NOT_READABLE_STATUSES:
            refusals.append(f"the {route.scope} aggregate returned {response.status}")
            continue
        if response.status != 200:
            raise _unexpected(response, f"{route.scope} premium-request usage")
        if reason is None:
            return PremiumRequestResult(
                rows=_residual_rows(
                    response.body,
                    [],
                    day=day,
                    org=org,
                    scope=route.scope,
                    fetched_at=fetched_at,
                ),
                per_user=True,
                scope=route.scope,
            )
        return PremiumRequestResult(
            rows=_premium_rows(
                response.body,
                day=day,
                org=org,
                user_login=None,
                scope=route.scope,
                fetched_at=fetched_at,
                per_user_unavailable_reason=reason,
            ),
            per_user=False,
            scope=route.scope,
            reason=reason,
        )
    raise CopilotImportError(
        "Premium-request spend could not be read: "
        + "; ".join(refusals)
        + ". The organization route needs an organization administrator token; "
        "the enterprise route needs an enterprise admin, billing manager or a "
        "token with enterprise billing read access. A Copilot metrics "
        "permission does not grant billing access."
    )


# ---------------------------------------------------------------------------
# Usage metrics (adoption, not dollars)
# ---------------------------------------------------------------------------


@dataclass
class MetricsResult:
    """Per-user usage-metrics rows for one day, or why there are none."""

    rows: List[Dict[str, Any]]
    available: bool
    reason: Optional[str] = None


def parse_report(text: str) -> List[Dict[str, Any]]:
    """Parse a usage-metrics report file (NDJSON, or one JSON array).

    Args:
        text: Downloaded report body.

    Returns:
        One dict per record; malformed lines are skipped.
    """
    stripped = text.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [record for record in parsed if isinstance(record, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    records = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


_COUNT_FIELDS = (
    "user_initiated_interaction_count",
    "code_generation_activity_count",
    "code_acceptance_activity_count",
)


def _counts(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: entry[key]
        for key in _COUNT_FIELDS
        if isinstance(entry.get(key), (int, float))
    }


def reduce_metrics_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only counters, editors, features and models from one record.

    An explicit allowlist, so nothing free-form from the report is stored.

    Args:
        record: One per-user usage-metrics record.

    Returns:
        The reduced record.
    """
    editors = sorted(
        {
            str(entry["ide"])
            for entry in record.get("totals_by_ide") or []
            if isinstance(entry, dict) and entry.get("ide")
        }
    )
    features = [
        {"feature": str(entry["feature"]), **_counts(entry)}
        for entry in record.get("totals_by_feature") or []
        if isinstance(entry, dict) and entry.get("feature")
    ]
    model_features = [
        {
            "model": str(entry["model"]),
            "feature": str(entry.get("feature") or ""),
            **_counts(entry),
        }
        for entry in record.get("totals_by_model_feature") or []
        if isinstance(entry, dict) and entry.get("model")
    ]
    reduced: Dict[str, Any] = {
        **_counts(record),
        "editors": editors,
        "features": features,
        "models": model_features,
    }
    credits = record.get("ai_credits_used")
    if isinstance(credits, (int, float)):
        reduced["ai_credits_used"] = credits
    return reduced


def fetch_user_metrics(
    client: GitHubCopilotClient,
    *,
    org: str,
    day: date,
    fetched_at: datetime,
) -> MetricsResult:
    """Import the per-user usage-metrics report for one day.

    A missing report never fails the sync: the metrics are adoption data,
    and the reason is surfaced on the Cost page instead.

    Args:
        client: Client with a token that can read Copilot metrics.
        org: Organization login.
        day: Report day.
        fetched_at: Timestamp stored on the rows.

    Returns:
        The rows, or ``available=False`` and a reason.
    """
    response = client.get(
        f"/orgs/{quote(org, safe='')}/copilot/metrics/reports/users-1-day",
        {"day": day.isoformat()},
    )
    if response.status == 204:
        return MetricsResult(rows=[], available=True)
    if response.status == 403:
        return MetricsResult(
            rows=[],
            available=False,
            reason=(
                "GitHub returned 403 for the usage-metrics report. The "
                "enterprise 'Copilot usage metrics' policy must be enabled and "
                "the token needs organization owner or 'View Organization "
                "Copilot Metrics' access (classic scope read:org)."
            ),
        )
    if response.status == 404:
        return MetricsResult(
            rows=[],
            available=False,
            reason=(
                "GitHub returned 404 for the usage-metrics report. Organization "
                "reports start on 12 December 2025 and need the 'Copilot usage "
                "metrics' policy."
            ),
        )
    if response.status != 200 or not isinstance(response.body, dict):
        return MetricsResult(
            rows=[],
            available=False,
            reason=f"GitHub returned {response.status} for the usage-metrics report.",
        )
    links = response.body.get("download_links") or []
    rows: List[Dict[str, Any]] = []
    try:
        for link in links:
            for record in parse_report(client.download(str(link))):
                login = record.get("user_login")
                if not login:
                    continue
                rows.append(
                    {
                        "provider": COPILOT_PROVIDER,
                        "granularity": "1d",
                        "bucket_start": day_start(day),
                        "bucket_end": day_start(day + timedelta(days=1)),
                        "line_item": LINE_ITEM_USAGE_METRICS,
                        "project_or_workspace_id": org,
                        "user_login": str(login),
                        "usage_source": IMPORTED_USAGE_SOURCE,
                        "cost_amount": None,
                        "raw": reduce_metrics_record(record),
                        "fetched_at": fetched_at,
                    }
                )
    except CopilotImportError as exc:
        return MetricsResult(rows=[], available=False, reason=str(exc))
    return MetricsResult(rows=rows, available=True)


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------


def _seat_rows(
    *,
    seats: List[SeatInfo],
    summary: Dict[str, Any],
    total_seats: int,
    org: str,
    day: date,
    fetched_at: datetime,
) -> List[Dict[str, Any]]:
    common = {
        "provider": COPILOT_PROVIDER,
        "granularity": "1d",
        "bucket_start": day_start(day),
        "bucket_end": day_start(day + timedelta(days=1)),
        "project_or_workspace_id": org,
        "usage_source": IMPORTED_USAGE_SOURCE,
        "cost_amount": None,
        "fetched_at": fetched_at,
    }
    breakdown = summary.get("seat_breakdown") or {}
    reported_total = breakdown.get("total")
    rows: List[Dict[str, Any]] = [
        {
            **common,
            "line_item": LINE_ITEM_SEAT_SUMMARY,
            "raw": {
                "total_seats": (
                    int(reported_total)
                    if isinstance(reported_total, int)
                    else total_seats
                ),
                "seat_breakdown": breakdown,
                "plan_type": summary.get("plan_type"),
            },
        }
    ]
    for seat in seats:
        rows.append(
            {
                **common,
                "line_item": LINE_ITEM_SEAT,
                "user_login": seat.login,
                "raw": {
                    "last_activity_at": seat.last_activity_at,
                    "last_activity_editor": seat.last_activity_editor,
                    "created_at": seat.created_at,
                    "pending_cancellation_date": seat.pending_cancellation_date,
                    "plan_type": seat.plan_type,
                },
            }
        )
    return rows


def _resolve_token(db: Session, secret_id: Any, account_id: Any) -> Optional[str]:
    if secret_id is None:
        return None
    from preloop.services.secret_service import get_secret_service

    secret = crud_secret_reference.get_for_account(
        db, secret_id=str(secret_id), account_id=str(account_id)
    )
    if secret is None:
        return None
    return get_secret_service().resolve_secret_reference(secret).value


def _default_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "preloop-copilot-usage-import"},
    )


def sync_connection(
    db: Session,
    connection: models.CopilotImportConnection,
    *,
    now: Optional[datetime] = None,
    http_client_factory: HttpClientFactory = _default_http_client,
) -> Dict[str, Any]:
    """Run one sync for a connection and record the outcome on it.

    Seats are refreshed for today's date; premium requests and usage metrics
    are imported for each day chosen by :func:`days_to_sync`. Each day's rows
    replace that day's previous rows, so re-running is idempotent. A failure
    is recorded in ``last_error`` with a message the Cost page shows as is.

    Args:
        db: Database session.
        connection: The connection to sync.
        now: Current time (injected by tests).
        http_client_factory: Builds the HTTP client (tests pass a mock
            transport).

    Returns:
        A summary with ``days``, ``per_user``, ``error`` and ``warning``.
    """
    now = now or datetime.now(UTC)
    available_day = latest_available_day(now)
    days = days_to_sync(connection.last_synced_day, available_day)
    org = connection.organization
    synced: List[date] = []
    per_user_status: Optional[str] = None
    per_user_reason: Optional[str] = None
    metrics_status: Optional[str] = None
    metrics_reason: Optional[str] = None
    error: Optional[str] = None
    warnings: List[str] = []
    try:
        org_token = _resolve_token(
            db, connection.secret_reference_id, connection.account_id
        )
        if not org_token:
            raise CopilotImportError("The organization token is missing.")
        enterprise_token = _resolve_token(
            db, connection.enterprise_secret_reference_id, connection.account_id
        )
        with http_client_factory() as http:
            org_client = GitHubCopilotClient(http, org_token)
            enterprise_client = (
                GitHubCopilotClient(http, enterprise_token)
                if enterprise_token
                else None
            )
            summary = fetch_seat_summary(org_client, org)
            seats, total_seats = fetch_seats(org_client, org)
            today = now.astimezone(UTC).date()
            crud_copilot_usage.replace_day_rows(
                db,
                account_id=connection.account_id,
                bucket_start=day_start(today),
                line_items=(LINE_ITEM_SEAT, LINE_ITEM_SEAT_SUMMARY),
                rows=_seat_rows(
                    seats=seats,
                    summary=summary,
                    total_seats=total_seats,
                    org=org,
                    day=today,
                    fetched_at=now,
                ),
            )
            if total_seats > len(seats):
                warnings.append(
                    f"GitHub reported {total_seats} seats but only {len(seats)} "
                    "were listed, so some developers are counted as unattributed "
                    "spend."
                )
            for day in days:
                premium = fetch_premium_requests(
                    org_client=org_client,
                    enterprise_client=enterprise_client,
                    org=org,
                    enterprise=connection.enterprise,
                    day=day,
                    logins=active_logins(seats, day),
                    fetched_at=now,
                    has_seats=bool(seats),
                )
                if premium.warning and premium.warning not in warnings:
                    warnings.append(premium.warning)
                metrics = fetch_user_metrics(
                    org_client, org=org, day=day, fetched_at=now
                )
                crud_copilot_usage.replace_day_rows(
                    db,
                    account_id=connection.account_id,
                    bucket_start=day_start(day),
                    line_items=(LINE_ITEM_PREMIUM_REQUEST,),
                    rows=premium.rows,
                )
                if metrics.available:
                    crud_copilot_usage.replace_day_rows(
                        db,
                        account_id=connection.account_id,
                        bucket_start=day_start(day),
                        line_items=(LINE_ITEM_USAGE_METRICS,),
                        rows=metrics.rows,
                    )
                per_user_status = (
                    STATUS_AVAILABLE if premium.per_user else STATUS_UNAVAILABLE
                )
                per_user_reason = premium.reason
                metrics_status = (
                    STATUS_AVAILABLE if metrics.available else STATUS_UNAVAILABLE
                )
                metrics_reason = metrics.reason
                synced.append(day)
    except CopilotImportError as exc:
        db.rollback()
        error = str(exc)
        logger.warning(
            "Copilot usage import failed for account %s: %s",
            connection.account_id,
            error,
        )
    crud_copilot_import_connection.record_sync(
        db,
        connection=connection,
        synced_at=now,
        synced_day=synced[-1] if synced else None,
        error=error,
        per_user_billing_status=per_user_status,
        per_user_billing_reason=per_user_reason,
        metrics_status=metrics_status,
        metrics_reason=metrics_reason,
        warning=" ".join(warnings) or None,
    )
    return {
        "days": [day.isoformat() for day in synced],
        "per_user": per_user_status == STATUS_AVAILABLE if per_user_status else None,
        "error": error,
        "warning": " ".join(warnings) or None,
    }


def ingest_copilot_usage(
    db: Session,
    *,
    account_id: Optional[str] = None,
    now: Optional[datetime] = None,
    http_client_factory: HttpClientFactory = _default_http_client,
) -> Dict[str, Dict[str, Any]]:
    """Sync every active connection, or one account's.

    Args:
        db: Database session.
        account_id: Restrict to one account.
        now: Current time (injected by tests).
        http_client_factory: Builds the HTTP client.

    Returns:
        Per-account sync summaries keyed by account id.
    """
    if account_id:
        connection = crud_copilot_import_connection.get_for_account(
            db, account_id=account_id
        )
        connections = [connection] if connection and connection.is_active else []
    else:
        connections = crud_copilot_import_connection.list_active(db)
    results: Dict[str, Dict[str, Any]] = {}
    for connection in connections:
        results[str(connection.account_id)] = sync_connection(
            db, connection, now=now, http_client_factory=http_client_factory
        )
    return results


# ---------------------------------------------------------------------------
# Cost page summary
# ---------------------------------------------------------------------------


def _share_rows(values: Dict[str, float]) -> List[Dict[str, Any]]:
    total = sum(values.values())
    if total <= 0:
        return []
    return [
        {"model": model, "value": value, "share": value / total}
        for model, value in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        if value > 0
    ]


def _metrics_requests_by_user_model(
    rows: Iterable[models.ProviderBillingSnapshot],
) -> Dict[str, Dict[str, float]]:
    by_user: Dict[str, Dict[str, float]] = {}
    for row in rows:
        if not row.user_login:
            continue
        raw = row.raw or {}
        for entry in raw.get("models") or []:
            count = entry.get("user_initiated_interaction_count")
            if not isinstance(count, (int, float)) or not entry.get("model"):
                continue
            models_for_user = by_user.setdefault(row.user_login, {})
            models_for_user[entry["model"]] = models_for_user.get(
                entry["model"], 0.0
            ) + float(count)
    return by_user


def build_copilot_summary(
    db: Session,
    *,
    account_id: str,
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    """Assemble the Copilot section of the Cost page for one window.

    Nothing here is merged into gateway totals. The seat estimate is only
    computed when the operator entered a seat price; otherwise it is None so
    the page shows seats without a dollar seat line. While a connection
    exists only its organization's rows are read, so rows kept from a
    previously configured organization never mix into the current one.

    Args:
        db: Database session.
        account_id: Account id.
        start: Window start (inclusive).
        end: Window end (exclusive).

    Returns:
        A dict matching ``CopilotUsageSummaryResponse``.
    """
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account_id
    )
    organization = connection.organization if connection else None
    totals = crud_copilot_usage.premium_request_totals(
        db,
        account_id=account_id,
        start=start,
        end=end,
        organization=organization,
    )
    premium_rows = crud_copilot_usage.list_rows(
        db,
        account_id=account_id,
        line_item=LINE_ITEM_PREMIUM_REQUEST,
        start=start,
        end=end,
        organization=organization,
    )
    # Two kinds of rows have no login: the organization total stored when
    # GitHub refused per-user answers, and the unattributed residual of a
    # per-user day (spend by developers who no longer hold a seat).
    aggregate_rows = [
        row
        for row in premium_rows
        if not row.user_login and not (row.raw or {}).get("unattributed")
    ]
    unattributed_rows = [
        row
        for row in premium_rows
        if not row.user_login and (row.raw or {}).get("unattributed")
    ]
    aggregate_days = {row.bucket_start for row in aggregate_rows}
    org_aggregate_amount = sum(float(row.cost_amount or 0) for row in aggregate_rows)
    unattributed_amount = sum(float(row.cost_amount or 0) for row in unattributed_rows)

    by_developer: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    amount_by_user_model: Dict[str, Dict[str, float]] = {}
    total_amount = 0.0
    for row in totals:
        amount = row["net_amount"] or 0.0
        quantity = row["net_quantity"] or 0.0
        model = row["model"] or "unknown"
        total_amount += amount
        model_entry = by_model.setdefault(
            model, {"model": model, "net_amount": 0.0, "net_quantity": 0.0}
        )
        model_entry["net_amount"] += amount
        model_entry["net_quantity"] += quantity
        login = row["user_login"]
        if not login:
            continue
        developer = by_developer.setdefault(
            login, {"login": login, "net_amount": 0.0, "net_quantity": 0.0}
        )
        developer["net_amount"] += amount
        developer["net_quantity"] += quantity
        amount_by_user_model.setdefault(login, {})[model] = (
            amount_by_user_model.get(login, {}).get(model, 0.0) + amount
        )

    metrics_rows = crud_copilot_usage.list_rows(
        db,
        account_id=account_id,
        line_item=LINE_ITEM_USAGE_METRICS,
        start=start,
        end=end,
        organization=organization,
    )
    requests_by_user_model = _metrics_requests_by_user_model(metrics_rows)
    model_mix = []
    for login in sorted(set(amount_by_user_model) | set(requests_by_user_model)):
        shares = _share_rows(amount_by_user_model.get(login, {}))
        basis = "net_amount"
        if not shares:
            shares = _share_rows(requests_by_user_model.get(login, {}))
            basis = "requests"
        if shares:
            model_mix.append({"login": login, "basis": basis, "models": shares})

    if aggregate_rows:
        per_user_status = STATUS_UNAVAILABLE
        latest = max(aggregate_rows, key=lambda row: row.bucket_start)
        per_user_reason = (latest.raw or {}).get("per_user_unavailable_reason")
    elif by_developer or unattributed_rows:
        per_user_status = STATUS_AVAILABLE
        per_user_reason = None
    else:
        per_user_status = (
            connection.per_user_billing_status if connection else None
        ) or "no_data"
        per_user_reason = connection.per_user_billing_reason if connection else None

    seat_snapshot = crud_copilot_usage.latest_seat_snapshot(
        db, account_id=account_id, before=end, organization=organization
    )
    seat_summary = seat_snapshot["summary"]
    total_seats: Optional[int] = None
    plan_type: Optional[str] = None
    seats_as_of: Optional[datetime] = None
    if seat_summary is not None:
        raw = seat_summary.raw or {}
        total_seats = raw.get("total_seats")
        plan_type = raw.get("plan_type")
        seats_as_of = seat_summary.bucket_start
    seat_price = connection.seat_price_monthly if connection else None
    monthly_estimate = (
        seat_price * total_seats
        if seat_price is not None and total_seats is not None
        else None
    )

    return {
        "metered_by_gateway": False,
        "marker": NOT_METERED_MARKER,
        "period_start": start,
        "period_end": end,
        "connection": connection_payload(connection) if connection else None,
        "seats": {
            "total_seats": total_seats,
            "plan_type": plan_type,
            "as_of": seats_as_of,
            "seat_price_monthly": seat_price,
            "currency": connection.currency if connection else "USD",
            "monthly_seat_estimate": monthly_estimate,
            "assigned": [
                {
                    "login": seat.user_login,
                    "last_activity_at": (seat.raw or {}).get("last_activity_at"),
                    "last_activity_editor": (seat.raw or {}).get(
                        "last_activity_editor"
                    ),
                }
                for seat in seat_snapshot["seats"]
            ],
        },
        "premium_requests": {
            "total_net_amount": total_amount if totals else None,
            "currency": "USD",
            "per_user_status": per_user_status,
            "per_user_unavailable_reason": per_user_reason,
            "org_aggregate_net_amount": (
                org_aggregate_amount if aggregate_rows else None
            ),
            "unattributed_net_amount": (
                unattributed_amount if unattributed_rows else None
            ),
            "aggregate_days": len(aggregate_days),
            "by_developer": sorted(
                by_developer.values(),
                key=lambda row: (-row["net_amount"], row["login"]),
            ),
            "by_model": sorted(
                by_model.values(), key=lambda row: (-row["net_amount"], row["model"])
            ),
        },
        "model_mix": model_mix,
    }


def connection_payload(
    connection: models.CopilotImportConnection,
) -> Dict[str, Any]:
    """Serialize a connection without any token material."""
    return {
        "id": connection.id,
        "organization": connection.organization,
        "enterprise": connection.enterprise,
        "has_enterprise_token": connection.enterprise_secret_reference_id is not None,
        "seat_price_monthly": connection.seat_price_monthly,
        "currency": connection.currency,
        "is_active": connection.is_active,
        "last_synced_at": connection.last_synced_at,
        "last_synced_day": connection.last_synced_day,
        "last_error": connection.last_error,
        "per_user_billing_status": connection.per_user_billing_status,
        "per_user_billing_reason": connection.per_user_billing_reason,
        "metrics_status": connection.metrics_status,
        "metrics_reason": connection.metrics_reason,
        "last_warning": connection.last_warning,
    }
