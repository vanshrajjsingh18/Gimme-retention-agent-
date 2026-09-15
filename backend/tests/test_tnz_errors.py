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


def test_connection_test_does_not_call_a_400_a_success(monkeypatch):
    """A refusal is not a working connection.

    Only 401/403 used to count as failure, so TNZ refusing the request outright
    was reported as "Connected" — and the go-live checklist, whose whole job is
    to withhold that reassurance until it is earned, passed it on.
    """
    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k):
            return _response(400, json={"Error": "Missing or empty sender"})

    monkeypatch.setattr("app.integrations.tnz.httpx.Client", _Client)
    adapter = TnzSmsAdapter(credentials={"auth_token": "t", "sender": "GIMME"})
    result = adapter.validate_credentials()

    assert result.status == "ERROR", f"400 reported as {result.status}: {result.message}"
    assert "Missing or empty sender" in result.message


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
