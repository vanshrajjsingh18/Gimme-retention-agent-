"""The Smart Reorder dashboard endpoints."""
from __future__ import annotations

from datetime import timedelta

from app.models.base import utcnow


def test_overview_is_honest_about_an_empty_engine(client, auth_headers):
    body = client.get("/api/v1/smart-reorder/overview", headers=auth_headers).json()
    assert "eligible_customers" in body
    assert "predicted_next_24h" in body
    assert body["accuracy"]["total_resolved"] >= 0


def test_upcoming_returns_a_window(client, auth_headers):
    response = client.get("/api/v1/smart-reorder/upcoming?hours=24", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["horizon_hours"] == 24
    assert body["total"] == len(body["customers"])
    assert body["due_now"] + body["upcoming"] == body["total"]


def test_upcoming_rejects_an_absurd_window(client, auth_headers):
    """A year-long 'upcoming' view is a different question."""
    assert client.get(
        "/api/v1/smart-reorder/upcoming?hours=99999", headers=auth_headers
    ).status_code == 422


def test_accuracy_reports_zero_resolved_rather_than_a_fake_score(client, auth_headers):
    body = client.get("/api/v1/smart-reorder/accuracy?days=30", headers=auth_headers).json()
    assert body["window_days"] == 30
    # A young campaign has nothing to report, and must say so rather than
    # presenting 0% as a tested-and-failed result.
    assert body["total_resolved"] == 0
    assert body["accuracy_pct"] == 0.0


def test_the_endpoints_require_authentication(client):
    for path in (
        "/api/v1/smart-reorder/overview",
        "/api/v1/smart-reorder/upcoming",
        "/api/v1/smart-reorder/accuracy",
    ):
        assert client.get(path).status_code == 401, path
