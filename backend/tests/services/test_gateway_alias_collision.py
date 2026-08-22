"""Gateway alias collision handling: create, import, and resolve paths.

Regression tests for the routing/attribution bug where an agent-onboarding
import silently took a user-created binding's gateway alias (two bindings
answering to ``zai/glm-5.3``) and the gateway resolved — and billed — the
import instead of the model the user explicitly added.
"""

import pytest

from preloop.models.crud import crud_ai_model
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_runtime_resolver import effective_gateway_alias
from preloop.services.openai_gateway import OpenAIGatewayService


def _model_payload(
    name: str,
    *,
    alias: str = "zai/glm-5.3",
    identifier: str = "glm-5.3",
    provider: str = "zai",
    managed_by: str | None = None,
):
    meta = {
        "gateway": {
            "enabled": True,
            "model_alias": alias,
            "provider_adapter": "preloop",
        }
    }
    if managed_by:
        meta["managed_by"] = managed_by
        meta["source_agent"] = "opencode"
    return {
        "name": name,
        "provider_name": provider,
        "model_identifier": identifier,
        "api_key": "test-key",
        "meta_data": meta,
    }


def _service(db_session, test_user) -> OpenAIGatewayService:
    return OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )


# ---------------------------------------------------------------------------
# Resolve path
# ---------------------------------------------------------------------------


def test_resolve_prefers_user_created_binding_on_alias_collision(
    db_session, test_user, caplog
):
    """On an exact-alias tie the explicit user binding wins over the import."""
    imported = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload(
            "OpenCode zai/glm-5-3-turbo", managed_by="preloop agents onboard"
        ),
        account_id=test_user.account_id,
    )
    # Simulate the legacy collision (write-time validation now auto-suffixes
    # imports, so force the clash directly as it exists in old data).
    user_created = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3", alias="zai/glm-5.3-tmp"),
        account_id=test_user.account_id,
    )
    user_created.meta_data = {
        **user_created.meta_data,
        "gateway": {**user_created.meta_data["gateway"], "model_alias": "zai/glm-5.3"},
    }
    imported.meta_data = {
        **imported.meta_data,
        "gateway": {**imported.meta_data["gateway"], "model_alias": "zai/glm-5.3"},
    }
    db_session.flush()

    service = _service(db_session, test_user)
    with caplog.at_level("WARNING"):
        resolved = service._resolve_requested_model("zai/glm-5.3", provider="openai")

    assert resolved.id == user_created.id
    assert service.alias_collision_warning is not None
    assert "zai/glm-5.3" in service.alias_collision_warning
    assert str(imported.id) in service.alias_collision_warning
    assert any("gateway_alias_collision" in r.message for r in caplog.records)


def test_resolve_prefers_user_created_binding_on_suffix_collision(
    db_session, test_user
):
    """Bare-suffix requests apply the same user-over-import preference."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload(
            "OpenCode import",
            alias="zai-agent/glm-5.3",
            managed_by="preloop agents onboard",
        ),
        account_id=test_user.account_id,
    )
    user_created = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3", alias="zai/glm-5.3"),
        account_id=test_user.account_id,
    )

    service = _service(db_session, test_user)
    resolved = service._resolve_requested_model("glm-5.3", provider="openai")

    assert resolved.id == user_created.id
    assert service.alias_collision_warning is not None


def test_resolve_without_collision_sets_no_warning(db_session, test_user):
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    service = _service(db_session, test_user)
    resolved = service._resolve_requested_model("zai/glm-5.3", provider="openai")
    assert resolved.name == "glm-5.3"
    assert service.alias_collision_warning is None


# ---------------------------------------------------------------------------
# Create path (explicit user writes)
# ---------------------------------------------------------------------------


def test_user_create_with_colliding_alias_is_rejected(db_session, test_user):
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    with pytest.raises(ValueError, match="already used"):
        crud_ai_model.create_with_account(
            db=db_session,
            obj_in=_model_payload("glm-5.3 duplicate"),
            account_id=test_user.account_id,
        )


def test_user_create_default_alias_collision_is_rejected(db_session, test_user):
    """The default provider/identifier alias collides with an explicit one."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    payload = _model_payload("implicit alias")
    del payload["meta_data"]["gateway"]["model_alias"]  # defaults to zai/glm-5.3
    with pytest.raises(ValueError, match="already used"):
        crud_ai_model.create_with_account(
            db=db_session, obj_in=payload, account_id=test_user.account_id
        )


