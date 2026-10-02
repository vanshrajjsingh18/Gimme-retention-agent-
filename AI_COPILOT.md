# AI Copilot

A natural-language command layer over the GIMME Retention Engine. An operator
types what they want ("Create a Smart Reorder campaign for lapsed beer
customers…"); the Copilot inspects the engine with approved tools, builds a
concrete plan, shows a preview computed by the engine itself, and changes
something only when the operator presses **Confirm** on that specific plan.

**The engine stays the source of truth.** The model never touches the
database, SQL, Python or a shell. It chooses which registered tool to call;
the tools wrap the existing services (campaigns, automations, Smart Reorder
queue and predictions, segments, merge tags, compliance, analytics,
scheduler); the services validate and execute.

- UI: the **AI Copilot** button in the header (slide-out panel) or `/ai-copilot`
  (full console with history, current context and recent actions).
- API: `/api/v1/ai/*`.
- Code: `backend/app/copilot/`, `backend/app/api/v1/ai.py`,
  `frontend/src/features/copilot/`.

---

## Architecture

```
operator message
  └─► CopilotEngine.chat()                         backend/app/copilot/engine.py
        ├─ attach operational context (once per message, replayed unchanged)
        ├─ AIProvider.chat(system prompt, transcript, tool schemas)
        │     OpenAI-compatible │ Anthropic (official SDK) │ offline planner
        ├─ for each tool call ─ validate arguments against the tool schema
        │     READ  ─► run in a sandbox transaction (always rolled back)
        │     WRITE ─► plan_write(): run the real handler in the sandbox,
        │              add the tool's previewer (e.g. a Smart Reorder dry run),
        │              store a pending CopilotAction bound to an id
        └─ loop until the model answers in text (max AI_MAX_TOOL_STEPS)

operator presses Confirm on action #N
  └─► CopilotEngine.confirm(N)
        ├─ checks: owner, not viewer, status PENDING, not expired
        ├─ atomic claim PENDING → EXECUTING (a double click runs once)
        ├─ run the handler for real, with the confirmed arguments
        ├─ CopilotExecution receipt (before/after state) + AuditLog entry
        └─ engine note in the transcript so the model knows the outcome
```

| Piece | File |
| --- | --- |
| Tool registry, risk levels, result contract, argument validation | `copilot/registry.py` |
| Tools (63) | `copilot/tools/*.py` |
| Providers | `copilot/providers.py`, `copilot/mock.py` |
| System prompt, operational context | `copilot/prompts.py` |
| Sandbox transaction | `copilot/sandbox.py` |
| Chat loop, planning, confirmation, receipts, idempotency | `copilot/engine.py` |
| Purchase-history cohorts (new engine service) | `services/purchase_cohorts.py` |
| Tables | `CopilotConversation`, `CopilotMessage`, `CopilotAction`, `CopilotExecution` in `models/entities.py` |

### The preview sandbox

`sandbox_session()` opens a connection, begins a transaction, and binds a
session with `join_transaction_mode="rollback_only"`: service code inside may
`commit()` as usual, but nothing reaches the database, and the outer
transaction is always rolled back. A guard refuses any commit after an inner
rollback. Two consequences:

- **Reads can never write.** Every READ tool runs here.
- **Previews are the real thing.** A planned "create Smart Reorder campaign" is
  actually created inside the sandbox, the engine's own dry run
  (`smart_reorder_queue.build_queue(dry_run=True)` plus the compliance
  recipient check) runs against it, and the result is rolled back. The numbers
  on the card are the rules executed, not a second simulation of them.

### Tool result contract

```json
{ "success": true, "data": {}, "metadata": {"count": 1284, "period": "last week (21 Sep–27 Sep)", "generated_at": "…", "source": "database"}, "errors": [] }
```

Results longer than 14,000 characters are shortened structurally for the model
(lists trimmed with "N more not shown"), never cut mid-JSON. The full result is
stored on the message for the UI.

---

## Tools and permissions

Every tool declares a risk. `GET /api/v1/ai/tools` lists them all.

| Risk | Behaviour | Examples |
| --- | --- | --- |
| `READ` | Runs immediately, in the sandbox | `search_customers`, `diagnose_customer_delivery`, `preview_smart_reorder`, `get_revenue_analytics` |
| `WRITE` | Planned + previewed; runs only on Confirm | `create_smart_reorder_campaign` (always a DRAFT), `update_coupon_allocation`, `update_message_template`, `create_segment`, `pause_campaign` |
| `HIGH_RISK_WRITE` | Same, with a red "Confirm — go live" card | `activate_smart_reorder_campaign`, `activate_campaign`, `resume_campaign`, `cancel_campaign`, `bulk_update_campaigns` |

Groups: **customers** (search, profile, 360, orders, predictions, campaign
history, delivery diagnosis), **segments/cohorts** (list, get, fields, preview,
create, update, export, purchase cohorts), **smart reorder** (overview,
predictions, upcoming, dry run, create, update, activate, pause, cancel),
**campaigns** (list with filters, get, preview, create, update, pause, resume,
cancel, activate, bulk), **messaging** (merge tags, validate, render, update
copy), **coupons** (create variant, re-split, preview assignment, analytics),
**analytics** (retention overview, campaigns, revenue/attribution/coupon
revenue, conversion, segment, prediction accuracy, delivery), **products**
(search, product, performance, top products incl. repeat rate, customer
affinity — derived from order lines; stock is never claimed), **scheduler**
(scheduled messages, reschedule, cancel one), **system** (status, errors,
scheduler, active jobs).

## Approval model

1. **Read** — executes automatically.
2. **Write** — the model's call creates a pending action with a preview. The
   operator reviews it and presses Confirm; only then does it execute.
3. **High-risk** — the same, always with an explicit, visually distinct
   confirmation. Activating an automation records the confirming user's
   approval (`approve`) and activates it; it is refused if the copy has any
   blocking compliance finding, because every send would be blocked anyway.

Rules the engine enforces regardless of what the model says:

- Confirmation is bound to the action id. Typed "yes / go ahead" executes
  nothing — the model is told to point to the card.
- One planned change at a time; a new plan supersedes the previous pending one.
- Pending actions expire after `AI_ACTION_TTL_MINUTES` (default 30): the
  preview may be stale.
- Viewers can ask anything but cannot plan or confirm changes.
- Changing copy, audience or settings goes through the engine's own
  `apply_update` / `revoke_approval`, so approval is withdrawn exactly as it is
  when the change is made in the UI.
- Coupon codes must appear in the operator's own messages or already be
  verified; a code the model invents is refused. Operator-supplied codes that
  are not yet verified are added to Brand Settings' verified list as an
  explicit, previewed part of the action.
- Copy is validated with the merge-tag whitelist and the compliance engine
  (`compliance/engine.py`, configured from Brand Settings). Unknown merge tags
  and prohibited claims (health, excessive drinking, minors, …) are refused.

## Audit, idempotency and errors

- **Receipts:** every confirmed write creates a `CopilotExecution` with tool,
  arguments, target, before/after state, confirmation flags, success, error and
  duration, plus an `AuditLog` row (`COPILOT_ACTION_EXECUTED` / `_FAILED`).
  Deleting a conversation keeps its receipts. `GET /api/v1/ai/executions`.
- **Idempotency:** the key is `sha256(conversation, tool, canonical args)`.
  Repeating a request reuses the pending action; repeating a create after it
  executed returns `ALREADY_DONE` with the receipt instead of a duplicate; a
  second Confirm returns the existing receipt (`duplicate: true`); the
  PENDING→EXECUTING claim is a conditional UPDATE so concurrent confirms run once.
- **Errors:** a failed tool returns `success: false` with the reason, which the
  model is told verbatim; a failed execution is recorded and shown on the card
  with **Retry** (`POST /api/v1/ai/retry`). Provider failures say "Nothing was
  changed". Reads are retried once on a locked database.

## Conversation state

`CopilotConversation` stores `active_entity_type/id` (the campaign, segment or
customer being worked on), `working_state.last_result_set` (the customer list
"them" refers to — up to 5,000 ids) and `pending_action_id`. Each user message
is stored with the operational context as it was when sent (focus, pending
action, last result set, segments with live counts, lifecycle stages, order
categories, verified coupon codes, send window, business time). This is how
"change the coupon split" knows which campaign, and how business words
("VIP", "high value", "tonight") map onto the engine's existing definitions
instead of hard-coded guesses.

## AI providers

| `AI_PROVIDER` | Behaviour |
| --- | --- |
| *(empty)* | `openai` if `LLM_PROVIDER=openai` and `LLM_API_KEY` is set, else `mock` |
| `mock` | Deterministic offline planner (`copilot/mock.py`). No key, no network. |
| `openai` | Any OpenAI-compatible `/chat/completions` with tool calling. Key/model/base URL fall back to `LLM_*`. |
| `anthropic` | Official `anthropic` SDK, Messages API with tools. Default model `claude-opus-5-5`, `output_config.effort` from `AI_EFFORT` (default `medium`), server-side refusal fallbacks (`fallbacks: "default"`) unless `AI_ANTHROPIC_FALLBACKS=false`. The assistant's raw content blocks are stored and replayed verbatim, and the transcript is append-only. Key from `AI_API_KEY` or the SDK's usual resolution (`ANTHROPIC_API_KEY`, …). |

Environment variables: `AI_PROVIDER`, `AI_MODEL`, `AI_API_KEY`, `AI_BASE_URL`,
`AI_EFFORT`, `AI_ANTHROPIC_FALLBACKS`, `AI_MAX_TOOL_STEPS` (8),
`AI_TIMEOUT_SECONDS` (120), `AI_ACTION_TTL_MINUTES` (30). Keys never reach the
browser and are never logged.

**The offline planner** understands the common command shapes by pattern (see
the examples below) and composes its answers only from tool results. It is a
development and testing tool: it handles one request per message and does not
carry a half-specified request across messages (e.g. "Create an Oktoberfest
campaign" → it asks for details, but the follow-up answer needs a live model
to be combined with the first message). Unrecognised requests get an honest
"didn't recognise that" with examples — never a guess.

## Observability

Logger `app.copilot`: `copilot.request` (conversation, user, provider),
`copilot.model` (step, tools selected, token usage, latency),
`copilot.tool` (tool, success, latency), `copilot.planned`,
`copilot.plan_refused`, `copilot.executed`, `copilot.cancelled`,
`copilot.llm_error`, `copilot.tool_error`. Arguments, contact details and keys
are not logged.

## API

| Method | Path | |
| --- | --- | --- |
| POST | `/api/v1/ai/chat` | `{message, conversation_id?}` → conversation view |
| POST | `/api/v1/ai/confirm` | `{action_id}` — write access |
| POST | `/api/v1/ai/cancel` | `{action_id}` |
| POST | `/api/v1/ai/retry` | `{action_id}` of a FAILED action — write access |
| GET | `/api/v1/ai/conversations` | the caller's conversations |
| GET / DELETE | `/api/v1/ai/conversations/{id}` | view / delete (receipts kept) |
| GET | `/api/v1/ai/tools` | registry with risk levels |
| GET | `/api/v1/ai/actions/{id}` | one planned action |
| GET | `/api/v1/ai/executions[?conversation_id=]`, `/api/v1/ai/executions/{id}` | receipts |
| POST | `/api/v1/ai/dry-run` | `{automation_id? , conversation_id?}` — Smart Reorder dry run |

No streaming: responses arrive when the turn completes, with tool steps shown
as indicators. Streaming is a possible follow-up.

## Example commands

- "Show today's predicted reorders" · "How many customers are predicted to order tonight?"
- "Create a Smart Reorder campaign called Beer Lapsed for customers who ordered
  beer at least twice and haven't ordered in 14 days. Send SMS 30 minutes before
  their predicted reorder time. Use three coupon codes FIRST7, LUCKY7 and
  COMEAGAIN7 with equal allocation. Write a short cheeky message using
  #first_name#, #product# and #coupon_code#."
- "Change the coupon split to 50%, 25%, 25%." · "Change the message to be more
  Kiwi and dry." · "Change the reminder to 20 minutes before predicted order." ·
  "Exclude low-confidence predictions." · "Use WhatsApp instead of SMS." ·
  "Make the audience only customers who haven't ordered in 30 days."
- "Show me a dry run." · "Activate it." · "Pause the campaign." · "show campaign 12"
- "Show me customers who haven't ordered for 30 days but historically ordered
  more than $70." → "Create a segment from them called Lapsed spenders" /
  "Create a reactivation campaign for them."
- "Why didn't Sarah Patel receive the reminder yesterday?"
- "Create a new segment for customers whose average order value is above $80."
  · "Export the customers in this segment."
- "How much revenue did Smart Reorder generate last week?" · "Which products
  have the highest repeat purchase rate?" · "What campaigns are running?" ·
  "Pause all active Smart Reorder campaigns." · "system status"

## Testing

- `backend/tests/test_copilot.py` (31 tests): reads, customer lookup and
  ambiguity, segment focus, creation planned-then-confirmed, coupon re-split,
  dry run writes nothing, receipts and audit, cancellation, activation
  protection and typed-approval refusal, Smart Reorder settings, invented and
  invalid coupon codes, merge tags, analytics against direct queries, delivery
  diagnosis from a recorded cancellation, tool and provider failures,
  idempotency, conversation and working state, bulk confirmation, compliance,
  viewer role, sandbox, provider selection and the Anthropic transcript shape,
  API auth, oversized results, and the end-to-end acceptance flow over HTTP.
  All deterministic; no external API.
- `frontend/src/tests/Copilot.test.tsx`: Markdown safety and merge tags, action
  card confirm/high-risk/executed/failed states.
- `frontend/e2e/copilot.spec.ts`: the acceptance conversation in a real browser.

```
make test-backend
cd frontend && npm test
cd frontend && npm run test:e2e   # needs backend + frontend running
```

## Adding a tool

1. Write a function in `backend/app/copilot/tools/<group>.py` taking
   `(ctx: ToolContext, **args)` and returning `ToolResult`. Use existing
   services; raise `ToolError("…")` for anything the operator should read.
2. Decorate it with `@tool(name, description=…, risk=Risk.READ|WRITE|HIGH_RISK_WRITE, group=…, properties={…}, required=[…])`.
   Descriptions are what the model reads — say what it does and when to use it.
3. For a write: return `target_type`, `target_id`, `before`, `after` (use the
   snapshot helpers in `_common.py`), set `creates=True` for creations, add a
   `summarize` for the card title, and a `previewer` if the card should show
   more than before/after (see `smart_reorder._preview_after`). The handler
   must have no effects outside the database, because previews run it.
4. Return `focus=(type, id)` to move the conversation's focus and
   `result_set=…` for lists "them" should refer to.
5. Add a test with `ScriptedProvider` in `tests/test_copilot.py`.

## Adding a provider

Subclass `AIProvider` in `copilot/providers.py`: implement
`chat(system=, messages=, tools=, context=) -> AIResponse`, translating the
provider-neutral transcript (user / assistant with `tool_calls` / tool results)
to the provider's format and back, mapping its errors to `AIProviderError`
with a message that never includes the response body. Return provider-native
content in `AIResponse.raw` if it must be replayed verbatim. Wire it into
`_resolved()` / `get_ai_provider()` and add a transcript-shape test.
