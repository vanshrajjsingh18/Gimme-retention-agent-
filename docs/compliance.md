# Compliance

GIMME sells alcohol, so every outbound message carries regulatory weight. This
document describes what the system enforces, where it is enforced, and what it
deliberately does not attempt.

> **This is not legal advice.** The rules implemented here reflect common
> obligations for alcohol marketing in New Zealand and general direct-marketing
> practice. Confirm your own obligations — including current ANZA advertising
> codes, the Sale and Supply of Alcohol Act, the Unsolicited Electronic
> Messages Act and the Privacy Act — with a qualified adviser before sending to
> real customers.

---

## The two questions

The engine separates two things that are easy to conflate:

1. **Who may receive this?** — per-recipient eligibility.
2. **May we say this?** — per-message content.

Both are enforced in application code. Neither consults the LLM; the LLM's
output is an *input* to the content checks.

---

## Per-recipient eligibility

`check_recipient()` runs for every customer, in this fixed order. The order
matters: the reported exclusion reason is the most serious rule they trip, not
the first one evaluated.

| # | Check | Outcome when it fails |
| - | ----- | --------------------- |
| 1 | **Age verification** — `age_verified` must be true | `EXCLUDED_AGE` |
| 2 | **Minimum age** — date of birth, when known, must meet the configured minimum | `EXCLUDED_AGE` |
| 3 | **Suppression** — global, or for this channel | `EXCLUDED_SUPPRESSED` |
| 4 | **Marketing consent** | `EXCLUDED_NO_CONSENT` |
| 5 | **Channel consent** — email consent does not imply SMS consent | `EXCLUDED_NO_CONSENT` |
| 6 | **Contactability** — an address or number on file | `EXCLUDED_MISSING_CONTACT` |
| 7 | **Frequency caps** — 4 per 30 days, 2 per 7 days by default | `EXCLUDED_FREQUENCY_CAP` |
| 8 | **Quiet hours** — 21:00–09:00, SMS and WhatsApp only | `EXCLUDED_QUIET_HOURS` |

Email is exempt from quiet hours: an email arriving at midnight is not
intrusive in the way a phone alert is.

**These run twice.** Once when the audience is previewed and snapshotted, and
again for each recipient at send time. A customer who withdraws consent between
approval and send is excluded — the audience snapshot is a record, not a
licence.

---

## Content rules

`check_content()` runs over the subject and body together.

### Prohibited alcohol claims — all blocking

| Code | What it catches |
| ---- | --------------- |
| `HEALTH_CLAIM` | Alcohol improves health, or has health benefits |
| `EMOTIONAL_WELLBEING_CLAIM` | Drinking cures sadness, anxiety, stress or sorrows |
| `SOCIAL_SUCCESS_CLAIM` | Drinking makes you popular, admired, the life of the party |
| `SEXUAL_SUCCESS_CLAIM` | Drinking improves sexual or romantic success |
| `PROFESSIONAL_SUCCESS_CLAIM` | Drinking closes deals or advances a career |
| `EXCESSIVE_CONSUMPTION` | Encourages getting drunk, binge drinking, drinking without limits |
| `UNDERAGE_APPEAL` | Language addressing or appealing to minors |
| `DRINK_DRIVING` | Associates drinking with driving |

Patterns are deliberately narrow. "Bottoms up" and "get wasted" trip
`EXCESSIVE_CONSUMPTION`; ordinary copy about restocking a favourite beer does
not. The trade-off is conscious: a rule that fires on normal marketing gets
switched off, and a rule that is off protects nobody.

### Grounding rules — all blocking

The system holds order history and brand configuration. It does not hold a
live price list, stock levels or a promotions calendar. Anything it cannot
verify, it will not let you send.

