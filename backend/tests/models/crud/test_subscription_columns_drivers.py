"""Subscription aggregates must parse with both supported PostgreSQL drivers."""

import pytest
from sqlalchemy import create_engine, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from preloop.models.crud.managed_agent import _subscription_columns


@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_subscription_aggregate_accepts_bound_jsonpath(
    db_engine: Engine, driver: str
) -> None:
    """Execute the real expression, including its driver-specific bind casts."""
    engine = create_engine(db_engine.url.set(drivername=f"postgresql+{driver}"))
    try:
        with engine.connect() as connection:
            # Shadow the table with a synthetic row so both drivers evaluate
            # the function, including the vars argument's JSON object type.
            usage = select(
                literal(1).label("id"),
                literal(10).label("prompt_tokens"),
                literal(5).label("completion_tokens"),
                literal(15).label("total_tokens"),
                literal({"api_equivalent_cost": 1.25}, JSONB).label("meta_data"),
            ).cte("api_usage")
            row = connection.execute(
                select(*_subscription_columns()).add_cte(usage)
            ).one()
        assert row.sub_requests == 1
        assert row.sub_covered == 1
        assert row.sub_equivalent == 1.25
    finally:
        engine.dispose()
