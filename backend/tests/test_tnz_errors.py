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
