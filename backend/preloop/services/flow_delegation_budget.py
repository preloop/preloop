"""What a delegation tree is allowed to cost, and what it has cost so far (#631).

``flow_delegation_call`` decides whether one execution may start another.
This module decides whether the account can afford it. The two are separate
on purpose: every other delegation rule answers from a flow definition, this
one answers from money already spent and money already promised.

The shape of the answer, which is the whole design:

* Every delegated child is admitted under a **ceiling**, in USD. The agent
  may ask for one (``max_cost_usd``), the calling flow's ``callable_flows``
  entry may cap what the agent may ask for (``max_usd_per_child``, #627),
  and an instance default applies when neither says anything.
* A ceiling covers a **subtree**, not one run. A child admitted at 5 USD may
  spend 5 USD itself, or spend 2 and let its own children spend 3. That is
  what stops a cap being avoided by delegating one level deeper.
* A tree nobody delegated (a root run) is covered by the instance ceiling
  ``FLOW_DELEGATION_MAX_TREE_USD``.
* A child is refused **before it starts**, never killed once it is running.
  A child killed halfway has already been paid for and produced nothing, so
  a ceiling reached after a child started is a ceiling that binds the *next*
  child, not the ones already running.

Because a running child has spent less than it will spend, an in flight
child counts at its ceiling (what it may still cost) and a finished child
counts at its actual ``estimated_cost`` (what it did cost). Counting a
running child at its current spend would admit a second fan out on the
strength of work that has not been paid for yet.

Admissions of one tree are serialized on PostgreSQL with a transaction
scoped advisory lock on the root execution id, taken before the snapshot is
read and released when the child insert commits. Two concurrent
``run_flow`` calls then cannot both admit against the same remaining
allowance.

Not a budget policy: ``BudgetPolicy`` (account, flow, api key, managed
agent) is untouched, and every existing budget rule still applies to every
execution in a tree exactly as it applies to a run nobody delegated. This is
an admission rule layered on top of them, which is the option #621's open
decision 4 offers as an alternative to inventing a ``tree`` subject type.
See ``docs/guide/flows/flow-delegation.md``.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution

logger = logging.getLogger(__name__)

#: Key the delegation record is written under on a child's trigger details.
#: Defined here rather than in ``flow_delegation_call`` so both modules can
#: read it without importing each other.
DELEGATION_DETAILS_KEY = "delegation"

#: Key of the ceiling inside that record. A child carries the ceiling it was
#: admitted under, so the ceiling survives a restart and can be read back by
#: anything summing the tree.
COST_CEILING_KEY = "max_cost_usd"

#: Namespace for deriving one fan out's ``batch_id`` from its parent.
#: Derived rather than stored so every sibling of one fan out agrees on the
#: id without reading the rows its siblings wrote.
BATCH_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "delegation.preloop.ai")

#: Statuses at which an execution can spend nothing more, so it counts at
#: what it actually cost rather than at what it was allowed to cost. Mirrors
#: the terminal set the batch rollup endpoint uses, plus the spellings the
#: executor also writes.
TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
        "TIMEOUT",
        "TIMED_OUT",
        "ABORTED",
        "CANCELLED",
        "CANCELED",
        # A pre-check skip (#2541) is inserted already terminal and spends
        # nothing.
        "SKIPPED",
    }
)

#: Rounding slack, in USD. Costs are stored as ``Numeric(10, 4)``, so a
#: comparison that is exact to a hundredth of a cent is exact enough, and
#: this keeps a ceiling of 1.0 from refusing a request for 1.0.
_EPSILON = 1e-9

#: Hard stop for the walk up the ancestor chain, independent of the depth
#: cap: the walk is over data, so it is bounded rather than trusted.
_MAX_ANCESTOR_WALK = 64


class DelegationBudgetError(Exception):
    """The tree cannot afford this child.

    Carries the message the refusal record and the audit row both show. The
    refusal reason is always ``budget_exceeded``; the message names what is
    left, because "no" without a number is not actionable for an agent
    deciding how to split its remaining work.
    """


@dataclass(frozen=True)
class TreeBudget:
    """One allowance and what has been committed against it."""

    #: Execution whose ceiling this is.
    execution_id: str
    #: The ceiling in USD, or None when nothing bounds this subtree.
    allowance_usd: Optional[float]
    #: Spend plus outstanding reservations under (and including) that row.
    committed_usd: float

    @property
    def remaining_usd(self) -> Optional[float]:
        """USD this subtree may still commit, or None when unbounded."""
        if self.allowance_usd is None:
            return None
        return max(0.0, self.allowance_usd - self.committed_usd)


def instance_tree_ceiling() -> Optional[float]:
    """Allowance of a delegation tree whose root nobody delegated.

    Returns None when the setting is zero or negative, which is how an
    operator says "no ceiling from me": the per entry ceilings on each flow's
    allowlist are then the only money rule delegation adds.
    """
    try:
        value = float(settings.flow_delegation_max_tree_usd)
    except (TypeError, ValueError):  # pragma: no cover - settings are typed
        return None
    return value if value > 0 else None


def default_child_ceiling() -> Optional[float]:
    """Ceiling for a child nobody named one for.

    Without this a call that omits ``max_cost_usd`` would reserve nothing,
    and a fan out of silent children would be free to commit the whole tree
    allowance twice over.
    """
    try:
        value = float(settings.flow_delegation_default_child_usd)
    except (TypeError, ValueError):  # pragma: no cover - settings are typed
        return None
    return value if value > 0 else None


def _positive(value: Any) -> Optional[float]:
    """Read a positive finite USD amount, or None. Never raises."""
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or not math.isfinite(amount):
        return None
    return amount


def resolve_child_ceiling(
    *, requested: Optional[float], entry_ceiling: Optional[float]
) -> Optional[float]:
    """Decide the ceiling one child is admitted under.

    The entry ceiling clamps rather than refuses: an agent asking for more
    than its flow's allowlist permits gets the allowlist's number and a
    child, not a refusal, because the operator has already answered the
    question "how much may this flow spend per child of that one". What is
    refused, elsewhere in this module, is a ceiling the *tree* cannot afford.

    Args:
        requested: ``max_cost_usd`` from the call, if the agent named one.
        entry_ceiling: ``max_usd_per_child`` on the matching allowlist entry.

    Returns:
        The ceiling in USD, or None when nothing bounds this child.
    """
    asked = _positive(requested)
    capped = _positive(entry_ceiling)
    if asked is not None and capped is not None:
        return min(asked, capped)
    if capped is not None:
        return capped
    if asked is not None:
        return asked
    return default_child_ceiling()


def recorded_ceiling(execution: FlowExecution) -> Optional[float]:
    """The ceiling a delegated execution was admitted under, if any.

    None for every execution that was not started by ``run_flow`` and for
    children admitted while no ceiling applied.
    """
    details = getattr(execution, "trigger_event_details", None) or {}
    if not isinstance(details, dict):
        return None
    delegation = details.get(DELEGATION_DETAILS_KEY) or {}
    if not isinstance(delegation, dict):
        return None
    return _positive(delegation.get(COST_CEILING_KEY))


def execution_spend(execution: FlowExecution) -> float:
    """What one execution has cost so far, in USD."""
    try:
        return max(0.0, float(getattr(execution, "estimated_cost", 0) or 0))
    except (TypeError, ValueError):  # pragma: no cover - column is Numeric
        return 0.0


def is_terminal(execution: FlowExecution) -> bool:
    """True when the execution can no longer spend anything."""
    return str(getattr(execution, "status", "") or "") in TERMINAL_STATUSES


def fanout_batch_id(parent_execution_id: Any) -> uuid.UUID:
    """The ``batch_id`` every child of one parent shares.

    Derived from the parent execution id, so siblings agree without a read
    and two concurrent calls from the same turn cannot mint two batches. The
    existing rollup endpoint (``GET /flows/batches/{batch_id}/executions``)
    then reports the fan out total with no new query.
    """
    return uuid.uuid5(BATCH_NAMESPACE, f"delegation:{parent_execution_id}")


def _subtree_rows(
    db: Session, *, root_execution_id: Any, account_id: Any
) -> List[FlowExecution]:
    """Every execution of one delegation tree, the root row included."""
    rows = crud_flow_execution.get_by_root(
        db, root_execution_id=root_execution_id, account_id=account_id
    )
    known = {str(row.id) for row in rows}
    if str(root_execution_id) not in known:
        root = crud_flow_execution.get(
            db, id=str(root_execution_id), account_id=str(account_id)
        )
        if root is not None:
            rows = [root, *rows]
    return rows


def _children_index(rows: Sequence[FlowExecution]) -> Dict[str, List[FlowExecution]]:
    """Map every execution id to the executions it started."""
    index: Dict[str, List[FlowExecution]] = {}
    for row in rows:
        parent_id = getattr(row, "parent_execution_id", None)
        if parent_id is None:
            continue
        index.setdefault(str(parent_id), []).append(row)
    return index


def subtree_committed_usd(
    execution: FlowExecution,
    *,
    children_index: Dict[str, List[FlowExecution]],
    _depth: int = 0,
) -> float:
    """USD spent or promised under (and including) one execution.

    An execution contributes its own spend. Each child contributes the
    larger of its outstanding reservation and what its own subtree has
    already committed: a running child that is over its ceiling counts at
    what it really cost, and a running child that is under it counts at what
    it may still cost.
    """
    total = execution_spend(execution)
    if _depth >= _MAX_ANCESTOR_WALK:  # pragma: no cover - lineage cannot loop
        logger.warning(
            "Delegation subtree walk hit the depth guard at execution %s",
            execution.id,
        )
        return total
    for child in children_index.get(str(execution.id), []):
        reserved = 0.0 if is_terminal(child) else (recorded_ceiling(child) or 0.0)
        spent = subtree_committed_usd(
            child, children_index=children_index, _depth=_depth + 1
        )
        total += max(reserved, spent)
    return total


def _ancestor_chain(
    db: Session,
    *,
    execution: FlowExecution,
    by_id: Dict[str, FlowExecution],
    account_id: Any,
) -> List[FlowExecution]:
    """``execution`` and every execution above it, nearest first."""
    chain: List[FlowExecution] = []
    seen: set[str] = set()
    current: Optional[FlowExecution] = execution
    while current is not None and len(chain) < _MAX_ANCESTOR_WALK:
        if str(current.id) in seen:  # pragma: no cover - lineage cannot loop
            break
        seen.add(str(current.id))
        chain.append(current)
        parent_id = getattr(current, "parent_execution_id", None)
        if parent_id is None:
            break
        current = by_id.get(str(parent_id)) or crud_flow_execution.get(
            db, id=str(parent_id), account_id=str(account_id)
        )
    return chain


def _lock_delegation_tree(db: Session, *, root_execution_id: Any) -> None:
    """Hold a tree-wide admission lock until this transaction ends.

    The affordability snapshot and the child-row insert share this session;
    ``trigger_flow`` commits the child, which releases the lock. Postgres
    only; other dialects keep the previous best-effort snapshot.
    """
    bind = getattr(db, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"flow_delegation_tree:{root_execution_id}"},
    )


def tree_budgets(
    db: Session, *, parent_execution: FlowExecution, account_id: Any
) -> List[TreeBudget]:
    """Every allowance a new child of ``parent_execution`` sits inside.

    One per ancestor that has a ceiling, nearest first, ending at the tree
    allowance of the root. A child has to fit inside all of them: a
    grandchild that fits under its parent's remaining ceiling but not under
    the tree's is still a child the account cannot afford.

    Reads the tree in one indexed query on ``root_execution_id`` and walks it
    in memory. The tree is bounded by the depth and fan out caps, so this is
    a few hundred rows at the very most. On PostgreSQL the read is taken
    under a transaction scoped advisory lock on the root, held through the
    later child insert.
    """
    root_execution_id = (
        getattr(parent_execution, "root_execution_id", None) or parent_execution.id
    )
    _lock_delegation_tree(db, root_execution_id=root_execution_id)
    rows = _subtree_rows(db, root_execution_id=root_execution_id, account_id=account_id)
    by_id = {str(row.id): row for row in rows}
    by_id.setdefault(str(parent_execution.id), parent_execution)
    children_index = _children_index(list(by_id.values()))

    budgets: List[TreeBudget] = []
    for ancestor in _ancestor_chain(
        db, execution=parent_execution, by_id=by_id, account_id=account_id
    ):
        allowance = recorded_ceiling(ancestor)
        if allowance is None:
            if getattr(ancestor, "parent_execution_id", None) is not None:
                # A delegated execution with no ceiling of its own is covered
                # by whatever covers its parent; it adds no allowance here.
                continue
            allowance = instance_tree_ceiling()
        if allowance is None:
            continue
        budgets.append(
            TreeBudget(
                execution_id=str(ancestor.id),
                allowance_usd=allowance,
                committed_usd=subtree_committed_usd(
                    ancestor, children_index=children_index
                ),
            )
        )
    return budgets


def _format_usd(amount: float) -> str:
    """Money as an agent should read it back: two decimals, no currency word."""
    return f"{amount:.2f}"


def check_child_affordable(
    db: Session,
    *,
    parent_execution: FlowExecution,
    account_id: Any,
    ceiling_usd: Optional[float],
    target_name: Optional[str] = None,
) -> None:
    """Refuse a child the tree cannot afford, before any row is created.

    Args:
        db: Database session.
        parent_execution: The execution making the call.
        account_id: Account of that execution, for scoping the tree read.
        ceiling_usd: What the child would be admitted at, already clamped by
            the allowlist entry. None means nothing bounds the child, which
            is affordable only inside an unbounded tree.
        target_name: Flow the call named, for the message.

    Raises:
        DelegationBudgetError: The child does not fit in some allowance
            above it. The message names the remaining allowance.
    """
    budgets = tree_budgets(db, parent_execution=parent_execution, account_id=account_id)
    if not budgets:
        return

    named = f"'{target_name}'" if target_name else "this child"
    for budget in budgets:
        remaining = budget.remaining_usd
        if remaining is None:  # pragma: no cover - unbounded rows are skipped
            continue
        if ceiling_usd is None:
            raise DelegationBudgetError(
                f"starting {named} without a cost ceiling is refused inside a "
                f"tree that has one; this tree has "
                f"{_format_usd(remaining)} USD left of its "
                f"{_format_usd(budget.allowance_usd or 0.0)} USD allowance, so "
                f"pass max_cost_usd no greater than that"
            )
        if ceiling_usd > remaining + _EPSILON:
            raise DelegationBudgetError(
                f"starting {named} would commit "
                f"{_format_usd(ceiling_usd)} USD; this delegation tree has "
                f"{_format_usd(remaining)} USD left of its "
                f"{_format_usd(budget.allowance_usd or 0.0)} USD allowance "
                f"(spent or promised so far: "
                f"{_format_usd(budget.committed_usd)} USD)"
            )
