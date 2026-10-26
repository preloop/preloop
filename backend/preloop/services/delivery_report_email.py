"""Email the Delivery Report to its recipients (#2020).

Both kinds of report go out the same way once a run has finished: the
deterministic report (no model call) and the assessed one. Recipients come
from the account policy (the account owner, or a sponsoring lead), minus
the people who opted out. Every email carries the report link and a signed
opt-out link; the same link is the ``List-Unsubscribe`` header, and a POST
to it opts out in one click (RFC 8058).

A run sends at most once: the outcome is stored on the result as
``distribution``, and a result that already has one is not sent again.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from preloop.services.delivery_report import (
    PURPOSE_PARTIAL,
    DeliveryReportPolicy,
    _summary_lines,
    change_detail_lines,
)

logger = logging.getLogger(__name__)

#: Result key holding the send outcome.
DISTRIBUTION_KEY = "distribution"
#: Rows per section in the email; the report page has the rest.
EMAIL_ITEMS = 10
OPT_OUT_PATH = "/api/v1/delivery-report/opt-out"
#: SMTP socket timeout, so a stuck relay cannot hold the run open.
SEND_TIMEOUT_SECONDS = 30.0


def _base_url() -> str:
    from preloop.config import settings

    return str(settings.preloop_url).rstrip("/")


def report_url(execution_id: Any) -> str:
    """The console page of one report."""
    return f"{_base_url()}/console/delivery-reports/{execution_id}"


def opt_out_url(*, user_id: Any, account_id: Any, email: str) -> str:
    """The signed opt-out link for one recipient of one account's report."""
    from preloop.utils.tokens import create_report_opt_out_token

    token = create_report_opt_out_token(
        user_id=user_id, account_id=account_id, email=email
    )
    return f"{_base_url()}{OPT_OUT_PATH}?token={quote(token)}"


def _day(value: Any) -> str:
    text = str(value or "")
    return text[:10] if text else "start"


def email_subject(report: Mapping[str, Any]) -> str:
    """``Delivery report 2026-10-18 to 2026-10-25: 12 shipped, 3 need you``."""
    window = report.get("window") or {}
    summary = report.get("summary") or {}
    needs = len(report.get("needs_you") or [])
    shipped = summary.get("shipped", len(report.get("shipped") or []))
    return (
        f"Delivery report {_day(window.get('from'))} to {_day(window.get('to'))}: "
        f"{shipped} shipped, {needs} need{'s' if needs == 1 else ''} you"
    )


def _label(item: Mapping[str, Any]) -> str:
    return f"{item.get('ref') or ''} {item.get('title') or ''}".strip()


def _needs_line(item: Mapping[str, Any]) -> str:
    kind = item.get("kind")
    if kind == "reopen_request":
        return f"Proposed reopen: {_label(item)}"
    if kind == "closed_unverified":
        if item.get("verdict") == PURPOSE_PARTIAL:
            criteria = (
                "; ".join(item.get("unmet_criteria") or []) or "criteria unchecked"
            )
            return f"Closed, partly covered: {_label(item)}. Not shown met: {criteria}"
        return f"Closed with an evidence gap: {_label(item)}"
    if kind == "risk":
        return f"Risk ({', '.join(item.get('flags') or [])}): {_label(item)}"
    return f"Open {item.get('age_days')} days: {_label(item)}"


