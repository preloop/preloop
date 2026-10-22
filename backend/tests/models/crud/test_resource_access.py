"""Portable exact-resource gates, atomic owner writes and preview discipline."""

from __future__ import annotations

from tests.credential_fixtures import ai_model_with_secret

import threading
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud.access_rule import (
    AccessReferenceError,
    AccessRuleConflictError,
    apply_policy_section,
    validate_policy_section,
)
from preloop.schemas.access_rule import (
    AccessRuleDefinition,
    ResourceAccessRequirement,
    ResourceAccessUpdate,
)
from preloop.services.policy.loader import (
    compute_policy_diff,
    export_current_policy,
    load_policy_from_string,
)

from .test_access_rule import storage  # noqa: F401 - shared PostgreSQL fixture


@pytest.fixture
def resource(storage: Any) -> Any:  # noqa: F811 - imported shared fixture
    crud, owners = storage
    account, user = owners[0]
    ids = []
    with crud.session() as db, db.begin():
        for owner, _ in owners:
            row = ai_model_with_secret(
                account_id=owner,
                name="Example model",
                provider_name="example",
                model_identifier="example-model",
                api_key="synthetic-secret",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
    try:
        yield crud, owners, ids
    finally:
        with crud.session() as db, db.begin():
            for key in ids:
                row = db.get(models.AIModel, key)
                if row:
                    db.delete(row)


def local_rule(resource_id: UUID, **values: Any) -> AccessRuleDefinition:
    raw = dict(
        id=uuid4(),
        name="Example local permit",
        effect="permit",
        actions=["model:invoke"],
        resource_type="ai_model",
        resource_selector={"ids": [str(resource_id)]},
    )
    raw.update(values)
    return AccessRuleDefinition.model_validate(raw)


def update(rule: AccessRuleDefinition, restricted: bool = True) -> ResourceAccessUpdate:
    return ResourceAccessUpdate(
        rules=[rule],
        requirements=[
            ResourceAccessRequirement(action="model:invoke", permit_rule_ids=[rule.id])
        ]
        if restricted
        else [],
    )


def save(resource: Any, document: ResourceAccessUpdate, **tokens: Any) -> Any:
    crud, owners, ids = resource
    account, user = owners[0]
    current = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    return crud.write_resource_access(
        account_id=account,
        user_id=user,
        resource_type="ai_model",
        resource_id=ids[0],
        document=document,
        expected_version=tokens.get("version", current.version),
        expected_generation=tokens.get("generation", current.generation),
        losing_subjects=[],
    )


@pytest.mark.parametrize(
    "selector,type_",
    [
        ({"ids": [str(uuid4())]}, None),
        ({"ids": ["invalid"]}, "ai_model"),
        ({"ids": ["11111111-1111-4111-8111-111111111111"] * 2}, "ai_model"),
        ({"ids": [str(uuid4()) for _ in range(101)]}, "ai_model"),
    ],
)
def test_reject_invalid_resource_ids(selector: Any, type_: Any) -> None:
    with pytest.raises(ValidationError):
        AccessRuleDefinition(
            name="Example",
            effect="permit",
            actions=["model:invoke"],
            resource_selector=selector,
            resource_type=type_,
        )


def test_legacy_and_ids_plus_tags_roundtrip() -> None:
    legacy = AccessRuleDefinition(name="Legacy", effect="forbid", actions=["tool:call"])
    assert legacy.resource_selector.ids == []
    key = uuid4()
    parsed = AccessRuleDefinition.model_validate(
        {
            "name": "Intersection",
            "effect": "permit",
            "actions": ["model:invoke"],
            "resource": {
                "type": "ai_model",
                "ids": [str(key)],
                "matchLabels": {"env": "production"},
                "account": {"matchLabels": {"group": "example"}},
            },
        }
    )
    assert parsed.resource_selector.ids == [key]
    assert parsed.resource_selector.match_labels == {"env": "production"}
    assert parsed.resource_selector.account.match_labels == {"group": "example"}
    assert AccessRuleDefinition.model_validate_json(parsed.model_dump_json()) == parsed


def test_owner_type_and_subject_references(resource: Any) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    for rule in (
        local_rule(ids[1]),
        local_rule(ids[0], resource_type="mcp_server"),
        local_rule(
            ids[0], subject_selector={"kind": "user", "ids": [str(owners[1][1])]}
        ),
        local_rule(ids[0], subject_selector={"teams": [str(uuid4())]}),
        local_rule(ids[0], subject_selector={"roles": [str(uuid4())]}),
    ):
        with pytest.raises(AccessReferenceError):
            save(resource, update(rule))
    valid = local_rule(ids[0], subject_selector={"kind": "user", "ids": [str(user)]})
    assert save(resource, update(valid)).rules[0].subject_selector.ids == [user]
    with pytest.raises(AccessReferenceError):
        crud.resource_access(
            account_id=owners[1][0], resource_type="ai_model", resource_id=ids[0]
        )


def test_atomic_local_write_version_metadata_and_no_account_mode_mutation(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    broad = crud.write(
        account_id=account,
        user_id=user,
        definition=AccessRuleDefinition(
            name="Broad permit", effect="permit", actions=["model:invoke"]
        ),
    )
    with crud.session() as db, db.begin():
        row = db.get(models.Account, account)
        row.meta_data = {
            "unrelated": {"setting": 17},
            "access_rule_mode": {"model:invoke": "require_permit"},
        }
    initial = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    assert initial.version == 0 and initial.requirements == []
    rule = local_rule(ids[0])
    saved = save(resource, update(rule))
    assert saved.version == 1 and saved.generation > initial.generation
    assert saved.requirements[0].permit_rule_ids == [rule.id]
    with crud.session() as db:
        metadata = db.get(models.Account, account).meta_data
        assert metadata["unrelated"] == {"setting": 17}
        assert metadata["access_rule_mode"] == {"model:invoke": "require_permit"}
        snapshot = db.scalars(
            select(models.PolicySnapshot)
            .where(models.PolicySnapshot.account_id == account)
            .order_by(models.PolicySnapshot.version_number.desc())
        ).first()
        assert snapshot.snapshot_data["resource_access"][0]["requirements"][0][
            "permit_rule_ids"
        ] == [str(rule.id)]
        audit = db.scalars(
            select(models.AuditLog).where(
                models.AuditLog.account_id == account,
                models.AuditLog.resource_type == "resource_access",
            )
        ).first()
        assert audit.details["before"]["version"] == 0
        assert audit.details["before"]["requirements"] == []
        assert audit.details["after"]["version"] == 1
        assert audit.details["after"]["requirements"][0]["permit_rule_ids"] == [
            str(rule.id)
        ]
    with pytest.raises(AccessRuleConflictError):
        save(resource, update(rule), version=0)
    with pytest.raises(AccessRuleConflictError):
        save(resource, update(rule), generation=initial.generation)
    for operation in ("write", "delete"):
        with pytest.raises(AccessRuleConflictError, match="resource access document"):
            if operation == "write":
                crud.write(
                    account_id=account,
                    user_id=user,
                    rule_id=rule.id,
                    expected_version=1,
                    definition=rule,
                )
            else:
                crud.delete(
                    account_id=account,
                    user_id=user,
                    rule_id=rule.id,
                    expected_version=1,
                )
    cleared = save(resource, ResourceAccessUpdate())
    assert cleared.version == 2 and cleared.requirements == [] and cleared.rules == []
    assert [row["id"] for row in crud.rules(account_id=account)] == [broad["id"]]


@pytest.mark.parametrize(
    "change",
    [
        {"resource_selector": {}},
        {"scope": "self_and_subaccounts"},
        {"effect": "forbid"},
        {"is_enabled": False},
        {"actions": ["tool:call"]},
        {"resource_selector": {"ids": []}},
    ],
)
def test_restrictions_cannot_bind_broad_or_mismatched_rules(
    resource: Any, change: Any
) -> None:
    crud, owners, ids = resource
    account, _ = owners[0]
    generation = crud.generation(account_id=account)
    with pytest.raises(AccessRuleConflictError):
        save(resource, update(local_rule(ids[0], **change)))
    assert crud.generation(account_id=account) == generation
    assert crud.rules(account_id=account) == []


def test_dangling_foreign_duplicate_and_wrong_action_bindings(resource: Any) -> None:
    crud, owners, ids = resource
    rule = local_rule(ids[0])
    with pytest.raises(ValidationError):
        save(
            resource,
            ResourceAccessUpdate(
                rules=[rule],
                requirements=[
                    ResourceAccessRequirement(
                        action="model:invoke", permit_rule_ids=[uuid4()]
                    )
                ],
            ),
        )
    with pytest.raises(ValidationError):
        save(
            resource,
            ResourceAccessUpdate(
                rules=[rule],
                requirements=[
                    ResourceAccessRequirement(action="model:invoke"),
                    ResourceAccessRequirement(action="model:invoke"),
                ],
            ),
        )
    with pytest.raises(AccessRuleConflictError):
        save(resource, ResourceAccessUpdate(rules=[rule, rule]))
    with pytest.raises(AccessRuleConflictError, match="mismatched"):
        save(
            resource,
            ResourceAccessUpdate(
                rules=[rule],
                requirements=[
                    ResourceAccessRequirement(
                        action="resource:view", permit_rule_ids=[rule.id]
                    )
                ],
            ),
        )
    foreign = crud.write(
        account_id=owners[1][0], user_id=owners[1][1], definition=local_rule(ids[1])
    )
    with pytest.raises(AccessRuleConflictError, match="already in use"):
        save(resource, update(local_rule(ids[0], id=foreign["id"])))


def test_team_selection_alone_inherits_and_empty_restriction_denies_all(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    with crud.session() as db, db.begin():
        team = models.Team(account_id=owners[0][0], name="Example team")
        db.add(team)
        db.flush()
        team_id = team.id
    saved = save(
        resource,
        update(local_rule(ids[0], subject_selector={"teams": [str(team_id)]}), False),
    )
    assert saved.requirements == []
    assert saved.rules[0].subject_selector.teams == [str(team_id)]
    restricted = save(
        resource,
        ResourceAccessUpdate(
            requirements=[ResourceAccessRequirement(action="model:invoke")]
        ),
    )
    assert restricted.requirements[0].permit_rule_ids == []


def test_export_import_preserves_and_rejects_tightening_and_dangling_removal(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    with crud.session() as db:
        original = export_current_policy(db, account)
    rule = local_rule(ids[0])
    saved = save(resource, update(rule))
    with crud.session() as db:
        exported = export_current_policy(db, account)
        assert exported.resource_access[0].resource_id == ids[0]
        paths = [item.path for item in compute_policy_diff(original, exported).changes]
        assert "$.resource_access" in paths
        parsed, validation = load_policy_from_string(exported.model_dump_json())
        assert validation.is_valid
        assert parsed is not None
        assert parsed.resource_access == exported.resource_access
    with crud.session() as db, db.begin():
        apply_policy_section(
            db,
            account,
            user,
            exported.access_rules,
            exported.access_rule_mode,
            exported.resource_access,
        )
    with crud.session() as db:
        with pytest.raises(AccessRuleConflictError, match="preview"):
            changed = exported.resource_access[0].model_copy(
                update={
                    "requirements": [ResourceAccessRequirement(action="model:invoke")]
                }
            )
            validate_policy_section(db, account, exported.access_rules, None, [changed])
        with pytest.raises(AccessRuleConflictError, match="exact-resource"):
            validate_policy_section(db, account, [], None)
    save(resource, ResourceAccessUpdate())
    with crud.session() as db:
        with pytest.raises(AccessRuleConflictError, match="preview"):
            validate_policy_section(
                db, account, exported.access_rules, None, exported.resource_access
            )
    assert saved.generation > 0


def test_bundle_preserves_independent_account_ancestor_and_local_gates(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    child, _ = owners[1]
    with crud.session() as db, db.begin():
        row = db.get(models.Account, child)
        row.parent_account_id = account
        row.hierarchy_path = [account, child]
        row.root_account_id = account
        row.hierarchy_depth = 1
    saved = save(resource, update(local_rule(ids[0])))
    crud.set_mode(
        account_id=account,
        user_id=user,
        action="model:invoke",
        mode="require_permit",
        expected_generation=crud.generation(account_id=account),
        losing_subjects=[],
    )
    broad = crud.write(
        account_id=account,
        user_id=user,
        definition=AccessRuleDefinition(
            name="Broad parent permit",
            effect="permit",
            actions=["model:invoke"],
            scope="self_and_subaccounts",
        ),
    )
    bundle = crud.bundle(account_id=child)
    binding = bundle["resource_access"][0]
    assert binding["account_id"] == str(account)
    assert binding["resource_id"] == str(ids[0])
    assert binding["requirements"][0]["permit_rule_ids"] == [str(saved.rules[0].id)]
    assert str(broad["id"]) not in binding["requirements"][0]["permit_rule_ids"]
    assert bundle["modes"]["model:invoke"] == "require_permit"
    assert bundle["ancestor_require_permit"] == ["model:invoke"]
    # Local permits travel with the gate even for an ancestor's scope=self rule,
    # and never union into the account/ancestor allowlist.
    for key in (account, child):
        detached = crud.bundle(account_id=key)
        assert [row["id"] for row in detached["rules"]] == [broad["id"]]
        assert [row["id"] for row in detached["resource_access_rules"]] == [
            saved.rules[0].id
        ]
        assert detached["resource_access_rules"][0]["account_id"] == str(account)
    assert "synthetic-secret" not in repr(bundle)


def test_unrelated_metadata_concurrent_writer_survives(resource: Any) -> None:
    crud, owners, ids = resource
    account, _ = owners[0]
    initial = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    with crud.session() as db, db.begin():
        row = db.get(models.Account, account)
        row.meta_data = {**(row.meta_data or {}), "concurrent_setting": "preserved"}
    with pytest.raises(AccessRuleConflictError, match="preview"):
        save(resource, update(local_rule(ids[0])), generation=initial.generation)
    save(resource, update(local_rule(ids[0])))
    with crud.session() as db:
        assert (
            db.get(models.Account, account).meta_data["concurrent_setting"]
            == "preserved"
        )


def test_local_requirement_committed_invalidation_and_rollback(
    resource: Any, monkeypatch: Any
) -> None:
    import preloop.models.crud.access_rule as module

    crud, owners, ids = resource
    account, _ = owners[0]
    ready, stop, received = threading.Event(), threading.Event(), threading.Event()

    def changed(key: str, generation: int) -> None:
        if key == str(account):
            received.set()

    thread = threading.Thread(
        target=crud.listen, args=(changed, ready, stop), daemon=True
    )
    thread.start()
    original = module._record_change
    generation = crud.generation(account_id=account)

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("Synthetic snapshot failure")

    try:
        assert ready.wait(5)
        monkeypatch.setattr(module, "_record_change", fail)
        with pytest.raises(RuntimeError, match="snapshot failure"):
            save(resource, update(local_rule(ids[0])))
        assert crud.generation(account_id=account) == generation
        assert crud.rules(account_id=account) == []
        assert not received.wait(0.2)
        monkeypatch.setattr(module, "_record_change", original)
        save(resource, update(local_rule(ids[0])))
        assert received.wait(5)
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive()


def test_yaml_cannot_detach_local_permits_or_edit_active_allowlists(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    saved = save(resource, update(local_rule(ids[0])))
    with crud.session() as db:
        exported = export_current_policy(db, account)
        with pytest.raises(AccessRuleConflictError, match="detached"):
            validate_policy_section(db, account, None, None, [])
        altered = saved.rules[0].model_copy(
            update={
                "subject_selector": saved.rules[0].subject_selector.model_copy(
                    update={"kind": "user", "ids": [user]}
                )
            }
        )
        with pytest.raises(AccessRuleConflictError, match="preview"):
            validate_policy_section(
                db, account, [altered], None, exported.resource_access
            )
    with crud.session() as db, db.begin():
        apply_policy_section(db, account, user, [], None, [])
    assert crud.rules(account_id=account) == []
    cleared = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    assert cleared.requirements == [] and cleared.rules == []


def test_foreign_resource_rule_ids_are_validated_in_general_crud_and_import(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    for rule in (local_rule(ids[1]), local_rule(ids[0], resource_type="mcp_server")):
        with pytest.raises(AccessReferenceError):
            crud.write(account_id=account, user_id=user, definition=rule)
        with crud.session() as db:
            with pytest.raises(AccessReferenceError):
                validate_policy_section(db, account, [rule], None)


def test_two_resource_documents_cannot_steal_each_others_permits(resource: Any) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    saved = save(resource, update(local_rule(ids[0])))
    with crud.session() as db, db.begin():
        other = models.AIModel(
            account_id=account,
            name="Another example",
            provider_name="example",
            model_identifier="other-example",
        )
        db.add(other)
        db.flush()
        other_id = other.id
    try:
        with pytest.raises(AccessRuleConflictError, match="already in use"):
            crud.write_resource_access(
                account_id=account,
                user_id=user,
                resource_type="ai_model",
                resource_id=other_id,
                document=update(local_rule(other_id, id=saved.rules[0].id)),
                expected_version=0,
                expected_generation=crud.generation(account_id=account),
                losing_subjects=[],
            )
        assert (
            crud.resource_access(
                account_id=account, resource_type="ai_model", resource_id=ids[0]
            ).rules
            == saved.rules
        )
    finally:
        with crud.session() as db, db.begin():
            db.delete(db.get(models.AIModel, other_id))


def test_requirement_only_write_invalidates_and_unsupported_action_rolls_back(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, _ = owners[0]
    initial = crud.generation(account_id=account)
    saved = save(
        resource,
        ResourceAccessUpdate(
            requirements=[ResourceAccessRequirement(action="model:invoke")]
        ),
    )
    assert saved.generation > initial
    assert saved.rules == []
    assert (
        crud.bundle(account_id=account)["resource_access"][0]["requirements"][0][
            "permit_rule_ids"
        ]
        == []
    )
    with pytest.raises(AccessRuleConflictError, match="unsupported"):
        save(
            resource,
            ResourceAccessUpdate(
                requirements=[ResourceAccessRequirement(action="runner:accept")]
            ),
        )
    assert crud.generation(account_id=account) == saved.generation
    assert (
        crud.resource_access(
            account_id=account, resource_type="ai_model", resource_id=ids[0]
        )
        == saved
    )


def test_concurrent_resource_writers_only_one_generation_can_commit(
    resource: Any,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    crud, owners, ids = resource
    account, user = owners[0]
    initial = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    barrier = threading.Barrier(2)

    def write() -> str:
        rule = local_rule(ids[0])
        barrier.wait(timeout=5)
        try:
            crud.write_resource_access(
                account_id=account,
                user_id=user,
                resource_type="ai_model",
                resource_id=ids[0],
                document=update(rule),
                expected_version=initial.version,
                expected_generation=initial.generation,
                losing_subjects=[],
            )
            return "committed"
        except AccessRuleConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: write(), range(2)))
    assert sorted(outcomes) == ["committed", "conflict"]
    saved = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    assert saved.version == 1 and len(saved.rules) == 1
    assert saved.requirements[0].permit_rule_ids == [saved.rules[0].id]


@pytest.mark.parametrize("delete_kind", ["crud", "bulk"])
def test_deleted_target_cleanup_is_atomic_and_preserves_other_resources(
    resource: Any, delete_kind: str
) -> None:
    from sqlalchemy import delete
    from preloop.models.crud import crud_ai_model

    crud, owners, ids = resource
    account, user = owners[0]
    local = save(resource, update(local_rule(ids[0])))
    with crud.session() as db, db.begin():
        other = models.AIModel(
            account_id=account,
            name="Other model",
            provider_name="example",
            model_identifier="other",
        )
        db.add(other)
        db.flush()
        other_id = other.id
        row = db.get(models.Account, account)
        row.meta_data = {**row.meta_data, "unrelated_setting": "preserved"}
    singleton = crud.write(
        account_id=account,
        user_id=user,
        definition=local_rule(ids[0], name="Ordinary singleton"),
    )
    multiple = crud.write(
        account_id=account,
        user_id=user,
        definition=local_rule(
            ids[0],
            name="Ordinary multiple",
            resource_selector={"ids": [str(ids[0]), str(other_id)]},
        ),
    )
    try:
        with crud.session() as db:
            if delete_kind == "crud":
                crud_ai_model.remove(db, id=ids[0])
            else:
                db.execute(delete(models.AIModel).where(models.AIModel.id == ids[0]))
                db.commit()
        rules = crud.rules(account_id=account)
        assert [row["id"] for row in rules] == [multiple["id"]]
        assert rules[0]["resource_selector"]["ids"] == [str(other_id)]
        assert rules[0]["version"] == 2
        assert singleton["id"] != local.rules[0].id
        with crud.session() as db, db.begin():
            owner = db.get(models.Account, account)
            assert owner.meta_data["resource_access"] == []
            assert owner.meta_data["unrelated_setting"] == "preserved"
            exported = export_current_policy(db, account)
            apply_policy_section(
                db,
                account,
                user,
                exported.access_rules,
                exported.access_rule_mode,
                exported.resource_access,
            )
        # A deleted resource no longer blocks unrelated edits or policy roundtrips.
        changed = crud.write_resource_access(
            account_id=account,
            user_id=user,
            resource_type="ai_model",
            resource_id=other_id,
            document=update(local_rule(other_id, name="Other local permit")),
            expected_version=0,
            expected_generation=crud.generation(account_id=account),
            losing_subjects=[],
        )
        assert changed.version == 1
        assert all(
            item["resource_id"] != str(ids[0])
            for item in crud.bundle(account_id=account)["resource_access"]
        )
    finally:
        with crud.session() as db, db.begin():
            db.delete(db.get(models.AIModel, other_id))


def test_target_deletion_rollback_preserves_gate_and_generation(resource: Any) -> None:
    crud, owners, ids = resource
    account, _ = owners[0]
    saved = save(resource, update(local_rule(ids[0])))
    with crud.session() as db:
        db.delete(db.get(models.AIModel, ids[0]))
        db.flush()
        db.rollback()
        db.commit()  # A later commit cannot replay cleanup for the rolled-back delete.
    assert (
        crud.resource_access(
            account_id=account, resource_type="ai_model", resource_id=ids[0]
        )
        == saved
    )


def test_cleanup_failure_rolls_back_target_deletion_and_local_gate(
    resource: Any, monkeypatch: Any
) -> None:
    import preloop.models.crud.access_rule as module

    crud, owners, ids = resource
    account, _ = owners[0]
    saved = save(resource, update(local_rule(ids[0])))
    original = module._record_change

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("Synthetic cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "_record_change", fail)
        with crud.session() as db:
            db.delete(db.get(models.AIModel, ids[0]))
            with pytest.raises(RuntimeError, match="cleanup failure"):
                db.commit()
            db.rollback()
    assert (
        crud.resource_access(
            account_id=account, resource_type="ai_model", resource_id=ids[0]
        )
        == saved
    )


def test_resource_reference_validation_batches_ids_across_import_rules(
    resource: Any,
) -> None:
    from sqlalchemy import event

    crud, owners, ids = resource
    account, _ = owners[0]
    resource_selects = []

    def executed(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "FROM ai_model" in statement:
            resource_selects.append(statement)

    event.listen(crud.engine, "before_cursor_execute", executed)
    try:
        with crud.session() as db:
            validate_policy_section(
                db,
                account,
                [local_rule(ids[0], name=f"Batch rule {index}") for index in range(25)],
                None,
            )
    finally:
        event.remove(crud.engine, "before_cursor_execute", executed)
    assert len(resource_selects) == 1


def test_yaml_can_restore_inheritance_without_turning_local_permits_into_account_grants(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    saved = save(resource, update(local_rule(ids[0])))
    with crud.session() as db, db.begin():
        exported = export_current_policy(db, account)
        inherited = exported.resource_access[0].model_copy(update={"requirements": []})
        apply_policy_section(
            db, account, user, exported.access_rules, None, [inherited]
        )
    current = crud.resource_access(
        account_id=account, resource_type="ai_model", resource_id=ids[0]
    )
    assert current.requirements == [] and current.rules == saved.rules
    bundle = crud.bundle(account_id=account)
    assert bundle["rules"] == []
    assert [row["id"] for row in bundle["resource_access_rules"]] == [saved.rules[0].id]


def test_cascading_resource_delete_cleans_child_resource_gates(resource: Any) -> None:
    from sqlalchemy import delete

    crud, owners, _ = resource
    account, user = owners[0]
    with crud.session() as db, db.begin():
        server = models.MCPServer(
            account_id=account,
            name="Example server",
            url="https://example.com/mcp",
            transport="http-streaming",
        )
        db.add(server)
        db.flush()
        tool = models.ToolConfiguration(
            account_id=account,
            mcp_server_id=server.id,
            tool_name="example_tool",
            tool_source="mcp",
        )
        db.add(tool)
        db.flush()
        server_id, tool_id = server.id, tool.id
    rule = local_rule(
        tool_id,
        name="Example tool gate",
        resource_type="mcp_tool",
        actions=["tool:call"],
    )
    crud.write_resource_access(
        account_id=account,
        user_id=user,
        resource_type="mcp_tool",
        resource_id=tool_id,
        document=ResourceAccessUpdate(
            rules=[rule],
            requirements=[
                ResourceAccessRequirement(action="tool:call", permit_rule_ids=[rule.id])
            ],
        ),
        expected_version=0,
        expected_generation=crud.generation(account_id=account),
        losing_subjects=[],
    )
    with crud.session() as db, db.begin():
        db.execute(delete(models.MCPServer).where(models.MCPServer.id == server_id))
    with crud.session() as db:
        assert db.get(models.ToolConfiguration, tool_id) is None
        assert db.get(models.Account, account).meta_data["resource_access"] == []
    assert crud.rules(account_id=account) == []


def test_savepoint_rollback_preserves_live_gate_but_cleans_outer_deleted_target(
    resource: Any,
) -> None:
    crud, owners, ids = resource
    account, user = owners[0]
    save(resource, update(local_rule(ids[0])))
    with crud.session() as db, db.begin():
        other = models.AIModel(
            account_id=account,
            name="Savepoint model",
            provider_name="example",
            model_identifier="savepoint",
        )
        db.add(other)
        db.flush()
        other_id = other.id
    other_saved = crud.write_resource_access(
        account_id=account,
        user_id=user,
        resource_type="ai_model",
        resource_id=other_id,
        document=update(local_rule(other_id)),
        expected_version=0,
        expected_generation=crud.generation(account_id=account),
        losing_subjects=[],
    )
    try:
        with crud.session() as db, db.begin():
            db.delete(db.get(models.AIModel, ids[0]))
            db.flush()
            nested = db.begin_nested()
            db.delete(db.get(models.AIModel, other_id))
            db.flush()
            nested.rollback()
        current = crud.resource_access(
            account_id=account, resource_type="ai_model", resource_id=other_id
        )
        assert (
            current.rules == other_saved.rules
            and current.requirements == other_saved.requirements
        )
        with crud.session() as db:
            metadata = db.get(models.Account, account).meta_data
            assert [item["resource_id"] for item in metadata["resource_access"]] == [
                str(other_id)
            ]
    finally:
        with crud.session() as db, db.begin():
            db.delete(db.get(models.AIModel, other_id))


def test_legacy_subject_ids_without_kind_remain_owner_scoped(resource: Any) -> None:
    crud, owners, _ = resource
    account, user = owners[0]
    legacy = AccessRuleDefinition(
        name="Legacy kind-less subject",
        effect="permit",
        actions=["resource:view"],
        subject_selector={"ids": [str(user)]},
    )
    saved = crud.write(account_id=account, user_id=user, definition=legacy)
    assert saved["subject_selector"]["ids"] == [str(user)]
    assert saved["subject_selector"]["kind"] is None
    with pytest.raises(AccessReferenceError):
        crud.write(
            account_id=account,
            user_id=user,
            definition=legacy.model_copy(
                update={
                    "subject_selector": legacy.subject_selector.model_copy(
                        update={"ids": [owners[1][1]]}
                    )
                }
            ),
        )
