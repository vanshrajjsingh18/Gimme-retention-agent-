"""The demo data has to describe the business it is demonstrating.

The seeder picks an order hour "skewed to evenings" and wrote it straight into
a column that holds naive UTC. New Zealand runs twelve or thirteen hours ahead,
so a 7pm order was stored as 19:00 and read back as 7am: every synthetic
customer was a breakfast buyer, and Smart Reorder learned to remind them at
dawn. Nothing caught it while the only reader was a hand-picked customer on one
screen — the comment said evenings and the rows said otherwise.

It matters beyond tidiness: the seeded database is the first thing anybody
evaluating this product looks at, and a dashboard full of 5am reorder windows
says the prediction engine is broken when the data is.
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from app.core.timezones import to_local
from app.models.entities import Customer, Order

EVENING = range(16, 23)  # 4pm to 10pm inclusive


def test_seeded_orders_land_in_the_customers_evening(db, seeded):
    """Asserted against the suite's own synthetic dataset.

    Scoped to the generated customers rather than every order in the
    database: other tests add orders at times chosen to exercise other rules,
    and counting those would make this pass or fail on test ordering.
    """
    hours = Counter(
        to_local(moment).hour
        for moment in db.execute(
            select(Order.ordered_at)
            .join(Customer, Customer.id == Order.customer_id)
            .where(Customer.external_id.like("CUST-%"))
        ).scalars().all()
    )
    assert hours, "no seeded orders to check"

    evening = sum(count for hour, count in hours.items() if hour in EVENING)
    share = evening / sum(hours.values())
    assert share > 0.6, (
        f"only {share:.0%} of seeded orders fall in a local evening — "
        f"busiest local hours were {hours.most_common(3)}"
    )

    # 3am is not a plausible drinks delivery, and a small-hours peak is
    # exactly what writing a local hour into a UTC column produced.
    small_hours = sum(count for hour, count in hours.items() if 1 <= hour <= 8)
    assert small_hours == 0, (
        f"{small_hours} orders placed between 1am and 8am local — "
        f"busiest local hours were {hours.most_common(3)}"
    )
