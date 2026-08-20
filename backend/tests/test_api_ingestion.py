"""Ingestion tests: API payloads, CSV upload, validation and persistence."""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.entities import Customer, Order, OrderItem

NOW = datetime.utcnow()


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


@pytest.fixture()
def key_headers(api_key) -> dict:
    return {"X-API-Key": api_key}


def customer_payload(external_id: str, **overrides) -> dict:
    base = {
        "external_id": external_id,
        "email": f"{external_id.lower()}@example.test",
        "phone": "+64211110000",
        "first_name": "Test",
        "last_name": "Customer",
        "age_verified": True,
        "city": "Auckland",
        "signup_date": iso(200),
        "marketing_consent": True,
        "email_consent": True,
    }
    base.update(overrides)
    return base


# ==========================================================================
# API ingestion
# ==========================================================================
def test_customer_api_ingestion_persists(client, key_headers, db):
    response = client.post(
        "/api/v1/customers", json=[customer_payload("API-CUST-1")], headers=key_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted_rows"] == 1
    assert body["rejected_rows"] == 0

    row = db.execute(
        select(Customer).where(Customer.external_id == "API-CUST-1")
    ).scalar_one()
    assert row.email == "api-cust-1@example.test"
    assert row.marketing_consent is True


def test_reposting_the_same_customer_updates_rather_than_duplicates(
    client, key_headers, db
):
    client.post(
        "/api/v1/customers", json=[customer_payload("API-CUST-2")], headers=key_headers
    )
    second = client.post(
        "/api/v1/customers",
        json=[customer_payload("API-CUST-2", city="Wellington")],
        headers=key_headers,
    )
    assert second.json()["updated_rows"] == 1
    assert second.json()["accepted_rows"] == 0

    rows = db.execute(
        select(Customer).where(Customer.external_id == "API-CUST-2")
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].city == "Wellington"


def test_customer_without_contact_details_rejected(client, key_headers):
    response = client.post(
        "/api/v1/customers",
        json=[{"external_id": "API-CUST-NOCONTACT", "first_name": "No"}],
        headers=key_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted_rows"] == 0
    assert body["rejected_rows"] == 1
    assert "email address or a phone number" in body["errors"][0]["error"]


def test_bad_email_rejects_the_row_not_the_batch(client, key_headers):
    """One malformed row must not fail an otherwise good import."""
    response = client.post(
        "/api/v1/customers",
        json=[
            {"external_id": "API-BADMAIL", "email": "not-an-email"},
            customer_payload("API-GOODMAIL"),
        ],
        headers=key_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted_rows"] == 1
    assert body["rejected_rows"] == 1
    assert "not a valid email address" in body["errors"][0]["error"]


def test_structurally_invalid_payload_returns_422(client, key_headers):
    """A type error is a client bug, not a data problem, so the batch fails."""
    response = client.post(
        "/api/v1/orders",
        json=[
            {
                "external_id": "API-ORD-BADTYPE",
                "customer_external_id": "API-CUST-1",
                "ordered_at": "not-a-timestamp",
                "total_amount": "free",
            }
        ],
        headers=key_headers,
    )
    assert response.status_code == 422
    assert "errors" in response.json()


def test_order_ingestion_requires_a_known_customer(client, key_headers):
    response = client.post(
        "/api/v1/orders",
        json=[
            {
                "external_id": "API-ORD-ORPHAN",
                "customer_external_id": "DOES-NOT-EXIST",
                "ordered_at": iso(1),
                "total_amount": 50.0,
            }
        ],
        headers=key_headers,
    )
    assert response.json()["rejected_rows"] == 1
    assert "No customer found" in response.json()["errors"][0]["error"]


def test_negative_amount_rejected_by_schema(client, key_headers):
    response = client.post(
        "/api/v1/orders",
        json=[
            {
                "external_id": "API-ORD-NEG",
                "customer_external_id": "API-CUST-1",
                "ordered_at": iso(1),
                "total_amount": -10.0,
            }
        ],
        headers=key_headers,
    )
    assert response.status_code == 422


def test_order_and_items_ingest_and_drive_metrics(client, key_headers, db, auth_headers):
    client.post(
        "/api/v1/customers", json=[customer_payload("API-CUST-3")], headers=key_headers
    )
    orders = [
        {
            "external_id": f"API-ORD-3-{i}",
            "customer_external_id": "API-CUST-3",
            "ordered_at": iso(days),
            "status": "COMPLETED",
            "total_amount": 100.0,
        }
        for i, days in enumerate([10, 40, 70], start=1)
    ]
    response = client.post("/api/v1/orders", json=orders, headers=key_headers)
    assert response.json()["accepted_rows"] == 3

    items = client.post(
        "/api/v1/order-items",
        json=[
            {
                "external_id": "API-ITEM-3-1",
                "order_external_id": "API-ORD-3-1",
                "sku": "BEER-STE-12",
                "product_name": "Steinlager Classic 12pk",
                "category": "Beer",
                "brand": "Steinlager",
                "quantity": 2,
                "unit_price": 28.99,
            }
        ],
        headers=key_headers,
    )
    assert items.json()["accepted_rows"] == 1

    customer = db.execute(
        select(Customer).where(Customer.external_id == "API-CUST-3")
    ).scalar_one()
    detail = client.get(f"/api/v1/customers/{customer.id}", headers=auth_headers).json()
    profile = detail["profile"]
    assert profile["completed_orders"] == 3
    assert profile["lifetime_revenue"] == 300.0
    assert profile["average_order_value"] == 100.0
    assert profile["days_since_last_order"] == 10
    assert profile["median_purchase_interval_days"] == 30.0
    assert profile["lifecycle_stage"] in {"REGULAR", "ACTIVATING"}
    assert profile["churn_score"] >= 0


def test_event_ingestion(client, key_headers):
    client.post(
        "/api/v1/customers", json=[customer_payload("API-CUST-4")], headers=key_headers
    )
    response = client.post(
        "/api/v1/events",
        json=[
            {
                "customer_external_id": "API-CUST-4",
                "event_type": "ORDER_COMPLETED",
                "occurred_at": iso(2),
                "payload": {"note": "imported"},
            }
        ],
        headers=key_headers,
    )
    assert response.json()["accepted_rows"] == 1


def test_unknown_event_type_rejected(client, key_headers):
    response = client.post(
        "/api/v1/events",
        json=[{"customer_external_id": "API-CUST-4", "event_type": "NOT_A_REAL_EVENT"}],
        headers=key_headers,
    )
    assert response.json()["rejected_rows"] == 1
    assert "not a supported event type" in response.json()["errors"][0]["error"]


def test_consent_event_updates_customer_state(client, key_headers, db):
    client.post(
        "/api/v1/customers", json=[customer_payload("API-CUST-5")], headers=key_headers
    )
    response = client.post(
        "/api/v1/consent-events",
        json=[
            {
                "customer_external_id": "API-CUST-5",
                "consent_type": "MARKETING",
                "granted": False,
                "source": "unsubscribe link",
            }
        ],
        headers=key_headers,
    )
    assert response.json()["accepted_rows"] == 1

    db.expire_all()
    customer = db.execute(
        select(Customer).where(Customer.external_id == "API-CUST-5")
    ).scalar_one()
    assert customer.marketing_consent is False
    # Revoking blanket marketing consent revokes every channel with it.
    assert customer.email_consent is False


# ==========================================================================
# CSV upload
# ==========================================================================
def csv_file(text: str, name: str = "upload.csv"):
    return {"file": (name, io.BytesIO(text.encode()), "text/csv")}


def test_csv_preview_reports_missing_columns(client, auth_headers):
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files=csv_file("first_name,last_name\nSam,Smith\n"),
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "external_id" in body["missing_required_columns"]


def test_csv_preview_accepts_a_valid_file(client, auth_headers):
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files=csv_file(
            "external_id,email,first_name,last_name,age_verified,marketing_consent,email_consent\n"
            "CSV-1,csv1@example.test,Sam,Smith,true,true,true\n"
        ),
        headers=auth_headers,
    )
    body = response.json()
    assert body["valid"] is True
    assert body["total_rows"] == 1
    assert body["sample_rows"][0]["external_id"] == "CSV-1"


def test_csv_upload_imports_and_reports_partial_failures(client, auth_headers, db):
    content = (
        "external_id,email,first_name,last_name,age_verified,signup_date,"
        "marketing_consent,email_consent\n"
        "CSV-10,csv10@example.test,Ana,Ngata,true,2025-01-15,true,true\n"
        "CSV-11,csv11@example.test,Ben,Cooper,true,2025-02-20,true,true\n"
        ",missing@example.test,No,Id,true,2025-02-20,true,true\n"
        "CSV-12,csv12@example.test,Bad,Date,true,not-a-date,true,true\n"
    )
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files=csv_file(content, "customers.csv"),
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "COMPLETED"
    assert job["total_rows"] == 4
    assert job["accepted_rows"] == 2
    assert job["rejected_rows"] == 2
    reasons = " ".join(e["error"] for e in job["errors"])
    assert "'external_id' is required" in reasons
    assert "not a recognised date" in reasons

    assert db.execute(
        select(Customer).where(Customer.external_id == "CSV-10")
    ).scalar_one_or_none() is not None


def test_duplicate_rows_within_a_file_are_reported(client, auth_headers):
    content = (
        "external_id,email,age_verified\n"
        "CSV-DUP,dup@example.test,true\n"
        "CSV-DUP,dup@example.test,true\n"
    )
    job = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files=csv_file(content),
        headers=auth_headers,
    ).json()
    assert job["duplicate_rows"] == 1
    assert job["accepted_rows"] == 1


def test_upload_with_missing_columns_fails_the_job(client, auth_headers):
    job = client.post(
        "/api/v1/uploads",
        data={"entity_type": "orders"},
        files=csv_file("external_id\nORD-1\n"),
        headers=auth_headers,
    ).json()
    assert job["status"] == "FAILED"
    assert "missing required columns" in job["errors"][0]["error"]


def test_empty_file_rejected(client, auth_headers):
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files=csv_file(""),
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_unknown_entity_type_rejected(client, auth_headers):
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "widgets"},
        files=csv_file("a,b\n1,2\n"),
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "Unknown entity type" in response.json()["detail"]


