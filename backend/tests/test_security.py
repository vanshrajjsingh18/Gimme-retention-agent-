"""Security properties, asserted rather than assumed.

These cover the ways the product could be made to do something it must never
do: send without approval, send to someone who withdrew consent, leak a
secret through an API response, or let a non-admin change enforcement.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.campaigns.service import CampaignError, run_campaign
from app.core.enums import CampaignStatus, Channel, RecipientStatus
from app.core.security import api_keys_match, hash_api_key, hash_password, verify_password
from app.models.entities import ApiKey, Campaign, CampaignRecipient, Customer, Message, User

BACKEND = pathlib.Path(__file__).resolve().parents[1]

AUTH_DEPENDENCIES = {"get_current_user", "require_admin", "require_write", "get_api_key"}

# Routes that are public by design, with the reason they are safe.
INTENTIONALLY_PUBLIC = {
    ("main", "health"): "liveness probe exposing no data",
    ("auth", "login"): "the endpoint that establishes a session",
    (
        "integrations",
        "receive_webhook",
    ): "providers post from their own infrastructure; only records events for known messages",
}


# ==========================================================================
# Route authentication coverage
# ==========================================================================
def _route_functions():
    """Yield (module, function, auth dependency names) for every API route."""
    for path in sorted((BACKEND / "app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"get", "post", "patch", "put", "delete"}
                for d in node.decorator_list
            )
            if not is_route:
                continue
            deps = set()
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
            for default in defaults:
                if (
                    isinstance(default, ast.Call)
                    and getattr(default.func, "id", "") == "Depends"
                    and default.args
                    and isinstance(default.args[0], ast.Name)
                ):
                    deps.add(default.args[0].id)
            yield path.stem, node.name, deps


def test_every_route_requires_authentication():
    """A new endpoint added without an auth dependency fails this test."""
    unprotected = [
        f"{module}.{name}"
        for module, name, deps in _route_functions()
        if not (deps & AUTH_DEPENDENCIES) and (module, name) not in INTENTIONALLY_PUBLIC
    ]
    assert unprotected == [], f"routes missing authentication: {unprotected}"


def test_route_coverage_is_meaningful():
    """Guard against the check above silently finding nothing to inspect."""
    assert len(list(_route_functions())) > 50


# ==========================================================================
# Credential handling
# ==========================================================================
def test_passwords_are_hashed_not_stored():
    hashed = hash_password("GimmeAdmin123!")
    assert hashed != "GimmeAdmin123!"
    assert verify_password("GimmeAdmin123!", hashed)
    assert not verify_password("wrong", hashed)


def test_password_hashes_are_salted():
    """Two users with the same password must not share a hash."""
    assert hash_password("same-password") != hash_password("same-password")


def test_api_keys_are_stored_hashed(client, auth_headers, db):
    created = client.post(
        "/api/v1/api-keys", json={"name": "hash-check"}, headers=auth_headers
    ).json()
    full_key = created["api_key"]

    row = db.execute(select(ApiKey).where(ApiKey.id == created["id"])).scalar_one()
    assert row.key_hash != full_key
    assert full_key not in row.key_hash
    assert api_keys_match(full_key, row.key_hash)
    assert not api_keys_match("gimme_sk_wrong", row.key_hash)


def test_api_key_hash_is_bound_to_the_secret_key():
    """Rotating SECRET_KEY must invalidate existing keys, not silently accept them."""
    from app.core.config import settings

    original = settings.SECRET_KEY
    first = hash_api_key("gimme_sk_example")
    try:
        settings.SECRET_KEY = "a-different-secret"
        assert hash_api_key("gimme_sk_example") != first
    finally:
        settings.SECRET_KEY = original


def test_user_response_never_includes_the_password_hash(client, auth_headers):
    body = client.get("/api/v1/auth/me", headers=auth_headers).text
    assert "hashed_password" not in body
    assert "$2b$" not in body  # a bcrypt hash prefix


def test_integration_credentials_are_masked_in_responses(client, auth_headers, db):
    from app.models.entities import Integration

    integration = db.execute(select(Integration)).scalars().first()
    integration.credentials = {"client_secret": "super-secret-value-1234"}
    db.commit()

    body = client.get("/api/v1/integrations", headers=auth_headers).text
    assert "super-secret-value-1234" not in body
    # Only a presence flag and a short suffix hint are exposed.
    assert "1234" in body
    assert '"configured":true' in body


def test_integration_audit_log_records_key_names_not_values(client, auth_headers, db):
    from app.models.entities import AuditLog, Integration

    integration = db.execute(select(Integration)).scalars().first()
    client.patch(
        f"/api/v1/integrations/{integration.id}",
        json={"mode": "mock", "credentials": {"auth_token": "leak-me-if-you-can"}},
        headers=auth_headers,
    )
    entries = db.execute(
        select(AuditLog).where(AuditLog.action == "INTEGRATION_UPDATED")
    ).scalars().all()
    assert entries
    for entry in entries:
        assert "leak-me-if-you-can" not in str(entry.detail)
    assert any("auth_token" in str(e.detail) for e in entries)


def test_ingestion_request_log_does_not_store_the_body(client, api_key, db):
    from app.models.entities import ApiRequestLog

    client.post(
        "/api/v1/customers",
        json=[
            {
                "external_id": "SEC-PII-1",
                "email": "private.person@example.test",
                "first_name": "Private",
                "age_verified": True,
            }
        ],
        headers={"X-API-Key": api_key},
    )
    logs = db.execute(select(ApiRequestLog)).scalars().all()
    assert logs
    for log in logs:
        assert "private.person@example.test" not in str(log.path)
        assert "private.person" not in str(log.error_message or "")


# ==========================================================================
# Authorization
# ==========================================================================
def test_viewer_role_cannot_mutate(client, db, auth_headers):
    """A read-only account may read but not change anything."""
    from app.core.security import create_access_token

    viewer = User(
        email="viewer@gimmedelivery.co.nz",
        full_name="Read Only",
        hashed_password=hash_password("ViewerPass123!"),
        role="VIEWER",
        is_active=True,
    )
    db.add(viewer)
    db.commit()

    headers = {"Authorization": f"Bearer {create_access_token(viewer.email, {'role': 'VIEWER'})}"}

    # Reading is allowed.
    assert client.get("/api/v1/customers", headers=headers).status_code == 200

    # Writing is not.
    assert (
        client.post(
            "/api/v1/segments",
            json={"name": "viewer-attempt", "rule_definition": {}},
            headers=headers,
        ).status_code
        == 403
    )


def test_non_admin_cannot_change_compliance_rules(client, db):
    """Compliance enforcement must not be editable by a read-only account."""
    from app.core.security import create_access_token
    from app.models.entities import ComplianceRule

    viewer = db.execute(select(User).where(User.role == "VIEWER")).scalars().first()
    if viewer is None:
        viewer = User(
            email="viewer2@gimmedelivery.co.nz",
            full_name="Read Only",
            hashed_password=hash_password("ViewerPass123!"),
            role="VIEWER",
            is_active=True,
        )
        db.add(viewer)
        db.commit()

    headers = {"Authorization": f"Bearer {create_access_token(viewer.email, {'role': 'VIEWER'})}"}
    rule = db.execute(select(ComplianceRule)).scalars().first()
    response = client.patch(
        f"/api/v1/compliance/rules/{rule.id}", json={"enabled": False}, headers=headers
    )
    assert response.status_code == 403


def test_inactive_user_is_rejected(client, db):
    from app.core.security import create_access_token

    user = User(
        email="disabled@gimmedelivery.co.nz",
        full_name="Disabled",
        hashed_password=hash_password("Whatever123!"),
        role="ADMIN",
        is_active=False,
    )
    db.add(user)
    db.commit()

    headers = {"Authorization": f"Bearer {create_access_token(user.email, {'role': 'ADMIN'})}"}
    assert client.get("/api/v1/customers", headers=headers).status_code == 401


def test_token_for_a_deleted_user_is_rejected(client):
    from app.core.security import create_access_token

    headers = {
        "Authorization": f"Bearer {create_access_token('ghost@nowhere.test', {'role': 'ADMIN'})}"
    }
    assert client.get("/api/v1/customers", headers=headers).status_code == 401


def test_tampered_token_is_rejected(client, auth_headers):
    token = auth_headers["Authorization"].split(" ", 1)[1]
    # Flip a character in the signature.
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert (
        client.get("/api/v1/customers", headers={"Authorization": f"Bearer {tampered}"}).status_code
        == 401
    )


def test_expired_token_is_rejected(client):
    from datetime import timezone

    from jose import jwt

    from app.core.config import settings

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired = jwt.encode(
        {"sub": "admin@gimmedelivery.co.nz", "exp": int(past.timestamp())},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    assert (
        client.get("/api/v1/customers", headers={"Authorization": f"Bearer {expired}"}).status_code
        == 401
    )


# ==========================================================================
# The rules that must not be bypassable
# ==========================================================================
@pytest.fixture()
def sendable_campaign(db, bootstrapped):
    """A campaign with one eligible recipient, approved and compliance-clean."""
    from app.models.base import utcnow
    from app.services.brand import get_brand_settings

    brand = get_brand_settings(db)
    customer = Customer(
        external_id=f"SEC-{utcnow().timestamp()}",
        email="sec@example.test",
        first_name="Sec",
        last_name="Test",
        age_verified=True,
        marketing_consent=True,
        email_consent=True,
        signup_date=utcnow() - timedelta(days=200),
    )
    db.add(customer)
    db.flush()

    campaign = Campaign(
        name=f"Security check {utcnow().timestamp()}",
        objective="RETENTION",
        channel=Channel.EMAIL.value,
        status=CampaignStatus.APPROVED.value,
        subject="A note",
        body=(
            "Hi there, just checking in.\n\n"
            f"{brand.responsible_drinking_statement}\n{brand.age_restriction_statement}"
        ),
        compliance_result={"passed": True, "blocking_count": 0, "findings": []},
    )
    db.add(campaign)
    db.flush()
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()
    return campaign, customer


def test_campaign_cannot_send_without_approval(db, sendable_campaign):
    campaign, _ = sendable_campaign
    campaign.status = CampaignStatus.DRAFT.value
    db.commit()

    with pytest.raises(CampaignError, match="approved"):
        run_campaign(db, campaign, generate_per_customer=False, simulate_engagement=False)


def test_campaign_cannot_send_with_failing_compliance(db, sendable_campaign):
    campaign, _ = sendable_campaign
    campaign.compliance_result = {
        "passed": False,
        "blocking_count": 1,
        "findings": [{"code": "HEALTH_CLAIM", "blocks_send": True}],
    }
    db.commit()

    with pytest.raises(CampaignError, match="compliance"):
        run_campaign(db, campaign, generate_per_customer=False, simulate_engagement=False)


def test_consent_withdrawn_after_approval_is_still_honoured(db, sendable_campaign):
    """The audience snapshot is a record, not a licence to send."""
    campaign, customer = sendable_campaign
    customer.marketing_consent = False
    db.commit()

    stats = run_campaign(db, campaign, generate_per_customer=False, simulate_engagement=False)
    assert stats["sent"] == 0
    assert stats["skipped_ineligible"] == 1

    recipient = db.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ).scalar_one()
    assert recipient.status == RecipientStatus.EXCLUDED_NO_CONSENT.value


def test_suppression_after_approval_is_still_honoured(db, sendable_campaign):
    campaign, customer = sendable_campaign
    customer.is_suppressed = True
    db.commit()

    stats = run_campaign(db, campaign, generate_per_customer=False, simulate_engagement=False)
    assert stats["sent"] == 0
    recipient = db.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ).scalar_one()
    assert recipient.status == RecipientStatus.EXCLUDED_SUPPRESSED.value


def test_age_verification_revoked_after_approval_is_still_honoured(db, sendable_campaign):
    campaign, customer = sendable_campaign
    customer.age_verified = False
    db.commit()

    stats = run_campaign(db, campaign, generate_per_customer=False, simulate_engagement=False)
    assert stats["sent"] == 0
    recipient = db.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ).scalar_one()
    assert recipient.status == RecipientStatus.EXCLUDED_AGE.value


def test_message_failing_validation_is_never_sent(db, sendable_campaign, monkeypatch):
    """A generated message that fails grounding validation must not go out."""
    from app.services import messaging

    real_generate = messaging.generate_message

    def poisoned(db_session, customer, **kwargs):
        message = real_generate(db_session, customer, **kwargs)
        # Simulate a model that invented an offer despite the prompt.
        message.body = "Take 60% off with code FAKE99! Only 1 left in stock."
        db_session.commit()
        messaging.revalidate_message(db_session, message)
        return message

    monkeypatch.setattr("app.campaigns.service.generate_message", poisoned)

    campaign, _ = sendable_campaign
    stats = run_campaign(db, campaign, generate_per_customer=True, simulate_engagement=False)

    assert stats["sent"] == 0
    assert stats["generation_failed"] == 1

    sent = db.execute(
        select(Message).where(Message.campaign_id == campaign.id, Message.status == "SENT")
    ).scalars().all()
    assert sent == []


def test_message_cannot_be_approved_while_validation_fails(client, auth_headers, db, seeded):
    customer = db.execute(select(Customer)).scalars().first()
    message = client.post(
        "/api/v1/messages/generate",
        json={"customer_id": customer.id, "channel": "EMAIL", "objective": "RETENTION"},
        headers=auth_headers,
    ).json()

    client.patch(
        f"/api/v1/messages/{message['id']}",
        json={"body": "Grab 70% off with code BOGUS42 — good for your heart!"},
        headers=auth_headers,
    )
    response = client.post(f"/api/v1/messages/{message['id']}/approve", headers=auth_headers)
    assert response.status_code == 400
    assert "cannot be approved" in response.json()["detail"]


def test_upload_larger_than_the_limit_is_rejected(client, auth_headers):
    import io

    from app.core.config import settings

    oversized = b"external_id,email\n" + b"x" * (settings.MAX_UPLOAD_BYTES + 1)
    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "customers"},
        files={"file": ("big.csv", io.BytesIO(oversized), "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 413


def test_unknown_entity_type_cannot_reach_the_ingestor(client, auth_headers):
    import io

    response = client.post(
        "/api/v1/uploads",
        data={"entity_type": "../../etc/passwd"},
        files={"file": ("x.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_webhook_ignores_events_for_unknown_messages(client):
    """The unauthenticated webhook must not create records from arbitrary input."""
    response = client.post(
        "/api/v1/webhooks/whatsapp",
        json={"event": "read", "message_id": "not-a-message-we-sent"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["events_recorded"] == 0
    assert body["events_ignored_unknown_message"] >= 1


def test_webhook_rejects_an_unknown_provider(client):
    assert client.post("/api/v1/webhooks/pigeon", json={}).status_code == 404


# ==========================================================================
# Configuration hygiene
# ==========================================================================
def test_cors_is_not_a_wildcard():
    from app.core.config import settings

    assert "*" not in settings.cors_origin_list
    assert settings.cors_origin_list


def test_no_secrets_are_committed():
    """The .env file and the database must never be tracked by git."""
    gitignore = (BACKEND.parent / ".gitignore").read_text()
    for pattern in (".env", "*.db", "data/"):
        assert pattern in gitignore, f"{pattern} is not gitignored"
