"""Endpoint tests for the usage ingest API (issue #123).

Covers POST /api/v1/usage/import (normalized JSON events) and
POST /api/v1/usage/import/csv (Cursor dashboard Usage export), plus the
integration contract with GET /api/v1/cost/summary: imported spend appears
as a separate ``imported_usage`` block and never inflates the
gateway-metered ``estimated_cost``.
"""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_api_usage, crud_managed_agent

IMPORT_URL = "/api/v1/usage/import"
IMPORT_CSV_URL = "/api/v1/usage/import/csv"
COST_SUMMARY_URL = "/api/v1/cost/summary"

SAMPLE_CSV = (
    "Date,Kind,Model,Max Mode,Input (w/ Cache Write),Input (w/o Cache Write),"
    "Cache Read,Output Tokens,Total Tokens,Cost\n"
    "{date},Usage-based,claude-4.5-sonnet,No,1200,300,4500,850,6850,$0.42\n"
    "{date},Included,composer,No,0,150,0,90,240,Included\n"
)


def _make_cursor_agent(db, account_id, *, source_id="cursor-ws-1", name="Cursor"):
    """Register a managed Cursor agent like `preloop agents onboard cursor`."""
    return crud_managed_agent.upsert_from_runtime_session(
        db,
        account_id=account_id,
        runtime_session_id=None,
        session_source_type="cursor",
        session_source_id=source_id,
        display_name=name,
    )


def _events_payload(**overrides):
    """Build a minimal valid ingest payload."""
    timestamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    payload = {
        "events": [
            {
                "timestamp": timestamp,
                "model": "composer",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost_usd": 0.25,
            },
            {
                "timestamp": timestamp,
                "model": "claude-4.5-sonnet",
                "total_tokens": 600,
                "charged_cents": 125.0,
                "session_id": "sess-1",
            },
        ],
    }
    payload.update(overrides)
    return payload


class TestImportEvents:
    """POST /api/v1/usage/import."""

    def test_import_attributes_to_default_cursor_agent(
        self, client, db_session, test_user
    ):
        """Events land on the account's managed Cursor agent by default."""
        agent = _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = client.post(IMPORT_URL, json=_events_payload())

        assert response.status_code == 200
        body = response.json()
        assert body["imported"] == 2
        assert body["skipped_duplicates"] == 0
        assert body["agent_id"] == str(agent.id)
        assert body["agent_display_name"] == "Cursor"
        assert body["source"] == "cursor"

    def test_import_is_idempotent_on_replay(self, client, db_session, test_user):
        """Re-sending the same batch skips every event as a duplicate."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        payload = _events_payload()

        first = client.post(IMPORT_URL, json=payload).json()
        second = client.post(IMPORT_URL, json=payload).json()

        assert first["imported"] == 2
        assert second["imported"] == 0
        assert second["skipped_duplicates"] == 2

    def test_import_without_cursor_agent_returns_422(
        self, client, db_session, test_user
    ):
        """With no onboarded Cursor agent and no agent_id, ingest fails."""
        response = client.post(IMPORT_URL, json=_events_payload())

        assert response.status_code == 422
        assert "onboard" in response.json()["detail"].lower()

    def test_import_with_ambiguous_default_returns_422(
        self, client, db_session, test_user
    ):
        """Several Cursor agents require an explicit agent_id."""
        _make_cursor_agent(db_session, test_user.account_id, source_id="ws-1")
        _make_cursor_agent(db_session, test_user.account_id, source_id="ws-2")
        db_session.commit()

        response = client.post(IMPORT_URL, json=_events_payload())

        assert response.status_code == 422
        assert "agent_id" in response.json()["detail"]

    def test_import_with_explicit_agent_id(self, client, db_session, test_user):
        """An explicit agent_id disambiguates between agents."""
        _make_cursor_agent(db_session, test_user.account_id, source_id="ws-1")
        chosen = _make_cursor_agent(
            db_session, test_user.account_id, source_id="ws-2", name="Cursor B"
        )
        db_session.commit()

        response = client.post(
            IMPORT_URL, json=_events_payload(agent_id=str(chosen.id))
        )

        assert response.status_code == 200
        assert response.json()["agent_id"] == str(chosen.id)

    def test_import_rejects_foreign_account_agent(self, client, db_session, test_user):
        """An agent belonging to another account is not addressable."""
        from preloop.models.crud import crud_account

        other = crud_account.create(
            db_session, obj_in={"organization_name": "Other Org", "is_active": True}
        )
        foreign_agent = _make_cursor_agent(db_session, other.id)
        db_session.commit()

        response = client.post(
            IMPORT_URL, json=_events_payload(agent_id=str(foreign_agent.id))
        )

        assert response.status_code == 422
        assert "not found" in response.json()["detail"]

    def test_import_rejects_event_without_measurement(
        self, client, db_session, test_user
    ):
        """An event carrying neither tokens nor money fails validation."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        payload = {
            "events": [
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "model": "composer",
                }
            ]
        }
        response = client.post(IMPORT_URL, json=payload)

        assert response.status_code == 422

    def test_import_rejects_empty_batch(self, client, db_session, test_user):
        """An empty events list fails request validation."""
        response = client.post(IMPORT_URL, json={"events": []})
        assert response.status_code == 422


