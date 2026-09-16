"""The ORDERING PATTERN panel's endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.timezones import to_utc_naive
from app.models.base import utcnow
from app.models.entities import Customer, Order


def _customer_with_local_orders(db, local_times):
    stamp = utcnow().timestamp()
    customer = Customer(
        external_id=f"PAT-{stamp}",
        email=f"pat{stamp}@example.test",
        first_name="Sam",
        last_name="Pattern",
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=utcnow() - timedelta(days=400),
    )
    db.add(customer)
    db.flush()
    for i, local in enumerate(local_times):
        db.add(
            Order(
                external_id=f"PATORD-{customer.id}-{i}",
                customer_id=customer.id,
                ordered_at=to_utc_naive(local),
                total_amount=62.0,
            )
        )
    db.commit()
    return customer


def _recent_wednesdays(count: int, *, clock=(19, 40)) -> list[datetime]:
    """Wednesdays ending last week, so the prediction is genuinely upcoming."""
    local_today = datetime.utcnow() + timedelta(hours=12)
    last_wed = local_today - timedelta(days=(local_today.weekday() - 2) % 7 or 7)
    last_wed = last_wed.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    return [last_wed - timedelta(weeks=i) for i in reversed(range(count))]


def test_pattern_endpoint_reports_the_routine_in_local_time(db, client, auth_headers):
    customer = _customer_with_local_orders(db, _recent_wednesdays(5))

    response = client.get(
        f"/api/v1/customers/{customer.id}/order-pattern", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["has_prediction"] is True
    assert body["preferred_weekday_name"] == "Wednesday"
    assert body["preferred_hour"] == 19, f"UTC leaked into the API: {body}"
    assert body["preferred_time_label"] == "7:40 PM"
    assert body["interval_label"] == "7 days"
    assert body["confidence_band"] == "HIGH"
    assert body["timezone"] == "Pacific/Auckland"
    assert body["reminder_at"] is not None
    assert body["reminder_at"] < body["predicted_next_order_at"]


def test_a_customer_without_history_gets_a_reason_not_an_error(db, client, auth_headers):
    customer = _customer_with_local_orders(db, _recent_wednesdays(2))

    response = client.get(
        f"/api/v1/customers/{customer.id}/order-pattern", headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_prediction"] is False
    assert body["predicted_next_order_at"] is None
    assert body["reminder_at"] is None
    assert "at least 3" in body["reason"]


def test_unknown_customer_is_a_404(client, auth_headers):
    assert client.get(
        "/api/v1/customers/98765432/order-pattern", headers=auth_headers
    ).status_code == 404


def test_the_endpoint_requires_authentication(client):
    assert client.get("/api/v1/customers/1/order-pattern").status_code == 401
