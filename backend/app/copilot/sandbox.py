"""A session whose writes can never be kept.

Read tools run here so a bug in one can never change data, and write tools run
here to build their preview: the real handler, against the real engine, with
the transaction rolled back afterwards. The service code inside may call
``commit()`` freely — in ``rollback_only`` mode that does not reach the
connection's outer transaction, which is always rolled back on exit.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.orm import Session


class SandboxViolation(RuntimeError):
    """Something tried to commit after the sandbox transaction had ended."""


@contextmanager
def sandbox_session() -> Iterator[Session]:
    from app.core import database

    connection = database.engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="rollback_only",
        autoflush=False,
        expire_on_commit=False,
    )

    @event.listens_for(session, "before_commit")
    def _refuse_escape(_session) -> None:
        # A rollback inside ends the outer transaction; a commit after that
        # would begin and commit a new one for real. Fail instead.
        if not outer.is_active:
            raise SandboxViolation("Preview transaction already ended; refusing to commit.")

    try:
        yield session
    finally:
        session.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
