"""Contract tests for the publication-gate verification module (issue #428).

The profile fixtures here mirror the acceptance matrix: docs-only,
migration, frontend, shared-code and unknown changes. The deny matrix pins
the fail-closed behaviour of the publisher decision: no publication on
failed, missing, forged, wrong-SHA, stale or dirty evidence.
"""

import pytest

from preloop.services.verification import (
    VERIFICATION_PRODUCER,
    VERIFIER_VERSION,
    EnvironmentDigest,
    VerificationCommand,
    VerificationProfile,
    evaluate_publication,
    resolve_verification_policy,
    select_required_checks,
)

# --- Shared fixture commands ---------------------------------------------


def _cmd(id_: str, scope: str = "unknown", command: str = "true") -> dict:
    return {
        "id": id_,
        "command": command,
        "reason": f"{id_} protects the {scope} surface",
        "scope": scope,
    }


def _profile(**overrides) -> VerificationProfile:
    """A profile covering the acceptance fixture categories."""

    data = {
        "profile_id": "default",
        "description": "Test profile",
        "always": [
            _cmd("lint", scope="backend"),
            _cmd("format-check", scope="backend"),
        ],
        "rules": [
            {
                "id": "docs",
                "description": "Documentation-only changes",
                "path_globs": ["docs/*", "*.md"],
                "commands": [_cmd("docs-links", scope="docs")],
            },
            {
                "id": "migration",
                "description": "Database migration changes",
                "path_globs": ["backend/preloop/models/alembic/*"],
                "commands": [
                    _cmd("alembic-heads", scope="migration"),
                    _cmd("alembic-upgrade", scope="migration"),
                ],
            },
            {
                "id": "frontend",
                "description": "Frontend component changes",
                "path_globs": ["frontend/*"],
                "commands": [_cmd("frontend-component-tests", scope="frontend")],
            },
            {
                "id": "api-schema",
                "description": "API contract changes",
                "path_globs": ["openapi.yaml", "backend/preloop/models/schemas/*"],
                "commands": [_cmd("openapi-contract", scope="api")],
            },
            {
                "id": "shared-code",
                "description": "Models and shared interfaces",
                "path_globs": ["backend/preloop/models/*"],
                "commands": [_cmd("backend-suite", scope="shared")],
            },
        ],
        "unknown_default": [
            _cmd("fast-tests", scope="unknown"),
            _cmd("lint", scope="backend"),
        ],
    }
    data.update(overrides)
    return VerificationProfile.model_validate(data)


PROFILE = _profile()


# --- Selection ------------------------------------------------------------


class TestCheckSelection:
    def test_docs_only_change_requires_no_frontend_or_migration(self):
        selected = select_required_checks(PROFILE, ["docs/guide/flows/x.md"])
        ids = set(selected.command_ids)
        assert "docs-links" in ids
        assert "frontend-component-tests" not in ids
        assert "alembic-heads" not in ids
        assert "alembic-upgrade" not in ids
        assert "backend-suite" not in ids

    def test_migration_change_pulls_graph_and_upgrade_checks(self):
        selected = select_required_checks(
            PROFILE,
            ["backend/preloop/models/alembic/versions/2026_add_thing.py"],
        )
        ids = set(selected.command_ids)
        assert {"alembic-heads", "alembic-upgrade"} <= ids
        assert "frontend-component-tests" not in ids

    def test_frontend_change_requires_browser_component_tests(self):
        selected = select_required_checks(
            PROFILE, ["frontend/src/components/issue-card.ts"]
        )
        assert "frontend-component-tests" in selected.command_ids
        assert "alembic-heads" not in selected.command_ids

    def test_api_schema_change_requires_contract_check(self):
        selected = select_required_checks(PROFILE, ["openapi.yaml"])
        assert "openapi-contract" in selected.command_ids

    def test_shared_code_change_requires_broad_suite(self):
        selected = select_required_checks(
            PROFILE, ["backend/preloop/models/models/flow.py"]
        )
        assert "backend-suite" in selected.command_ids

    def test_unknown_impact_uses_conservative_default_not_empty(self):
        selected = select_required_checks(PROFILE, ["scripts/oddity.bash"])
        assert selected.used_unknown_default is True
        assert "fast-tests" in selected.command_ids
        assert len(selected.command_ids) > 0

    def test_always_hooks_are_required_on_every_change(self):
        for changed in (
            ["docs/a.md"],
            ["frontend/app.ts"],
            ["backend/preloop/models/alembic/versions/x.py"],
            ["no-rule-matches.bin"],
        ):
            selected = select_required_checks(PROFILE, changed)
            for hook in ("lint", "format-check"):
                assert hook in selected.command_ids, changed

    def test_multi_match_unions_rules_and_records_reasons(self):
        # A migration under the models package matches both the migration
        # and the shared-code rules: both suites are required.
        selected = select_required_checks(
            PROFILE,
            ["backend/preloop/models/alembic/env.py"],
        )
        assert set(selected.matched_rule_ids) == {"migration", "shared-code"}
        by_id = {s.command.id: s for s in selected.checks}
        assert set(by_id["alembic-heads"].selected_by) == {"migration"}
        assert set(by_id["backend-suite"].selected_by) == {"shared-code"}

    def test_selected_by_records_every_triggering_rule(self):
        profile = _profile(
            always=[],
            rules=[
                {
                    "id": "r1",
                    "description": "one",
                    "path_globs": ["a/*"],
                    "commands": [_cmd("c1")],
                },
                {
                    "id": "r2",
                    "description": "two",
                    "path_globs": ["b/*"],
                    "commands": [_cmd("c1")],
                },
            ],
        )
        selected = select_required_checks(profile, ["a/x", "b/y"])
        assert selected.command_ids == ["c1"]
        assert set(selected.checks[0].selected_by) == {"r1", "r2"}

    def test_basename_and_star_patterns_match(self):
        profile = _profile(
            rules=[
                {
                    "id": "makefile",
                    "description": "build files",
                    "path_globs": ["Makefile"],
                    "commands": [_cmd("build-check")],
                }
            ]
        )
        selected = select_required_checks(profile, ["cli/Makefile"])
        assert "build-check" in selected.command_ids

    def test_changed_files_are_reported_with_the_selection(self):
        selected = select_required_checks(PROFILE, ["docs/a.md", "docs/b.md"])
        assert selected.changed_files == ["docs/a.md", "docs/b.md"]