def test_user_update_into_colliding_alias_is_rejected(db_session, test_user):
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    other = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("other", alias="zai/glm-4.6"),
        account_id=test_user.account_id,
    )
    with pytest.raises(ValueError, match="already used"):
        crud_ai_model.update(
            db=db_session,
            db_obj=other,
            obj_in={
                "meta_data": {
                    **other.meta_data,
                    "gateway": {
                        **other.meta_data["gateway"],
                        "model_alias": "zai/glm-5.3",
                    },
                }
            },
        )


def test_unrelated_update_on_legacy_collision_still_allowed(db_session, test_user):
    """Rows that already collide stay editable for non-alias fields."""
    first = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    second = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("legacy dup", alias="zai/glm-5.3-tmp"),
        account_id=test_user.account_id,
    )
    second.meta_data = {
        **second.meta_data,
        "gateway": {**second.meta_data["gateway"], "model_alias": "zai/glm-5.3"},
    }
    db_session.flush()
    assert effective_gateway_alias(first) == effective_gateway_alias(second)

    updated = crud_ai_model.update(
        db=db_session, db_obj=second, obj_in={"description": "still editable"}
    )
    assert updated.description == "still editable"


# ---------------------------------------------------------------------------
# Import path (agent onboarding)
# ---------------------------------------------------------------------------


def test_import_with_colliding_alias_is_auto_suffixed(db_session, test_user):
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    imported = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload(
            "OpenCode zai/glm-5-3-turbo", managed_by="preloop agents onboard"
        ),
        account_id=test_user.account_id,
    )
    assert effective_gateway_alias(imported) == "zai/glm-5.3-2"

    second_import = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("OpenCode another", managed_by="preloop agents onboard"),
        account_id=test_user.account_id,
    )
    assert effective_gateway_alias(second_import) == "zai/glm-5.3-3"


def test_import_without_collision_keeps_requested_alias(db_session, test_user):
    imported = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload(
            "OpenCode zai/glm-5-3-turbo",
            alias="zai/glm-5-3-turbo",
            managed_by="preloop agents onboard",
        ),
        account_id=test_user.account_id,
    )
    assert effective_gateway_alias(imported) == "zai/glm-5-3-turbo"


def test_import_update_into_collision_is_auto_suffixed(db_session, test_user):
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("glm-5.3"),
        account_id=test_user.account_id,
    )
    imported = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload(
            "OpenCode zai/glm-5-3-turbo",
            alias="zai/glm-5-3-turbo",
            managed_by="preloop agents onboard",
        ),
        account_id=test_user.account_id,
    )
    updated = crud_ai_model.update(
        db=db_session,
        db_obj=imported,
        obj_in={
            "meta_data": {
                **imported.meta_data,
                "gateway": {
                    **imported.meta_data["gateway"],
                    "model_alias": "zai/glm-5.3",
                },
            }
        },
    )
    assert effective_gateway_alias(updated) == "zai/glm-5.3-2"


# ---------------------------------------------------------------------------
# Effective-alias consistency (validation vs runtime)
# ---------------------------------------------------------------------------


def test_whitespace_padded_alias_agrees_between_validation_and_runtime(
    db_session, test_user
):
    """One alias definition everywhere: a padded ``model_alias`` strips.

    Regression: write-time validation and the audit used the stripped
    ``effective_gateway_alias`` while ``resolve_ai_model_runtime`` used the
    raw configured string, so a whitespace-padded alias was a collision at
    write time yet a *distinct* alias at runtime (false-positive 400,
    unroutable spelling).
    """
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    padded = crud_ai_model.create_with_account(
        db=db_session,
        obj_in=_model_payload("padded", alias=" zai/glm-5.3 "),
        account_id=test_user.account_id,
    )
    assert effective_gateway_alias(padded) == "zai/glm-5.3"
    # Runtime resolution must agree with the write-time/audit definition.
    assert resolve_ai_model_runtime(padded).model_gateway_model_alias == "zai/glm-5.3"
    # ... which makes the padded row reachable by its stripped spelling.
    service = _service(db_session, test_user)
    resolved = service._resolve_requested_model("zai/glm-5.3", provider="openai")
    assert resolved.id == padded.id
    assert service.alias_collision_warning is None
    # And a second write on the stripped spelling is a real collision.
    with pytest.raises(ValueError, match="already used"):
        crud_ai_model.create_with_account(
            db=db_session,
            obj_in=_model_payload("collides"),
            account_id=test_user.account_id,
        )
