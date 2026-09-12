# Deploying to Railway

One service and one database. The API serves the dashboard from the same
origin, so there is no second service to deploy, no CORS to configure, and no
API URL baked into the frontend build — the three things most likely to be
wrong on a first deploy, and which all fail in ways that look like the app is
simply broken.

---

## 1. Create the project

1. Push this branch to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo**, and pick it.
3. Railway reads `railway.json` and builds the root `Dockerfile`. Nothing else
   to configure for the build.

## 2. Add the database

**New → Database → PostgreSQL**, in the same project.

Railway injects `DATABASE_URL` automatically. It publishes it in the
`postgres://` form, which SQLAlchemy 2 no longer accepts — the app rewrites
that on startup, so it works as injected with nothing for you to change.

The schema creates itself on first boot. There is no migration step to run.

## 3. Set the variables

On the service, under **Variables**:

| Variable | Required | What to set it to |
| --- | --- | --- |
| `SECRET_KEY` | **Yes** | A long random string. Signs login tokens — anyone who has it can forge a session. |
| `ADMIN_EMAIL` | **Yes** | Your login. |
| `ADMIN_PASSWORD` | **Yes** | A real password. |
| `ENVIRONMENT` | Already set | `production`, from the Dockerfile. |
| `PUBLIC_BASE_URL` | Recommended | `https://<your-app>.up.railway.app`. Used for the webhook address shown in the dashboard. |
| `TNZ_AUTH_TOKEN` | For live SMS | From your TNZ account. |
| `TNZ_SENDER` | For live SMS | The sender ID or number TNZ registered for GIMME. |
| `TNZ_WEBHOOK_SECRET` | For live SMS | A long random string; the same value goes in TNZ's webhook settings. |
| `LLM_PROVIDER` | Optional | `openai` to write copy with a model; leave unset for the built-in writer. |
| `LLM_API_KEY` | If above | Your key. |

**The app refuses to start in production if `SECRET_KEY` or `ADMIN_PASSWORD`
is still the development default.** That is deliberate: a live host on a
published secret is not "mostly fine", and a warning in a log nobody reads is
not a safeguard.

### Why TNZ credentials go in the environment

They can also be typed into the dashboard, which stores them in the database.
On a deployed host the environment is the better home: the values never sit in
a row that a database backup would carry off, and rotating a token is a
redeploy rather than an edit. **Where both exist, the environment wins**, and
the dashboard labels each credential with where it came from.

## 4. First boot

Open the Railway URL. You'll get the login page, and the database will be
empty — no customers, no campaigns.

To load your data: **Data & imports → Upload a CSV**. Preview first; it runs
the real import over every row and discards the result, so it tells you exactly
what would happen before anything is written.

To try it with generated data instead, run this once from Railway's shell:

```bash
cd /app/backend && python -m scripts.seed_demo --customers 1000 --reset
```

That writes 1,000 invented customers. Don't run it on a database holding real
ones — `--reset` means what it says.

## 5. Point TNZ at the webhook

In TNZ's settings, set the delivery/reply webhook to:

```
https://<your-app>.up.railway.app/api/v1/webhooks/tnz
```

with the header `X-Webhook-Secret: <your TNZ_WEBHOOK_SECRET>`, or append
`?secret=<value>` if TNZ only lets you configure a URL.

This is not optional for a live integration. An inbound reply is matched by
phone number — deliberately, so an opt-out is honoured even when the provider
does not echo our message id back — which means an unauthenticated endpoint
lets anyone who knows a customer's number forge a `STOP`, or a `START` that
restores consent they withdrew. **A live integration with no secret configured
refuses inbound webhooks outright.**

## 6. Go live

**Integrations → TNZ go-live readiness** lists what is still blocking, with the
reason and the fix for each. Work down it until it reports no blockers, then
switch the integration from Mock to Live.

Read `docs/tnz-go-live.md` before you do — particularly the ordering: test the
connection *before* flipping, and dry-run every active automation, because an
automation that is already ACTIVE sends for real on its next scheduled run the
moment the integration flips.

---

## What was verified, and what was not

Verified here:

- All **579 tests pass against real PostgreSQL 16**, not only SQLite. This
  caught a genuine bug: the analytics month-grouping used `strftime()`, a
  SQLite-only function, so every analytics page raised `UndefinedFunction` on
  Postgres. Now dialect-aware and covered by a test.
- The full schema (37 tables) creates on Postgres, bootstrap runs, and reads
  and writes work.
- The app boots in `ENVIRONMENT=production` against Postgres, serves the
  dashboard and the API on one origin, and a browser can log in and load the
  overview with nothing reaching a baked-in localhost URL.
- TNZ credentials supplied purely as environment variables reach the adapter
  and satisfy the readiness checks.

**Not verified:** the Docker image has never been built — there is no Docker
daemon in the environment this was developed in. The Dockerfile's individual
steps are all exercised (the frontend build, the Python install including
`psycopg2`, the start command), but the first `docker build` will happen on
Railway. If it fails, it will fail at build time with a clear error rather than
silently.
