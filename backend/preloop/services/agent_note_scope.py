"""Which agent may note which target, and who may widen that.

An agent to agent note channel with no scope is a way for any agent in an
account to put text into any other agent's next turn. That text is read by a
model, so the blast radius is the other agent's behaviour, not just its
transcript. This module is the answer to "who may do that to whom", so that
enabling ``send_note`` is not itself a decision that needs a threat model.

**The default is descent.** An agent may note the runs it started, directly or
transitively: its children, their children, and so on. Nothing else. Lineage
is the key because it is the only relationship here that Preloop can verify
rather than accept from the caller: ``parent_execution_id`` is written by the
platform when the child execution is created (#626), never supplied by an
agent, while "we are on the same team" is a claim an agent could simply make.

Four consequences, all of them deliberate:

* A **sibling** is out of scope. Two children of the same parent cannot steer
  each other; the parent that started both can steer either.
* A **grandchild is in scope**: delegation is transitive, and a scope that
  stopped at one hop would be widened by inserting a middleman, which is not
  a boundary.
* An **ancestor** is out of scope. A child cannot write into the turn of the
  run that is supervising it.
* A caller with **no lineage** can note nothing. A top level execution that
  started no children, and an enrolled agent that is not running inside an
  execution at all, both have an empty descendant set, so every target is
  refused. This is the quiet case that makes the default safe: no lineage is
  no reach, rather than no restriction.

**Wider than that is a grant, not a setting.** When the default refuses, the
call is put to the tool policy path every other tool call already goes through
(:func:`preloop.services.policy_evaluator.evaluate_policy`) as a rule
evaluation on ``send_note``, with the scope facts visible as arguments
(``note_scope``, ``note_target_relation``, the author and target ids). Only a
rule that explicitly allows it is a grant: the default allow that a tool with
no rules returns is not, or enabling the tool would silently mean "note
anyone". A rule that denies refuses the call, and rules are evaluated in
priority order exactly as everywhere else, so a deny placed above a grant wins.

That choice is what makes a grant auditable and revocable like everything else:
it is a ``ToolAccessRule`` row, it shows up in the policy decision audit, and
deleting or disabling it takes the reach away on the next call with nothing to
restart.

The account boundary is not part of this model and cannot be granted away:
targets are resolved inside the calling agent's account before scope is even
considered, so ``account`` is the widest scope that exists.

Every refusal returns a distinct reason code. The first of a given
author, target and reason in a short window writes an
``agent.note_scope_denied`` audit row, so "my agent cannot reach that run"
has an answer in the record rather than in a log line. Repeats of the same
refusal, and a small per-author ceiling, do not spend the note budget and
do not fill the audit log.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log
from preloop.services import operator_notes

logger = logging.getLogger(__name__)

#: The default scope: the executions this agent started, at any depth.
SCOPE_DESCENDANTS = "descendants"

#: The one wider scope, and the widest that can exist: any target inside the
#: calling agent's own account. Only reachable through an explicit policy rule.
SCOPE_ACCOUNT = "account"

#: How the target's run relates to the caller's run. Reported to the caller,
#: recorded in the audit row, and readable by a rule as
#: ``args.note_target_relation`` so a grant can be narrower than "anyone".
RELATION_SELF = "self"
RELATION_DESCENDANT = "descendant"
RELATION_ANCESTOR = "ancestor"
RELATION_SAME_TREE = "same_tree"
RELATION_UNRELATED = "unrelated"
#: The target is not running inside any execution we can see (an agent that
#: never ran a flow, or a session with no governed call attributed to a run).
RELATION_NO_LINEAGE = "no_lineage"

#: Refusal codes. One per reason, stable, and the same string the audit row
#: carries: a model reads it on its next turn, a human greps it in the record.
REASON_NO_LINEAGE = "note_scope_no_lineage"
REASON_OUT_OF_SCOPE = "note_scope_out_of_scope"
REASON_DENIED_BY_RULE = "note_scope_denied_by_rule"
REASON_GRANT_UNAVAILABLE = "note_scope_grant_unavailable"

#: Bound on the parent walk. Delegation depth is capped far below this
#: (see #630); the bound exists so a cycle written by a bug cannot spin here.
MAX_ANCESTOR_WALK = 32

#: How many of a session's most recent runs are considered when the target was
#: named as an agent or a session rather than as an execution. A session that
#: has carried more runs than this is not a delegation hand off.
MAX_TARGET_EXECUTIONS = 25

#: Scope refusals do not spend the note budget, so a looping agent would
#: otherwise append an unbounded number of ``agent.note_scope_denied`` rows.
#: Skip a duplicate author+target+reason inside this window, and cap how
#: many distinct refusals one author can record in the same window.
NOTE_SCOPE_DENIED_WINDOW = timedelta(minutes=15)
NOTE_SCOPE_DENIED_CEILING = 25


@dataclass
class NoteScopeDecision:
    """What the scope model says about one attempted note.

    Attributes:
        allowed: Whether the note may be written.
        scope: The scope that permitted it (:data:`SCOPE_DESCENDANTS` or
            :data:`SCOPE_ACCOUNT`), or None when refused.
        required_scope: The scope this call would have needed, when refused.
        relation: How the target's run relates to the caller's run.
        reason_code: Distinct refusal code, or None when allowed.
        message: What the calling model is told, naming the required scope.
        rule_description: The rule that granted or denied, when one did.
        facts: The argument bindings the policy path was given, so the audit
            row records exactly what a rule was evaluated against.
    """

    allowed: bool
    scope: Optional[str] = None
    required_scope: Optional[str] = None
    relation: str = RELATION_NO_LINEAGE
    reason_code: Optional[str] = None
    message: Optional[str] = None
    rule_description: Optional[str] = None
    facts: Dict[str, Any] = field(default_factory=dict)


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    """Parse a UUID from whatever the caller had; None when it is not one."""
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def ancestor_chain(
    db: Session,
    *,
    account_id: str,
    execution_id: Any,
    cache: Optional[Dict[uuid.UUID, List[uuid.UUID]]] = None,
) -> List[uuid.UUID]:
    """The execution and its parents, nearest first, inside one account.

    Every hop is an account-scoped read, so a parent id that points outside
    the account ends the walk instead of being followed. Bounded by
    :data:`MAX_ANCESTOR_WALK` and by a seen-set, because a cycle in the data
    must not become a loop in an enforcement path.

    *cache*, when given, reuses already-walked suffixes inside one call so
    overlapping trees (the common case on a session with many runs) do not
    repeat the same parent hops. The bound is unchanged.

    Args:
        db: Database session.
        account_id: Account the whole chain must live in.
        execution_id: Where to start. Included in the result.
        cache: Optional per-call map of execution id to remaining chain.

    Returns:
        Execution ids, starting with *execution_id*. Empty when it does not
        resolve inside the account.
    """
    from preloop.models.crud import crud_flow_execution

    start = _as_uuid(execution_id)
    if start is None:
        return []
    if cache is not None and start in cache:
        return list(cache[start])
    chain: List[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    current: Optional[uuid.UUID] = start
    while current is not None and len(chain) < MAX_ANCESTOR_WALK:
        if current in seen:
            logger.warning("Execution lineage cycle at %s; stopping walk", current)
            break
        if cache is not None and current in cache:
            for exec_id in cache[current]:
                if len(chain) >= MAX_ANCESTOR_WALK or exec_id in seen:
                    break
                seen.add(exec_id)
                chain.append(exec_id)
            break
        execution = crud_flow_execution.get(db, current, account_id=str(account_id))
        if execution is None:
            break
        seen.add(current)
        chain.append(current)
        current = _as_uuid(execution.parent_execution_id)
    if cache is not None:
        for index, exec_id in enumerate(chain):
            cache.setdefault(exec_id, list(chain[index:]))
        if start not in cache:
            cache[start] = list(chain)
    return chain


def target_execution_ids(
    db: Session,
    *,
    account_id: str,
    execution_id: Any = None,
    runtime_session_id: Any = None,
) -> List[uuid.UUID]:
    """The runs a note would land in, newest first.

    A note is addressed to an agent, a session or an execution, but it is
    delivered into whatever turn that target takes next, so the runs that
    matter are the ones attributed to the target's session. When the caller
    named an execution outright, that execution is the answer.

    Args:
        db: Database session.
        account_id: Account scope for every lookup.
        execution_id: The execution the caller named, if it named one.
        runtime_session_id: The session the target resolved to.

    Returns:
        Execution ids, bounded by :data:`MAX_TARGET_EXECUTIONS`.
    """
    from preloop.models.crud import crud_api_usage

    named = _as_uuid(execution_id)
    if named is not None:
        return [named]
    session_id = _as_uuid(runtime_session_id)
    if session_id is None:
        return []
    return crud_api_usage.list_execution_ids_for_session(
        db,
        account_id=str(account_id),
        runtime_session_id=session_id,
        limit=MAX_TARGET_EXECUTIONS,
    )


def _relation(
    db: Session,
    *,
    account_id: str,
    author_execution_id: Optional[uuid.UUID],
    target_ids: List[uuid.UUID],
) -> tuple[str, Optional[uuid.UUID]]:
    """How the target's runs relate to the caller's run.

    Returns the strongest relation found across the target's runs, with the
    execution that produced it: descendant beats self beats ancestor beats
    same tree beats unrelated. Descendant is the only one the default scope
    permits; the rest are reported so a refusal can say what the target
    actually is, and so a rule can be written against it.
    """
    if not target_ids:
        return RELATION_NO_LINEAGE, None
    if author_execution_id is None:
        return RELATION_UNRELATED, target_ids[0]

    walked: Dict[uuid.UUID, List[uuid.UUID]] = {}
    author_chain = ancestor_chain(
        db,
        account_id=account_id,
        execution_id=author_execution_id,
        cache=walked,
    )
    author_ancestors = set(author_chain)
    best = RELATION_UNRELATED
    best_id = target_ids[0]
    ranking = {
        RELATION_UNRELATED: 0,
        RELATION_SAME_TREE: 1,
        RELATION_ANCESTOR: 2,
        RELATION_SELF: 3,
        RELATION_DESCENDANT: 4,
    }
    for target_id in target_ids:
        chain = ancestor_chain(
            db, account_id=account_id, execution_id=target_id, cache=walked
        )
        if not chain:
            continue
        if target_id == author_execution_id:
            relation = RELATION_SELF
        elif author_execution_id in chain[1:]:
            # The caller is one of the target's parents: the target descends
            # from the call, which is the one thing the default scope allows.
            relation = RELATION_DESCENDANT
        elif target_id in author_ancestors:
            relation = RELATION_ANCESTOR
        elif author_chain and chain[-1] == author_chain[-1]:
            relation = RELATION_SAME_TREE
        else:
            relation = RELATION_UNRELATED
        if ranking[relation] > ranking[best]:
            best, best_id = relation, target_id
        if best == RELATION_DESCENDANT:
            break
    return best, best_id


def _grant_facts(
    *,
    account_id: str,
    author_agent_id: Any,
    author_execution_id: Optional[uuid.UUID],
    target_agent_id: Any,
    target_session_id: Any,
    target_execution_id: Optional[uuid.UUID],
    relation: str,
    text: str,
) -> Dict[str, Any]:
    """The bindings a rule sees when it is asked to widen the scope.

    ``text`` is the caller's. ``agent_id``, ``runtime_session_id``, and
    ``execution_id`` are the target as the platform resolved it, under the
    names the tool uses: they can be present even when the caller never
    named them. A rule should key on the ``note_*`` facts, which name
    author and target unambiguously. Top-level ``execution_id`` in the
    rule context is the author run only on grant evaluation.
    """
    return {
        "text": text,
        "agent_id": str(target_agent_id) if target_agent_id else None,
        "runtime_session_id": str(target_session_id) if target_session_id else None,
        "execution_id": str(target_execution_id) if target_execution_id else None,
        "note_scope": SCOPE_ACCOUNT,
        "note_default_scope": SCOPE_DESCENDANTS,
        "note_target_relation": relation,
        "note_author_managed_agent_id": str(author_agent_id)
        if author_agent_id
        else None,
        "note_author_execution_id": (
            str(author_execution_id) if author_execution_id else None
        ),
        "note_target_managed_agent_id": (
            str(target_agent_id) if target_agent_id else None
        ),
        "note_target_runtime_session_id": (
            str(target_session_id) if target_session_id else None
        ),
        "note_target_execution_id": (
            str(target_execution_id) if target_execution_id else None
        ),
        "account_id": str(account_id),
    }


def caller_subject_context(
    *,
    subject_context: Optional[Dict[str, Any]] = None,
    author_agent_id: Any = None,
) -> Dict[str, Any]:
    """The same subject chain the preceding ``send_note`` evaluation used.

    Target identity belongs in the ``note_*`` facts, not here. Putting the
    target's session on ``runtime_session_id`` would make a rule against that
    key mean two different things across the two evaluations of the same
    rule, and omitting ``api_key_id`` would skip API-key-scoped governance
    that the plain call already applied.

    Args:
        subject_context: Caller attributes from the authenticated context.
        author_agent_id: Fallback managed-agent id when the context omitted it.

    Returns:
        The keys ``evaluate_policy`` walks for subject-scoped tool rules.
    """
    context: Dict[str, Any] = {}
    if subject_context:
        for key in (
            "api_key_id",
            "managed_agent_id",
            "runtime_session_id",
            "runtime_principal_type",
            "runtime_principal_id",
            "runtime_principal_name",
        ):
            value = subject_context.get(key)
            if value is not None and value != "":
                context[key] = str(value)
    if "managed_agent_id" not in context and author_agent_id:
        context["managed_agent_id"] = str(author_agent_id)
    return context


def _explain(relation: str) -> str:
    """One clause saying what the target is, for the refusal message."""
    return {
        RELATION_SELF: "the target is the calling run itself",
        RELATION_ANCESTOR: "the target started this run, it is not one of its runs",
        RELATION_SAME_TREE: (
            "the target shares an ancestor with this run but does not descend from it"
        ),
        RELATION_UNRELATED: "the target belongs to an unrelated run",
        RELATION_NO_LINEAGE: (
            "the target is not running inside any execution this run started"
        ),
        RELATION_DESCENDANT: "the target descends from this run",
    }.get(relation, "the target is not one of this run's descendants")


def evaluate_note_scope(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    author_execution_id: Any = None,
    target_agent_id: Any = None,
    target_session_id: Any = None,
    named_execution_id: Any = None,
    text: str = "",
    subject_context: Optional[Dict[str, Any]] = None,
) -> NoteScopeDecision:
    """Decide whether this agent may note this target.

    The default scope is evaluated first and needs no policy read: a note to a
    run the caller started is the case the tool exists for. Only a call the
    default refuses is put to the policy path, so a grant rule is consulted
    exactly when it can change the answer.

    Args:
        db: Database session. Read only here; nothing is written on the
            allowed path.
        account_id: Account of the calling agent, and of every lookup.
        author_agent_id: The calling managed agent.
        author_execution_id: The execution the call was made from, as the
            platform recorded it on the caller's identity. None when the
            caller is not running inside an execution.
        target_agent_id: Managed agent the target resolved to, if any.
        target_session_id: Runtime session the target resolved to, if any.
        named_execution_id: The execution the caller named, when it addressed
            one directly.
        text: The note body, passed through to a rule unchanged.
        subject_context: The same caller attributes the preceding ``send_note``
            policy evaluation used (``api_key_id``, the caller's
            ``runtime_session_id``, and the rest of that chain). Target
            identity stays in the ``note_*`` facts.

    Returns:
        A :class:`NoteScopeDecision`. Never raises for a policy failure: an
        evaluation that cannot be completed refuses the note, because failing
        open here would make the grant path the way around the default.
    """
    author_execution = _as_uuid(author_execution_id)
    target_ids = target_execution_ids(
        db,
        account_id=account_id,
        execution_id=named_execution_id,
        runtime_session_id=target_session_id,
    )
    relation, matched_target = _relation(
        db,
        account_id=account_id,
        author_execution_id=author_execution,
        target_ids=target_ids,
    )
    facts = _grant_facts(
        account_id=account_id,
        author_agent_id=author_agent_id,
        author_execution_id=author_execution,
        target_agent_id=target_agent_id,
        target_session_id=target_session_id,
        target_execution_id=matched_target,
        relation=relation,
        text=text,
    )

    if relation == RELATION_DESCENDANT:
        return NoteScopeDecision(
            allowed=True,
            scope=SCOPE_DESCENDANTS,
            relation=relation,
            facts=facts,
        )

    base_reason = REASON_NO_LINEAGE if author_execution is None else REASON_OUT_OF_SCOPE
    grant = _consult_policy(
        db,
        account_id=account_id,
        author_agent_id=author_agent_id,
        author_execution_id=author_execution,
        facts=facts,
        subject_context=subject_context,
    )
    action, rule_description, from_rule = grant

    if action == "deny":
        return NoteScopeDecision(
            allowed=False,
            required_scope=SCOPE_ACCOUNT,
            relation=relation,
            reason_code=REASON_DENIED_BY_RULE,
            rule_description=rule_description,
            message=(
                "A tool access rule on send_note denies this note: "
                f"{rule_description or 'no description'}."
            ),
            facts=facts,
        )
    if action == "allow" and from_rule:
        return NoteScopeDecision(
            allowed=True,
            scope=SCOPE_ACCOUNT,
            relation=relation,
            rule_description=rule_description,
            facts=facts,
        )
    if action == "unavailable":
        return NoteScopeDecision(
            allowed=False,
            required_scope=SCOPE_ACCOUNT,
            relation=relation,
            reason_code=REASON_GRANT_UNAVAILABLE,
            rule_description=rule_description,
            message=(
                "This note needs the wider 'account' note scope, and the rule "
                "that would grant it could not be evaluated, so it was "
                f"refused: {rule_description or 'policy evaluation failed'}."
            ),
            facts=facts,
        )

    if base_reason == REASON_NO_LINEAGE:
        message = (
            "send_note reaches only the runs this agent started (scope "
            f"'{SCOPE_DESCENDANTS}'), and this call carries no execution "
            "lineage, so it has no runs to reach. A wider scope "
            f"('{SCOPE_ACCOUNT}') has to be granted by a tool access rule on "
            "send_note."
        )
    else:
        message = (
            "send_note reaches only the runs this agent started (scope "
            f"'{SCOPE_DESCENDANTS}'): {_explain(relation)}. A wider scope "
            f"('{SCOPE_ACCOUNT}') has to be granted by a tool access rule on "
            "send_note."
        )
    return NoteScopeDecision(
        allowed=False,
        required_scope=SCOPE_ACCOUNT,
        relation=relation,
        reason_code=base_reason,
        message=message,
        facts=facts,
    )


def _consult_policy(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    author_execution_id: Optional[uuid.UUID],
    facts: Dict[str, Any],
    subject_context: Optional[Dict[str, Any]] = None,
) -> tuple[str, Optional[str], bool]:
    """Ask the tool policy path whether a rule widens the scope.

    The same evaluator, the same ``ToolAccessRule`` rows, the same subject
    chain and the same priority order the ``send_note`` call itself went
    through; only the bindings differ, because only here are the lineage
    facts known.

    Returns:
        ``(action, rule_description, from_rule)``. ``action`` is ``allow``,
        ``deny`` or ``unavailable``; ``from_rule`` says whether a rule decided,
        which is what separates a grant from the default allow a tool with no
        rules returns.
    """
    from preloop.services.approval_rule_context import (
        SOURCE_SUBJECT_SCOPED_RULE,
        SOURCE_TOOL_ACCESS_RULE,
    )
    from preloop.services.policy_evaluator import evaluate_policy
    from preloop.tools.builtin_defs import SEND_NOTE_TOOL

    try:
        decision = evaluate_policy(
            db,
            tool_name=SEND_NOTE_TOOL["name"],
            tool_args=facts,
            account_id=uuid.UUID(str(account_id)),
            execution_id=author_execution_id,
            subject_context=caller_subject_context(
                subject_context=subject_context,
                author_agent_id=author_agent_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a broken read must not widen scope
        logger.error("send_note scope grant evaluation failed: %s", exc, exc_info=True)
        return "unavailable", f"policy evaluation failed: {exc}", False

    action = decision[0]
    rule_description = decision[2]
    source = getattr(decision, "source", None)
    from_rule = source in (SOURCE_TOOL_ACCESS_RULE, SOURCE_SUBJECT_SCOPED_RULE)
    if action == "require_approval":
        # There is no one to ask at this point: the call already passed its
        # approval gate, and a rule asking for approval of a scope widening is
        # not a grant. Refuse and say so.
        return "unavailable", rule_description, from_rule
    if action == "deny":
        return "deny", rule_description, from_rule
    return "allow", rule_description if from_rule else None, from_rule


def _should_record_refusal(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    decision: NoteScopeDecision,
) -> bool:
    """Whether this refusal still deserves an audit row.

    Scope refusals spend no note budget, so the only bound on a looping
    agent is this record. A duplicate author+target+reason inside the
    window is skipped; a small per-author ceiling covers distinct targets.
    """
    facts = decision.facts or {}
    author = str(author_agent_id) if author_agent_id else None
    target_agent = facts.get("note_target_managed_agent_id")
    target_session = facts.get("note_target_runtime_session_id")
    reason = decision.reason_code
    since = datetime.now(timezone.utc).replace(tzinfo=None) - NOTE_SCOPE_DENIED_WINDOW
    rows = crud_audit_log.get_by_account(
        db,
        account_id=account_id,
        action=operator_notes.AUDIT_NOTE_SCOPE_DENIED,
        start_date=since,
        limit=max(NOTE_SCOPE_DENIED_CEILING * 8, 50),
    )
    from_author = 0
    for row in rows:
        details = row.details or {}
        if details.get("actor_managed_agent_id") != author:
            continue
        from_author += 1
        same_target = (
            details.get("managed_agent_id") == target_agent
            and details.get("runtime_session_id") == target_session
        )
        if details.get("reason_code") == reason and same_target:
            return False
    return from_author < NOTE_SCOPE_DENIED_CEILING


def audit_refusal(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    decision: NoteScopeDecision,
    commit: bool = True,
) -> None:
    """Record one refused note, with the reason code that refused it.

    Written whether or not anything else in the transaction survives: a note
    that was not written leaves no other trace, and "my agent says it cannot
    reach that run" has to be answerable from the record. Repeats of the
    same author, target and reason inside :data:`NOTE_SCOPE_DENIED_WINDOW`
    are skipped, as are further rows once the author has hit
    :data:`NOTE_SCOPE_DENIED_CEILING` in that window.
    """
    if not _should_record_refusal(
        db,
        account_id=account_id,
        author_agent_id=author_agent_id,
        decision=decision,
    ):
        return
    facts = decision.facts or {}
    try:
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=None,
            action=operator_notes.AUDIT_NOTE_SCOPE_DENIED,
            resource_type="operator_note",
            resource_id=facts.get("note_target_managed_agent_id")
            or facts.get("note_target_runtime_session_id"),
            status="denied",
            details={
                "actor_type": "managed_agent",
                "actor_managed_agent_id": (
                    str(author_agent_id) if author_agent_id else None
                ),
                "reason_code": decision.reason_code,
                "required_scope": decision.required_scope,
                "default_scope": SCOPE_DESCENDANTS,
                "target_relation": decision.relation,
                "managed_agent_id": facts.get("note_target_managed_agent_id"),
                "runtime_session_id": facts.get("note_target_runtime_session_id"),
                "target_execution_id": facts.get("note_target_execution_id"),
                "author_execution_id": facts.get("note_author_execution_id"),
                "rule_description": decision.rule_description,
            },
            commit=commit,
        )
    except Exception as exc:  # noqa: BLE001 - the refusal still stands
        logger.error("Failed to record a send_note scope refusal: %s", exc)