def test_error_report_downloads_as_csv(client, auth_headers):
    job = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files=csv_file("external_id,email,age_verified\n,bad@example.test,true\n"),
        headers=auth_headers,
    ).json()
    response = client.get(
        f"/api/v1/uploads/{job['id']}/errors.csv", headers=auth_headers
    )
    assert response.status_code == 200
    assert "row,error,identifiers" in response.text
    assert "required" in response.text


def test_templates_available_for_every_entity(client, auth_headers):
    for entity in ("customers", "orders", "order_items", "events", "consent_events"):
        response = client.get(
            f"/api/v1/uploads/templates/{entity}.csv", headers=auth_headers
        )
        assert response.status_code == 200, entity
        assert response.text.strip()


def test_ingestion_jobs_are_listed(client, auth_headers):
    response = client.get("/api/v1/uploads", headers=auth_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_csv_ingestion_handles_utf8_bom(client, auth_headers, db):
    content = "external_id,email,age_verified\nCSV-BOM,bom@example.test,true\n"
    job = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files={"file": ("bom.csv", io.BytesIO(content.encode("utf-8-sig")), "text/csv")},
        headers=auth_headers,
    ).json()
    assert job["accepted_rows"] == 1
    assert db.execute(
        select(Customer).where(Customer.external_id == "CSV-BOM")
    ).scalar_one_or_none() is not None
