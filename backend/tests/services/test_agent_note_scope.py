"""Which agent may note which target: the scope model for ``send_note``.

The default is descent, keyed on execution lineage, because lineage is the one
relationship the platform writes rather than accepts from the caller. These
tests pin the rule that is documented in ``docs/guide/operator-notes.md``:
a run reaches the runs it started at any depth and nothing else, a wider reach
is an explicit tool access rule, a deny above that rule wins, and a refusal
leaves an audit row carrying its own reason code (duplicates in a short window
do not).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_agent_control_command,
    crud_audit_log,
    crud_managed_agent,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services import agent_note_scope, operator_notes
from preloop.services.agent_send_note import send_note_from_agent
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    set_subject_governance,
)
from preloop.tools.builtin_defs import SEND_NOTE_TOOL

#: The grant expression the guide recommends. The ``has()`` guard is not
#: decoration: see ``test_the_documented_grant_expression_is_inert``.
GRANT_EXPRESSION = 'has(args.note_scope) && args.note_scope == "account"'


@pytest.fixture
def account(db_session):
    return crud_account.create(
        db_session,
        obj_in={"organization_name": "Delegation Org", "is_active": True},
    )


@pytest.fixture
def author(db_session, account):
    return crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account.id,
        display_name="Coordinator",
        commit=True,
    )


def _run(db_session, account, *, parent=None):
    """One flow execution, started by *parent* when there is one."""
    flow = models.Flow(
        account_id=account.id,
        name="Delegating flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(
        flow_id=flow.id,
        parent_execution_id=(parent.id if parent is not None else None),
        root_execution_id=(
            (parent.root_execution_id or parent.id) if parent is not None else None
        ),
        delegation_depth=((parent.delegation_depth or 0) + 1 if parent else 0),
    )
    db_session.add(execution)
    db_session.flush()
    return execution


def _agent_running(db_session, account, run, name):
    """An agent whose session is carrying *run*, which is how a note lands."""
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account.id,
        display_name=name,
        commit=True,
    )
    session = RuntimeSession(
        id=uuid4(),
        account_id=account.id,
        session_source_type="claude_code",
        session_source_id=f"workspace-{name.lower().replace(' ', '-')}",
        session_reference="/repo",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    agent.runtime_session_id = session.id
    db_session.flush()
    if run is not None:
        db_session.add(
            ApiUsage(
                account_id=account.id,
                endpoint="/v1/chat/completions",
                method="POST",
                status_code=200,
                duration=0.2,
                flow_execution_id=run.id,
                runtime_session_id=session.id,
                timestamp=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        db_session.flush()
    return agent, session


@pytest.fixture
def tree(db_session, account, author):
    """A delegation tree with the author in the middle of it.

    ``root`` started ``author_run`` and ``sibling_run``; ``author_run``
    started ``child_run``, which started ``grandchild_run``. Every run that
    can be a target has an agent and a session carrying it.
    """
    root = _run(db_session, account)
    author_run = _run(db_session, account, parent=root)
    sibling_run = _run(db_session, account, parent=root)
    child_run = _run(db_session, account, parent=author_run)
    grandchild_run = _run(db_session, account, parent=child_run)
    stranger_run = _run(db_session, account)

    root_agent, _ = _agent_running(db_session, account, root, "Root")
    sibling, _ = _agent_running(db_session, account, sibling_run, "Sibling")
    child, child_session = _agent_running(db_session, account, child_run, "Child")
    grandchild, _ = _agent_running(db_session, account, grandchild_run, "Grandchild")
    stranger, _ = _agent_running(db_session, account, stranger_run, "Stranger")
    unlaunched, _ = _agent_running(db_session, account, None, "Unlaunched")

    return {
        "root": root,
        "root_agent": root_agent,
        "author_run": author_run,
        "sibling_run": sibling_run,
        "sibling": sibling,
        "child_run": child_run,
        "child": child,
        "child_session": child_session,
        "grandchild_run": grandchild_run,
        "grandchild": grandchild,
        "stranger": stranger,
        "unlaunched": unlaunched,
    }


def _send(db_session, account, author, tree, target_agent, *, run="author_run"):
    """One note from the author's run to *target_agent*."""
    return send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree[run].id),
        text="The base branch moved; rebase before you push.",
        agent_id=str(target_agent.id),
    )


def _notes_written(db_session, account) -> int:
    from preloop.models.models.agent_control_command import AgentControlCommand

    return (
        db_session.query(AgentControlCommand)
        .filter(
            AgentControlCommand.account_id == account.id,
            AgentControlCommand.kind == "note",
        )
        .count()
    )


