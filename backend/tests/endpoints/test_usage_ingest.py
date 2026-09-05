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
from preloop.models.models.runtime_session import RuntimeSession

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
            {"external_id": "turn-1", "deduplicated": False, "conflict": False},
            {"external_id": "turn-2", "deduplicated": False, "conflict": False},
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
        assert worker.conversation_id == "conv-w1"
        assert worker.parent_conversation_id == "conv-parent"
        assert worker.cost_basis == "estimated"
        assert worker.meta_data["event_type"] == "usage"
        assert worker.endpoint == "/usage/ingest/cursor"

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
        assert body["conflicts"] == 0
        assert body["results"] == [
            {"external_id": "turn-1", "deduplicated": True, "conflict": False}
        ]

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
            ApiUsage.parent_conversation_id, ApiUsage.conversation_id
        )
        rollup = (
            db_session.query(
                thread.label("thread"),
                func.sum(ApiUsage.estimated_cost).label("charged"),
                func.count(distinct(ApiUsage.conversation_id))
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

    def test_conflicting_replay_is_flagged_first_write_wins(
        self, client, db_session, test_user
    ):
        """A replay with a different payload gets conflict=true, not a 409.

        Shippers must be retry-dumb: the batch still returns 200 with the
        record deduplicated, the stored row is unchanged (first write
        wins), and the per-item conflict marker plus the top-level count
        tell the operator the source re-emitted the id with new data.
        """
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        first = client.post(INGEST_URL, json=_payload([_record(external_id="turn-1")]))
        conflicting = client.post(
            INGEST_URL,
            json=_payload([_record(external_id="turn-1", charged_cost="9.99")]),
        )

        assert first.status_code == 200
        assert conflicting.status_code == 200
        body = conflicting.json()
        assert body["accepted"] == 0
        assert body["deduplicated"] == 1
        assert body["conflicts"] == 1
        assert body["results"] == [
            {"external_id": "turn-1", "deduplicated": True, "conflict": True}
        ]

        # First write wins: the stored charge is the original one.
        row = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .one()
        )
        assert abs(row.estimated_cost - 0.42) < 1e-9

    def test_lifecycle_events_count_fanout_without_spend(
        self, client, db_session, test_user
    ):
        """Hook-shaped lifecycle events land cost-free and count fan-out."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        base = {
            "timestamp": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "parent_conversation_id": "conv-parent",
        }
        records = [
            {
                **base,
                "external_id": f"gen-start-{i}",
                "event_type": "subagent_start",
                "conversation_id": f"conv-worker-{i}",
            }
            for i in range(3)
        ]
        records.append(
            {
                **base,
                "external_id": "gen-compaction",
                "event_type": "compaction",
                "conversation_id": "conv-parent",
            }
        )
        response = client.post(INGEST_URL, json=_payload(records))

        assert response.status_code == 200
        assert response.json()["accepted"] == 4

        rows = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .all()
        )
        assert all(row.estimated_cost is None for row in rows)
        assert all(row.model_alias is None for row in rows)

        # Near-real-time fan-out: subagent_start count per parent thread.
        fanout = (
            db_session.query(func.count(ApiUsage.id))
            .filter(
                ApiUsage.account_id == test_user.account_id,
                ApiUsage.action_type == "imported_usage",
                ApiUsage.parent_conversation_id == "conv-parent",
                ApiUsage.meta_data["event_type"].astext == "subagent_start",
            )
            .scalar()
        )
        assert fanout == 3

        # Lifecycle events never contribute charged spend.
        summary = client.get(COST_SUMMARY_URL).json()
        imported = summary["imported_usage"]
        assert imported["imported_cost"] == 0
        assert imported["event_count"] == 4

    def test_usage_event_requires_model(self, client, db_session, test_user):
        """event_type='usage' (the default) still requires a model name."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        response = client.post(INGEST_URL, json=_payload([_record(model=None)]))

        assert response.status_code == 422
        assert "model" in response.text

    def test_growth_tripwire_counts_are_stored(self, client, db_session, test_user):
        """message_count / tool_call_count land in first-class columns."""
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [_record(external_id="turn-1", message_count=41, tool_call_count=7)]
        assert client.post(INGEST_URL, json=_payload(records)).status_code == 200

        row = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .one()
        )
        assert row.message_count == 41
        assert row.tool_call_count == 7

    def test_reconciled_supersedes_estimated_in_cost_summary(
        self, client, db_session, test_user
    ):
        """Reconciled billing rows replace estimates for the same scope.

        conv-1 has two hook-derived estimates ($2.00 + $0.30) and one
        reconciled billing-export record ($1.80): only the reconciled
        dollars count. conv-2 has an un-reconciled estimate ($0.50): it
        keeps counting. Estimated and reconciled dollars are never summed
        for the same conversation; event/token totals still include all
        rows (billing exports rarely carry tokens).
        """
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [
            _record(
                external_id="turn-a",
                conversation_id="conv-1",
                charged_cost="2.00",
                input_tokens=1000,
                output_tokens=200,
            ),
            _record(
                external_id="turn-b",
                conversation_id="conv-1",
                charged_cost="0.30",
                input_tokens=300,
                output_tokens=100,
            ),
            _record(
                external_id="billing-conv-1",
                conversation_id="conv-1",
                charged_cost="1.80",
                cost_basis="reconciled",
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
            ),
            _record(
                external_id="turn-c",
                conversation_id="conv-2",
                charged_cost="0.50",
                input_tokens=100,
                output_tokens=50,
            ),
        ]
        assert client.post(INGEST_URL, json=_payload(records)).json()["accepted"] == 4

        summary = client.get(COST_SUMMARY_URL).json()
        imported = summary["imported_usage"]
        # 1.80 (reconciled, conv-1) + 0.50 (estimated, conv-2). NOT 4.60.
        assert abs(imported["imported_cost"] - 2.30) < 1e-9
        # Event and token truth stays with all rows.
        assert imported["event_count"] == 4
        assert imported["total_tokens"] == 1750

    def test_summary_rolls_up_conversations_with_split_bases(
        self, client, db_session, test_user
    ):
        """The cost summary exposes a per-conversation rollup block.

        The design-partner contract: subagent workers surface under their
        parent thread via parent_conversation_id, and estimated vs
        reconciled amounts arrive as separate fields with nulls preserved
        (never fabricated zeros, never one summed number).
        """
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()

        records = [
            _record(
                external_id="turn-parent-1",
                conversation_id="conv-parent",
                charged_cost="2.00",
                input_tokens=1000,
                output_tokens=200,
            ),
            _record(
                external_id="billing-parent",
                conversation_id="conv-parent",
                charged_cost="1.80",
                cost_basis="reconciled",
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
            ),
            _record(
                external_id="turn-worker-1",
                conversation_id="conv-worker",
                parent_conversation_id="conv-parent",
                charged_cost="0.40",
                input_tokens=300,
                output_tokens=100,
            ),
            {
                "external_id": "gen-worker-start",
                "timestamp": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                "event_type": "subagent_start",
                "conversation_id": "conv-lifecycle-only",
                "parent_conversation_id": "conv-parent",
            },
        ]
        assert client.post(INGEST_URL, json=_payload(records)).json()["accepted"] == 4

        summary = client.get(COST_SUMMARY_URL).json()
        conversations = summary["imported_usage"]["usage_by_conversation"]
        by_id = {row["conversation_id"]: row for row in conversations}
        assert set(by_id) == {"conv-parent", "conv-worker", "conv-lifecycle-only"}

        parent = by_id["conv-parent"]
        assert parent["parent_conversation_id"] is None
        assert parent["event_count"] == 2
        assert abs(parent["estimated_cost"] - 2.00) < 1e-9
        assert abs(parent["reconciled_cost"] - 1.80) < 1e-9
        assert parent["total_tokens"] == 1200
        assert parent["source"] == "cursor"

        worker = by_id["conv-worker"]
        assert worker["parent_conversation_id"] == "conv-parent"
        assert abs(worker["estimated_cost"] - 0.40) < 1e-9
        # Never billed and never estimated: null, not 0.0.
        assert worker["reconciled_cost"] is None

        lifecycle = by_id["conv-lifecycle-only"]
        assert lifecycle["parent_conversation_id"] == "conv-parent"
        assert lifecycle["event_count"] == 1
        assert lifecycle["total_tokens"] is None
        assert lifecycle["estimated_cost"] is None
        assert lifecycle["reconciled_cost"] is None


