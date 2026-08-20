"""Helpers for constructing order histories in tests."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.analytics.metrics import OrderFact
from app.core.enums import OrderStatus

NOW = datetime(2025, 6, 1, 12, 0, 0)


def order(
    days_ago: float,
    amount: float = 100.0,
    *,
    discount: float = 0.0,
    status: str = OrderStatus.COMPLETED.value,
    category: str = "Beer",
    brand: str = "Steinlager",
    product: str = "Steinlager Classic 12pk",
    quantity: int = 1,
    now: datetime = NOW,
) -> OrderFact:
    return OrderFact(
        ordered_at=now - timedelta(days=days_ago),
        total_amount=amount,
        discount_amount=discount,
        status=status,
        items=[
            {
                "category": category,
                "brand": brand,
                "product_name": product,
                "quantity": quantity,
            }
        ],
    )


def cadence_history(
    *, count: int, interval_days: float, last_order_days_ago: float, amount: float = 100.0
) -> list[OrderFact]:
    """Evenly spaced order history ending ``last_order_days_ago`` days back."""
    return [
        order(last_order_days_ago + i * interval_days, amount=amount)
        for i in range(count)
    ]