def _scope_refusals(db_session, account):
    """Audit rows written by a refused note, oldest first."""
    since = datetime.now(UTC) - timedelta(minutes=5)
    rows = [
        row
        for row in crud_audit_log.get_by_account(
            db_session, account_id=account.id, limit=100
        )
        if row.action == operator_notes.AUDIT_NOTE_SCOPE_DENIED
        and row.timestamp.replace(tzinfo=UTC) >= since
    ]
    # The account query answers newest first; a reader of this test wants the
    # refusals in the order the calls were made.
    return list(reversed(rows))


def _sent_audit(db_session, account, note_id):
    return next(
        row
        for row in crud_audit_log.get_by_account(
            db_session, account_id=account.id, limit=100
        )
        if row.action == operator_notes.AUDIT_NOTE_SENT and row.resource_id == note_id
    )


def _grant_rule(
    db_session,
    account,
    *,
    action="allow",
    expression=GRANT_EXPRESSION,
    condition_type="cel",
    priority=20,
    description="Coordinators may note any run in the account",
    config=None,
):
    """One tool access rule on ``send_note``, as the console would write it."""
    if config is None:
        config = models.ToolConfiguration(
            id=uuid4(),
            account_id=account.id,
            tool_name=SEND_NOTE_TOOL["name"],
            tool_source="builtin",
            is_enabled=True,
        )
        db_session.add(config)
        db_session.flush()
    rule = models.ToolAccessRule(
        id=uuid4(),
        account_id=account.id,
        tool_configuration_id=config.id,
        condition_type=condition_type,
        condition_expression=expression,
        action=action,
        priority=priority,
        description=description,
        is_enabled=True,
    )
    db_session.add(rule)
    db_session.flush()
    return config, rule


# --- the default scope ------------------------------------------------------


def test_an_agent_notes_the_run_it_started(db_session, account, author, tree):
    """The case the tool exists for: a hand off down the delegation."""
    result = _send(db_session, account, author, tree, tree["child"])

    assert result["ok"] is True
    assert result["note"]["managed_agent_id"] == str(tree["child"].id)
    row = _sent_audit(db_session, account, result["note"]["note_id"])
    assert row.details["note_scope"] == agent_note_scope.SCOPE_DESCENDANTS
    assert row.details["target_relation"] == agent_note_scope.RELATION_DESCENDANT
    assert row.details["scope_rule_description"] is None


