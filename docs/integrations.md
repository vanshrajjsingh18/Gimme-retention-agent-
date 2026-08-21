# Integrations

Every messaging provider sits behind one interface, so the campaign engine
never branches on which provider is configured:

```python
validate_credentials()   # are the stored credentials usable?
send_message()           # send one message
send_test_message()      # send one, tagged as a test
fetch_delivery_status()  # poll for a message's status
process_webhook()        # raw provider payload -> normalized events
normalize_event()        # one raw event -> our vocabulary
```

Each provider has a live adapter and a mock counterpart implementing the same
interface.

---

## Mock mode

The default, and fully functional. Mock adapters:

- accept sends and return a provider message ID
- produce a small, deterministic rate of hard failures, so the failure path is
  exercised in every demo run
- emit realistic delivery, open, click, reply and opt-out sequences
- scale engagement by the customer's own engagement score, so an engaged
  customer behaves differently from a disengaged one
- tag every record `is_simulated = true`

All randomness is seeded from `MOCK_SEED`, so a given customer and message
always produce the same behaviour. Simulated events are visibly labelled in
the UI and are never mistaken for real customer behaviour.

**Nothing leaves the machine in mock mode.**

---

## Switching a provider live

1. **Integrations** → pick the provider → **Configure**
2. Switch mode to **Live**
3. Enter credentials → **Save**
4. **Test connection**, then **Send test**

Credentials are stored server-side. API responses return only a presence flag
and a four-character hint — the secret itself never reaches the browser.
Leaving a field blank on a later edit keeps the stored value.

**A half-configured provider falls back to mock.** If mode is `live` but a
required credential is missing, `get_adapter()` returns the mock adapter
rather than attempting a send that would fail. Messages are recorded, not
dropped.

---

## Microsoft Outlook (email)

Uses Microsoft Graph with the client-credentials flow.

**Setup**

1. Azure Portal → **Microsoft Entra ID** → **App registrations** → **New**
2. **Certificates & secrets** → new client secret; copy the value now
3. **API permissions** → Microsoft Graph → **Application permissions** →
   `Mail.Send` → **Grant admin consent**
4. Note the Directory (tenant) ID and Application (client) ID

**Credentials**

| Field | Where it comes from |
| ----- | ------------------- |
| `tenant_id` | Entra ID → Overview → Directory (tenant) ID |
| `client_id` | App registration → Overview → Application (client) ID |
| `client_secret` | App registration → Certificates & secrets |
| `sender_address` | The mailbox that sends, e.g. `retention@gimmedelivery.co.nz` |

**Sends via** `POST /v1.0/users/{sender}/sendMail`. Tokens are cached until a
minute before expiry.

**Limitation worth knowing:** Graph's `sendMail` returns `202 Accepted` with no
message ID and reports no bounce, open or click events. Delivery signals need
Exchange message trace or a mail gateway in front. `process_webhook()` already
accepts the normalized shape those systems produce, so wiring one in needs no
code change here.

---

## TNZ Group (SMS)

New Zealand SMS provider, REST API.

**Setup**

1. A TNZ Group account with REST API access enabled
2. Generate an auth token in the TNZ dashboard
3. Register your sender ID or number

**Credentials**

| Field | Meaning |
| ----- | ------- |
| `auth_token` | TNZ API auth token |
| `sender` | Registered sender number or alphanumeric ID |

Optional `base_url` in config, defaulting to `https://api.tnz.co.nz`.

**Sends via** `POST /api/v2.04/send/sms`. Delivery receipts arrive either by
polling `fetch_delivery_status()` or by webhook.

**Status mapping**

| TNZ status | Recorded event |
| ---------- | -------------- |
| `sent` | `SMS_SENT` |
| `delivered` | `SMS_DELIVERED` |
| `failed`, `undelivered`, `rejected` | `SMS_FAILED` |
| `optout`, `stop` | `CUSTOMER_OPTED_OUT` |

An `optout` revokes SMS consent and writes a suppression record immediately.

---

## WhatsApp

WhatsApp Business messaging is sold by several providers with near-identical
request shapes, so this adapter is driven by a provider profile rather than
hard-coded to one.

| Profile | Provider | Required credentials |
| ------- | -------- | -------------------- |
| `meta_cloud` (default) | Meta WhatsApp Cloud API | `access_token`, `phone_number_id` |
| `twilio` | Twilio WhatsApp | `account_sid`, `auth_token`, `from_number` |
| `360dialog` | 360dialog | `api_key` |

Switching provider is a settings change on the Integrations screen.

**Setup (Meta Cloud API)**

1. Meta for Developers → create an app → add **WhatsApp**
2. Note the phone number ID and generate a permanent access token
3. Configure the webhook URL and verify token in the app dashboard

`process_webhook()` normalizes Meta Cloud, Twilio and generic payload shapes.

**Status mapping**

| Provider status | Recorded event |
| --------------- | -------------- |
| `sent` | `WHATSAPP_SENT` |
| `delivered` | `WHATSAPP_DELIVERED` |
| `read` | `WHATSAPP_READ` |
| inbound message | `WHATSAPP_REPLIED` |
| `failed`, `undelivered` | `MESSAGE_FAILED` |

---

## Webhooks

Point each provider at:

```
POST http://your-host:8000/api/v1/webhooks/outlook
POST http://your-host:8000/api/v1/webhooks/tnz
POST http://your-host:8000/api/v1/webhooks/whatsapp
```

These endpoints are unauthenticated, because providers post from their own
infrastructure. They are safe because they only ever *record an event against a
message the system already sent*: an unrecognised provider message ID is
counted and discarded rather than creating a record. The response reports what
happened:

```json
{
  "provider": "whatsapp",
  "events_received": 3,
  "events_recorded": 2,
  "events_ignored_unknown_message": 1
}
```

Event writes are idempotent on a key derived from the provider message ID,
event type and timestamp, so a provider redelivering a webhook cannot inflate
your open rate.

For local testing, tunnel with ngrok or Cloudflare Tunnel and use the public
URL. Or simply post the generic shape directly:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/webhooks/whatsapp \
  -H 'Content-Type: application/json' \
  -d '{"event":"read","message_id":"mock-whatsapp_mock-abc123"}'
```

---

## The language model

Configured through environment variables rather than the UI, so a key is never
handled by the browser.

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

Any OpenAI-compatible `POST /chat/completions` endpoint works: OpenAI, an
Azure OpenAI gateway, vLLM, Ollama's compatibility layer, OpenRouter.

If `LLM_API_KEY` is empty the factory returns the mock provider regardless of
`LLM_PROVIDER`, so a misconfiguration degrades to working local generation
rather than failing sends.

The provider only ever receives a prompt and returns text. Context assembly and
output validation happen outside it, so **changing provider cannot change what
the system is willing to send**.

---

## Adding a provider

1. Subclass `MessagingAdapter` in `app/integrations/`
2. Declare `provider`, `channel` and `required_credentials`
3. Implement the six interface methods
4. Register it in `LIVE_ADAPTERS` in `app/integrations/registry.py`
5. Add a default row to `DEFAULT_INTEGRATIONS`

The campaign engine, journey engine and webhook handler need no changes — they
only know the interface.
