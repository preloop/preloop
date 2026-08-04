"""Agent-kind decoupling tests (#123 followup).

``agent_kind`` records *which product* an agent is (``cursor``); the
``session_source_type`` records *how it connects* (``desktop_agent``) and is
part of the durable v2 principal-id fingerprint. These tests pin that split,
because collapsing the two is what made every Cursor agent look ``custom``
and what would silently re-key existing enrollments if "fixed" naively.
"""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_managed_agent
from preloop.models.crud.managed_agent import (
    normalize_managed_agent_kind,
    should_refine_agent_kind,
)
from preloop.services.usage_import import resolve_target_agent
from preloop.utils.agent_kind import is_valid_agent_kind, normalize_agent_kind

AGENTS_URL = "/api/v1/agents"
TOKEN_URL = "/api/v1/auth/runtime-sessions/token"
IMPORT_URL = "/api/v1/usage/import"


def _events_payload():
    timestamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    return {
        "events": [
            {
                "timestamp": timestamp,
                "model": "composer",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost_usd": 0.25,
            }
        ]
    }


class TestApiCreatedAgentKind:
    """POST /api/v1/agents must be able to declare a real product kind."""

    def test_defaults_to_custom_when_kind_omitted(self, client):
        """Omitting agent_kind keeps the historical 'custom' behaviour."""
        response = client.post(AGENTS_URL, json={"display_name": "Bespoke bot"})

        assert response.status_code == 201
        body = response.json()
        assert body["agent_kind"] == "custom"
        assert body["session_source_type"] == "custom"

    def test_accepts_declared_kind(self, client):
        """A declared kind is stored without changing the source type."""
        response = client.post(
            AGENTS_URL,
            json={"display_name": "My Cursor", "agent_kind": "cursor"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["agent_kind"] == "cursor"
        # The transport must stay 'custom': it drives the generated-id dedupe
        # contract and the v2 fingerprint.
        assert body["session_source_type"] == "custom"

    def test_kind_is_normalized(self, client):
        """Kinds are case/separator normalized so filters stay predictable."""
        response = client.post(
            AGENTS_URL,
            json={"display_name": "Gem", "agent_kind": " Gemini-CLI "},
        )

        assert response.status_code == 201
        assert response.json()["agent_kind"] == "gemini_cli"

    def test_rejects_malformed_kind(self, client):
        """Kinds are echoed into comma-separated filters, so shape is checked."""
        response = client.post(
            AGENTS_URL,
            json={"display_name": "Bad", "agent_kind": "cursor,windsurf"},
        )

        assert response.status_code == 422

    def test_api_created_cursor_agent_is_listable_by_kind(self, client):
        """The console filters by kind, so it must find API-created agents."""
        client.post(
            AGENTS_URL,
            json={"display_name": "My Cursor", "agent_kind": "cursor"},
        )

        response = client.get(f"{AGENTS_URL}?agent_kind=cursor")

        assert response.status_code == 200
        assert [i["display_name"] for i in response.json()["items"]] == ["My Cursor"]


class TestUsageImportDefaultResolution:
    """The #123 symptom: /usage/import 422s for API-created Cursor agents."""

    def test_import_resolves_api_created_cursor_agent(self, client, db_session):
        """An API-created cursor agent is now a valid default target."""
        created = client.post(
            AGENTS_URL,
            json={"display_name": "My Cursor", "agent_kind": "cursor"},
        )
        assert created.status_code == 201
        agent_id = created.json()["id"]

        response = client.post(IMPORT_URL, json=_events_payload())

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == agent_id

    def test_import_without_any_cursor_agent_still_errors(self, client):
        """A custom-only account keeps the explicit, actionable error."""
        client.post(AGENTS_URL, json={"display_name": "Bespoke bot"})

        response = client.post(IMPORT_URL, json=_events_payload())

        assert response.status_code == 422
        assert "No managed 'cursor' agent found" in response.text


class TestExistingAgentSurvivesUpgrade:
    """The TRAP: pre-fix agents must keep working after the upgrade."""

    def test_existing_desktop_agent_keeps_identity_and_authenticates(
        self, client, db_session, test_user
    ):
        """A pre-fix Cursor enrollment must not be re-keyed by the upgrade.

        Before this change the CLI enrolled Cursor as ``desktop_agent`` with a
        principal id derived from that source type. After the upgrade the CLI
        sends ``agent_kind='cursor'`` but the *same* source type and id, so the
        existing row must be refined in place, not duplicated.
        """
        # Pre-fix enrollment: transport-derived id, no product kind.
        legacy = crud_managed_agent.upsert_from_runtime_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=None,
            session_source_type="desktop_agent",
            session_source_id="desktop-agent-aef692c7e4cb",
            display_name="Cursor",
        )
        db_session.commit()
        assert legacy.agent_kind == "desktop_agent"
        legacy_id = str(legacy.id)

        # Upgraded CLI re-enrolls: same identity, now declaring the product.
        response = client.post(
            TOKEN_URL,
            json={
                "session_source_type": "desktop_agent",
                "session_source_id": "desktop-agent-aef692c7e4cb",
                "runtime_principal_id": "desktop-agent-aef692c7e4cb",
                "runtime_principal_name": "Cursor",
                "agent_kind": "cursor",
            },
        )

        assert response.status_code == 201, response.text

        agents = client.get(AGENTS_URL).json()["items"]
        # Refined in place: no duplicate row, identity preserved.
        assert len(agents) == 1
        assert agents[0]["id"] == legacy_id
        assert agents[0]["session_source_id"] == "desktop-agent-aef692c7e4cb"
        assert agents[0]["session_source_type"] == "desktop_agent"
        assert agents[0]["agent_kind"] == "cursor"

    def test_older_cli_does_not_regress_a_known_kind(
        self, client, db_session, test_user
    ):
        """An older CLI omits agent_kind; that must not reset a known kind."""
        crud_managed_agent.upsert_from_runtime_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=None,
            session_source_type="desktop_agent",
            session_source_id="desktop-agent-aef692c7e4cb",
            display_name="Cursor",
            agent_kind="cursor",
        )
        db_session.commit()

        # Older CLI: same enrollment, no agent_kind field at all.
        response = client.post(
            TOKEN_URL,
            json={
                "session_source_type": "desktop_agent",
                "session_source_id": "desktop-agent-aef692c7e4cb",
                "runtime_principal_id": "desktop-agent-aef692c7e4cb",
                "runtime_principal_name": "Cursor",
            },
        )

        assert response.status_code == 201, response.text
        agents = client.get(AGENTS_URL).json()["items"]
        assert len(agents) == 1
        assert agents[0]["agent_kind"] == "cursor"

    def test_source_type_allowlist_is_unchanged(self, client):
        """Product kinds must not leak into the source-type allowlist.

        If 'cursor' were accepted as a source type, a new CLI would enroll
        under an id the old server cannot mint and existing rows would fork.
        """
        response = client.post(
            TOKEN_URL,
            json={
                "session_source_type": "cursor",
                "session_source_id": "cursor-ws-1",
            },
        )

        assert response.status_code == 400
        assert "Unsupported session_source_type" in response.text