| Code | What it catches |
| ---- | --------------- |
| `UNVERIFIED_COUPON_CODE` | A code not in the brand's active coupon list |
| `UNVERIFIED_PROMOTION` | A discount or offer not on the approved promotions list |
| `UNVERIFIED_PRODUCT` | A product outside this customer's purchase history and the verified catalogue |
| `UNVERIFIED_PRICE` | A price not present in verified product or promotion data |
| `UNVERIFIED_DELIVERY_CLAIM` | A delivery time faster than the configured promise |
| `UNVERIFIED_STOCK_CLAIM` | "Only 3 left", "while stocks last" — unverifiable from the data held |
| `INVENTED_CUSTOMER_FACT` | Birthdays, moods, family, "we know you've had a tough week" |
| `UNRESOLVED_PLACEHOLDER` | `{{first_name}}` or `[NAME]` surviving into the body |

Note the asymmetry on delivery: claiming *slower* than the promise is fine;
claiming *faster* is not.

### Mandatory statements

| Code | Severity | Applies to |
| ---- | -------- | ---------- |
| `MISSING_RESPONSIBLE_DRINKING` | Blocking | Email only |
| `MISSING_AGE_STATEMENT` | Warning | Email only |

SMS is exempt — 320 characters does not accommodate a legal footer, and a
truncated statement is worse than none. Both statements are configured in
**Brand**, and matching tolerates whitespace and light rewording.

### Brand-configured prohibitions

Any phrase added to *Additional prohibited claims* in Brand blocks a message
outright. *Words to avoid* raises a warning instead — it is a style preference,
not a legal boundary.

---

## Targeting rules

`check_targeting()` inspects the segment rule behind a campaign.

| Code | Severity | Rationale |
| ---- | -------- | --------- |
| `VULNERABILITY_TARGETING` | Warning | Selecting on discount dependency ≥ 0.7 can select for financial vulnerability |
| `HEAVY_CONSUMPTION_TARGETING` | Blocking | Pushing reorders at customers ordering 12+ times a month encourages excessive consumption |

Targeting on lifecycle stage, recency, revenue or product preference is not
flagged — that is ordinary retention marketing.

---

## Human approval

No campaign can send without an explicit human approval action. This is not a
setting; it is a state transition:

```
DRAFT → COMPLIANCE_CHECKED → AWAITING_APPROVAL → APPROVED → RUNNING → COMPLETED
```

`run_campaign()` refuses any campaign not in `APPROVED` or `SCHEDULED`, and
independently refuses one whose stored compliance report has blocking
findings. Editing a campaign's copy or audience clears its approval and
returns it to `DRAFT`.

Message-level approval is separate: a message failing validation cannot be
approved, and editing an approved message revokes that approval.

---

## Opt-outs

An opt-out is honoured immediately and in two ways: channel consent is set to
false, *and* a suppression record is written for that channel. Both are
checked on every subsequent send, so a consent flag reset by a later data
import cannot silently re-enable messaging.

---

## Configuration

Rules can be disabled individually on the **Compliance** screen. Disabling a
blocking rule asks for confirmation and writes an audit entry naming the actor,
the rule and the change. Disabling `PROHIBITED_CLAIMS` or `GROUNDED_CLAIMS`
disables the whole family of codes each owns.

Frequency caps, quiet hours and the minimum age are stored on their rules and
can be edited without code changes.

---

## Audit trail

Every consequential action is recorded in `audit_logs`: logins, campaign
approvals, message approvals and rejections, consent changes, suppressions,
compliance rule changes, integration credential updates (key names only, never
values) and demo-data regeneration. Visible under **Settings → Audit log**.

---

## What this system does not do

Stated plainly, because a compliance feature that is assumed to exist is worse
than one known to be absent:

- **It does not verify age.** It enforces the `age_verified` flag your source
  system sets. It cannot tell you whether that verification was sound.
- **It does not check stock or price.** It refuses to make claims about them.
- **It does not interpret law.** The rules encode a reasonable reading of
  common obligations; they are not a legal opinion, and jurisdictions differ.
- **It does not moderate images.** Only text is checked.
- **It does not enforce a global send quota** beyond per-customer frequency
  caps.
- **It does not detect vulnerability beyond the two heuristics above.** Those
  are crude proxies, not a safeguarding system.
