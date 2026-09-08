# Taking TNZ live

Everything in this system runs in mock mode by default: messages are recorded
locally and never sent. This is the checklist for the switch to live, and the
reasoning behind each item. The same list is computed at runtime and shown on
**Integrations → TNZ go-live readiness**, so the screen and this document
cannot drift.

`GET /api/v1/integrations/{id}/readiness` returns it as JSON.

---

## Blocking

These stop a live send from being safe or from working at all.

### Provider credentials

`auth_token` and `sender`, from a TNZ Group account with REST API access.

### Connection test

Reaches TNZ with the stored credentials. Green here only means the credentials
work — it says nothing about the rest of this list, which is why the readiness
panel exists alongside it.

### Webhook secret

**This one is not optional, and not merely hygiene.**

Delivery receipts are matched to a message by the provider id we issued, so an
unrecognised one is ignored. An inbound *reply* is not: it is matched by phone
number, deliberately, so that an opt-out is still honoured when a provider
fails to echo our id back. Losing an opt-out is worse than accepting an
unmatched one.

That same path, unauthenticated, is a public consent endpoint. Anyone who knows
a customer's number could POST `STOP` to suppress them — or `START`, which
clears the suppression and switches marketing consent back on for somebody who
withdrew it. The system would then text a person who opted out.

So: generate a secret, save it against the integration, and configure the same
value on TNZ's webhook. It is accepted as an `X-Webhook-Secret` header or a
`?secret=` query parameter, for providers that only let you configure a URL.

A live integration with **no** secret configured refuses inbound webhooks
outright. That fails closed on purpose — losing delivery receipts is
recoverable, and an open consent endpoint is not.

### Sender ID

An alphanumeric sender is capped at 11 characters and **cannot receive
replies** — which means STOP never reaches this system, and the opt-out
handling described above is inert. Use a registered number if you rely on it.
Readiness passes an alphanumeric sender but says so.

### Active automations are approved

An automation that is ACTIVE but unapproved is harmless in mock mode and sends
for real on its next scheduled run once the integration flips.

### Opt-out enforcement

`MISSING_SMS_OPT_OUT` must be enabled. Every SMS is checked for an opt-out
instruction before it sends, drafted or hand-written.

---

## Advisory

Worth knowing; not a veto.

### Reachable audience

How many consenting customers hold a mobile number an SMS can actually reach.
Numbers are canonicalised to E.164 on import and again before dispatch
(`app/core/phone.py`), and anything that cannot be resolved — a landline, a
freephone, a malformed import — is refused rather than guessed at, because a
wrong number is a text to a stranger. Those customers are skipped safely, but
they are also never reached, so a large share is a data problem to fix before
spending money on sends.

### Send window

Sends are confined to 09:00–19:00 NZ time. Two things enforce it: the
`QUIET_HOURS` compliance rule blocks a recipient, and `SEND_WINDOW` defers an
automation candidate. Readiness compares them, because when they disagree the
tighter one silently wins for automations only — and a campaign can then send
in the gap.

### What goes live immediately

Which automations are active and will send for real on their next run. Dry-run
each one first; the preview is produced by the same code path as a live send.

---

## Order of operations

1. Configure credentials and the webhook secret. Leave the integration in mock.
2. Point TNZ's webhook at `POST /api/v1/webhooks/tnz`, with the secret.
3. Run a connection test.
4. Confirm readiness reports no blockers.
5. Dry-run every active automation and read the copy it produces.
6. Switch the integration to live.
7. Watch the first run's send ledger before letting the scheduler run unattended.
