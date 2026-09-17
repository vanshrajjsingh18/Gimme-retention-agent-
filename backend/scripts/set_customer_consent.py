#!/usr/bin/env python3
"""One-time script: set all customers to have positive consent and refresh segments.

Run this after initial customer CSV import to enable all segmentation rules.
Used with: python -m scripts.set_customer_consent
"""
import sys
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.database import engine
from app.models.entities import Customer
from app.services.intelligence import refresh_customer, refresh_rfm


def main() -> None:
    with Session(engine) as db:
        # Count before
        total_before = db.query(Customer).count()
        if total_before == 0:
            print("No customers found in database.")
            return

        print(f"Setting positive consent for {total_before} customers...")

        # Update all customers to have positive consent
        db.execute(
            update(Customer).values(
                marketing_consent=True,
                email_consent=True,
                sms_consent=True,
                whatsapp_consent=True,
            )
        )
        db.commit()
        print(f"✓ Updated {total_before} customers with positive consent")

        # Refresh customer intelligence for segmentation
        customers = db.query(Customer).all()
        for i, customer in enumerate(customers, 1):
            refresh_customer(db, customer, commit=False)
            if i % 100 == 0:
                print(f"  Refreshed {i}/{len(customers)} customers...")

        db.commit()
        print(f"✓ Refreshed customer intelligence")

        # Refresh RFM scores
        refresh_rfm(db)
        print(f"✓ Refreshed RFM scores")
        print("\n✅ All customers now have positive consent and segments are recalculated!")


if __name__ == "__main__":
    main()
