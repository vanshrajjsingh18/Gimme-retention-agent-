#!/usr/bin/env python
"""Create the database schema and baseline configuration.

Usage:  python -m scripts.init_db
"""
from __future__ import annotations

import logging
import sys

from app.core.config import settings
from app.core.database import session_scope
from app.services.bootstrap import bootstrap, create_tables

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    print(f"Database: {settings.DATABASE_URL}")
    create_tables()
    print("Schema created.")
    with session_scope() as db:
        result = bootstrap(db)
    print("Baseline configuration ready:")
    for key, value in result.items():
        print(f"  {key}: {value}")
    print(f"\nLogin with: {settings.ADMIN_EMAIL} / {settings.ADMIN_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