class TestEstimatedPricing:
    """Estimated records with tokens and no billed amount get a catalog price."""

    def _ingest_one(self, client, db_session, test_user, record):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        response = client.post(INGEST_URL, json=_payload([record]))
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1
        row = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .one()
        )
        return row

    def test_estimated_record_with_tokens_gets_catalog_cost(
        self, client, db_session, test_user
    ):
        """gpt-5 at 1200 in / 850 out prices to $0.01 from the catalog."""
        record = _record(external_id="turn-est", model="gpt-5")
        del record["charged_cost"]
        row = self._ingest_one(client, db_session, test_user, record)

        assert abs(row.estimated_cost - 0.01) < 1e-9
        assert row.cost_source == "catalog"
        assert row.cost_basis == "estimated"
        assert row.currency == "USD"
        assert row.meta_data["pricing"] == {"source": "catalog", "model": "gpt-5"}

    def test_cursor_model_spelling_is_priced(self, client, db_session, test_user):
        """Cursor's claude-4.5-sonnet is priced as the catalog's claude-sonnet-4-5."""
        record = _record(external_id="turn-cursor", model="claude-4.5-sonnet")
        del record["charged_cost"]
        row = self._ingest_one(client, db_session, test_user, record)

        # 1200 * $3/Mtok + 850 * $15/Mtok
        assert abs(row.estimated_cost - 0.01635) < 1e-9
        assert row.cost_source == "catalog"
        assert row.meta_data["pricing"]["model"] == "claude-sonnet-4-5"

    def test_unpriced_model_stays_null(self, client, db_session, test_user):
        """A model the catalog does not know stays unpriced: null, not $0."""
        record = _record(external_id="turn-composer", model="composer")
        del record["charged_cost"]
        row = self._ingest_one(client, db_session, test_user, record)

        assert row.estimated_cost is None
        assert row.cost_source is None
        assert row.currency is None
        assert "pricing" not in row.meta_data

    def test_charged_cost_is_never_replaced_by_an_estimate(
        self, client, db_session, test_user
    ):
        """A vendor charge on a reconciled record is stored as-is."""
        record = _record(
            external_id="turn-billed",
            model="gpt-5",
            charged_cost="0.42",
            cost_basis="reconciled",
        )
        row = self._ingest_one(client, db_session, test_user, record)

        assert abs(row.estimated_cost - 0.42) < 1e-9
        assert row.cost_source == "imported"
        assert row.cost_basis == "reconciled"
        assert "pricing" not in row.meta_data

    def test_reconciled_without_amount_is_not_estimated(
        self, client, db_session, test_user
    ):
        """An 'Included' reconciled row must not be back-filled with a guess."""
        record = _record(
            external_id="turn-included", model="gpt-5", cost_basis="reconciled"
        )
        del record["charged_cost"]
        row = self._ingest_one(client, db_session, test_user, record)

        assert row.estimated_cost is None
        assert row.cost_source is None

    def test_cursor_hook_response_record_is_priced(self, client, db_session, test_user):
        """The shape `preloop usage hook` ships for Cursor stop events is priced."""
        record = {
            "external_id": "stop:gen-1:0",
            "conversation_id": "conv-hook",
            "timestamp": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "event_type": "response",
            "model": "claude-4.5-sonnet",
            "cost_basis": "estimated",
            "input_tokens": 1200,
            "output_tokens": 850,
            "metadata": {
                "hook_event_name": "stop",
                "token_estimate": {
                    "method": "transcript_chars",
                    "chars_per_token": 4,
                    "transcript_bytes": 8200,
                },
            },
        }
        row = self._ingest_one(client, db_session, test_user, record)

        assert abs(row.estimated_cost - 0.01635) < 1e-9
        assert row.cost_source == "catalog"
        assert row.cost_basis == "estimated"
        assert row.meta_data["token_estimate"]["method"] == "transcript_chars"

        summary = client.get(COST_SUMMARY_URL).json()
        imported = summary["imported_usage"]
        assert abs(imported["imported_cost"] - 0.01635) < 1e-9
        conversation = imported["usage_by_conversation"][0]
        assert conversation["conversation_id"] == "conv-hook"
        assert abs(conversation["estimated_cost"] - 0.01635) < 1e-9
        assert conversation["reconciled_cost"] is None

    def test_lifecycle_record_without_tokens_is_not_priced(
        self, client, db_session, test_user
    ):
        """A bare lifecycle marker with a model but no tokens stays unpriced."""
        record = {
            "external_id": "sessionStart:conv-x",
            "conversation_id": "conv-x",
            "timestamp": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "event_type": "session_start",
            "model": "gpt-5",
        }
        row = self._ingest_one(client, db_session, test_user, record)

        assert row.estimated_cost is None
        assert row.cost_source is None


