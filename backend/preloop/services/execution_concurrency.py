"""One place that answers "may this account start another execution?".

Before this module the answer was "yes, always". The instance-wide worker
pool was the only bound, so the account that queued work first held every
slot until its own runs finished. On 2026-09-15 one label-trigger defect
turned four issues into 21 executions on a three-slot instance: three ran,
sixteen queued, and no other account could start a flow at all. An
unbounded share of a shared pool is a denial of service with no attacker in
it.

The cap is per account and is enforced where admission actually happens
(``crud_flow_execution.claim_execution``), not at creation time, so retries,
resumes and re-dispatches obey it too. Precedence:

1. ``account.meta_data["flow_execution_max_running_per_account"]``
2. ``settings.flow_execution_max_running_per_account`` (5)

The cap bounds the shared hosted pool. An execution assigned to one of the
account's private runners is bounded by that runner's own capacity instead,
so it is not counted here: charging an account's own compute against the
shared allowance punishes exactly the accounts that bring capacity.

Unlike the approval window cap, an account override may raise as well as
lower the deployment default: this is an operator-granted allowance for a
larger tenant, not something a tenant sets about itself. One is the floor;
a cap of zero would be a halt, and halts are the kill switch's job.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from preloop.config import settings

logger = logging.getLogger(__name__)

#: Account metadata key holding a per-account allowance.
ACCOUNT_CAP_KEY = "flow_execution_max_running_per_account"

#: Smallest meaningful cap. Zero would stop the account entirely, which is
#: what the kill switch is for.
MIN_ACCOUNT_CAP = 1

#: Written to ``flow_execution.queued_reason`` when a claim was refused
#: because the account is already at its cap. Machine readable on purpose:
#: the console and the reaper both branch on it.
QUEUED_REASON_ACCOUNT_CAP = "account_concurrency_cap"

#: How long a worker leaves a capped message on the stream before it is
#: redelivered. Long enough that a busy account does not spin through the
#: same sixteen messages, short enough that a freed slot is taken well
#: inside one reaper interval (30s).
ACCOUNT_CAP_NAK_DELAY_SECONDS = 10


def _coerce(value: Any) -> Optional[int]:
    """Positive int or None; a garbage setting must not decide admission."""
    if value is None or isinstance(value, bool):
        return None
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def default_account_cap() -> int:
    """The deployment-wide allowance, floored at one."""
    return max(
        MIN_ACCOUNT_CAP,
        _coerce(getattr(settings, "flow_execution_max_running_per_account", 5))
        or MIN_ACCOUNT_CAP,
    )


def account_running_cap(account: Any) -> int:
    """How many executions this account may have admitted at once.

    Duck-typed on the account row so the CRUD layer can pass whatever it
    already loaded, and tests can pass a stub.
    """
    meta = getattr(account, "meta_data", None)
    override = _coerce(meta.get(ACCOUNT_CAP_KEY)) if isinstance(meta, dict) else None
    if override is None:
        return default_account_cap()
    return max(MIN_ACCOUNT_CAP, override)