class TestImportCsv:
    """POST /api/v1/usage/import/csv."""

    def _upload(self, client, csv_text, **form):
        return client.post(
            IMPORT_CSV_URL,
            files={"file": ("usage.csv", csv_text.encode("utf-8"), "text/csv")},
            data=form,
        )

    def test_csv_import_success(self, client, db_session, test_user):
        """A canonical Cursor export imports every row."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        csv_text = SAMPLE_CSV.format(
            date=(datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        )

        response = self._upload(client, csv_text)

        assert response.status_code == 200
        body = response.json()
        assert body["parsed_rows"] == 2
        assert body["imported"] == 2
        assert body["skipped_rows"] == 0
        assert body["source"] == "cursor"

    def test_csv_reimport_is_idempotent(self, client, db_session, test_user):
        """Uploading the same file twice never double-counts spend."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        csv_text = SAMPLE_CSV.format(
            date=(datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        )

        first = self._upload(client, csv_text).json()
        second = self._upload(client, csv_text).json()

        assert first["imported"] == 2
        assert second["imported"] == 0
        assert second["skipped_duplicates"] == 2

    def test_csv_unrecognized_header_returns_422(self, client, db_session, test_user):
        """A CSV without Date/Model columns is rejected with guidance."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = self._upload(client, "Foo,Bar\n1,2\n")

        assert response.status_code == 422
        assert "column_map" in response.json()["detail"]

    def test_csv_with_column_map_override(self, client, db_session, test_user):
        """column_map maps a foreign export shape onto logical fields."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        csv_text = "When,Which Model,Tokens,Charged\n2026-07-28,composer,42,$0.05\n"

        response = self._upload(
            client,
            csv_text,
            column_map=(
                '{"date": "When", "model": "Which Model", '
                '"total_tokens": "Tokens", "cost": "Charged"}'
            ),
        )

        assert response.status_code == 200
        assert response.json()["imported"] == 1

    def test_csv_invalid_column_map_json_returns_422(
        self, client, db_session, test_user
    ):
        """A malformed column_map form field is rejected."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = self._upload(client, "Date,Model\n", column_map="{not json")

        assert response.status_code == 422
        assert "not valid JSON" in response.json()["detail"]

    def test_csv_exceeding_size_limit_returns_413(
        self, client, db_session, test_user, monkeypatch
    ):
        """An oversized upload is rejected without ingesting anything."""
        import preloop.api.endpoints.usage_import as usage_import_endpoint

        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        monkeypatch.setattr(usage_import_endpoint, "MAX_CSV_BYTES", 64)

        oversized = "Date,Model,Total Tokens,Cost\n" + (
            "2026-07-28,composer,10,$0.01\n" * 20
        )
        response = self._upload(client, oversized)

        assert response.status_code == 413
        assert "exceeds" in response.json()["detail"]

    def test_csv_exceeding_row_limit_returns_422(
        self, client, db_session, test_user, monkeypatch
    ):
        """A CSV with more data rows than the cap is rejected with guidance."""
        import preloop.services.usage_import as usage_import_service

        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        monkeypatch.setitem(
            usage_import_service.parse_cursor_usage_csv.__kwdefaults__,
            "max_rows",
            2,
        )

        csv_text = "Date,Model,Total Tokens,Cost\n" + (
            "2026-07-28,composer,10,$0.01\n" * 3
        )
        response = self._upload(client, csv_text)

        assert response.status_code == 422
        assert "data rows" in response.json()["detail"]


class TestCostSummaryIntegration:
    """Imported spend in GET /api/v1/cost/summary."""

    def test_imported_spend_is_separate_from_gateway_cost(
        self, client, db_session, test_user
    ):
        """Imported spend shows in its own block; gateway cost is untouched."""
        _make_cursor_agent(db_session, test_user.account_id)
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=200,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            model_alias="openai/gpt-5",
            provider_name="openai",
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
            estimated_cost=0.05,
        )
        db_session.commit()

        imported = client.post(IMPORT_URL, json=_events_payload())
        assert imported.status_code == 200

        body = client.get(COST_SUMMARY_URL).json()

        # Gateway-metered figures unchanged by the import.
        assert body["total_requests"] == 1
        assert body["estimated_cost"] == 0.05
        # Imported block reported separately.
        block = body["imported_usage"]
        assert block is not None
        assert block["event_count"] == 2
        assert block["imported_cost"] == 1.5  # 0.25 + 125 cents
        models = {row["model_alias"] for row in block["usage_by_model"]}
        assert models == {"composer", "claude-4.5-sonnet"}

    def test_summary_without_imports_has_no_block(self, client, db_session, test_user):
        """With no imported rows the block stays null (not a zero object)."""
        body = client.get(COST_SUMMARY_URL).json()
        assert body["imported_usage"] is None

    def test_summary_principal_filter_scopes_imported_block(
        self, client, db_session, test_user
    ):
        """runtime_principal_id filters imported spend like gateway spend."""
        agent_a = _make_cursor_agent(db_session, test_user.account_id, source_id="ws-A")
        agent_b = _make_cursor_agent(db_session, test_user.account_id, source_id="ws-B")
        db_session.commit()

        client.post(IMPORT_URL, json=_events_payload(agent_id=str(agent_a.id)))
        client.post(IMPORT_URL, json=_events_payload(agent_id=str(agent_b.id)))

        body = client.get(
            COST_SUMMARY_URL,
            params={"runtime_principal_id": agent_a.session_source_id},
        ).json()

        assert body["imported_usage"]["event_count"] == 2