def render_email(
    report: Mapping[str, Any], *, link: str, opt_out: str
) -> tuple[str, str, str]:
    """The subject, plain text and HTML of one report email.

    Args:
        report: The finalized report document.
        link: The report page.
        opt_out: The recipient's opt-out link.

    Returns:
        ``(subject, text, html)``.
    """
    subject = email_subject(report)
    needs = list(report.get("needs_you") or [])
    shipped = [c for c in report.get("shipped") or [] if isinstance(c, Mapping)]
    summary = report.get("summary") or {}

    text: List[str] = [subject, "", *_summary_lines(summary), "", "Needs you:"]
    for item in needs[:EMAIL_ITEMS]:
        text.append(f"- {_needs_line(item)}")
    if not needs:
        text.append("- Nothing needs you this period.")
    text += ["", "Shipped:"]
    for change in shipped[:EMAIL_ITEMS]:
        text.append(f"- {_label(change)}")
        text.extend(f"  {line}" for line in change_detail_lines(change))
    if not shipped:
        text.append("- Nothing merged in the window.")
    hidden = max(len(needs) - EMAIL_ITEMS, 0) + max(len(shipped) - EMAIL_ITEMS, 0)
    if hidden:
        text.append(f"{hidden} more on the report page.")
    text += [
        "",
        f"Full report: {link}",
        "",
        "You receive this because you are the report's recipient for this "
        "account. Stop these emails:",
        opt_out,
    ]

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def items(rows: Sequence[str], empty: str) -> str:
        if not rows:
            return f"<p>{esc(empty)}</p>"
        return "<ul>" + "".join(f"<li>{row}</li>" for row in rows) + "</ul>"

    summary_html = "".join(
        f"<p>{esc(line.removeprefix('- '))}</p>"
        for line in _summary_lines(summary)
        if line
    )
    needs_html = [esc(_needs_line(i)) for i in needs[:EMAIL_ITEMS]]
    shipped_html = []
    for change in shipped[:EMAIL_ITEMS]:
        label = esc(_label(change))
        url = change.get("url")
        head = f'<a href="{esc(url)}">{label}</a>' if url else label
        details = "".join(
            f'<br><span style="color:#555">{esc(line)}</span>'
            for line in change_detail_lines(change)
        )
        shipped_html.append(head + details)
    body_html = (
        '<html><body style="font-family:sans-serif;line-height:1.5">'
        f"<h2>{esc(subject)}</h2>{summary_html}"
        f"<h3>Needs you</h3>{items(needs_html, 'Nothing needs you this period.')}"
        f"<h3>Shipped</h3>{items(shipped_html, 'Nothing merged in the window.')}"
        + (f"<p>{hidden} more on the report page.</p>" if hidden else "")
        + f'<p><a href="{esc(link)}">Open the full report</a></p>'
        '<p style="color:#777;font-size:12px">You receive this because you are '
        "the report's recipient for this account. "
        f'<a href="{esc(opt_out)}">Stop these emails</a>.</p>'
        "</body></html>"
    )
    return subject, "\n".join(text) + "\n", body_html


async def distribute_report(
    db: Any,
    *,
    flow: Any,
    execution: Any,
    report: Mapping[str, Any],
    policy: Optional[DeliveryReportPolicy],
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Email a finished report to its recipients, once.

    Nothing is sent for a report that did not complete, for a turned-off
    report, or for a result that was already distributed. A failed send to
    one recipient is recorded and does not fail the run. The blocking SMTP
    send runs in a worker thread so a slow relay does not hold the event
    loop; database reads stay on the caller's thread.

    Args:
        db: Session.
        flow: The Delivery Report flow.
        execution: The finished execution.
        report: The finalized result.
        policy: The policy the run started with; None reads the defaults.
            Recipients, opt-outs and the on switch are re-read at send time.
        now: Clock for the record.

    Returns:
        The ``distribution`` record to store on the result, or None when
        the report is not one that is sent.
    """
    from preloop.models.crud import crud_delivery_report_policy
    from preloop.services.delivery_report import DEFAULT_POLICY, resolve_policy
    from preloop.utils.email import EmailError, send_email

    if not isinstance(report, Mapping) or report.get("status") != "success":
        return None
    if isinstance(report.get(DISTRIBUTION_KEY), Mapping):
        return None
    policy = policy or DEFAULT_POLICY
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    account_id = getattr(flow, "account_id", None)
    # Who receives it is read now, not from the snapshot the run started
    # with, so an opt-out or a turn-off during the run is honoured at once.
    current = resolve_policy(
        crud_delivery_report_policy.get_overrides(db, account_id=account_id)
    )
    if not (policy.enabled and current.enabled):
        return {"status": "disabled", "sent_to": [], "at": stamp}
    recipients = crud_delivery_report_policy.report_recipients(
        db,
        account_id=account_id,
        recipient_rule=current.recipients,
        sponsoring_lead_user_id=current.sponsoring_lead_user_id,
        opted_out_user_ids=current.opted_out_user_ids,
    )
    if not recipients:
        return {"status": "no_recipients", "sent_to": [], "at": stamp}
    link = report_url(getattr(execution, "id", ""))
    sent: List[str] = []
    failed: List[str] = []
    for user in recipients:
        unsubscribe = opt_out_url(
            user_id=user.id, account_id=account_id, email=user.email
        )
        subject, text, body = render_email(report, link=link, opt_out=unsubscribe)
        try:
            delivered = await asyncio.to_thread(
                send_email,
                user.email,
                subject,
                text,
                body,
                timeout=SEND_TIMEOUT_SECONDS,
                headers={
                    "List-Unsubscribe": f"<{unsubscribe}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            )
            if not delivered:
                return {
                    "status": "not_configured",
                    "sent_to": sent,
                    "failed": failed,
                    "at": stamp,
                }
            sent.append(str(user.id))
        except EmailError:
            logger.warning("Could not email the delivery report", exc_info=True)
            failed.append(str(user.id))
    return {
        "status": "failed" if failed else "sent",
        "sent_to": sent,
        "failed": failed,
        "at": stamp,
    }
