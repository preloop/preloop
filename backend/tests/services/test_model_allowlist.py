"""Unit tests for allowlist entry resolution shared by the gateway and console."""

from preloop.models.crud import crud_ai_model
from preloop.services.model_allowlist import (
    allowlist_entry_matches_model,
    allowlist_permits_model,
    format_model_not_allowed_detail,
    is_model_not_allowed_detail,
    normalize_allowed_models,
    requested_model_label,
    resolve_allowed_model_ids,
)


def _model(db_session, test_user, **overrides):
    obj_in = {
        "name": "Alpha Chat",
        "provider_name": "acme",
        "model_identifier": "alpha-chat",
        "meta_data": {"gateway": {"enabled": True}},
    }
    obj_in.update(overrides)
    return crud_ai_model.create_with_account(
        db=db_session, obj_in=obj_in, account_id=test_user.account_id
    )


def test_normalize_allowed_models_trims_and_dedupes():
    assert normalize_allowed_models([" a ", "", "b", "a"]) == ["a", "b"]
    assert normalize_allowed_models(None) == []


def test_normalize_allowed_models_drops_non_strings():
    """JSON null/numbers must not stringify into deny-all entries."""
    assert normalize_allowed_models([None, 3, "", " a ", "a"]) == ["a"]
    assert normalize_allowed_models([None]) == []


def test_entry_matches_by_name_id_and_every_alias_spelling(db_session, test_user):
    model = _model(db_session, test_user)
    assert allowlist_entry_matches_model("alpha chat", model)
    assert allowlist_entry_matches_model(str(model.id).upper(), model)
    assert allowlist_entry_matches_model("acme/alpha-chat", model)
    assert allowlist_entry_matches_model("alpha-chat", model)
    assert not allowlist_entry_matches_model("Beta Flash", model)
    assert not allowlist_entry_matches_model("", model)


def test_resolve_allowed_model_ids_maps_entries_onto_inventory(db_session, test_user):
    alpha = _model(db_session, test_user)
    beta = _model(
        db_session,
        test_user,
        name="Beta Flash",
        provider_name="other",
        model_identifier="beta-flash",
    )
    other = _model(
        db_session,
        test_user,
        name="Other",
        provider_name="openai",
        model_identifier="gpt-5",
    )
    inventory = [alpha, beta, other]

    resolved = resolve_allowed_model_ids(
        ["Beta Flash", "Alpha Chat", "typo"], inventory
    )

    assert resolved == {str(alpha.id), str(beta.id)}
    assert resolve_allowed_model_ids([], inventory) == set()


def test_allowlist_permits_model_semantics(db_session, test_user):
    model = _model(db_session, test_user)
    assert allowlist_permits_model([], model)
    assert allowlist_permits_model([None], model)
    assert allowlist_permits_model(["Alpha Chat"], model)
    assert allowlist_permits_model(
        ["legacy-spelling"], model, requested_spellings={"legacy-spelling"}
    )
    assert not allowlist_permits_model(["Beta Flash"], model)


def test_requested_model_label_prefers_wire_string_then_alias(db_session, test_user):
    model = _model(db_session, test_user)
    assert requested_model_label(model, " vendor/alpha-chat ") == "vendor/alpha-chat"
    assert requested_model_label(model, None) == "acme/alpha-chat"
    direct = _model(db_session, test_user, name="Direct", meta_data={})
    assert requested_model_label(direct, "") == "alpha-chat"


def test_format_model_not_allowed_detail_lists_at_most_five_entries():
    detail = format_model_not_allowed_detail(
        "vendor/alpha-chat", ["Beta Flash", "Alpha Chat"]
    )
    assert detail == (
        "Model 'vendor/alpha-chat' is not in this agent's allowed models "
        "(Beta Flash, Alpha Chat). Edit the agent's governance in the Preloop "
        "console or pick an allowed model."
    )
    assert is_model_not_allowed_detail(detail)
    assert not is_model_not_allowed_detail("Model gateway budget exceeded")

    long_detail = format_model_not_allowed_detail("x", [f"m{i}" for i in range(7)])
    assert "(m0, m1, m2, m3, m4, ...)" in long_detail
    assert "m5" not in long_detail
