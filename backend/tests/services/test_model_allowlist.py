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
        "name": "Kimi K3",
        "provider_name": "moonshot",
        "model_identifier": "kimi-k3",
        "meta_data": {"gateway": {"enabled": True}},
    }
    obj_in.update(overrides)
    return crud_ai_model.create_with_account(
        db=db_session, obj_in=obj_in, account_id=test_user.account_id
    )


def test_normalize_allowed_models_trims_and_dedupes():
    assert normalize_allowed_models([" a ", "", "b", "a", None, 3]) == [
        "a",
        "b",
        "None",
        "3",
    ]
    assert normalize_allowed_models(None) == []


def test_entry_matches_by_name_id_and_every_alias_spelling(db_session, test_user):
    model = _model(db_session, test_user)
    assert allowlist_entry_matches_model("kimi k3", model)
    assert allowlist_entry_matches_model(str(model.id).upper(), model)
    assert allowlist_entry_matches_model("moonshot/kimi-k3", model)
    assert allowlist_entry_matches_model("kimi-k3", model)
    assert not allowlist_entry_matches_model("GLM 5.3 Flash", model)
    assert not allowlist_entry_matches_model("", model)


def test_resolve_allowed_model_ids_maps_entries_onto_inventory(db_session, test_user):
    kimi = _model(db_session, test_user)
    glm = _model(
        db_session,
        test_user,
        name="GLM 5.3 Flash",
        provider_name="zai",
        model_identifier="glm-5.3-flash",
    )
    other = _model(
        db_session,
        test_user,
        name="Other",
        provider_name="openai",
        model_identifier="gpt-5",
    )
    inventory = [kimi, glm, other]

    resolved = resolve_allowed_model_ids(
        ["GLM 5.3 Flash", "Kimi K3", "typo"], inventory
    )

    assert resolved == {str(kimi.id), str(glm.id)}
    assert resolve_allowed_model_ids([], inventory) == set()


def test_allowlist_permits_model_semantics(db_session, test_user):
    model = _model(db_session, test_user)
    assert allowlist_permits_model([], model)
    assert allowlist_permits_model(["Kimi K3"], model)
    assert allowlist_permits_model(
        ["legacy-spelling"], model, requested_spellings={"legacy-spelling"}
    )
    assert not allowlist_permits_model(["GLM 5.3 Flash"], model)


def test_requested_model_label_prefers_wire_string_then_alias(db_session, test_user):
    model = _model(db_session, test_user)
    assert requested_model_label(model, " moonshotai/kimi-k3 ") == "moonshotai/kimi-k3"
    assert requested_model_label(model, None) == "moonshot/kimi-k3"
    direct = _model(db_session, test_user, name="Direct", meta_data={})
    assert requested_model_label(direct, "") == "kimi-k3"


def test_format_model_not_allowed_detail_lists_at_most_five_entries():
    detail = format_model_not_allowed_detail(
        "moonshotai/kimi-k3", ["GLM 5.3 Flash", "Kimi K3"]
    )
    assert detail == (
        "Model 'moonshotai/kimi-k3' is not in this agent's allowed models "
        "(GLM 5.3 Flash, Kimi K3). Edit the agent's governance in the Preloop "
        "console or pick an allowed model."
    )
    assert is_model_not_allowed_detail(detail)
    assert not is_model_not_allowed_detail("Model gateway budget exceeded")

    long_detail = format_model_not_allowed_detail("x", [f"m{i}" for i in range(7)])
    assert "(m0, m1, m2, m3, m4, ...)" in long_detail
    assert "m5" not in long_detail
