"""Additive schema reconciliation for an existing local database.

``Base.metadata.create_all`` creates missing *tables* but never missing
*columns*, so adding a field to a model leaves anyone with an existing
database with an app that fails at startup on "no such column". For a
local-first tool that is the common case, not an edge case: the developer
already has data they do not want to throw away.

This closes that gap in the narrowest way that is safe:

* **additive only** — columns are added, never dropped, renamed or retyped, so
  running it can never destroy data;
* **nullable or defaulted only** — a NOT NULL column with no default cannot be
  added to a table with existing rows, and is reported rather than attempted;
* **idempotent** — already-present columns are skipped.

Anything beyond adding a column (a type change, a new constraint, a backfill)
is a real migration and belongs in Alembic, which is already a dependency.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)


def missing_columns(engine: Engine) -> dict[str, list[str]]:
    """Columns the models declare that the database does not have."""
    from app.core.database import Base

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    gaps: dict[str, list[str]] = {}

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all will build it whole.
        present = {column["name"] for column in inspector.get_columns(table.name)}
        absent = [column.name for column in table.columns if column.name not in present]
        if absent:
            gaps[table.name] = absent
    return gaps


def reconcile_schema(engine: Engine) -> dict[str, list[str]]:
    """Add any columns the models declare but the database lacks.

    Returns what was added, per table. Columns that cannot be added safely are
    logged with the reason and left alone — failing loudly at startup is better
    than a half-applied schema.
    """
    from app.core.database import Base

    tables = {table.name: table for table in Base.metadata.sorted_tables}
    added: dict[str, list[str]] = {}

    for table_name, column_names in missing_columns(engine).items():
        table = tables[table_name]
        for column_name in column_names:
            column = table.columns[column_name]
            default = _literal_default(column)
            # A NOT NULL column needs a value the DDL can actually carry.
            # "Has a default" is not enough: a Python-side callable default
            # (``default=list``) exists but cannot be written into an ALTER,
            # so SQLite rejects the statement. What matters is whether a
            # literal could be derived.
            if not column.nullable and column.server_default is None and default is None:
                logger.error(
                    "Cannot add %s.%s automatically: it is NOT NULL and no literal "
                    "default can be derived for it. Existing rows would have no value, "
                    "so this needs a real migration.",
                    table_name,
                    column_name,
                )
                continue

            # DDL cannot be parameterised, so identifiers are interpolated.
            # They come from the model metadata this process defines, never
            # from a request — and they are quoted by the dialect's preparer
            # so the statement stays well-formed whatever a column is called.
            quote = engine.dialect.identifier_preparer.quote
            table_sql, column_sql = quote(table_name), quote(column_name)

            spec = CreateColumn(column).compile(engine).string
            statement = f"ALTER TABLE {table_sql} ADD COLUMN {spec}"
            if default is not None and "DEFAULT" not in spec.upper():
                statement += f" DEFAULT {default}"

            with engine.begin() as connection:
                connection.execute(text(statement))
                if default is not None:
                    # SQLite backfills existing rows from the DEFAULT clause,
                    # but a Python-side default has no clause, so set it here.
                    connection.execute(
                        text(
                            f"UPDATE {table_sql} SET {column_sql} = :value "
                            f"WHERE {column_sql} IS NULL"
                        ),
                        {"value": _default_value(column)},
                    )
            added.setdefault(table_name, []).append(column_name)
            logger.info("Added column %s.%s", table_name, column_name)

    return added


def _default_value(column):
    """The Python value a column's default produces, or None.

    A callable default is invoked: ``default=list`` on a JSON column is the
    normal way to say "empty list", and refusing to add such a column would
    make every JSON field un-addable.
    """
    default = column.default
    if default is None or column.server_default is not None:
        return None
    if default.is_callable:
        try:
            value = default.arg(None)
        except TypeError:
            try:
                value = default.arg()
            except Exception:  # noqa: BLE001 - a default we cannot evaluate
                return None
        except Exception:  # noqa: BLE001
            return None
    elif default.is_scalar:
        value = default.arg
    else:
        return None

    if isinstance(value, (list, dict)):
        # JSON columns round-trip through the serialiser, not a raw literal.
        return json.dumps(value)
    return value


def _literal_default(column) -> str | None:
    """A SQL literal for a column's default, if one can be derived."""
    if column.server_default is not None:
        return None  # The DDL already carries it.
    value = _default_value(column)
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None
