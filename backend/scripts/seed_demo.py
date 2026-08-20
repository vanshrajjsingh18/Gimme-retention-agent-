#!/usr/bin/env python
"""Generate the reproducible synthetic demo dataset.

Usage:
    python -m scripts.seed_demo                 # 1000 customers, keeps existing data
    python -m scripts.seed_demo --reset         # wipe demo data first
    python -m scripts.seed_demo --customers 250 # smaller dataset
    python -m scripts.seed_demo --no-campaigns  # skip historical campaigns
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from app.core.config import settings
from app.core.database import session_scope
from app.services.bootstrap import bootstrap, create_tables
from app.services.intelligence import refresh_all_customers
from app.services.seed import clear_demo_data, generate_customers, summary
from app.services.seed_campaigns import seed_campaigns
from app.services.segments import refresh_all_segments

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the GIMME Retention Engine demo data.")
    parser.add_argument("--customers", type=int, default=1000, help="How many customers to create.")
    parser.add_argument("--reset", action="store_true", help="Delete existing demo data first.")
    parser.add_argument("--no-campaigns", action="store_true", help="Skip historical campaigns.")
    parser.add_argument("--seed", type=int, default=settings.MOCK_SEED, help="Random seed.")
    args = parser.parse_args()

    started = time.time()
    create_tables()

    with session_scope() as db:
        bootstrap(db)

    if args.reset:
        print("Clearing existing demo data...")
        with session_scope() as db:
            clear_demo_data(db)

    print(f"Generating {args.customers} customers with 12 months of order history...")
    with session_scope() as db:
        counts = generate_customers(db, count=args.customers, seed=args.seed)
    print(
        f"  {counts['customers']} customers, {counts['orders']} orders, "
        f"{counts['order_items']} order items, {counts['consent_events']} consent events"
    )

    print("Computing metrics, lifecycle, churn, RFM and recommendations...")
    with session_scope() as db:
        intel = refresh_all_customers(db)
    print(f"  {intel['customers_processed']} customers scored, {intel['rfm_scored']} RFM scores")

    print("Evaluating segments...")
    with session_scope() as db:
        segments = refresh_all_segments(db)
    for name, count in sorted(segments.items()):
        print(f"  {name}: {count}")

    if not args.no_campaigns:
        print("Seeding historical campaigns with engagement and attribution...")
        with session_scope() as db:
            campaigns = seed_campaigns(db, seed=args.seed)
        print(
            f"  {campaigns['campaigns']} campaigns, {campaigns['messages_sent']} messages, "
            f"{campaigns['communication_events']} events"
        )
        print(
            f"  attribution: {campaigns['attribution']['orders_attributed']} orders, "
            f"${campaigns['attribution']['attributed_revenue']:,.2f} revenue, "
            f"{campaigns['attribution']['reactivations']} reactivations"
        )

        # Lifecycle and churn shift once campaign engagement exists.
        print("Recomputing intelligence after campaign activity...")
        with session_scope() as db:
            refresh_all_customers(db)
            refresh_all_segments(db)

    with session_scope() as db:
        totals = summary(db)

    print(f"\nSeed complete in {time.time() - started:.1f}s")
    for key, value in totals.items():
        print(f"  {key}: {value}")
    print(f"\nLogin with: {settings.ADMIN_EMAIL} / {settings.ADMIN_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