class TestNormalizeManagedAgentKind:
    """Unit-level contract for the kind/source-type split."""

    def test_explicit_kind_wins_over_source_type(self):
        assert (
            normalize_managed_agent_kind("desktop_agent", agent_kind="cursor")
            == "cursor"
        )

    def test_falls_back_to_source_type(self):
        assert normalize_managed_agent_kind("claude_code") == "claude_code"

    def test_blank_kind_falls_back(self):
        assert (
            normalize_managed_agent_kind("claude_code", agent_kind="  ")
            == "claude_code"
        )

    def test_empty_source_type_defaults_to_external_agent(self):
        assert normalize_managed_agent_kind(None) == "external_agent"

    def test_separators_and_case_are_insignificant(self):
        """A kind must normalize the same wherever it enters the system.

        Default-agent resolution compares kinds for equality, so if the
        token endpoint folded "Gemini-CLI" differently from the CRUD layer
        the agent would be stored under a kind no lookup could find.
        """
        for raw in ("Gemini CLI", "gemini-cli", "  GEMINI_CLI  "):
            assert normalize_agent_kind(raw) == "gemini_cli"
            assert normalize_managed_agent_kind("custom", agent_kind=raw) == (
                "gemini_cli"
            )

    def test_rejects_shapes_that_would_break_filter_query_strings(self):
        """Kinds are echoed into comma-separated filters, so stay identifiers."""
        assert is_valid_agent_kind("cursor")
        # Spaces and hyphens are folded into underscores, not rejected.
        assert is_valid_agent_kind(normalize_agent_kind("vs code"))
        for bad in ("a,b", "a/b", "a.b", "a:b", ""):
            assert not is_valid_agent_kind(normalize_agent_kind(bad))


class TestShouldRefineAgentKind:
    """Refine the kind, never regress it."""

    def test_explicit_kind_always_wins(self):
        assert should_refine_agent_kind(
            "cursor", session_source_type="desktop_agent", agent_kind="windsurf"
        )

    def test_older_client_cannot_regress_a_known_kind(self):
        """The core guard: a CLI that predates #123 sends no kind.

        Without this, every re-enrollment from an old CLI would reset a
        known "cursor" back to the generic transport value.
        """
        assert not should_refine_agent_kind(
            "cursor", session_source_type="desktop_agent", agent_kind=None
        )

    def test_generic_stored_kind_may_be_filled_in(self):
        assert should_refine_agent_kind(
            "desktop_agent", session_source_type="desktop_agent", agent_kind=None
        )

    def test_empty_stored_kind_may_be_filled_in(self):
        assert should_refine_agent_kind(
            None, session_source_type="desktop_agent", agent_kind=None
        )


class TestResolveTargetAgentPrefersKind:
    """resolve_target_agent keys off agent_kind, not session_source_type."""

    def test_resolves_custom_transport_cursor_agent(self, db_session, test_user):
        agent = crud_managed_agent.create_custom_agent(
            db_session,
            account_id=test_user.account_id,
            display_name="My Cursor",
            agent_kind="cursor",
        )
        db_session.commit()

        resolved = resolve_target_agent(
            db_session, account_id=str(test_user.account_id)
        )

        assert str(resolved.id) == str(agent.id)
        assert resolved.session_source_type == "custom"
