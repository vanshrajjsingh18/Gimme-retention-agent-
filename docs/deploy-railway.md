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

**Check the healthcheck actually took.** The `deploy` half of `railway.json`
did not reach the service on our deploy: the service still had no
`healthcheckPath`, so Railway marked the deployment healthy as soon as the
container stayed up, without ever asking the app for a response. A container
that boots and then serves nothing but errors would have passed. Set
**Settings → Deploy → Healthcheck Path** to `/health` by hand and confirm it
is there; `/health` is unauthenticated and excluded from the dashboard
catch-all, so it answers without a login.

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

**It also refuses to start in production on SQLite.** If you deploy without
attaching a database, `DATABASE_URL` is unset and the app would fall back to a
SQLite file on the container's disk — which does not survive a redeploy. It
would run perfectly, accept an import of your real customers, and lose all of
it the next time anything ships. That failure is silent and arrives late, so
the app stops instead and tells you to attach Postgres. Set
`ALLOW_SQLITE_IN_PRODUCTION=true` only if the data genuinely is disposable.

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

- All **583 tests pass against real PostgreSQL 16**, not only SQLite. This
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

- The production guards fire on a real boot, not only in a unit test: starting
  with `ENVIRONMENT=production` on SQLite stops with a message naming the fix.
- `npm run build` (`tsc -b && vite build`) — the exact command the image runs —
  completes, and every Python dependency resolves to a prebuilt wheel, so the
  image needs no compiler.

Verified on Railway itself, from the build and deploy logs of the live
deployment:

- The image builds. The build log shows the dashboard stage running
  `npm run build` under vite, then `COPY --from=dashboard /build/dist
  ./frontend/dist` into the app stage — so the multi-stage `Dockerfile` is what
  Railway used, and the dashboard it serves is a real build.
- The app boots against the attached Postgres, creates the schema, seeds the
  admin user, and starts the scheduler with its 5 jobs on `0.0.0.0:$PORT`.
  Because it booted with `ENVIRONMENT=production`, the SQLite guard above is
  itself the proof that the Postgres connection is live.

**Not verified:** no request has been made to the public URL from outside
Railway. The network this was developed in cannot reach `*.up.railway.app`, and
the healthcheck that would have had Railway prove it (see step 1) was not
applied to the service. Everything above is evidence the app is running
correctly; none of it is a page actually fetched over the internet. Open the
URL and confirm.

**A note on Railway's build infrastructure.** Three consecutive `redeploy`
calls failed within 90 seconds, each logging only `scheduling build on Metal
builder` and no build output whatsoever, always on the same builder — for a
commit that had built and deployed successfully hours earlier. That is a
Railway-side build failure, not a fault in this repository. A redeploy reuses a
stored build snapshot; pushing a commit triggers a fresh build instead, which
is the better thing to try when redeploys fail this way.