class TestRuntimeSessions:
    """Pushed records register the conversation as a runtime session."""

    @staticmethod
    def _session(db_session, account_id, conversation_id, source="cursor"):
        return (
            db_session.query(RuntimeSession)
            .filter(
                RuntimeSession.account_id == account_id,
                RuntimeSession.session_source_type == source,
                RuntimeSession.session_source_id == conversation_id,
            )
            .one_or_none()
        )

    @staticmethod
    def _lifecycle(event_type, conversation_id, external_id, minutes_ago, **extra):
        record = {
            "external_id": external_id,
            "conversation_id": conversation_id,
            "timestamp": (
                datetime.now(UTC) - timedelta(minutes=minutes_ago)
            ).isoformat(),
            "event_type": event_type,
            "model": "claude-4.5-sonnet",
            "cost_basis": "estimated",
        }
        record.update(extra)
        return record

    def test_session_start_registers_session_with_principal_and_default_title(
        self, client, db_session, test_user
    ):
        agent = _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        started = self._lifecycle(
            "session_start",
            "conv-ses",
            "sessionStart:conv-ses",
            10,
            metadata={
                "hook_event_name": "sessionStart",
                "session_title_default": "Cursor conversation conv-ses",
            },
        )
        response = client.post(INGEST_URL, json=_payload([started]))
        assert response.status_code == 200, response.text

        session = self._session(db_session, test_user.account_id, "conv-ses")
        assert session is not None
        assert session.runtime_principal_type == agent.session_source_type
        assert session.runtime_principal_id == agent.session_source_id
        assert session.runtime_principal_name == "Cursor"
        assert session.title == "Cursor conversation conv-ses"
        assert session.summary is None
        assert session.ended_at is None
        assert session.started_at == session.last_activity_at
        assert session.activities == []

        row = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .one()
        )
        assert row.runtime_session_id == session.id

    def test_stop_touches_activity_and_sets_title_and_summary(
        self, client, db_session, test_user
    ):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        started = self._lifecycle(
            "session_start",
            "conv-live",
            "sessionStart:conv-live",
            10,
            metadata={"session_title_default": "Cursor conversation conv-liv"},
        )
        stop = self._lifecycle(
            "response",
            "conv-live",
            "stop:gen-1:0",
            5,
            input_tokens=28,
            output_tokens=36,
            metadata={
                "hook_event_name": "stop",
                "session_title": "Count the Go files in cli",
                "session_summary": "That is the whole count; nothing else changed.",
            },
        )
        assert (
            client.post(INGEST_URL, json=_payload([started, stop])).json()["accepted"]
            == 2
        )

        session = self._session(db_session, test_user.account_id, "conv-live")
        assert session.title == "Count the Go files in cli"
        assert session.summary == "That is the whole count; nothing else changed."
        assert session.summary_updated_at is not None
        assert session.last_activity_at > session.started_at
        assert session.ended_at is None
        # Summaries only: no transcript text was stored.
        assert session.activities == []

        # A later default title never overrides a real one.
        later = self._lifecycle(
            "response",
            "conv-live",
            "stop:gen-2:0",
            4,
            metadata={"session_title_default": "Cursor conversation conv-liv"},
        )
        client.post(INGEST_URL, json=_payload([later]))
        db_session.refresh(session)
        assert session.title == "Count the Go files in cli"

    def test_session_end_closes_and_session_start_reopens(
        self, client, db_session, test_user
    ):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        started = self._lifecycle(
            "session_start", "conv-end", "sessionStart:conv-end", 10
        )
        ended = self._lifecycle("session_end", "conv-end", "sessionEnd:conv-end", 2)
        client.post(INGEST_URL, json=_payload([started, ended]))

        session = self._session(db_session, test_user.account_id, "conv-end")
        assert session.ended_at is not None
        assert session.last_activity_at == session.ended_at

        # Out-of-order replays never move activity backwards.
        older = self._lifecycle("response", "conv-end", "stop:old:0", 30)
        client.post(INGEST_URL, json=_payload([older]))
        db_session.refresh(session)
        assert session.last_activity_at == session.ended_at

        resumed = self._lifecycle(
            "session_start", "conv-end", "sessionStart:conv-end:2", 1
        )
        client.post(INGEST_URL, json=_payload([resumed]))
        db_session.refresh(session)
        assert session.ended_at is None

    def test_opt_in_transcript_becomes_activities_once(
        self, client, db_session, test_user
    ):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        stop = self._lifecycle(
            "response",
            "conv-text",
            "stop:gen-1:0",
            5,
            input_tokens=28,
            output_tokens=36,
            transcript=[
                {"role": "user", "text": "List the Go files in cli and count them."},
                {"role": "assistant", "text": "I will list the files first."},
                {"role": "tool_use", "text": "Shell"},
                {"role": "assistant", "text": "There are 12 Go files in cli."},
            ],
        )
        assert client.post(INGEST_URL, json=_payload([stop])).json()["accepted"] == 1

        session = self._session(db_session, test_user.account_id, "conv-text")
        activities = sorted(session.activities, key=lambda item: item.summary)
        assert [item.activity_type for item in activities] == ["transcript_message"] * 4
        assert {item.status for item in activities} == {"user", "assistant", "tool_use"}
        by_text = {item.summary: item for item in activities}
        user = by_text["List the Go files in cli and count them."]
        assert user.metadata_["role"] == "user"
        assert user.metadata_["source"] == "usage_ingest:cursor"
        assert user.metadata_["external_id"] == "stop:gen-1:0"

        # Replaying the same record is deduplicated and stores nothing twice.
        replay = client.post(INGEST_URL, json=_payload([stop])).json()
        assert replay["deduplicated"] == 1
        db_session.refresh(session)
        assert len(session.activities) == 4

    def test_transcript_over_cap_is_rejected(self, client, db_session, test_user):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        stop = self._lifecycle(
            "response",
            "conv-big",
            "stop:gen-1:0",
            5,
            transcript=[{"role": "assistant", "text": "x" * 4000} for _ in range(17)],
        )
        response = client.post(INGEST_URL, json=_payload([stop]))
        assert response.status_code == 422
        assert "transcript exceeds" in response.text

    def test_record_without_conversation_registers_no_session(
        self, client, db_session, test_user
    ):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        response = client.post(
            INGEST_URL, json=_payload([_record(external_id="turn-solo")])
        )
        assert response.status_code == 200
        assert (
            db_session.query(RuntimeSession)
            .filter(RuntimeSession.account_id == test_user.account_id)
            .count()
            == 0
        )
        row = (
            db_session.query(ApiUsage)
            .filter(ApiUsage.action_type == "imported_usage")
            .one()
        )
        assert row.runtime_session_id is None

    def test_sessions_are_listed_in_the_runtime_sessions_explorer(
        self, client, db_session, test_user
    ):
        _make_cursor_agent(db_session, test_user.account_id)
        db_session.commit()
        started = self._lifecycle(
            "session_start",
            "conv-list",
            "sessionStart:conv-list",
            10,
            metadata={"session_title_default": "Cursor conversation conv-lis"},
        )
        client.post(INGEST_URL, json=_payload([started]))

        response = client.get("/api/v1/runtime-sessions?session_source_type=cursor")
        assert response.status_code == 200, response.text
        sessions = response.json()["items"]
        assert [item["session_source_id"] for item in sessions] == ["conv-list"]
        assert sessions[0]["title"] == "Cursor conversation conv-lis"
        assert sessions[0]["runtime_principal_name"] == "Cursor"
