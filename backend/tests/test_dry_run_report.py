"""The dry run, read as an operator reads it."""
from __future__ import annotations

from datetime import datetime

from app.automations.runtime import SKIP_EXPLANATIONS, RunReport, SendDecision
from app.core.enums import SendStatus, SkipReason


def _decision(status, reason=None, cid=1):
    return SendDecision(
        customer_id=cid,
        status=status,
        scheduled_for=datetime(2026, 9, 9, 19, 9),
        local_date=datetime(2026, 9, 9).date(),
        skip_reason=reason,
    )


def _report(results, *, dry_run=True):
    return RunReport(
        automation_id=1,
        automation_name="Smart Reorder",
        kind="NUDGE",
        dry_run=dry_run,
        ran_at=datetime(2026, 9, 9, 18, 0),
        results=results,
        is_mock=True,
    )


def test_summary_reads_as_the_brief_specified():
    results = (
        [_decision(SendStatus.PREVIEW, cid=i) for i in range(418)]
        + [_decision(SendStatus.SKIPPED, SkipReason.NO_CONSENT, cid=i) for i in range(312)]
        + [_decision(SendStatus.SKIPPED, SkipReason.FREQUENCY_CAP, cid=i) for i in range(96)]
        + [_decision(SendStatus.SKIPPED, SkipReason.ALREADY_ORDERED, cid=i) for i in range(31)]
    )
    summary = _report(results).dry_run_summary()

    assert summary["headline"] == "DRY RUN RESULTS"
    text = "\n".join(summary["lines"])

    assert "857 customers evaluated" in text
    assert "418 eligible" in text
    assert "312 excluded — no marketing consent for this channel" in text
    assert "96 excluded — already messaged recently (frequency cap)" in text
    assert "31 excluded — they have already ordered" in text
    assert "418 messages would be sent" in text
    assert "No messages were actually sent." in text


def test_largest_exclusion_is_listed_first():
    """The biggest reason the audience shrank should not need hunting for."""
    results = (
        [_decision(SendStatus.SKIPPED, SkipReason.QUIET_HOURS, cid=i) for i in range(3)]
        + [_decision(SendStatus.SKIPPED, SkipReason.NO_CONSENT, cid=i) for i in range(90)]
        + [_decision(SendStatus.SKIPPED, SkipReason.SUPPRESSED, cid=i) for i in range(20)]
    )
    lines = _report(results).dry_run_summary()["lines"]
    excluded = [ln for ln in lines if "excluded" in ln]
    counts = [int(ln.split()[0].replace(",", "")) for ln in excluded]
    assert counts == sorted(counts, reverse=True), excluded


def test_a_thousand_customers_is_written_with_a_separator():
    results = [_decision(SendStatus.PREVIEW, cid=i) for i in range(1284)]
    assert "1,284 customers evaluated" in _report(results).dry_run_summary()["lines"][0]


def test_every_skip_reason_has_words():
    """A new reason must not surface to an operator as a bare enum name."""
    missing = [r.value for r in SkipReason if r.value not in SKIP_EXPLANATIONS]
    assert not missing, f"no plain-English explanation for: {missing}"


def test_a_live_run_does_not_claim_nothing_was_sent():
    results = [_decision(SendStatus.SENT, cid=i) for i in range(5)]
    summary = _report(results, dry_run=False).dry_run_summary()
    text = "\n".join(summary["lines"])
    assert summary["headline"] == "RUN RESULTS"
    assert "5 messages sent" in text
    assert "No messages were actually sent." not in text
    assert "Mock mode" in text, "a mock run should say so"
