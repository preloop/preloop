"""Pool timeout during usage refresh must not expire the committed row."""

from __future__ import annotations

from sqlalchemy import String, create_engine, event
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from preloop.models.crud.api_usage import _refresh_committed_row


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "usage_refresh_row"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    note: Mapped[str] = mapped_column(String)


def test_refresh_timeout_keeps_committed_columns_without_another_query() -> None:
    """A failed refresh restores the loaded columns instead of lazy-loading."""
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    row = _Row(id="usage-1", note="kept")
    db.add(row)
    db.commit()

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    def fail_refresh(instance: _Row, *args: object, **kwargs: object) -> None:
        db.expire(instance)
        raise SQLAlchemyTimeoutError("select", {}, Exception("pool"))

    db.refresh = fail_refresh  # type: ignore[method-assign]
    started = len(statements)
    _refresh_committed_row(db, row)
    assert row.id == "usage-1"
    assert row.note == "kept"
    assert statements[started:] == []
    db.close()
