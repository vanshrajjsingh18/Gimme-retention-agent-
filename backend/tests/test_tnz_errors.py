"""What the TNZ adapter tells you when TNZ says no.

A provider error is only useful if it survives the trip to the operator. TNZ
answers a malformed or incomplete send with 400 and names the offending field
in the body; reporting only the status code turned a one-line fix into
guesswork, so these pin the body to the message.
"""
from __future__ import annotations

import httpx
import pytest

from app.integrations.tnz import MAX_PROVIDER_ERROR, TnzSmsAdapter, describe_http_error


def _response(status: int, *, json=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://api.tnz.co.nz/api/v2.04/send/sms")
    if json is not None:
        return httpx.Response(status, json=json, request=request)
    return httpx.Response(status, text=text or "", request=request)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"Error": "Missing or empty sender"}, "Missing or empty sender"),
        ({"message": "Invalid recipient"}, "Invalid recipient"),
        ({"ErrorMessage": "Sender not registered"}, "Sender not registered"),
    ],
)
def test_error_body_reaches_the_operator(payload, expected):
    detail = describe_http_error(_response(400, json=payload))
    assert "400" in detail
    assert expected in detail, f"TNZ's reason was dropped: {detail!r}"


def test_unrecognised_json_shape_is_still_reported():
    """An unfamiliar key is no reason to say nothing."""
    detail = describe_http_error(_response(400, json={"Weird": "Sender rejected"}))
    assert "Sender rejected" in detail


def test_plain_text_body_is_reported():
    detail = describe_http_error(_response(400, text="Missing or empty sender"))
    assert "Missing or empty sender" in detail


def test_empty_body_degrades_to_the_status_code():
    assert describe_http_error(_response(400, text="")) == "TNZ returned HTTP 400."


def test_a_huge_html_error_page_is_truncated():
    """An error page must not flood the message log."""
    detail = describe_http_error(_response(500, text="<html>" + "x" * 5000 + "</html>"))
    assert len(detail) < MAX_PROVIDER_ERROR + 60
    assert detail.endswith("…")


def test_connection_test_reports_what_it_can_actually_establish(monkeypatch):
    """The probe asks after a message id that was never issued.

    An earlier version of this test asserted that a 400 here was an error. That
    was right while the check hit a general-purpose endpoint, but the check now
    queries the status of a deliberately made-up message id, and 400 or 404 is
    the expected answer to that — reaching it proves the token was read and
    accepted, which is the only thing a connection test can honestly establish.
    Credential rejection and provider outages are covered below, and they are
    what this test was really guarding.

    What it must never claim is that sending works. TNZ accepted our token for
    months while refusing every send we made, so the wording matters.
    """
    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k):
            return _response(404, json={"Result": "Failed"})

    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Client)
    adapter = TnzSmsAdapter(credentials={"auth_token": "t", "sender": "GIMME"})
    result = adapter.validate_credentials()

    assert result.status == "OK"
    assert "Authenticated" in result.message
    for overclaim in ("sent", "deliver", "message was"):
        assert overclaim not in result.message.lower()


def test_send_failure_carries_the_reason(monkeypatch):
    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k):
            return _response(400, json={"Error": "Missing or empty sender"})

    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Client)
    adapter = TnzSmsAdapter(credentials={"auth_token": "t", "sender": "GIMME"})
    result = adapter.send_message(to="+642905297394", subject="", body="Test")

    assert result.success is False
    assert "Missing or empty sender" in (result.error or "")


# ==========================================================================
# The request TNZ actually accepts
# ==========================================================================
class _Capturing:
    """Captures the POST body instead of sending it."""

    sent: dict | None = None

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, url, json=None, headers=None, **k):
        type(self).sent = {"url": url, "json": json, "headers": headers}
        return _response(200, json={"Result": "Success", "MessageID": "abc123"})


def test_payload_matches_tnz_v204_shape(monkeypatch):
    """Everything lives inside MessageData.

    Authenticated with an auth token, TNZ's own client sends exactly
    {"MessageData": {...}} and nothing beside it. Spreading Message,
    Destinations and FromNumber across the top level — and passing MessageData
    as a bare string — got "Failed to process your API request. Check your
    syntax." with no indication of which field was at fault.
    """
    _Capturing.sent = None
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Capturing)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": "GIMME"})
    result = adapter.send_message(
        to="+642905297394", subject="", body="Hi. Reply STOP to opt out.",
        metadata={"reference": "ref-1"},
    )

    assert result.success is True
    assert result.provider_message_id == "abc123"

    body = _Capturing.sent["json"]
    assert set(body) == {"MessageData"}, f"stray top-level fields: {sorted(body)}"

    data = body["MessageData"]
    assert data["Message"] == "Hi. Reply STOP to opt out."
    assert data["Destinations"] == [{"Recipient": "+642905297394"}]
    assert data["FromNumber"] == "GIMME"
    assert data["Reference"] == "ref-1"

    # The fields that never existed in this API.
    for dead in ("MessageType", "SendMode"):
        assert dead not in body and dead not in data

    assert _Capturing.sent["url"].endswith("/api/v2.04/send/sms")
    assert _Capturing.sent["headers"]["Authorization"] == "Basic tok"