def test_a_sibling_is_refused_with_the_scope_it_would_have_needed(
    db_session, account, author, tree
):
    """The same call, one hop sideways, is refused and says what it needed."""
    result = _send(db_session, account, author, tree, tree["sibling"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE
    assert agent_note_scope.SCOPE_DESCENDANTS in result["error"]["message"]
    assert result["error"]["required_scope"] == agent_note_scope.SCOPE_ACCOUNT
    assert result["error"]["target_relation"] == agent_note_scope.RELATION_SAME_TREE
    assert _notes_written(db_session, account) == 0


def test_a_grandchild_is_inside_the_default_scope(db_session, account, author, tree):
    """Delegation is transitive: a middleman does not widen anyone's reach."""
    result = _send(db_session, account, author, tree, tree["grandchild"])

    assert result["ok"] is True
    assert result["note"]["managed_agent_id"] == str(tree["grandchild"].id)
    row = _sent_audit(db_session, account, result["note"]["note_id"])
    assert row.details["note_scope"] == agent_note_scope.SCOPE_DESCENDANTS


def test_a_child_cannot_note_the_run_that_started_it(db_session, account, tree):
    """Descent has a direction. A child does not write into its parent's turn."""
    child_author = tree["child"]
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=child_author.id,
        author_execution_id=str(tree["child_run"].id),
        text="Stop what you are doing.",
        agent_id=str(tree["root_agent"].id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE
    assert result["error"]["target_relation"] == agent_note_scope.RELATION_ANCESTOR
    assert _notes_written(db_session, account) == 0


def test_an_agent_whose_run_started_nothing_can_note_nothing(
    db_session, account, author, tree
):
    """A leaf run has an empty descendant set, so every target is refused."""
    leaf = _run(db_session, account)

    for target in (tree["child"], tree["sibling"], tree["stranger"]):
        result = send_note_from_agent(
            db_session,
            account_id=str(account.id),
            author_agent_id=author.id,
            author_execution_id=str(leaf.id),
            text="anyone there",
            agent_id=str(target.id),
        )
        assert result["ok"] is False
        assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE

    assert _notes_written(db_session, account) == 0


def test_a_caller_with_no_execution_at_all_is_refused_with_its_own_reason(
    db_session, account, author, tree
):
    """An agent not running inside an execution has no lineage to key on."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="from nowhere",
        agent_id=str(tree["child"].id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_NO_LINEAGE
    assert "no execution or session lineage" in result["error"]["message"]
    assert result["error"]["required_scope"] == agent_note_scope.SCOPE_ACCOUNT
    assert _notes_written(db_session, account) == 0


def test_a_target_that_is_running_nothing_is_out_of_scope(
    db_session, account, author, tree
):
    """An agent with no run behind it cannot be a descendant of anything."""
    result = _send(db_session, account, author, tree, tree["unlaunched"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE
    assert result["error"]["target_relation"] == agent_note_scope.RELATION_NO_LINEAGE


def test_a_session_target_is_scoped_the_same_way_as_an_agent_target(
    db_session, account, author, tree
):
    """Naming the session instead of the agent does not skip the check."""
    allowed = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree["author_run"].id),
        text="Rebase first.",
        runtime_session_id=str(tree["child_session"].id),
    )
    assert allowed["ok"] is True

    refused = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree["sibling_run"].id),
        text="Rebase first.",
        runtime_session_id=str(tree["child_session"].id),
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


def test_an_execution_target_is_scoped_the_same_way(db_session, account, author, tree):
    """Nor does naming the execution outright."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree["author_run"].id),
        text="Your base branch moved.",
        execution_id=str(tree["sibling_run"].id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


# --- the grant --------------------------------------------------------------


def test_a_rule_grants_the_wider_scope_and_revoking_it_takes_it_back(
    db_session, account, author, tree
):
    """The refused call, permitted by a rule, refused again when it is gone."""
    before = _send(db_session, account, author, tree, tree["sibling"])
    assert before["ok"] is False

    _config, rule = _grant_rule(db_session, account)

    granted = _send(db_session, account, author, tree, tree["sibling"])
    assert granted["ok"] is True
    assert granted["note"]["managed_agent_id"] == str(tree["sibling"].id)
    row = _sent_audit(db_session, account, granted["note"]["note_id"])
    assert row.details["note_scope"] == agent_note_scope.SCOPE_ACCOUNT
    assert row.details["scope_rule_description"] == rule.description

    # Revoked in the same process, with nothing restarted and no cache to
    # invalidate: the next call reads the rules again.
    rule.is_enabled = False
    db_session.flush()

    after = _send(db_session, account, author, tree, tree["sibling"])
    assert after["ok"] is False
    assert after["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE
    assert _notes_written(db_session, account) == 1


def test_a_simple_expression_grants_as_well_as_a_cel_one(
    db_session, account, author, tree
):
    """Both condition types read the same facts, so either can express it."""
    _grant_rule(
        db_session,
        account,
        condition_type="simple",
        expression='args.note_target_relation == "same_tree"',
        description="Runs in one delegation tree may note each other",
    )

    assert _send(db_session, account, author, tree, tree["sibling"])["ok"] is True
    # The same rule does not reach a run in a different tree.
    refused = _send(db_session, account, author, tree, tree["stranger"])
    assert refused["ok"] is False
    assert refused["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


def test_a_deny_rule_beats_a_grant(db_session, account, author, tree):
    """Priority order decides, as for every other tool: the deny is above."""
    config, _grant = _grant_rule(db_session, account, priority=20)
    _grant_rule(
        db_session,
        account,
        config=config,
        action="deny",
        condition_type="simple",
        expression='args.note_scope == "account"',
        priority=10,
        description="No account wide notes",
    )

    result = _send(db_session, account, author, tree, tree["sibling"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_DENIED_BY_RULE
    assert "No account wide notes" in result["error"]["message"]
    assert _notes_written(db_session, account) == 0


def test_the_default_allow_of_a_tool_with_no_rules_is_not_a_grant(
    db_session, account, author, tree
):
    """Enabling send_note must not quietly mean "note anyone in the account"."""
    config = models.ToolConfiguration(
        id=uuid4(),
        account_id=account.id,
        tool_name=SEND_NOTE_TOOL["name"],
        tool_source="builtin",
        is_enabled=True,
    )
    db_session.add(config)
    db_session.flush()

    result = _send(db_session, account, author, tree, tree["sibling"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


def test_a_rule_that_does_not_match_leaves_the_default_in_place(
    db_session, account, author, tree
):
    """A grant for someone else is not a grant for this caller."""
    _grant_rule(
        db_session,
        account,
        condition_type="simple",
        expression=f'args.note_author_managed_agent_id == "{uuid4()}"',
        description="Another agent may note anyone",
    )

    result = _send(db_session, account, author, tree, tree["sibling"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


def test_a_rule_asking_for_approval_is_not_a_grant(db_session, account, author, tree):
    """There is nobody to ask here, and "maybe" must not read as "yes"."""
    _grant_rule(
        db_session,
        account,
        action="require_approval",
        condition_type="simple",
        expression='args.note_scope == "account"',
        description="Escalate account wide notes",
    )

    result = _send(db_session, account, author, tree, tree["sibling"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_GRANT_UNAVAILABLE
    assert _notes_written(db_session, account) == 0


def test_a_grant_cannot_reach_into_another_account(
    db_session, account, author, tree, monkeypatch
):
    """The account boundary is not a scope, so no rule can widen past it."""
    other = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Org", "is_active": True},
    )
    foreign = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=other.id,
        display_name="Their Worker",
        commit=True,
    )
    _grant_rule(db_session, account)

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree["author_run"].id),
        text="leak",
        agent_id=str(foreign.id),
    )

    assert result["ok"] is False
    # Not "out of scope": an id in another account does not exist here at all.
    assert result["error"]["code"] == "target_not_found"
    assert (
        crud_agent_control_command.list_notes(
            db_session, account_id=other.id, managed_agent_id=foreign.id
        )
        == []
    )


def test_a_broken_rule_refuses_rather_than_widening(
    db_session, account, author, tree, monkeypatch
):
    """Fail closed: the grant path must not be the way around the default."""

    def _explode(*args, **kwargs):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr("preloop.services.policy_evaluator.evaluate_policy", _explode)

    result = _send(db_session, account, author, tree, tree["sibling"])

    assert result["ok"] is False
    assert result["error"]["code"] == agent_note_scope.REASON_GRANT_UNAVAILABLE
    assert _notes_written(db_session, account) == 0


# --- the record -------------------------------------------------------------


def test_every_refusal_writes_one_audit_row_with_its_own_reason_code(
    db_session, account, author, tree
):
    """Four refusals, four rows, four distinct codes, no note rows."""
    _send(db_session, account, author, tree, tree["sibling"])
    send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="from nowhere",
        agent_id=str(tree["child"].id),
    )
    _grant_rule(
        db_session,
        account,
        action="deny",
        condition_type="simple",
        expression='args.note_scope == "account"',
        priority=5,
        description="No account wide notes",
    )
    _send(db_session, account, author, tree, tree["stranger"])

    rows = _scope_refusals(db_session, account)
    assert len(rows) == 3
    assert [row.details["reason_code"] for row in rows] == [
        agent_note_scope.REASON_OUT_OF_SCOPE,
        agent_note_scope.REASON_NO_LINEAGE,
        agent_note_scope.REASON_DENIED_BY_RULE,
    ]
    for row in rows:
        assert row.status == "denied"
        assert row.user_id is None
        assert row.details["actor_type"] == "managed_agent"
        assert row.details["actor_managed_agent_id"] == str(author.id)
        assert row.details["required_scope"] == agent_note_scope.SCOPE_ACCOUNT
        assert row.details["default_scope"] == agent_note_scope.SCOPE_DESCENDANTS
    assert rows[-1].details["rule_description"] == "No account wide notes"
    assert _notes_written(db_session, account) == 0


def test_a_permitted_note_writes_no_refusal_row(db_session, account, author, tree):
    """The refusal action is written only when something was refused."""
    assert _send(db_session, account, author, tree, tree["child"])["ok"] is True

    assert _scope_refusals(db_session, account) == []


# --- what the caller cannot do ----------------------------------------------


def test_the_tool_takes_no_lineage_argument(db_session):
    """An agent that could name its own lineage could name any lineage."""
    properties = SEND_NOTE_TOOL["schema"]["properties"]
    assert set(properties) == {
        "text",
        "agent_id",
        "runtime_session_id",
        "execution_id",
        # Aliases for a target, not lineage claims: ``children`` is resolved
        # against the caller's session as the platform recorded it (#1045).
        "external_session_id",
        "children",
    }
    assert SEND_NOTE_TOOL["schema"]["additionalProperties"] is False
    assert "scope" in SEND_NOTE_TOOL["description"] or (
        "started" in SEND_NOTE_TOOL["description"]
    )


def test_the_documented_grant_expression_is_inert_on_a_plain_call():
    """Why the guide writes ``has()``: the scope facts exist only at the scope
    evaluation, and a CEL rule that reads a missing key fails the call closed.
    """
    from preloop.services.policy_evaluator import _evaluate_rule_condition

    call_args = {"text": "Rebase.", "agent_id": str(uuid4())}

    assert (
        _evaluate_rule_condition(
            expression=GRANT_EXPRESSION,
            condition_type="cel",
            tool_args=call_args,
            context={},
        )
        is False
    )
    with pytest.raises(ValueError):
        _evaluate_rule_condition(
            expression='args.note_scope == "account"',
            condition_type="cel",
            tool_args=call_args,
            context={},
        )
    # The simple evaluator reads a missing key as a non-match, so the same
    # rule written in simple form needs no guard.
    assert (
        _evaluate_rule_condition(
            expression='args.note_scope == "account"',
            condition_type="simple",
            tool_args=call_args,
            context={},
        )
        is False
    )


def test_the_lineage_walk_is_bounded(db_session, account, author, tree, monkeypatch):
    """A cycle written by a bug must not become a loop on this path."""
    a = _run(db_session, account)
    b = _run(db_session, account, parent=a)
    a.parent_execution_id = b.id
    db_session.flush()

    chain = agent_note_scope.ancestor_chain(
        db_session, account_id=str(account.id), execution_id=a.id
    )

    assert chain == [a.id, b.id]


def test_shared_ancestor_segments_are_walked_once(
    db_session, account, author, tree, monkeypatch
):
    """Overlapping chains in one relation check reuse already-walked ids."""
    from preloop.models.crud import crud_flow_execution

    calls: list = []
    original = crud_flow_execution.get

    def counting_get(db, id, **kwargs):
        calls.append(id)
        return original(db, id, **kwargs)

    monkeypatch.setattr(crud_flow_execution, "get", counting_get)

    agent_note_scope._relation(
        db_session,
        account_id=str(account.id),
        author_execution_id=tree["author_run"].id,
        target_ids=[
            tree["grandchild_run"].id,
            tree["child_run"].id,
            tree["sibling_run"].id,
        ],
    )

    assert calls
    assert len(calls) == len(set(calls))


def test_an_api_key_scoped_grant_is_seen_on_the_grant_path(
    db_session, account, author, tree
):
    """The grant consults the same subject chain as the preceding send_note.

    Flow-runtime send_note governance often lives on the API-key subject.
    Omitting api_key_id would make this evaluation miss that grant.
    """
    api_key_id = str(uuid4())
    caller_session_id = str(uuid4())
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=api_key_id,
        config={
            "tool_rules": {
                SEND_NOTE_TOOL["name"]: [
                    {
                        "action": "allow",
                        "description": "Flow key may note anyone in the account",
                    }
                ]
            }
        },
    )
    db_session.flush()

    caller = {
        "api_key_id": api_key_id,
        "managed_agent_id": str(author.id),
        "runtime_session_id": caller_session_id,
    }
    granted = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree["author_run"].id),
        text="The base branch moved; rebase before you push.",
        agent_id=str(tree["sibling"].id),
        subject_context=caller,
    )
    assert granted["ok"] is True

    missed = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        author_execution_id=str(tree["author_run"].id),
        text="The base branch moved; rebase before you push.",
        agent_id=str(tree["stranger"].id),
    )
    assert missed["ok"] is False
    assert missed["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE


def test_the_grant_subject_context_keeps_the_caller_session():
    """Target identity is not copied onto the caller's subject chain."""
    context = agent_note_scope.caller_subject_context(
        subject_context={
            "api_key_id": "key-1",
            "managed_agent_id": "agent-1",
            "runtime_session_id": "caller-session",
        },
        author_agent_id="other-agent",
    )
    assert context["api_key_id"] == "key-1"
    assert context["managed_agent_id"] == "agent-1"
    assert context["runtime_session_id"] == "caller-session"


def test_a_repeated_scope_refusal_does_not_write_another_audit_row(
    db_session, account, author, tree
):
    """A looping agent does not fill the audit log or spend the note budget."""
    first = _send(db_session, account, author, tree, tree["sibling"])
    second = _send(db_session, account, author, tree, tree["sibling"])

    assert first["ok"] is False
    assert second["ok"] is False
    assert first["error"]["code"] == agent_note_scope.REASON_OUT_OF_SCOPE
    assert len(_scope_refusals(db_session, account)) == 1
    assert _notes_written(db_session, account) == 0


def test_scope_refusal_audit_rows_have_a_per_author_ceiling(
    db_session, account, author, tree, monkeypatch
):
    """Distinct targets still cannot append unbounded refusal rows."""
    monkeypatch.setattr(agent_note_scope, "NOTE_SCOPE_DENIED_CEILING", 1)

    _send(db_session, account, author, tree, tree["sibling"])
    _send(db_session, account, author, tree, tree["stranger"])

    assert len(_scope_refusals(db_session, account)) == 1
    assert _notes_written(db_session, account) == 0
