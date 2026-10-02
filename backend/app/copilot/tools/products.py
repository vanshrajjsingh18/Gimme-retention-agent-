"""Products, as the order history knows them.

There is no product catalogue or stock feed in this system: a product is what
appears on order lines. The tools say so, and never claim availability.
"""
from __future__ import annotations

from sqlalchemy import case, func, select

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import WINDOWS, find_customer, money, resolve_window
from app.core.enums import OrderStatus
from app.models.entities import Order, OrderItem

GROUP = "products"
NOTE = "Derived from order history. Stock and current availability are not known to this system."
COMPLETED = OrderStatus.COMPLETED.value


def _base(ctx: ToolContext, start=None, end=None):
    query = (
        select(
            OrderItem.product_name,
            OrderItem.category,
            OrderItem.brand,
            func.sum(OrderItem.quantity).label("units"),
            func.coalesce(func.sum(OrderItem.line_total), 0.0).label("revenue"),
            func.count(func.distinct(Order.id)).label("orders"),
            func.count(func.distinct(Order.customer_id)).label("customers"),
            func.max(Order.ordered_at).label("last_ordered"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.status == COMPLETED)
    )
    if start is not None:
        query = query.where(Order.ordered_at >= start, Order.ordered_at < end)
    return query.group_by(OrderItem.product_name, OrderItem.category, OrderItem.brand)


def _row(r) -> dict:
    return {
        "product": r.product_name,
        "category": r.category,
        "brand": r.brand,
        "units": int(r.units or 0),
        "revenue": money(r.revenue),
        "orders": r.orders,
        "customers": r.customers,
        "last_ordered": r.last_ordered.isoformat() if r.last_ordered else None,
    }


def _repeat_rate(ctx: ToolContext, product_name: str) -> float | None:
    per_customer = (
        select(Order.customer_id, func.count(func.distinct(Order.id)).label("n"))
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(Order.status == COMPLETED, OrderItem.product_name == product_name)
        .group_by(Order.customer_id)
        .subquery()
    )
    total, repeat = ctx.db.execute(
        select(func.count(), func.sum(case((per_customer.c.n >= 2, 1), else_=0))).select_from(per_customer)
    ).one()
    repeat = repeat or 0
    return round(repeat / total, 4) if total else None


@tool(
    "search_products",
    group=GROUP,
    risk=Risk.READ,
    description=f"Find products by name, category or brand, with units, revenue and buyers. {NOTE}",
    properties={
        "query": {"type": "string", "description": "Part of the product name."},
        "category": {"type": "string"},
        "brand": {"type": "string"},
        "limit": {"type": "integer"},
    },
)
def search_products(ctx: ToolContext, query: str | None = None, category: str | None = None,
                    brand: str | None = None, limit: int = 20) -> ToolResult:
    stmt = _base(ctx)
    if query:
        stmt = stmt.where(func.lower(OrderItem.product_name).contains(query.lower()))
    if category:
        stmt = stmt.where(func.lower(OrderItem.category) == category.lower())
    if brand:
        stmt = stmt.where(func.lower(OrderItem.brand) == brand.lower())
    rows = ctx.db.execute(stmt.order_by(func.sum(OrderItem.line_total).desc())).all()
    return ToolResult.ok([_row(r) for r in rows[: max(1, min(limit, 100))]],
                         metadata={"count": len(rows), "note": NOTE})


@tool(
    "get_product",
    group=GROUP,
    risk=Risk.READ,
    description=f"One product's all-time sales and repeat-purchase rate. {NOTE}",
    properties={"name": {"type": "string"}},
    required=["name"],
)
def get_product(ctx: ToolContext, name: str) -> ToolResult:
    rows = ctx.db.execute(
        _base(ctx).where(func.lower(OrderItem.product_name).contains(name.lower()))
        .order_by(func.sum(OrderItem.line_total).desc())
    ).all()
    if not rows:
        raise ToolError(f"No product matching '{name}' appears in any order.")
    if len(rows) > 1 and not any(r.product_name.lower() == name.lower() for r in rows):
        raise ToolError(
            f"'{name}' matches {len(rows)} products: " + "; ".join(r.product_name for r in rows[:8])
            + ". Which one?"
        )
    row = next((r for r in rows if r.product_name.lower() == name.lower()), rows[0])
    data = _row(row)
    data["repeat_purchase_rate"] = _repeat_rate(ctx, row.product_name)
    return ToolResult.ok(data, metadata={"note": NOTE, "period": "all time"})


@tool(
    "get_product_performance",
    group=GROUP,
    risk=Risk.READ,
    description=f"Sales of a product, category or brand in a period, with repeat-purchase rate per product. {NOTE}",
    properties={
        "product": {"type": "string"},
        "category": {"type": "string"},
        "brand": {"type": "string"},
        "period": {"type": "string", "enum": list(WINDOWS)},
    },
)
def get_product_performance(ctx: ToolContext, product: str | None = None, category: str | None = None,
                            brand: str | None = None, period: str = "last_30_days") -> ToolResult:
    if not any((product, category, brand)):
        raise ToolError("Name a product, category or brand.")
    start, end, label = resolve_window(period, ctx.now)
    stmt = _base(ctx, start, end)
    if product:
        stmt = stmt.where(func.lower(OrderItem.product_name).contains(product.lower()))
    if category:
        stmt = stmt.where(func.lower(OrderItem.category) == category.lower())
    if brand:
        stmt = stmt.where(func.lower(OrderItem.brand) == brand.lower())
    rows = ctx.db.execute(stmt.order_by(func.sum(OrderItem.line_total).desc())).all()
    products = [_row(r) | {"repeat_purchase_rate": _repeat_rate(ctx, r.product_name)} for r in rows[:25]]
    return ToolResult.ok(
        {
            "products": products,
            "total_units": sum(p["units"] for p in products),
            "total_revenue": money(sum(p["revenue"] for p in products)),
        },
        metadata={"period": label, "note": NOTE + " Repeat rate is all-time."},
    )


@tool(
    "get_top_products",
    group=GROUP,
    risk=Risk.READ,
    description=f"Top products in a period by revenue, units, or all-time repeat-purchase rate. {NOTE}",
    properties={
        "by": {"type": "string", "enum": ["revenue", "units", "repeat_rate"]},
        "category": {"type": "string"},
        "period": {"type": "string", "enum": list(WINDOWS)},
        "limit": {"type": "integer"},
    },
)
def get_top_products(ctx: ToolContext, by: str = "revenue", category: str | None = None,
                     period: str = "last_30_days", limit: int = 10) -> ToolResult:
    start, end, label = resolve_window(period, ctx.now)
    stmt = _base(ctx, start, end)
    if category:
        stmt = stmt.where(func.lower(OrderItem.category) == category.lower())
    rows = [_row(r) for r in ctx.db.execute(stmt).all()]
    if by == "repeat_rate":
        # Rates from a handful of buyers are noise; require a minimum base.
        rows = [r for r in rows if r["customers"] >= 5]
        for r in rows:
            r["repeat_purchase_rate"] = _repeat_rate(ctx, r["product"])
        rows.sort(key=lambda r: -(r["repeat_purchase_rate"] or 0))
    else:
        rows.sort(key=lambda r: -r[by])
    return ToolResult.ok(rows[: max(1, min(limit, 50))],
                         metadata={"period": label, "ranked_by": by, "note": NOTE})


@tool(
    "get_customer_product_affinity",
    group=GROUP,
    risk=Risk.READ,
    description="What one customer buys: products, categories and brands by number of orders.",
    properties={"customer": {"type": "string"}},
    required=["customer"],
)
def get_customer_product_affinity(ctx: ToolContext, customer: str) -> ToolResult:
    found = find_customer(ctx.db, customer)

    def grouped(column):
        return [
            {"name": name, "orders": n, "units": int(units or 0)}
            for name, n, units in ctx.db.execute(
                select(column, func.count(func.distinct(Order.id)), func.sum(OrderItem.quantity))
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.customer_id == found.id, Order.status == COMPLETED)
                .group_by(column)
                .order_by(func.count(func.distinct(Order.id)).desc())
                .limit(10)
            ).all()
        ]

    return ToolResult.ok(
        {
            "customer": found.full_name,
            "products": grouped(OrderItem.product_name),
            "categories": grouped(OrderItem.category),
            "brands": grouped(OrderItem.brand),
        },
        focus=("customer", found.id),
    )
