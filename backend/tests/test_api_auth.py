"""Authentication, authorization and API key tests."""
from __future__ import annotations


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_succeeds_with_correct_credentials(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gimmedelivery.co.nz", "password": "GimmeAdmin123!"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["user"]["email"] == "admin@gimmedelivery.co.nz"
    assert body["user"]["role"] == "ADMIN"
    assert "hashed_password" not in body["user"]


def test_login_rejects_wrong_password(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gimmedelivery.co.nz", "password": "nope"},
    )
    assert response.status_code == 401


def test_login_does_not_reveal_whether_an_account_exists(client):
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gimmedelivery.co.nz", "password": "nope"},
    )
    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["detail"] == wrong_password.json()["detail"]


def test_protected_endpoints_require_a_token(client):
    for path in (
        "/api/v1/customers",
        "/api/v1/segments",
        "/api/v1/campaigns",
        "/api/v1/analytics/overview",
        "/api/v1/brand",
    ):
        assert client.get(path).status_code == 401, path


def test_invalid_token_rejected(client):
    response = client.get(
        "/api/v1/customers", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_me_returns_the_authenticated_user(client, auth_headers):
    response = client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["email"] == "admin@gimmedelivery.co.nz"


def test_api_key_returned_once_and_never_again(client, auth_headers):
    created = client.post(
        "/api/v1/api-keys", json={"name": "one-time"}, headers=auth_headers
    )
    assert created.status_code == 201
    full_key = created.json()["api_key"]
    assert full_key.startswith("gimme_sk_")

    listed = client.get("/api/v1/api-keys", headers=auth_headers)
    assert listed.status_code == 200
    for entry in listed.json():
        assert "api_key" not in entry
        assert full_key not in str(entry)


def test_ingestion_requires_an_api_key(client):
    response = client.post("/api/v1/customers", json=[])
    assert response.status_code == 401


def test_ingestion_rejects_an_unknown_api_key(client):
    response = client.post(
        "/api/v1/customers", json=[], headers={"X-API-Key": "gimme_sk_bogus"}
    )
    assert response.status_code == 401


def test_revoked_api_key_stops_working(client, auth_headers):
    created = client.post(
        "/api/v1/api-keys", json={"name": "to-revoke"}, headers=auth_headers
    ).json()
    key = created["api_key"]

    ok = client.post("/api/v1/customers", json=[], headers={"X-API-Key": key})
    assert ok.status_code == 200

    revoked = client.delete(f"/api/v1/api-keys/{created['id']}", headers=auth_headers)
    assert revoked.status_code == 200
    assert revoked.json()["is_active"] is False

    after = client.post("/api/v1/customers", json=[], headers={"X-API-Key": key})
    assert after.status_code == 401
    assert "revoked" in after.json()["detail"].lower()


def test_dashboard_token_is_not_accepted_as_an_api_key(client, auth_headers):
    token = auth_headers["Authorization"].split(" ", 1)[1]
    response = client.post("/api/v1/customers", json=[], headers={"X-API-Key": token})
    assert response.status_code == 401
