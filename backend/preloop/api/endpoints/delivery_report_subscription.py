"""Per-person Delivery Report email subscription and the opt-out link (#2020).

Signed in, anyone in the account reads and changes their own subscription
(``/delivery-report/subscription``); opting out needs no budget permission,
because it changes only who receives the email.

The link in every report email is ``/delivery-report/opt-out?token=...``,
mounted without authentication: the signed token names the person and the
account. GET shows a confirmation page with one button, so a mail scanner
that follows links does not opt anyone out; POST opts out, which is also
what a mail client's one-click unsubscribe (RFC 8058) sends.
"""

from __future__ import annotations

import html
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.crud import crud_audit_log, crud_delivery_report_policy
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.services.delivery_report import resolve_policy
from preloop.utils.tokens import TokenError, verify_report_opt_out_token

router = APIRouter()
public_router = APIRouter()

SUBSCRIPTION_PATH = "/delivery-report/subscription"
OPT_OUT_PATH = "/delivery-report/opt-out"


class DeliveryReportSubscription(BaseModel):
    """Whether the signed-in person receives the report email."""

    #: The person asked not to receive the report.
    opted_out: bool
    #: This person is the one the report is sent to (owner or lead), as
    #: the sender resolves it, ignoring their own opt-out.
    is_recipient: bool
    #: The account's report is turned on at all.
    report_enabled: bool


class DeliveryReportSubscriptionUpdate(BaseModel):
    """Opt out of, or back into, the report email."""

    opted_out: bool


def _subscription(
    db: Session, account: Account, user: User
) -> DeliveryReportSubscription:
    policy = resolve_policy(
        crud_delivery_report_policy.get_overrides(db, account_id=account.id)
    )
    # The same resolution the sender uses, so an inactive lead hands the
    # toggle to the owner exactly as it hands them the email.
    recipients = crud_delivery_report_policy.report_recipients(
        db,
        account_id=account.id,
        recipient_rule=policy.recipients,
        sponsoring_lead_user_id=policy.sponsoring_lead_user_id,
        opted_out_user_ids=[],
    )
    return DeliveryReportSubscription(
        opted_out=str(user.id) in {str(u) for u in policy.opted_out_user_ids},
        is_recipient=any(u.id == user.id for u in recipients),
        report_enabled=policy.enabled,
    )


def _set(
    db: Session, *, account_id: uuid.UUID, user_id: uuid.UUID, opted_out: bool
) -> None:
    crud_delivery_report_policy.set_user_opt_out(
        db, account_id=account_id, user_id=user_id, opted_out=opted_out
    )
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action=(
            "delivery_report_opted_out" if opted_out else "delivery_report_opted_in"
        ),
        resource_type="delivery_report_policy",
        resource_id=str(account_id),
        status="success",
        details={},
        commit=False,
    )
    db.commit()


@router.get(
    SUBSCRIPTION_PATH,
    response_model=DeliveryReportSubscription,
    summary="Read your own Delivery Report email subscription",
)
def read_delivery_report_subscription(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportSubscription:
    """Return whether the caller receives the report email."""
    return _subscription(db, account, current_user)


@router.put(
    SUBSCRIPTION_PATH,
    response_model=DeliveryReportSubscription,
    summary="Opt out of, or back into, the Delivery Report email",
)
def update_delivery_report_subscription(
    payload: DeliveryReportSubscriptionUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DeliveryReportSubscription:
    """Change only the caller's own subscription."""
    _set(
        db, account_id=account.id, user_id=current_user.id, opted_out=payload.opted_out
    )
    return _subscription(db, account, current_user)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head>"
        '<body style="font-family:sans-serif;max-width:32rem;margin:4rem auto;'
        'line-height:1.5">'
        f'<h1 style="font-size:1.25rem">{html.escape(title)}</h1>{body}'
        "</body></html>",
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _invalid() -> HTMLResponse:
    return _page(
        "This link is not valid",
        "<p>The opt-out link is expired or damaged. You can turn the report "
        "email off under Account settings in the console.</p>",
        status_code=400,
    )


@public_router.get(OPT_OUT_PATH, response_class=HTMLResponse, include_in_schema=False)
def confirm_delivery_report_opt_out(
    token: Annotated[str, Query(max_length=2048)],
) -> HTMLResponse:
    """Ask for one click before opting out."""
    try:
        claims = verify_report_opt_out_token(token)
    except TokenError:
        return _invalid()
    action = html.escape(f"/api/v1{OPT_OUT_PATH}?token={token}", quote=True)
    return _page(
        "Stop the Delivery Report email",
        f"<p>{html.escape(claims.email)} will no longer receive the Delivery "
        "Report for this account. The report itself keeps running and stays "
        "on the console.</p>"
        f'<form method="post" action="{action}">'
        '<button type="submit">Stop these emails</button></form>',
    )


@public_router.post(OPT_OUT_PATH, response_class=HTMLResponse, include_in_schema=False)
def delivery_report_opt_out(
    token: Annotated[str, Query(max_length=2048)],
    db: Session = Depends(get_db_session),
) -> HTMLResponse:
    """Opt the person the token names out of that account's report email."""
    try:
        claims = verify_report_opt_out_token(token)
    except TokenError:
        return _invalid()
    if not crud_delivery_report_policy.user_in_account(
        db, account_id=claims.account_id, user_id=claims.user_id
    ):
        return _invalid()
    _set(db, account_id=claims.account_id, user_id=claims.user_id, opted_out=True)
    return _page(
        "You will not receive the Delivery Report email",
        "<p>You can turn it back on under Account settings in the console.</p>",
    )
