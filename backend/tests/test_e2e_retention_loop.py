"""End-to-end test of the complete retention loop.

Runs the full product workflow through the HTTP API in one ordered scenario:
upload data, inspect intelligence, configure brand, generate and approve a
message, build a segment, create a compliance-gated campaign, send it in MOCK
MODE, ingest a returning customer's order, and verify the reactivation,
attribution and analytics that follow.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.entities import (
    AttributionRecord,
    Campaign,
    CommunicationEvent,
    Customer,
    Message,
)

NOW = datetime.utcnow()


def iso(days_ago: float, hour: int = 18) -> str:
    return (
        (NOW - timedelta(days=days_ago))
        .replace(hour=hour, minute=0, second=0, microsecond=0)
        .isoformat()
    )


# The scenario builds one lapsed high-value customer plus supporting cast, so
# every eligibility and exclusion path has a subject.
SCENARIO_CUSTOMERS = [
    # The hero: high value, long lapsed, fully contactable.
    {
        "external_id": "E2E-LAPSED",
        "email": "lapsed@example.test",
        "phone": "+64211000001",
        "first_name": "Mere",
        "last_name": "Ngata",
        "age_verified": True,
        "city": "Auckland",
        "signup_date": iso(500),
        "marketing_consent": True,
        "email_consent": True,
    },
    # Excluded: no marketing consent.
    {
        "external_id": "E2E-NOCONSENT",
        "email": "noconsent@example.test",
        "first_name": "Tim",
        "last_name": "Walker",
        "age_verified": True,
        "signup_date": iso(500),
        "marketing_consent": False,
        "email_consent": False,
    },
    # Excluded: age not verified.
    {
        "external_id": "E2E-UNVERIFIED",
        "email": "unverified@example.test",
        "first_name": "Ana",
        "last_name": "Patel",
        "age_verified": False,
        "signup_date": iso(500),
        "marketing_consent": True,
        "email_consent": True,
    },
    # Excluded later by the suppression list.
    {
        "external_id": "E2E-SUPPRESS",
        "email": "suppress@example.test",
        "first_name": "Joe",
        "last_name": "Kaur",
        "age_verified": True,
        "signup_date": iso(500),
        "marketing_consent": True,
        "email_consent": True,
    },
]


def lapsed_orders(external_id: str) -> list[dict]:
    """Six orders on a ~30-day cadence, all more than 150 days ago."""
    return [
        {
            "external_id": f"E2E-ORD-{external_id}-{i}",
            "customer_external_id": external_id,
            "ordered_at": iso(160 + i * 30),
            "status": "COMPLETED",
            "total_amount": 140.0,
        }
        for i in range(6)
    ]


@pytest.fixture(scope="module")
def scenario(request):
    """Module-scoped client + auth so the workflow runs as one story."""
    from fastapi.testclient import TestClient

    from app.core.database import SessionLocal, get_db
    from app.main import app

    def override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    with client:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@gimmedelivery.co.nz", "password": "GimmeAdmin123!"},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        key = client.post(
            "/api/v1/api-keys", json={"name": "e2e"}, headers=headers
        ).json()["api_key"]
        yield {
            "client": client,
            "headers": headers,
            "key_headers": {"X-API-Key": key},
            "state": {},
        }
    app.dependency_overrides.clear()


@pytest.mark.usefixtures("bootstrapped")
class TestRetentionLoop:
    """Ordered scenario. Each step depends on the state the previous one left."""

    def test_01_upload_customers_by_csv(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        rows = [
            "external_id,email,phone,first_name,last_name,age_verified,city,"
            "signup_date,marketing_consent,email_consent"
        ]
        for c in SCENARIO_CUSTOMERS:
            rows.append(
                f"{c['external_id']},{c.get('email','')},{c.get('phone','')},"
                f"{c['first_name']},{c['last_name']},{str(c['age_verified']).lower()},"
                f"{c.get('city','')},{c['signup_date']},"
                f"{str(c['marketing_consent']).lower()},{str(c['email_consent']).lower()}"
            )
        content = "\n".join(rows) + "\n"

        response = client.post(
            "/api/v1/uploads",
            data={"entity_type": "customers"},
            files={"file": ("customers.csv", io.BytesIO(content.encode()), "text/csv")},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["status"] == "COMPLETED"
        assert job["accepted_rows"] == len(SCENARIO_CUSTOMERS)
        assert job["rejected_rows"] == 0

    def test_02_ingest_orders_via_api(self, scenario):
        client, key_headers = scenario["client"], scenario["key_headers"]
        orders = []
        for c in SCENARIO_CUSTOMERS:
            orders.extend(lapsed_orders(c["external_id"]))

        response = client.post("/api/v1/orders", json=orders, headers=key_headers)
        assert response.status_code == 200, response.text
        assert response.json()["accepted_rows"] == len(orders)

        items = [
            {
                "external_id": f"E2E-ITEM-{o['external_id']}",
                "order_external_id": o["external_id"],
                "sku": "WINE-CLO-SB",
                "product_name": "Cloudy Bay Sauvignon Blanc",
                "category": "Wine",
                "brand": "Cloudy Bay",
                "quantity": 2,
                "unit_price": 39.99,
            }
            for o in orders
        ]
        response = client.post("/api/v1/order-items", json=items, headers=key_headers)
        assert response.json()["accepted_rows"] == len(items)

    def test_03_customer_360_shows_computed_intelligence(self, scenario, db):
        client, headers = scenario["client"], scenario["headers"]
        customer = db.execute(
            select(Customer).where(Customer.external_id == "E2E-LAPSED")
        ).scalar_one()
        scenario["state"]["customer_id"] = customer.id

        response = client.get(f"/api/v1/customers/{customer.id}", headers=headers)
        assert response.status_code == 200
        profile = response.json()["profile"]

        # Metrics computed from the ingested orders, not defaults.
        assert profile["completed_orders"] == 6
        assert profile["lifetime_revenue"] == pytest.approx(840.0)
        assert profile["average_order_value"] == pytest.approx(140.0)
        assert profile["median_purchase_interval_days"] == pytest.approx(30.0, abs=1)
        assert profile["days_since_last_order"] >= 160
        assert profile["preferred_categories"] == ["Wine"]
        assert profile["top_products"][0]["product_name"] == "Cloudy Bay Sauvignon Blanc"

        # Lifecycle: 160+ days against a 30-day cycle is dormant or churned.
        assert profile["lifecycle_stage"] in {"DORMANT", "CHURNED"}
        assert profile["expected_cycle_days"] == pytest.approx(30.0, abs=1)
        assert profile["cadence_source"] == "personal"
        assert profile["days_overdue"] > 100

        # Churn: scored, banded and explained.
        assert profile["churn_score"] >= 45
        assert profile["churn_risk_band"] in {"HIGH", "CRITICAL"}
        assert "cadence_overdue" in {f["code"] for f in profile["churn_factors"]}
        assert str(profile["days_since_last_order"]) in profile["churn_explanation"]

        # RFM and next best action.
        assert profile["rfm_cell"] and len(profile["rfm_cell"]) == 3
        assert profile["rfm_segment"]
        assert profile["recommended_action"] in {"REACTIVATION", "WIN_BACK"}
        assert profile["recommendation_explanation"]
        scenario["state"]["stage_before"] = profile["lifecycle_stage"]
        scenario["state"]["churn_before"] = profile["churn_score"]
        scenario["state"]["revenue_before"] = profile["lifetime_revenue"]

    def test_04_configure_brand(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.put(
            "/api/v1/brand",
            json={
                "company_name": "GIMME",
                "delivery_promise": "Delivered in 60 minutes across our delivery areas",
                "responsible_drinking_statement": "Please enjoy responsibly.",
                "age_restriction_statement": (
                    "You must be 18 or over to purchase alcohol. We ID on delivery."
                ),
                "allowed_promotions": [],
                "active_coupon_codes": [],
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["delivery_promise"].startswith("Delivered in 60 minutes")

    def test_05_generate_a_grounded_message(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.post(
            "/api/v1/messages/generate",
            json={
                "customer_id": scenario["state"]["customer_id"],
                "channel": "EMAIL",
                "objective": "REACTIVATION",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        message = response.json()
        scenario["state"]["message_id"] = message["id"]

        assert message["status"] == "GENERATED"
        assert message["validation_result"]["valid"] is True
        assert message["llm_provider"] == "mock"
        assert message["prompt_version"]
        assert message["subject"] and message["body"]

        # Grounded in this customer's real data.
        assert "Mere" in message["body"]
        assert "Cloudy Bay Sauvignon Blanc" in message["body"]
        assert "Please enjoy responsibly." in message["body"]
        # No invented offers, since none are configured as verified.
        assert "%" not in message["body"]
        assert "{{" not in message["body"]

    def test_06_editing_in_an_invented_offer_blocks_approval(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        message_id = scenario["state"]["message_id"]

        edited = client.patch(
            f"/api/v1/messages/{message_id}",
            json={
                "body": (
                    "Hi Mere, take 40% off with code MEGA50! Only 2 left in stock. "
                    "We deliver in 10 minutes.\n\nPlease enjoy responsibly."
                )
            },
            headers=headers,
        )
        assert edited.status_code == 200
        result = edited.json()
        assert result["status"] == "VALIDATION_FAILED"
        assert result["validation_result"]["valid"] is False
        codes = {e["code"] for e in result["validation_result"]["errors"]}
        assert "UNVERIFIED_COUPON_CODE" in codes
        assert "UNVERIFIED_PROMOTION" in codes
        assert "UNVERIFIED_STOCK_CLAIM" in codes
        assert "UNVERIFIED_DELIVERY_CLAIM" in codes

        refused = client.post(
            f"/api/v1/messages/{message_id}/approve", headers=headers
        )
        assert refused.status_code == 400
        assert "cannot be approved" in refused.json()["detail"]

    def test_07_edit_back_to_grounded_copy_and_approve(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        message_id = scenario["state"]["message_id"]

        client.patch(
            f"/api/v1/messages/{message_id}",
            json={
                "body": (
                    "Hi Mere,\n\nIt has been a while since your last order. Your Cloudy Bay "
                    "Sauvignon Blanc is still in your order history.\n\nCheers,\nThe GIMME "
                    "Team\n\nPlease enjoy responsibly.\nYou must be 18 or over to purchase "
                    "alcohol. We ID on delivery."
                )
            },
            headers=headers,
        )
        approved = client.post(f"/api/v1/messages/{message_id}/approve", headers=headers)
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "APPROVED"
        assert body["approved_at"]
        assert body["was_edited"] is True

    def test_08_create_a_dynamic_segment_and_preview_it(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        rule = {
            "op": "AND",
            "conditions": [
                {
                    "field": "lifecycle_stage",
                    "operator": "in",
                    "value": ["DORMANT", "CHURNED"],
                },
                {"field": "lifetime_revenue", "operator": "gte", "value": 500},
            ],
        }

        preview = client.post(
            "/api/v1/segments/preview",
            json={"rule_definition": rule, "limit": 10},
            headers=headers,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["matched_customers"] >= 4

        created = client.post(
            "/api/v1/segments",
            json={
                "name": "E2E Lapsed High Value",
                "description": "Lapsed customers worth $500+.",
                "segment_type": "DYNAMIC",
                "rule_definition": rule,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        segment = created.json()
        scenario["state"]["segment_id"] = segment["id"]
        assert segment["member_count"] >= 4
        assert "Lifecycle stage is one of" in segment["rule_description"]

    def test_09_invalid_segment_rule_is_rejected(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.post(
            "/api/v1/segments/preview",
            json={
                "rule_definition": {
                    "field": "not_a_field",
                    "operator": "eq",
                    "value": 1,
                }
            },
            headers=headers,
        )
        assert response.status_code == 400
        assert "Unknown field" in response.json()["detail"]

    def test_10_suppress_one_customer(self, scenario, db):
        client, headers = scenario["client"], scenario["headers"]
        customer = db.execute(
            select(Customer).where(Customer.external_id == "E2E-SUPPRESS")
        ).scalar_one()
        response = client.post(
            f"/api/v1/customers/{customer.id}/suppress",
            json={"channel": "ALL", "reason": "Asked us to stop."},
            headers=headers,
        )
        assert response.status_code == 200
        db.expire_all()
        assert db.get(Customer, customer.id).is_suppressed is True

    def test_11_create_campaign(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.post(
            "/api/v1/campaigns",
            json={
                "name": "E2E Win Back",
                "objective": "REACTIVATION",
                "channel": "EMAIL",
                "segment_id": scenario["state"]["segment_id"],
                "attribution_window_hours": 72,
                "subject": "It has been a while",
                "body": (
                    "Hi there,\n\nIt has been a while since your last order. We are still "
                    "delivering.\n\nCheers,\nThe GIMME Team\n\nPlease enjoy responsibly.\n"
                    "You must be 18 or over to purchase alcohol. We ID on delivery."
                ),
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        campaign = response.json()
        scenario["state"]["campaign_id"] = campaign["id"]
        assert campaign["status"] == "DRAFT"

    def test_12_audience_preview_enforces_consent_age_and_suppression(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.get(
            f"/api/v1/campaigns/{scenario['state']['campaign_id']}/audience",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        audience = response.json()

        assert audience["eligible_count"] >= 1
        reasons = audience["excluded_by_reason"]
        assert reasons.get("EXCLUDED_NO_CONSENT", 0) >= 1
        assert reasons.get("EXCLUDED_AGE", 0) >= 1
        assert reasons.get("EXCLUDED_SUPPRESSED", 0) >= 1

        eligible_ids = {r["external_id"] for r in audience["sample_recipients"]}
        assert "E2E-LAPSED" in eligible_ids
        assert "E2E-NOCONSENT" not in eligible_ids
        assert "E2E-UNVERIFIED" not in eligible_ids
        assert "E2E-SUPPRESS" not in eligible_ids

    def test_13_sending_without_approval_is_refused(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.post(
            f"/api/v1/campaigns/{scenario['state']['campaign_id']}/run",
            json={},
            headers=headers,
        )
        assert response.status_code == 400
        assert "approved" in response.json()["detail"].lower()

    def test_14_compliance_blocks_a_prohibited_claim(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        campaign_id = scenario["state"]["campaign_id"]

        client.patch(
            f"/api/v1/campaigns/{campaign_id}",
            json={
                "body": (
                    "Hi there, a daily glass is good for your heart. Get wasted this "
                    "weekend!\n\nPlease enjoy responsibly."
                )
            },
            headers=headers,
        )
        report = client.post(
            f"/api/v1/campaigns/{campaign_id}/compliance-check", headers=headers
        ).json()
        assert report["passed"] is False
        codes = {f["code"] for f in report["findings"] if f["blocks_send"]}
        assert "HEALTH_CLAIM" in codes
        assert "EXCESSIVE_CONSUMPTION" in codes

        refused = client.post(f"/api/v1/campaigns/{campaign_id}/submit", headers=headers)
        assert refused.status_code == 400

    def test_15_fix_copy_run_compliance_and_approve(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        campaign_id = scenario["state"]["campaign_id"]

        client.patch(
            f"/api/v1/campaigns/{campaign_id}",
            json={
                "body": (
                    "Hi there,\n\nIt has been a while since your last order. We are still "
                    "delivering.\n\nCheers,\nThe GIMME Team\n\nPlease enjoy responsibly.\n"
                    "You must be 18 or over to purchase alcohol. We ID on delivery."
                )
            },
            headers=headers,
        )
        report = client.post(
            f"/api/v1/campaigns/{campaign_id}/compliance-check", headers=headers
        ).json()
        assert report["passed"] is True

        submitted = client.post(f"/api/v1/campaigns/{campaign_id}/submit", headers=headers)
        assert submitted.json()["status"] == "AWAITING_APPROVAL"

        approved = client.post(f"/api/v1/campaigns/{campaign_id}/approve", headers=headers)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
        assert approved.json()["approved_at"]

    def test_16_send_a_test_message(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        response = client.post(
            f"/api/v1/campaigns/{scenario['state']['campaign_id']}/send-test",
            json={"to": "marketing@gimmedelivery.co.nz"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["success"] is True
        assert result["is_simulated"] is True

    def test_17_run_the_campaign_in_mock_mode(self, scenario, db):
        client, headers = scenario["client"], scenario["headers"]
        campaign_id = scenario["state"]["campaign_id"]

        snapshot = client.post(
            f"/api/v1/campaigns/{campaign_id}/audience/snapshot", headers=headers
        ).json()
        assert snapshot["eligible_count"] >= 1

        stats = client.post(
            f"/api/v1/campaigns/{campaign_id}/run",
            json={"generate_per_customer": True, "simulate_engagement": True},
            headers=headers,
        )
        assert stats.status_code == 200, stats.text
        result = stats.json()
        assert result["is_mock"] is True
        assert result["sent"] >= 1
        assert result["campaign_status"] == "COMPLETED"

        # Messages and communication events are persisted.
        messages = db.execute(
            select(Message).where(
                Message.campaign_id == campaign_id, Message.is_test.is_(False)
            )
        ).scalars().all()
        assert len(messages) >= 1
        assert all(m.status in ("SENT", "FAILED") for m in messages)

        events = db.execute(
            select(CommunicationEvent).where(
                CommunicationEvent.campaign_id == campaign_id
            )
        ).scalars().all()
        assert events
        assert all(e.is_simulated for e in events)
        assert {"EMAIL_SENT"} <= {e.event_type for e in events}

    def test_18_suppressed_and_unconsented_customers_received_nothing(self, scenario, db):
        campaign_id = scenario["state"]["campaign_id"]
        blocked = db.execute(
            select(Customer).where(
                Customer.external_id.in_(
                    ["E2E-NOCONSENT", "E2E-UNVERIFIED", "E2E-SUPPRESS"]
                )
            )
        ).scalars().all()
        for customer in blocked:
            messages = db.execute(
                select(Message).where(
                    Message.campaign_id == campaign_id,
                    Message.customer_id == customer.id,
                    Message.status == "SENT",
                )
            ).scalars().all()
            assert not messages, f"{customer.external_id} should not have been messaged"

    def test_19_ingest_the_returning_order(self, scenario):
        client, key_headers = scenario["client"], scenario["key_headers"]
        response = client.post(
            "/api/v1/orders",
            json=[
                {
                    "external_id": "E2E-ORD-RETURN",
                    "customer_external_id": "E2E-LAPSED",
                    # Full precision: truncating to whole seconds can place the order a
                    # fraction of a second *before* the campaign send event.
                    "ordered_at": datetime.utcnow().isoformat(),
                    "status": "COMPLETED",
                    "total_amount": 175.50,
                }
            ],
            headers=key_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted_rows"] == 1

    def test_20_reactivation_detected_and_lifecycle_updated(self, scenario):
        client, headers = scenario["client"], scenario["headers"]
        customer_id = scenario["state"]["customer_id"]

        detail = client.get(f"/api/v1/customers/{customer_id}", headers=headers).json()
        profile = detail["profile"]

        assert profile["lifecycle_stage"] == "REACTIVATED"
        assert profile["days_since_last_order"] == 0
        assert profile["completed_orders"] == 7
        assert profile["lifetime_revenue"] == pytest.approx(
            scenario["state"]["revenue_before"] + 175.50
        )
        # Returning to purchase must collapse the churn score.
        assert profile["churn_score"] < scenario["state"]["churn_before"]
        assert profile["churn_risk_band"] == "LOW"

        transitions = [
            (h["from_stage"], h["to_stage"]) for h in detail["lifecycle_history"]
        ]
        assert (scenario["state"]["stage_before"], "REACTIVATED") in transitions

    def test_21_order_attributed_to_the_campaign(self, scenario, db):
        campaign_id = scenario["state"]["campaign_id"]
        customer_id = scenario["state"]["customer_id"]

        record = db.execute(
            select(AttributionRecord).where(
                AttributionRecord.customer_id == customer_id
            )
        ).scalar_one()
        assert record.campaign_id == campaign_id
        assert record.model == "LAST_TOUCH"
        assert record.revenue == pytest.approx(175.50)
        assert record.is_reactivation is True
        assert record.hours_since_touch <= record.window_hours

        db.expire_all()
        campaign = db.get(Campaign, campaign_id)
        assert campaign.conversions >= 1
        assert campaign.attributed_revenue == pytest.approx(175.50)

    def test_22_attribution_is_idempotent(self, scenario, db):
        """Re-ingesting the same order must not double-count revenue."""
        client, key_headers = scenario["client"], scenario["key_headers"]
        campaign_id = scenario["state"]["campaign_id"]
        db.expire_all()
        before = db.get(Campaign, campaign_id).attributed_revenue

        client.post(
            "/api/v1/orders",
            json=[
                {
                    "external_id": "E2E-ORD-RETURN",
                    "customer_external_id": "E2E-LAPSED",
                    # Full precision: truncating to whole seconds can place the order a
                    # fraction of a second *before* the campaign send event.
                    "ordered_at": datetime.utcnow().isoformat(),
                    "status": "COMPLETED",
                    "total_amount": 175.50,
                }
            ],
            headers=key_headers,
        )
        db.expire_all()
        assert db.get(Campaign, campaign_id).attributed_revenue == pytest.approx(before)

        records = db.execute(
            select(AttributionRecord).where(
                AttributionRecord.customer_id == scenario["state"]["customer_id"]
            )
        ).scalars().all()
        assert len(records) == 1

    def test_23_analytics_reflect_the_outcome(self, scenario):
        client, headers = scenario["client"], scenario["headers"]

        overview = client.get("/api/v1/analytics/overview", headers=headers).json()
        assert overview["total_customers"] >= len(SCENARIO_CUSTOMERS)
        assert overview["campaign_attributed_revenue"] >= 175.50
        assert overview["total_reactivations"] >= 1
        assert overview["total_revenue"] > 0

        campaigns = client.get("/api/v1/analytics/campaigns", headers=headers).json()
        assert campaigns["totals"]["messages_sent"] >= 1
        assert campaigns["totals"]["conversions"] >= 1
        assert campaigns["totals"]["attributed_revenue"] >= 175.50
        assert campaigns["totals"]["revenue_per_message"] > 0

        churn = client.get("/api/v1/analytics/churn", headers=headers).json()
        assert sum(b["count"] for b in churn["risk_distribution"]) >= 4
        assert churn["total_reactivations"] >= 1

        customers = client.get("/api/v1/analytics/customers", headers=headers).json()
        assert customers["lifecycle_distribution"]
        assert customers["rfm_distribution"]

        cohorts = client.get("/api/v1/analytics/cohorts", headers=headers).json()
        assert "cohorts" in cohorts

    def test_24_data_persists_across_a_new_session(self, scenario, db):
        """State lives in the database, not in process memory."""
        from app.core.database import SessionLocal

        fresh = SessionLocal()
        try:
            customer = fresh.execute(
                select(Customer).where(Customer.external_id == "E2E-LAPSED")
            ).scalar_one()
            assert customer.lifecycle_stage == "REACTIVATED"

            campaign = fresh.get(Campaign, scenario["state"]["campaign_id"])
            assert campaign.status == "COMPLETED"
            assert campaign.attributed_revenue == pytest.approx(175.50)

            record = fresh.execute(
                select(AttributionRecord).where(
                    AttributionRecord.customer_id == customer.id
                )
            ).scalar_one()
            assert record.is_reactivation is True
        finally:
            fresh.close()
