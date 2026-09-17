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


# ==========================================================================
# Preview as a real dry run
# ==========================================================================
CUSTOMER_CSV = (
    "external_id,email,phone,first_name,last_name\n"
    "PREVIEW-1,alice@example.test,021 123 4567,Alice,Reid\n"
    "PREVIEW-2,bob@example.test,093661234,Bob,Chen\n"
    "PREVIEW-3,,not-a-number,Carla,Ngata\n"
    "PREVIEW-1,dup@example.test,0211111111,Dup,Licate\n"
).encode()


def _preview(client, auth_headers, csv_bytes=CUSTOMER_CSV):
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files={"file": ("customers.csv", csv_bytes, "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_preview_writes_nothing(client, auth_headers, db, seeded):
    """The property the whole feature rests on.

    Every ingestor commits when it finishes, so a preview that simply called
    one and rolled back afterwards would import the file instead of describing
    it. This asserts the containment, not the intent.
    """
    from sqlalchemy import func, select

    from app.models.entities import Customer

    before = db.execute(select(func.count(Customer.id))).scalar_one()
    body = _preview(client, auth_headers)
    assert body["dry_run"]["accepted_rows"] >= 1

    db.expire_all()
    after = db.execute(select(func.count(Customer.id))).scalar_one()
    assert after == before
    assert (
        db.execute(
            select(Customer).where(Customer.external_id == "PREVIEW-1")
        ).scalar_one_or_none()
        is None
    )


def test_a_preview_reports_what_the_import_would_do_row_by_row(client, auth_headers, seeded):
    dry = _preview(client, auth_headers)["dry_run"]
    # Alice and Bob are importable; Carla has no email and an unusable number;
    # the fourth row repeats an external_id already seen in the file.
    assert dry["accepted_rows"] == 2
    assert dry["rejected_rows"] >= 1
    assert dry["duplicate_rows"] == 1
    reasons = " ".join(e["error"] for e in dry["errors"])
    assert "not a mobile number" in reasons


def test_a_preview_says_which_values_it_would_change(client, auth_headers, seeded):
    dry = _preview(client, auth_headers)["dry_run"]
    # "021 123 4567" is stored as +64211234567.
    assert dry["normalized_values"] >= 1
    # Bob's landline is dropped but Bob is kept, because he has an email.
    warnings = " ".join(w["warning"] for w in dry["warnings"])
    assert "email only" in warnings


def test_the_preview_and_the_import_agree(client, auth_headers, db, seeded):
    """A preview nobody can trust is worse than no preview."""
    dry = _preview(client, auth_headers)["dry_run"]
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files={"file": ("customers.csv", CUSTOMER_CSV, "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["accepted_rows"] == dry["accepted_rows"]
    assert job["rejected_rows"] == dry["rejected_rows"]
    assert job["duplicate_rows"] == dry["duplicate_rows"]


def test_a_customer_with_a_good_email_survives_an_unusable_phone(client, auth_headers, db, seeded):
    """A landline is not a reason to throw away an email customer."""
    from sqlalchemy import select

    from app.models.entities import Customer

    csv_bytes = (
        "external_id,email,phone,first_name\n"
        "LANDLINE-1,dee@example.test,09 366 1234,Dee\n"
    ).encode()
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files={"file": ("customers.csv", csv_bytes, "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted_rows"] == 1

    db.expire_all()
    customer = db.execute(
        select(Customer).where(Customer.external_id == "LANDLINE-1")
    ).scalar_one()
    assert customer.email == "dee@example.test"
    # The number they cannot be texted on is not kept as though it were usable.
    assert customer.phone is None


def test_a_row_with_only_an_unusable_phone_is_rejected(client, auth_headers, seeded):
    csv_bytes = "external_id,email,phone\nNOCONTACT-1,,09 366 1234\n".encode()
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files={"file": ("customers.csv", csv_bytes, "text/csv")},
        headers=auth_headers,
    )
    body = response.json()
    assert body["accepted_rows"] == 0
    assert body["rejected_rows"] == 1


def test_a_change_is_only_reported_when_the_row_actually_lands(client, auth_headers, seeded):
    """A row rejected for its date must not claim a phone rewrite that never happens.

    The phone is normalised early, before the fields that can still reject the
    row. Counting there overstated what the import would change — the number on
    screen has to describe rows that survive.
    """
    csv_bytes = (
        "external_id,email,phone,signup_date\n"
        "COUNT-OK,ok@example.test,021 555 0001,2025-03-14\n"
        "COUNT-BADDATE,bad@example.test,021 555 0002,not-a-date\n"
    ).encode()
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files={"file": ("customers.csv", csv_bytes, "text/csv")},
        headers=auth_headers,
    )
    dry = response.json()["dry_run"]
    assert dry["accepted_rows"] == 1
    assert dry["rejected_rows"] == 1
    # Two numbers were reshaped in passing; only one row keeps its version.
    assert dry["normalized_values"] == 1


def test_a_dropped_phone_is_not_reported_for_a_row_that_is_rejected_anyway(
    client, auth_headers, seeded
):
    csv_bytes = (
        "external_id,email,phone,signup_date\n"
        "WARN-BADDATE,bad@example.test,09 366 1234,not-a-date\n"
    ).encode()
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files={"file": ("customers.csv", csv_bytes, "text/csv")},
        headers=auth_headers,
    )
    dry = response.json()["dry_run"]
    assert dry["rejected_rows"] == 1
    assert dry["warnings"] == []


# ==========================================================================
# Consent survives the round trip
# ==========================================================================
# A customer list loaded without consent is not a smaller list — it is a list
# that every campaign silently skips, which looks from the dashboard like the
# product not working. These cover the two ways that happened: a file that
# never mentions consent, and an update that omits the column over customers
# who had already granted it.
def _upload(client, auth_headers, csv_text: str, entity_type: str = "customers"):
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": entity_type},
        files={"file": ("customers.csv", csv_text.encode(), "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_customers_template_carries_every_column_the_importer_reads(
    client, auth_headers
):
    response = client.get("/api/v1/uploads/templates/customers.csv", headers=auth_headers)
    headers = response.text.splitlines()[0].split(",")
    for column in ("marketing_consent", "email_consent", "sms_consent", "whatsapp_consent"):
        assert column in headers, column
    assert "external_id" in headers


def test_the_template_shows_how_to_fill_every_column(client, auth_headers):
    """A header row alone never says that blank consent means no consent."""
    response = client.get("/api/v1/uploads/templates/customers.csv", headers=auth_headers)
    lines = response.text.strip().splitlines()
    assert len(lines) == 2, "the customers template should carry a worked example"
    headers = lines[0].split(",")
    example = dict(zip(headers, lines[1].split(",")))
    assert all(example[column] for column in headers), example
    assert example["marketing_consent"] == "true"


def test_the_template_example_is_never_imported_as_a_customer(client, auth_headers, db):
    """Downloading the template and uploading it back creates nobody.

    The example exists to be read, and a spreadsheet filled in beneath it
    would otherwise turn the instructions into a customer who can be messaged.
    """
    from app.services.ingestion import TEMPLATE_EXAMPLE_ID

    template = client.get(
        "/api/v1/uploads/templates/customers.csv", headers=auth_headers
    ).text
    job = _upload(client, auth_headers, template)

    assert job["status"] == "COMPLETED"
    assert job["accepted_rows"] == 0
    db.expire_all()
    assert (
        db.execute(
            select(Customer).where(Customer.external_id == TEMPLATE_EXAMPLE_ID)
        ).scalar_one_or_none()
        is None
    )


def test_a_file_with_no_consent_columns_says_so_before_anything_is_written(
    client, auth_headers
):
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files={
            "file": (
                "customers.csv",
                b"external_id,email\nNOCONSENT-1,a@example.test\n",
                "text/csv",
            )
        },
        headers=auth_headers,
    )
    warnings = " ".join(response.json()["dry_run"]["file_warnings"])
    assert "no consent columns" in warnings
    assert "skipped by every campaign" in warnings


def test_a_file_that_states_consent_is_not_warned_about(client, auth_headers):
    response = client.post(
        "/api/v1/uploads/preview",
        data={"entity_type": "customers"},
        files={
            "file": (
                "customers.csv",
                b"external_id,email,marketing_consent\nSAIDSO-1,a@example.test,false\n",
                "text/csv",
            )
        },
        headers=auth_headers,
    )
    assert response.json()["dry_run"]["file_warnings"] == []


def test_an_update_that_omits_consent_leaves_it_alone(client, auth_headers, db):
    """The bug this pair exists for.

    A flag has no empty string, so the guard that keeps a blank text column
    from wiping stored data did not cover consent: an update file that simply
    did not mention it wrote False over everybody, revoking consent nobody
    asked to withdraw.
    """
    _upload(
        client,
        auth_headers,
        "external_id,email,marketing_consent,email_consent\n"
        "KEEP-1,keep@example.test,true,true\n",
    )
    # Every row needs a contact detail, even one that only changes a city.
    _upload(
        client, auth_headers, "external_id,email,city\nKEEP-1,keep@example.test,Wellington\n"
    )

    db.expire_all()
    customer = db.execute(
        select(Customer).where(Customer.external_id == "KEEP-1")
    ).scalar_one()
    assert customer.city == "Wellington", "the update should still have landed"
    assert customer.marketing_consent is True
    assert customer.email_consent is True


def test_an_update_that_says_false_does_revoke_consent(client, auth_headers, db):
    """The other half: silence leaves consent alone, but "false" still withdraws it."""
    _upload(
        client,
        auth_headers,
        "external_id,email,marketing_consent\nREVOKE-1,revoke@example.test,true\n",
    )
    _upload(
        client,
        auth_headers,
        "external_id,email,marketing_consent\nREVOKE-1,revoke@example.test,false\n",
    )

    db.expire_all()
    customer = db.execute(
        select(Customer).where(Customer.external_id == "REVOKE-1")
    ).scalar_one()
    assert customer.marketing_consent is False


def test_a_new_customer_the_file_said_nothing_about_has_consented_to_nothing(
    client, auth_headers, db
):
    """Unstated means "leave it alone" only when there is something to leave."""
    _upload(client, auth_headers, "external_id,email\nSILENT-1,silent@example.test\n")

    db.expire_all()
    customer = db.execute(
        select(Customer).where(Customer.external_id == "SILENT-1")
    ).scalar_one()
    assert customer.marketing_consent is False
    assert customer.email_consent is False


# ==========================================================================
# One file for all of it
# ==========================================================================
# Three uploads in a fixed order is a sequence somebody gets wrong, and getting
# it wrong fails confusingly — orders before customers rejects every row for a
# customer that exists in a file nobody has uploaded yet. The combined format
# takes the shape order data actually arrives in: one row per line, with the
# customer repeated down the file.
COMBINED_HEADER = (
    "customer_external_id,email,phone,marketing_consent,email_consent,"
    "order_external_id,ordered_at,status,total_amount,"
    "item_external_id,product_name,category,brand,quantity,unit_price\n"
)


def _combined(client, auth_headers, body: str, preview: bool = False):
    response = client.post(
        f"/api/v1/uploads{'/preview' if preview else ''}",
        data={"entity_type": "combined"},
        files={"file": ("all.csv", (COMBINED_HEADER + body).encode(), "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_one_file_loads_customers_orders_and_lines(client, auth_headers, db):
    job = _combined(
        client,
        auth_headers,
        "CMB-1,a@example.test,0211234567,true,true,CMB-O1,2026-08-01 19:00:00,COMPLETED,48.98,"
        "CMB-I1,Steinlager Classic 12pk,Beer,Steinlager,1,28.99\n"
        "CMB-1,a@example.test,0211234567,true,true,CMB-O1,2026-08-01 19:00:00,COMPLETED,48.98,"
        "CMB-I2,Fat Bird 750ml,Wine,Fat Bird,1,19.99\n"
        "CMB-1,a@example.test,0211234567,true,true,CMB-O2,2026-08-20 19:00:00,COMPLETED,28.99,"
        "CMB-I3,Steinlager Classic 12pk,Beer,Steinlager,1,28.99\n",
    )
    assert job["status"] == "COMPLETED"

    db.expire_all()
    customer = db.execute(
        select(Customer).where(Customer.external_id == "CMB-1")
    ).scalar_one()
    orders = db.execute(select(Order).where(Order.customer_id == customer.id)).scalars().all()
    assert len(orders) == 2
    assert sum(len(o.items) for o in orders) == 3
    assert customer.marketing_consent is True


def test_a_customer_repeated_down_the_file_is_written_once(client, auth_headers):
    """The repeats are how the format works, not duplicate rows to complain about."""
    dry = _combined(
        client,
        auth_headers,
        "CMB-R,r@example.test,0211234567,true,true,CMB-RO1,2026-08-01 19:00:00,COMPLETED,10.00,"
        "CMB-RI1,Steinlager Classic 12pk,Beer,Steinlager,1,10.00\n"
        "CMB-R,r@example.test,0211234567,true,true,CMB-RO1,2026-08-01 19:00:00,COMPLETED,10.00,"
        "CMB-RI2,Fat Bird 750ml,Wine,Fat Bird,1,10.00\n",
        preview=True,
    )["dry_run"]

    sections = {s["entity_type"]: s for s in dry["sections"]}
    assert sections["customers"]["new"] == 1
    assert sections["orders"]["new"] == 1
    assert sections["order_items"]["new"] == 2
    assert dry["duplicate_rows"] == 0


def test_an_error_names_the_line_of_the_file_it_came_from(client, auth_headers):
    """The split batches are shorter than the file.

    8,578 lines hold 993 customers, so an error reported against the customer
    batch would name a row the operator cannot find in the file they uploaded.
    """
    dry = _combined(
        client,
        auth_headers,
        "CMB-A,ok@example.test,0211234567,true,true,CMB-AO,2026-08-01 19:00:00,COMPLETED,10.00,"
        "CMB-AI,Steinlager Classic 12pk,Beer,Steinlager,1,10.00\n"
        "CMB-B,ok2@example.test,0211234567,true,true,CMB-BO,2026-08-02 19:00:00,COMPLETED,10.00,"
        "CMB-BI,Fat Bird 750ml,Wine,Fat Bird,1,10.00\n"
        "CMB-C,,not-a-phone,true,true,CMB-CO,2026-08-03 19:00:00,COMPLETED,10.00,"
        "CMB-CI,Corona 12pk,Beer,Corona,1,10.00\n",
        preview=True,
    )["dry_run"]

    assert [e["row"] for e in dry["errors"]] == [3]


def test_a_rejected_customer_does_not_report_its_orders_as_orphans(client, auth_headers):
    """One root cause, one error.

    A customer refused for an unusable phone used to take its order and line
    down with it, and the order's complaint — "No customer found with
    external_id CMB-C" — reads as though the customer were missing from the
    file, when it is three columns to the left of the real error.
    """
    dry = _combined(
        client,
        auth_headers,
        "CMB-D,,not-a-phone,true,true,CMB-DO,2026-08-03 19:00:00,COMPLETED,10.00,"
        "CMB-DI,Corona 12pk,Beer,Corona,1,10.00\n",
        preview=True,
    )["dry_run"]

    assert len(dry["errors"]) == 1
    assert "not a mobile number" in dry["errors"][0]["error"]
    assert "No customer found" not in " ".join(e["error"] for e in dry["errors"])


def test_a_line_without_a_sku_still_imports(client, auth_headers):
    """Order exports carry a product name and no SKU.

    Requiring one would mean inventing an id per line, and an id invented per
    line makes the same drink a new product on every order — so nothing is ever
    bought twice and nothing can be recommended.
    """
    dry = _combined(
        client,
        auth_headers,
        "CMB-S,s@example.test,0211234567,true,true,CMB-SO,2026-08-01 19:00:00,COMPLETED,10.00,"
        "CMB-SI,Steinlager Classic 12pk,Beer,Steinlager,1,10.00\n",
        preview=True,
    )["dry_run"]

    assert dry["errors"] == []
    assert {s["entity_type"]: s["new"] for s in dry["sections"]}["order_items"] == 1


def test_the_same_drink_on_two_orders_keeps_one_sku(client, auth_headers, db):
    _combined(
        client,
        auth_headers,
        "CMB-K,k@example.test,0211234567,true,true,CMB-KO1,2026-08-01 19:00:00,COMPLETED,10.00,"
        "CMB-KI1,Steinlager Classic 12pk,Beer,Steinlager,1,10.00\n"
        "CMB-K,k@example.test,0211234567,true,true,CMB-KO2,2026-08-09 19:00:00,COMPLETED,10.00,"
        "CMB-KI2,Steinlager Classic 12pk,Beer,Steinlager,1,10.00\n",
    )
    db.expire_all()
    skus = {
        row.sku
        for row in db.execute(
            select(OrderItem).where(OrderItem.external_id.in_(["CMB-KI1", "CMB-KI2"]))
        ).scalars()
    }
    assert len(skus) == 1


def test_the_combined_template_covers_all_three_entities(client, auth_headers):
    response = client.get("/api/v1/uploads/templates/combined.csv", headers=auth_headers)
    headers = response.text.splitlines()[0].split(",")
    for column in (
        "customer_external_id", "marketing_consent", "sms_consent",
        "order_external_id", "ordered_at", "total_amount",
        "item_external_id", "product_name", "category", "brand", "quantity",
    ):
        assert column in headers, column


def test_the_combined_template_shows_the_repeat(client, auth_headers):
    """Two lines of one order: the question anybody filling this in asks first
    is whether the customer columns repeat or are left blank below."""
    response = client.get("/api/v1/uploads/templates/combined.csv", headers=auth_headers)
    lines = response.text.strip().splitlines()
    assert len(lines) == 3
    headers = lines[0].split(",")
    first = dict(zip(headers, lines[1].split(",")))
    second = dict(zip(headers, lines[2].split(",")))
    assert first["order_external_id"] == second["order_external_id"]
    assert first["customer_external_id"] == second["customer_external_id"]
    assert first["item_external_id"] != second["item_external_id"]


def test_the_combined_template_creates_nothing_when_uploaded_back(
    client, auth_headers, db
):
    from app.services.ingestion import TEMPLATE_EXAMPLE_ID

    template = client.get(
        "/api/v1/uploads/templates/combined.csv", headers=auth_headers
    ).text
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "combined"},
        files={"file": ("all.csv", template.encode(), "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    db.expire_all()
    assert (
        db.execute(
            select(Customer).where(Customer.external_id == TEMPLATE_EXAMPLE_ID)
        ).scalar_one_or_none()
        is None
    )
    assert (
        db.execute(
            select(Order).where(Order.external_id == "EXAMPLE-ORDER-1")
        ).scalar_one_or_none()
        is None
    )
