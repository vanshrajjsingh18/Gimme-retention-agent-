"""Additive schema reconciliation.

`create_all` builds missing tables but never missing columns, so without this
an existing local database breaks at startup the moment a model gains a field.
The reconciler closes that gap, and must do so without ever risking data.
"""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from app.core.schema import missing_columns, reconcile_schema


@pytest.fixture()
def legacy_engine(tmp_path, monkeypatch):
    """A database built from an older schema, plus the current model metadata.

    The 'old' table is created without the columns the models now declare, and
    seeded with a row — which is what makes adding a NOT NULL column unsafe.
    """
    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}", future=True)

    old = MetaData()
    Table("widgets", old, Column("id", Integer, primary_key=True), Column("name", String(50)))
    old.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'existing')"))

    current = MetaData()
    Table(
        "widgets",
        current,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("colour", String(20), nullable=True),
        Column("weight", Integer, nullable=False, default=7),
        Column("label", String(20), nullable=False, default="unset"),
    )

    class FakeBase:
        metadata = current

    monkeypatch.setattr("app.core.database.Base", FakeBase, raising=False)
    return engine


def columns_of(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_missing_columns_reports_the_gap(legacy_engine):
    assert missing_columns(legacy_engine) == {"widgets": ["colour", "weight", "label"]}


def test_reconcile_adds_the_missing_columns(legacy_engine):
    added = reconcile_schema(legacy_engine)
    assert added == {"widgets": ["colour", "weight", "label"]}
    assert {"colour", "weight", "label"} <= columns_of(legacy_engine, "widgets")


def test_existing_rows_survive_and_are_backfilled(legacy_engine):
    """The whole point: nobody has to delete their data to get the new column."""
    reconcile_schema(legacy_engine)
    with legacy_engine.begin() as connection:
        row = connection.execute(
            text("SELECT name, colour, weight, label FROM widgets WHERE id = 1")
        ).one()
    assert row.name == "existing"
    assert row.colour is None  # nullable, no default
    assert row.weight == 7  # backfilled from the model default
    assert row.label == "unset"


def test_reconcile_is_idempotent(legacy_engine):
    reconcile_schema(legacy_engine)
    assert reconcile_schema(legacy_engine) == {}


def test_a_not_null_column_without_a_default_is_refused(tmp_path, monkeypatch, caplog):
    """Adding it would give existing rows no value, so it needs a real migration."""
    engine = create_engine(f"sqlite:///{tmp_path/'strict.db'}", future=True)
    old = MetaData()
    Table("gadgets", old, Column("id", Integer, primary_key=True))
    old.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO gadgets (id) VALUES (1)"))

    current = MetaData()
    Table(
        "gadgets",
        current,
        Column("id", Integer, primary_key=True),
        Column("owner", String(20), nullable=False),
    )

    class FakeBase:
        metadata = current

    monkeypatch.setattr("app.core.database.Base", FakeBase, raising=False)

    with caplog.at_level("ERROR"):
        assert reconcile_schema(engine) == {}
    assert "needs a real migration" in caplog.text
    # And the table is untouched rather than half-migrated.
    assert columns_of(engine, "gadgets") == {"id"}


def test_a_table_that_does_not_exist_yet_is_left_to_create_all(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'empty.db'}", future=True)
    current = MetaData()
    Table("brand_new", current, Column("id", Integer, primary_key=True))

    class FakeBase:
        metadata = current

    monkeypatch.setattr("app.core.database.Base", FakeBase, raising=False)
    assert missing_columns(engine) == {}
    assert reconcile_schema(engine) == {}


def test_the_real_schema_needs_no_reconciliation_after_create_all(engine):
    """A freshly created database must already match the models exactly."""
    assert missing_columns(engine) == {}
