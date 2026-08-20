# PROJECT STATUS — GIMME Retention Engine

**Last updated:** 2026-08-20

## Product Goal

A local-first, AI-assisted customer retention platform for GIMME Beverage
Delivery. It ingests customer/order data, builds Customer 360 profiles,
computes lifecycle stage, RFM, churn risk and next-best-action, generates
grounded personalised messages via an LLM, and runs compliance-gated campaigns
in MOCK MODE with full event tracking and revenue attribution.

## Current Phase

Phase 2 — Data foundation and persistence layer (engines complete and tested).

## Completed Features

- Repository scaffold, Python venv, dependency install.
- Configuration layer (`app/core/config.py`) with `.env` support.
- SQLAlchemy engine/session layer; SQLite with a PostgreSQL-portable schema.
- 33 ORM tables covering customers, orders, metrics, segments, campaigns,
  messages, events, journeys, compliance, integrations and operations.
- Security primitives: bcrypt password hashing, JWT issuing, API-key hashing.
- **Metrics engine** — behavioural metric computation (14 tests).
- **Lifecycle engine** — 9-stage deterministic classifier with per-customer
  cadence and global fallbacks (21 tests, all 9 stages proven reachable).
- **Churn engine** — 0-100 transparent weighted-factor scoring with named
  factors and human-readable explanations (19 tests).
- **RFM engine** — quantile scoring with absolute fallback for small/flat
  populations (11 tests).
- **Next Best Action engine** — priority-ordered rules with reason codes
  (29 tests).

## Partially Completed Features

None currently in flight.

## Missing Features

Persistence services, ingestion, API layer, auth endpoints, brand settings,
LLM message studio, campaign engine, compliance engine, event system,
attribution, analytics, journeys, seed data, frontend, Docker, docs.

## Known Bugs

None open.

## External Blockers

- No LLM API key available → MOCK LLM provider is the default.
- No Microsoft Graph / TNZ / WhatsApp credentials → MOCK adapters are the
  default. Live adapter code paths are implemented but unexercised.

## Last Successful Verification

`pytest tests/` — 94 passed (metrics, lifecycle, churn, RFM, recommendations).

## Next Task

Build the persistence services that write engine output to the database
(`intelligence` service), then CSV/API ingestion.
