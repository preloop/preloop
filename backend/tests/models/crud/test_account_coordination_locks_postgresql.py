"""Account coordination locks must not block child foreign-key checks.

Several CRUD helpers lock the account row only to serialize their own work.
Taking ``FOR UPDATE`` there also blocks the ``FOR KEY SHARE`` that PostgreSQL
takes on the account for every child insert (usage, audit, activity rows).
These locks take ``FOR NO KEY UPDATE`` instead: still exclusive against each
other, invisible to foreign-key checks. Each test runs in its own schema.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import cast

import pytest
from sqlalchemy import Table, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.expected_device import crud_expected_device
from preloop.models.crud.mcp_inventory import crud_mcp_inventory
from preloop.models.crud.runtime_session import crud_runtime_session

Locker = Callable[[Session, uuid.UUID], None]

LOCKERS: dict[str, Locker] = {
    "spawn_alias": lambda db, account_id: crud_runtime_session.lock_alias_account(
        db, account_id=account_id
    ),
    "mcp_inventory": lambda db, account_id: crud_mcp_inventory.lock(
        db, account_id=account_id
    ),
    "expected_device_replace": lambda db, account_id: crud_expected_device.replace(
        db, account_id=account_id, devices=[]
    ),
}


@pytest.fixture
def lock_database(db_engine: Engine) -> Iterator[Engine]:
    """Account, usage and expected-device tables in an isolated schema."""
    if db_engine.dialect.name != "postgresql":
        pytest.skip("Row lock modes need PostgreSQL")
    schema = "account_lock_" + uuid.uuid4().hex
    engine = create_engine(
        db_engine.url, connect_args={"options": f"-csearch_path={schema},public"}
    )
    pending: list[Table] = [
        cast(Table, model.__table__)
        for model in (models.Account, models.ApiUsage, models.ExpectedDevice)
    ]
    tables: dict[str, Table] = {}
    while pending:
        table = pending.pop()
        if table.name in tables:
            continue
        tables[table.name] = table
        pending.extend(fk.column.table for fk in table.foreign_keys)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        models.Base.metadata.create_all(
            connection, tables=list(tables.values()), checkfirst=False
        )
    try:
        yield engine
    finally:
        engine.dispose()
        with db_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def account_id(lock_database: Engine) -> uuid.UUID:
    with Session(lock_database) as db:
        account = models.Account(organization_name="Synthetic lock modes")
        db.add(account)
        db.commit()
        return account.id


def _short_lock_timeout(db: Session) -> None:
    db.execute(text("SET LOCAL lock_timeout = '500ms'"))


@pytest.mark.parametrize("locker", sorted(LOCKERS))
def test_lock_does_not_block_child_foreign_key_insert(
    lock_database: Engine, account_id: uuid.UUID, locker: str
) -> None:
    with Session(lock_database) as holder, Session(lock_database) as writer:
        LOCKERS[locker](holder, account_id)
        _short_lock_timeout(writer)
        writer.add(
            models.ApiUsage(
                account_id=account_id,
                endpoint="/v1/messages",
                method="POST",
                status_code=200,
                duration=0.1,
                action_type="model_gateway",
            )
        )
        writer.flush()  # FK check takes KEY SHARE on the account row
        writer.commit()
        holder.rollback()


@pytest.mark.parametrize("locker", sorted(LOCKERS))
def test_lock_stays_exclusive_between_holders(
    lock_database: Engine, account_id: uuid.UUID, locker: str
) -> None:
    with Session(lock_database) as holder, Session(lock_database) as contender:
        LOCKERS[locker](holder, account_id)
        _short_lock_timeout(contender)
        with pytest.raises(OperationalError, match="lock timeout"):
            LOCKERS[locker](contender, account_id)
        contender.rollback()
        holder.rollback()
