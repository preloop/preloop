"""Endpoint tests for the usage push-ingest API (evolution of issue #123).

Covers POST /api/v1/usage/ingest: continuous push of externally observed
spend records, idempotent on (source, external_id) per account, with
conversation ids stored so subagent workers billed on separate
conversations can be rolled up under their parent thread. Cache-read
tokens are reported distinctly and never treated as charged spend.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, distinct, func

from preloop.models.crud import crud_managed_agent
from preloop.models.models.api_usage import ApiUsage

INGEST_URL = "/api/v1/usage/ingest"
COST_SUMMARY_URL = "/api/v1/cost/summary"


def _make_cursor_agent(db, account_id, *, source_id="cursor-ws-1", name="Cursor"):
    """Register a managed Cursor agent like `preloop agents onboard Cursor`."""
    return crud_managed_agent.upsert_from_runtime_session(
        db,
        account_id=account_id,
        runtime_session_id=None,
        session_source_type="desktop_agent",
        session_source_id=source_id,
        display_name=name,
        agent_kind="cursor",
    )


def _record(**overrides):
    """Build one valid ingest record."""
    record = {
        "external_id": "turn-0001",
        "timestamp": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "model": "composer",
        "charged_cost": "0.42",
        "input_tokens": 1200,
        "output_tokens": 850,
        "cache_read_tokens": 4500,
    }
    record.update(overrides)
    return record


def _payload(records=None, **overrides):
    """Build a minimal valid ingest payload."""
    payload = {"source": "cursor", "records": records or [_record()]}
    payload.update(overrides)
    return payload


class TestIngestRecords:
    """POST /api/v1/usage/ingest."""

    def test_ingest_accepts_batch(self, client, db_session, test_user):
        """Records land on the default agent with per-record results."""
        agent = _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [
            _record(external_id="turn-1"),
            _record(
                external_id="turn-2",
                conversation_id="conv-w1",
                parent_conversation_id="conv-parent",
            ),
        ]
        response = client.post(INGEST_URL, json=_payload(records))

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 2
        assert body["deduplicated"] == 0
        assert body["agent_id"] == str(agent.id)
        assert body["source"] == "cursor"
        assert body["results"] == [
            {"external_id": "turn-1", "deduplicated": False},
            {"external_id": "turn-2", "deduplicated": False},
        ]

        rows = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .all()
        )
        assert len(rows) == 2
        by_ext = {row.meta_data["external_id"]: row for row in rows}
        worker = by_ext["turn-2"]
        assert worker.usage_source == "imported"
        assert worker.meta_data["conversation_id"] == "conv-w1"
        assert worker.meta_data["parent_conversation_id"] == "conv-parent"

    def test_replay_returns_200_with_deduplicated_flag(
        self, client, db_session, test_user
    ):
        """Replaying the same batch never double-counts spend."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        payload = _payload([_record(external_id="turn-1")])

        first = client.post(INGEST_URL, json=payload)
        second = client.post(INGEST_URL, json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["accepted"] == 1
        body = second.json()
        assert body["accepted"] == 0
        assert body["deduplicated"] == 1
        assert body["results"] == [{"external_id": "turn-1", "deduplicated": True}]

        total_cost = (
            db_session.query(func.sum(ApiUsage.estimated_cost))
            .filter(ApiUsage.action_type == "imported_usage")
            .scalar()
        )
        assert total_cost is not None
        assert abs(total_cost - 0.42) < 1e-9

    def test_duplicates_within_one_batch_are_deduplicated(
        self, client, db_session, test_user
    ):
        """The same external_id twice in one request lands only once."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [_record(external_id="turn-1"), _record(external_id="turn-1")]
        body = client.post(INGEST_URL, json=_payload(records)).json()

        assert body["accepted"] == 1
        assert body["deduplicated"] == 1

    def test_same_external_id_from_two_sources_both_land(
        self, client, db_session, test_user
    ):
        """Dedupe is scoped by source: ids from different vendors differ."""
        agent = _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        first = client.post(INGEST_URL, json=_payload([_record(external_id="turn-1")]))
        second = client.post(
            INGEST_URL,
            json=_payload(
                [_record(external_id="turn-1")],
                source="other-vendor",
                agent_id=str(agent.id),
            ),
        )

        assert first.json()["accepted"] == 1
        assert second.status_code == 200
        assert second.json()["accepted"] == 1

    def test_cache_read_tokens_are_never_charged(self, client, db_session, test_user):
        """Cache reads are reported distinctly, never counted as spend."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [
            # Cache-read-only record: reported, but carries no charge.
            _record(
                external_id="turn-cache-only",
                charged_cost=None,
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=250_000,
            ),
            # Charged record: cost must equal charged_cost exactly, and
            # total tokens must exclude the cache reads.
            _record(
                external_id="turn-charged",
                charged_cost="1.25",
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=90_000,
            ),
        ]
        response = client.post(INGEST_URL, json=_payload(records))
        assert response.status_code == 200
        assert response.json()["accepted"] == 2

        rows = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .all()
        )
        by_ext = {row.meta_data["external_id"]: row for row in rows}

        cache_only = by_ext["turn-cache-only"]
        assert cache_only.estimated_cost is None
        assert cache_only.cost_source is None
        assert cache_only.cache_read_tokens == 250_000
        assert cache_only.total_tokens in (None, 0)

        charged = by_ext["turn-charged"]
        assert abs(charged.estimated_cost - 1.25) < 1e-9
        assert charged.cost_source == "imported"
        assert charged.total_tokens == 150  # input + output only
        assert charged.cache_read_tokens == 90_000

        summary = client.get(COST_SUMMARY_URL).json()
        imported = summary["imported_usage"]
        assert abs(imported["imported_cost"] - 1.25) < 1e-9

    def test_parent_worker_rollup_query_is_possible(
        self, client, db_session, test_user
    ):
        """Workers billed on separate conversations roll up under the parent.

        The motivating scenario: a parent thread spawns many subagent
        workers, each billed on its own conversation id; reading only the
        parent's conversation misses the combined spend. The stored data
        model must answer: charged dollars and worker count per thread.
        """
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [
            _record(
                external_id="turn-parent",
                conversation_id="conv-parent",
                charged_cost="0.10",
            )
        ]
        for i in range(3):
            records.append(
                _record(
                    external_id=f"turn-worker-{i}",
                    conversation_id=f"conv-worker-{i}",
                    parent_conversation_id="conv-parent",
                    charged_cost="2.00",
                )
            )
        assert client.post(INGEST_URL, json=_payload(records)).json()["accepted"] == 4

        thread = func.coalesce(
            ApiUsage.meta_data["parent_conversation_id"].astext,
            ApiUsage.meta_data["conversation_id"].astext,
        )
        rollup = (
            db_session.query(
                thread.label("thread"),
                func.sum(ApiUsage.estimated_cost).label("charged"),
                func.count(distinct(ApiUsage.meta_data["conversation_id"].astext))
                .cast(Integer)
                .label("worker_count"),
            )
            .filter(
                ApiUsage.account_id == test_user.account_id,
                ApiUsage.action_type == "imported_usage",
            )
            .group_by(thread)
            .all()
        )
        assert len(rollup) == 1
        row = rollup[0]
        assert row.thread == "conv-parent"
        assert abs(row.charged - 6.10) < 1e-9
        assert row.worker_count == 4  # parent conversation + 3 workers

    def test_without_matching_agent_returns_422(self, client, db_session, test_user):
        """No managed agent of the source's kind and no agent_id: 422."""
        response = client.post(INGEST_URL, json=_payload())

        assert response.status_code == 422
        assert "onboard" in response.json()["detail"].lower()

    def test_validation_errors_are_field_scoped(self, client, db_session, test_user):
        """Shape violations return 422 with per-field error locations."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = client.post(
            INGEST_URL,
            json=_payload(
                [
                    _record(external_id="", input_tokens=-5),
                ]
            ),
        )

        assert response.status_code == 422
        locs = [tuple(err["loc"]) for err in response.json()["detail"]]
        assert ("body", "records", 0, "external_id") in locs
        assert ("body", "records", 0, "input_tokens") in locs

    def test_record_without_any_measurement_returns_422(
        self, client, db_session, test_user
    ):
        """A record with neither tokens nor charged_cost is rejected."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = client.post(
            INGEST_URL,
            json=_payload(
                [
                    _record(
                        charged_cost=None,
                        input_tokens=None,
                        output_tokens=None,
                        cache_read_tokens=None,
                    )
                ]
            ),
        )

        assert response.status_code == 422
        assert "charged_cost" in response.text

    def test_oversized_metadata_returns_422(self, client, db_session, test_user):
        """Metadata beyond the serialized size cap is rejected."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = client.post(
            INGEST_URL,
            json=_payload([_record(metadata={"blob": "x" * 9000})]),
        )

        assert response.status_code == 422
        assert "metadata" in response.text