def test_optional_fields_are_omitted_not_blank(monkeypatch):
    """An empty string is a value a provider may reject."""
    _Capturing.sent = None
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Capturing)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": "GIMME"})
    adapter.send_message(to="+642905297394", subject="", body="Hi", metadata=None)

    data = _Capturing.sent["json"]["MessageData"]
    assert "Reference" not in data, "an empty reference was sent as a blank string"


def test_a_blank_sender_is_refused_before_it_reaches_tnz(monkeypatch):
    """Sender is required, so the adapter stops rather than sending without it."""
    _Capturing.sent = None
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Capturing)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": ""})
    result = adapter.send_message(to="+642905297394", subject="", body="Hi")

    assert result.success is False
    assert "sender" in (result.error or "").lower()
    assert _Capturing.sent is None, "a request went out with no sender"


def test_the_real_error_reads_as_a_sentence():
    """The body TNZ actually returned, reported as prose rather than a dict.

    ErrorMessage is a list, so a string-only check skipped it and fell through
    to dumping the whole object — the one readable sentence arrived wrapped in
    Python dict syntax.
    """
    detail = describe_http_error(
        _response(400, json={
            "Result": "Failed",
            "ErrorMessage": ["Failed to process your API request. Check your syntax."],
        })
    )
    assert detail == (
        "TNZ returned HTTP 400: Failed to process your API request. Check your syntax."
    )
    assert "{" not in detail and "'" not in detail


def test_result_failed_on_a_200_is_not_a_send(monkeypatch):
    """A 2xx means TNZ parsed the request, not that it accepted the message."""
    class _Refusing(_Capturing):
        def post(self, url, json=None, headers=None, **k):
            return _response(200, json={
                "Result": "Failed",
                "ErrorMessage": ["Invalid recipient"],
            })

    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Refusing)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": "GIMME"})
    result = adapter.send_message(to="+64000", subject="", body="Hi")

    assert result.success is False, "a refusal was recorded as a delivered send"
    assert "Invalid recipient" in (result.error or "")


# ==========================================================================
# The status endpoint, which had never existed
# ==========================================================================
class _RecordingGet:
    url: str | None = None
    status: int = 404
    body: dict | None = None

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, headers=None, params=None, **k):
        type(self).url = url
        return _response(type(self).status, json=type(self).body or {"Result": "Failed"})


def test_connection_probe_uses_the_documented_status_path(monkeypatch):
    """/get/sms/status was invented and 404s.

    It answered 404 for every call, and the old check — which failed only on
    401/403 — reported that as a healthy connection. So the one control that
    exists to say "TNZ is reachable and your token works" was passing on the
    strength of an endpoint that was never there.
    """
    _RecordingGet.url = None
    _RecordingGet.status = 404
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _RecordingGet)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": "GIMME"})
    result = adapter.validate_credentials()

    assert "/api/v2.04/get/status/" in _RecordingGet.url
    assert "/get/sms/status" not in _RecordingGet.url

    # A 404 for an id that was never issued still proves the token was read.
    assert result.status == "OK", result.message
    assert "Authenticated" in result.message


def test_connection_probe_still_fails_on_bad_credentials(monkeypatch):
    _RecordingGet.status = 401
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _RecordingGet)
    adapter = TnzSmsAdapter(credentials={"auth_token": "bad", "sender": "GIMME"})
    result = adapter.validate_credentials()

    assert result.status == "ERROR"
    assert "rejected" in result.message.lower()


def test_connection_probe_fails_on_a_provider_outage(monkeypatch):
    """A 5xx is TNZ being broken, not us being connected."""
    _RecordingGet.status = 503
    _RecordingGet.body = {"ErrorMessage": ["Service unavailable"]}
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _RecordingGet)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": "GIMME"})
    result = adapter.validate_credentials()
    _RecordingGet.body = None

    assert result.status == "ERROR"
    assert "Service unavailable" in result.message


def test_delivery_status_puts_the_id_in_the_path(monkeypatch):
    """The message id is a path segment, not a query parameter."""
    _RecordingGet.url = None
    _RecordingGet.status = 200
    _RecordingGet.body = {"Result": "Success"}
    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _RecordingGet)
    adapter = TnzSmsAdapter(credentials={"auth_token": "tok", "sender": "GIMME"})
    adapter.fetch_delivery_status("MSG-123")
    _RecordingGet.body = None

    assert _RecordingGet.url.endswith("/api/v2.04/get/status/MSG-123")
    assert "MessageID=" not in (_RecordingGet.url or "")
