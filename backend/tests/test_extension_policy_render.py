"""Extension overlay safety contract (AC2–7, 11a); no vendor process here."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from preloop.schemas.extension_policy import EnforcementPolicy, EnforcementOptions
from preloop.services.extension_policy_keys import VERIFIED_KEYS
from preloop.services.extension_policy_render import RenderRefusalError, render_overlay


def policy(**options: bool) -> EnforcementPolicy:
    """Build a synthetic policy."""
    return EnforcementPolicy(options=options, revision=1)


def approval(
    identifier: str, state: str = "unapproved", **kwargs: str
) -> SimpleNamespace:
    """Build an approval-shaped input, not an inventory effective state."""
    return SimpleNamespace(
        extension_id=identifier,
        approval_state=state,
        agent_kind=kwargs.get("agent_kind", "claude_code"),
        extension_kind=kwargs.get("extension_kind", "plugin"),
        updated_at=datetime(2026, 11, 25, tzinfo=UTC),
    )


def test_plugin_filter() -> None:
    """protects=AC2 name denials; fails_when=approved, unknown, hashed or other
    agents/kinds are emitted; no_claim=synthetic approvals, no content pin enforcement.
    """
    result = render_overlay(
        policy(block_unapproved_plugins=True),
        [
            approval("a@m"),
            approval("b@m", "approved"),
            approval("c@m", "unknown"),
            approval("hash:1234"),
            approval("x@m", agent_kind="codex"),
            approval("skill@m", extension_kind="skill"),
        ],
        platform="darwin",
    )
    assert json.loads(result.settings_json) == {"enabledPlugins": {"a@m": False}}
    assert [(o.subject, o.reason) for o in result.omitted] == [
        ("hash:1234", "not a marketplace plugin id")
    ]
    assert result.inputs.approval_count == 5


@pytest.mark.parametrize(
    "options,rows",
    [
        ({}, []),
        ({"block_unapproved_plugins": True}, []),
        ({"block_unapproved_plugins": True}, [approval("b@m", "approved")]),
    ],
)
def test_nothing_to_enforce(options: dict, rows: list) -> None:
    """protects=AC2 empty approval safety; fails_when=an empty deny map is exported;
    no_claim=none beyond protects.
    """
    with pytest.raises(RenderRefusalError, match="nothing to enforce") as exc:
        render_overlay(policy(**options), rows, platform="linux")
    assert exc.value.status_code == 409


def test_marketplaces_and_lockout() -> None:
    """protects=AC3 safe source shapes; fails_when=empty allowlist locks all plugins;
    no_claim=vendor enforcement is covered by the manual rig.
    """
    p = policy(restrict_marketplaces=True)
    with pytest.raises(RenderRefusalError, match="official") as exc:
        render_overlay(p, [], platform="linux")
    assert exc.value.status_code == 422
    p = EnforcementPolicy(
        options=p.options,
        marketplaces=[
            {
                "name": "blocked",
                "state": "blocked",
                "source": {"source": "git", "url": "https://example.com/plugins.git"},
            },
            {
                "name": "official",
                "state": "allowed",
                "register": True,
                "source": {
                    "source": "github",
                    "repo": "anthropics/claude-plugins-official",
                },
            },
        ],
    )
    result = json.loads(render_overlay(p, [], platform="windows").settings_json)
    assert result == {
        "strictKnownMarketplaces": [
            {"source": "github", "repo": "anthropics/claude-plugins-official"}
        ],
        "blockedMarketplaces": [
            {"source": "git", "url": "https://example.com/plugins.git"}
        ],
        "extraKnownMarketplaces": {
            "official": {
                "source": {
                    "source": "github",
                    "repo": "anthropics/claude-plugins-official",
                }
            }
        },
    }


def test_managed_hook_prerequisite() -> None:
    """protects=AC4 user hook stays usable; fails_when=lock is emitted before hook
    registration; no_claim=registration is not device installation.
    """
    with pytest.raises(RenderRefusalError, match="claude-code") as exc:
        render_overlay(policy(managed_hooks_only=True), [], platform="darwin")
    assert exc.value.status_code == 409
    assert json.loads(
        render_overlay(
            policy(managed_hooks_only=True), [], platform="darwin", hook_registered=True
        ).settings_json
    ) == {"allowManagedHooksOnly": True}


@pytest.mark.parametrize(
    "option,key,value",
    [
        ("block_user_project_skills", "strictPluginOnlyCustomization", ["skills"]),
        ("disable_sideload_flags", "disableSideloadFlags", True),
        ("disable_skill_shell", "disableSkillShellExecution", True),
    ],
)
def test_verified_options(option: str, key: str, value: object) -> None:
    """protects=AC5 documented shapes, no MCP/hook lock; fails_when=other surfaces
    are locked; no_claim=documented behavior, not execution.
    """
    result = render_overlay(policy(**{option: True}), [], platform="linux")
    assert json.loads(result.settings_json) == {key: value}
    assert result.vendor_console_status == "unverified_for_preloop_routed_agents"


def test_every_key_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """protects=AC6 fail closed verification gate; fails_when=a key without a
    dated source is emitted; no_claim=does not fetch vendor docs during tests.
    """
    p = EnforcementPolicy(
        options={k: True for k in EnforcementOptions.model_fields},
        marketplaces=[
            {
                "name": "official",
                "state": "allowed",
                "register": True,
                "source": {
                    "source": "github",
                    "repo": "anthropics/claude-plugins-official",
                },
            }
        ],
    )
    first = render_overlay(p, [approval("a@m")], platform="linux", hook_registered=True)
    for key in first.keys:
        entry = VERIFIED_KEYS[key]
        assert entry.doc_url.startswith("https://code.claude.com/")
        assert entry.checked_on == "2026-11-25"
        with monkeypatch.context() as patch:
            patch.delitem(VERIFIED_KEYS, key)
            rendered = render_overlay(
                p, [approval("a@m")], platform="linux", hook_registered=True
            )
            assert key not in json.loads(rendered.settings_json)
            assert any(
                o.key == key and o.reason == "shape unverified"
                for o in rendered.omitted
            )


def test_determinism() -> None:
    """protects=AC7 exact byte identity across 100 reordered inputs; fails_when=hash
    excludes LF or iteration order leaks; no_claim=none beyond protects.
    """
    p = policy(block_unapproved_plugins=True, disable_skill_shell=True)
    rows = [approval("z@m"), approval("a@m"), approval("hash:z"), approval("hash:a")]
    first = render_overlay(p, rows, platform="darwin")
    for _ in range(100):
        rows.reverse()
        result = render_overlay(
            EnforcementPolicy.model_validate(
                dict(reversed(list(p.model_dump().items())))
            ),
            rows,
            platform="darwin",
        )
        assert result == first
    assert first.settings_json.endswith("\n")
    assert (
        first.policy_revision_hash
        == hashlib.sha256(first.settings_json.encode()).hexdigest()
    )
