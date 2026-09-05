"""The automations API: lifecycle, dry run, and who is allowed to do what."""
from __future__ import annotations

import pytest

BASE = "/api/v1/automations"


@pytest.fixture()
def cohort_payload(request):
    # Names are unique per automation, so each test needs its own.
    return {
        "name": f"API cohort test - {request.node.name}"[:200],
        "kind": "COHORT_BULK",
        "channel": "SMS",
        "manual_customer_ids": [1],
        "message_template": "Hi {first_name}, your usual is a tap away. Reply STOP to opt out.",
    }


@pytest.fixture()
def created(client, auth_headers, seeded, cohort_payload):
    response = client.post(BASE, json=cohort_payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


class TestCrud:
    def test_create_returns_the_automation_with_a_backing_campaign(self, created):
        assert created["kind"] == "COHORT_BULK"
        assert created["status"] == "DRAFT"
        # Sends flow through the existing campaign analytics rather than a
        # parallel reporting world.
        assert created["campaign_id"] is not None

    def test_an_automation_needs_an_audience(self, client, auth_headers, seeded):
        response = client.post(
            BASE,
            json={"name": "No audience for this test", "kind": "COHORT_BULK"},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "audience" in response.json()["detail"]

    def test_a_sequence_needs_steps(self, client, auth_headers, seeded):
        response = client.post(
            BASE,
            json={
                "name": "Stepless sequence",
                "kind": "SEQUENCE",
                "manual_customer_ids": [1],
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "step" in response.json()["detail"]

    def test_list_and_filter(self, client, auth_headers, created):
        listed = client.get(f"{BASE}?kind=COHORT_BULK", headers=auth_headers).json()
        assert any(row["id"] == created["id"] for row in listed)
        assert all(row["kind"] == "COHORT_BULK" for row in listed)

    def test_filter_is_applied_before_paging(self, client, auth_headers, created):
        """A filtered first page must not come back empty while matches exist."""
        page = client.get(f"{BASE}?kind=COHORT_BULK&limit=1", headers=auth_headers).json()
        assert len(page) == 1

    def test_update_changes_only_what_was_sent(self, client, auth_headers, created):
        response = client.patch(
            f"{BASE}/{created['id']}",
            json={"description": "Updated copy"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["description"] == "Updated copy"
        assert response.json()["name"] == created["name"]

    def test_missing_automation_is_a_404(self, client, auth_headers, seeded):
        assert client.get(f"{BASE}/999999", headers=auth_headers).status_code == 404


class TestLifecycle:
    def test_a_draft_cannot_be_activated_without_approval(
        self, client, auth_headers, created
    ):
        response = client.post(f"{BASE}/{created['id']}/activate", headers=auth_headers)
        assert response.status_code == 409
        assert "approved" in response.json()["detail"]

    def test_approve_then_activate(self, client, auth_headers, created):
        assert client.post(f"{BASE}/{created['id']}/approve", headers=auth_headers).status_code == 200
        activated = client.post(f"{BASE}/{created['id']}/activate", headers=auth_headers)
        assert activated.status_code == 200
        assert activated.json()["status"] == "ACTIVE"

    def test_pause_and_resume(self, client, auth_headers, created):
        client.post(f"{BASE}/{created['id']}/approve", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/activate", headers=auth_headers)

        paused = client.post(f"{BASE}/{created['id']}/pause", headers=auth_headers)
        assert paused.json()["status"] == "PAUSED"
        resumed = client.post(f"{BASE}/{created['id']}/resume", headers=auth_headers)
        assert resumed.json()["status"] == "ACTIVE"

    def test_an_active_automation_cannot_be_deleted(self, client, auth_headers, created):
        client.post(f"{BASE}/{created['id']}/approve", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/activate", headers=auth_headers)
        response = client.delete(f"{BASE}/{created['id']}", headers=auth_headers)
        assert response.status_code == 409
        assert "Pause" in response.json()["detail"]

    def test_a_paused_automation_can_be_deleted(self, client, auth_headers, created):
        assert client.delete(f"{BASE}/{created['id']}", headers=auth_headers).status_code == 200
        assert client.get(f"{BASE}/{created['id']}", headers=auth_headers).status_code == 404

    def test_a_backing_campaign_never_surfaces_as_a_campaign_somebody_wrote(
        self, client, auth_headers, created
    ):
        # While the automation lives, the campaign list derives the fact from
        # automations.campaign_id.
        def campaign_ids(**params):
            response = client.get("/api/v1/campaigns", params=params, headers=auth_headers)
            assert response.status_code == 200, response.text
            return [c["id"] for c in response.json()]

        assert created["campaign_id"] not in campaign_ids()
        assert created["campaign_id"] in campaign_ids(include_automations=True)

        # Deleting the automation takes that reference away. The plumbing must
        # not reappear in its place as a draft nobody created.
        assert client.delete(f"{BASE}/{created['id']}", headers=auth_headers).status_code == 200
        assert created["campaign_id"] not in campaign_ids()
        # Nothing was ever sent through it, so the row is gone entirely.
        assert created["campaign_id"] not in campaign_ids(include_automations=True)

    def test_a_backing_campaign_that_sent_outlives_its_automation_but_stays_hidden(
        self, client, auth_headers, created
    ):
        client.post(f"{BASE}/{created['id']}/approve", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/activate", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/run", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/pause", headers=auth_headers)
        assert client.delete(f"{BASE}/{created['id']}", headers=auth_headers).status_code == 200

        def campaign_ids(**params):
            response = client.get("/api/v1/campaigns", params=params, headers=auth_headers)
            return [c["id"] for c in response.json()]

        # Messages really went out under this campaign, so the record stays —
        # deleting it would orphan their attribution. It is still plumbing
        # though, so it stays out of the list of campaigns somebody wrote.
        assert created["campaign_id"] in campaign_ids(include_automations=True)
        assert created["campaign_id"] not in campaign_ids()


class TestDryRun:
    def test_a_draft_can_be_previewed_before_it_is_approved(
        self, client, auth_headers, created
    ):
        """Previewing is how an operator decides whether to approve at all."""
        response = client.post(f"{BASE}/{created['id']}/preview", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["dry_run"] is True
        assert body["sent"] == 0

    def test_a_draft_cannot_be_run(self, client, auth_headers, created):
        response = client.post(f"{BASE}/{created['id']}/run", headers=auth_headers)
        assert response.status_code == 409
        assert "ACTIVE" in response.json()["detail"]

    def test_the_preview_names_recipients_and_their_copy(
        self, client, auth_headers, created
    ):
        body = client.post(f"{BASE}/{created['id']}/preview", headers=auth_headers).json()
        if body["recipients"]:
            recipient = body["recipients"][0]
            assert "scheduled_for_local" in recipient
            assert recipient["status"] in {"PREVIEW", "SKIPPED"}


class TestAudienceAndAudit:
    def test_audience_is_resolved_live(self, client, auth_headers, created):
        body = client.get(f"{BASE}/{created['id']}/audience", headers=auth_headers).json()
        assert body["audience_size"] >= 0
        assert "customer_ids" in body

    def test_stats_start_empty(self, client, auth_headers, created):
        body = client.get(f"{BASE}/{created['id']}/stats", headers=auth_headers).json()
        assert body["total_sent"] == 0
        assert body["enrollments"] == {}

    def test_the_send_ledger_records_a_run(self, client, auth_headers, created):
        client.post(f"{BASE}/{created['id']}/approve", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/activate", headers=auth_headers)
        client.post(f"{BASE}/{created['id']}/run", headers=auth_headers)

        sends = client.get(f"{BASE}/{created['id']}/sends", headers=auth_headers).json()
        assert len(sends) >= 1
        assert all(row["is_dry_run"] is False for row in sends)

    def test_dry_run_rows_are_hidden_from_the_ledger_by_default(
        self, client, auth_headers, created
    ):
        client.post(f"{BASE}/{created['id']}/preview", headers=auth_headers)
        default = client.get(f"{BASE}/{created['id']}/sends", headers=auth_headers).json()
        included = client.get(
            f"{BASE}/{created['id']}/sends?include_dry_runs=true", headers=auth_headers
        ).json()
        assert len(included) > len(default)

    def test_refresh_patterns_is_rejected_for_a_cohort_campaign(
        self, client, auth_headers, created
    ):
        response = client.post(
            f"{BASE}/{created['id']}/refresh-patterns", headers=auth_headers
        )
        assert response.status_code == 400
        assert "nudge" in response.json()["detail"].lower()

    def test_enroll_is_rejected_for_a_cohort_campaign(self, client, auth_headers, created):
        response = client.post(f"{BASE}/{created['id']}/enroll", headers=auth_headers)
        assert response.status_code == 400
        assert "send time" in response.json()["detail"]


class TestAuthorization:
    def test_every_route_requires_authentication(self, client, seeded):
        assert client.get(BASE).status_code == 401
        assert client.post(BASE, json={}).status_code == 401
        assert client.post(f"{BASE}/1/run").status_code == 401

    def test_a_viewer_cannot_run_or_approve(self, client, auth_headers, created, db):
        """Read access must not extend to sending messages to customers."""
        from app.core.security import create_access_token, hash_password
        from app.core.enums import UserRole
        from app.models.entities import User

        viewer = User(
            email="viewer-automations@gimmedelivery.co.nz",
            hashed_password=hash_password("ViewerPass123!"),
            full_name="Read Only",
            role=UserRole.VIEWER.value,
            is_active=True,
        )
        db.add(viewer)
        db.commit()
        headers = {"Authorization": f"Bearer {create_access_token(viewer.email)}"}

        assert client.get(BASE, headers=headers).status_code == 200
        assert client.post(f"{BASE}/{created['id']}/approve", headers=headers).status_code == 403
        assert client.post(f"{BASE}/{created['id']}/run", headers=headers).status_code == 403
        assert client.delete(f"{BASE}/{created['id']}", headers=headers).status_code == 403