# --- Publication decision -------------------------------------------------


def _evidence(**overrides: object) -> dict:
    selected = select_required_checks(PROFILE, ["unknown.bin"])
    data = {
        "producer": VERIFICATION_PRODUCER,
        "verifier_version": VERIFIER_VERSION,
        "profile_id": "default",
        "profile_version": "v1",
        "commit_sha": "a" * 40,
        "tree_hash": "b" * 40,
        "clean_tree": True,
        "status": "passed",
        "environment": EnvironmentDigest().model_dump(),
        "checks": [
            {"id": command.id, "command": command.command, "exit_code": 0}
            for command in selected.commands
        ],
    }
    data.update(overrides)
    return data


def decision(evidence: dict | None):
    return evaluate_publication(
        evidence,
        profile=PROFILE,
        commit_sha="a" * 40,
        tree_hash="b" * 40,
        changed_files=["unknown.bin"],
    )


class TestPublicationDecision:
    def test_matching_evidence_allowed(self) -> None:
        assert decision(_evidence()).allowed

    @pytest.mark.parametrize(
        "override",
        [
            {"producer": "agent"},
            {"verifier_version": 0},
            {"profile_id": "other"},
            {"profile_version": "v0"},
            {"commit_sha": "c" * 40},
            {"tree_hash": "c" * 40},
            {"clean_tree": False},
            {"checks": []},
            {"status": "failed"},
            {"status": "blocked"},
        ],
    )
    def test_stale_missing_or_failed_evidence_denied(self, override: dict) -> None:
        assert not decision(_evidence(**override)).allowed

    def test_missing_or_malformed_evidence_denied(self) -> None:
        assert not decision(None).allowed
        assert not decision({"status": "passed"}).allowed

    @pytest.mark.parametrize(
        "change",
        [
            {"command": "true # different command"},
            {"exit_code": 1},
            {"exit_code": None},
            {"skipped_reason": "database unavailable"},
        ],
    )
    def test_checks_must_match_and_actually_pass(self, change: dict) -> None:
        evidence = _evidence()
        evidence["checks"][0].update(change)
        assert not decision(evidence).allowed


class TestPolicyResolution:
    def test_existing_flow_explicitly_ungated(self) -> None:
        assert resolve_verification_policy({}).mode == "off"
        assert "no verification policy" in resolve_verification_policy({}).reason

    def test_gate_resolves_profile(self) -> None:
        result = resolve_verification_policy(
            {"verification": {"mode": "gate", "profile": PROFILE.model_dump()}}
        )
        assert result.mode == "gate"
        assert result.profile == PROFILE

    @pytest.mark.parametrize(
        "policy", [{"mode": "gate", "profile": {}}, {"mode": "unknown"}, "bad"]
    )
    def test_invalid_configured_policy_fails_closed(self, policy: object) -> None:
        with pytest.raises(ValueError):
            resolve_verification_policy({"verification": policy})

    def test_mixed_docs_and_unknown_changes_require_conservative_checks(self) -> None:
        selected = select_required_checks(
            PROFILE, ["docs/change.md", "scripts/unknown.sh"]
        )
        assert "docs-links" in selected.command_ids
        assert "fast-tests" in selected.command_ids
        assert selected.used_unknown_default


@pytest.mark.parametrize(
    "override", [{"id": ""}, {"id": "../escape"}, {"command": ""}, {"command": " \n "}]
)
def test_empty_or_ambiguous_checks_are_rejected(override: dict) -> None:
    with pytest.raises(ValueError):
        VerificationCommand.model_validate({**_cmd("check"), **override})


def test_duplicate_ids_cannot_select_conflicting_commands() -> None:
    from preloop.services.verification import configured_verification_commands

    profile = _profile(
        always=[_cmd("same", command="true")],
        unknown_default=[_cmd("same", command="false")],
    )
    with pytest.raises(ValueError, match="Conflicting"):
        configured_verification_commands(profile)
