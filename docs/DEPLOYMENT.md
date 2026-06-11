# LORA — Deployment Runbook

**Target host:** Railway (Hobby plan recommended for solo dev; ~$15–25/month)
**Goal of this doc:** take you from "code in GitHub" to "live on a subdomain" without prior DevOps experience.

---

## 0. Prerequisites checklist

Before starting, confirm you have all of these:

- [ ] A **GitHub repo** with the code pushed to `main`
- [ ] A **Railway account** signed in with the same GitHub account
- [ ] A **subdomain** you control (e.g., `lora.yourcompany.com`) with access to its DNS settings
- [ ] **Zendesk admin access** (to update webhook URLs once the app is live)
- [ ] **PayPal developer/business access** (same reason)
- [ ] Production **secrets generated** — see "Generating secrets" below
- [ ] **Zendesk custom field IDs** for `client_email`, `phone`, `flight_details` (placeholders are `None` in `apps/integrations/services.py` until you fill them in — find them in Zendesk admin → Ticket Fields)

---

## 1. Generating production secrets

You need three independent secrets. Generate each with a fresh `python -c` call (don't reuse):

```bash
# Django session/CSRF signing key
python -c "import secrets; print(secrets.token_urlsafe(64))"

# Field-level encryption key (apps/config/encrypted_fields.py)
python -c "import secrets; print(secrets.token_urlsafe(32))"

# PII tokenization salt (apps/ai/tokenizer.py)
python -c "import secrets; print(secrets.token_hex(32))"
```

Save these somewhere safe (1Password, a `.env.production` you keep locally and never commit). You'll paste them into Railway env vars in step 4.

---

## 2. Railway project setup

1. Go to [railway.app/new](https://railway.app/new) → **Deploy from GitHub repo**.
2. Pick your LORA repo. Railway auto-detects the `Dockerfile` and starts the first build immediately. **The first build will fail** because env vars aren't set yet — that's expected. Click "Stop" if needed; we'll configure first.
3. Inside the project, click **+ New** → **Database** → **PostgreSQL**. Railway provisions it in ~30 seconds and automatically sets the `DATABASE_URL` env var on your web service. No manual wiring.

---

## 3. Required environment variables

In Railway → your web service → **Variables** tab, add each of these. (Don't add `DATABASE_URL` — Postgres add-on sets it for you.)

| Variable | Value | Notes |
|---|---|---|
| `SECRET_KEY` | (from step 1) | Django signing |
| `ENCRYPTION_KEY` | (from step 1) | Field-level encryption |
| `PII_TOKENIZATION_SALT` | (from step 1) | AI client tokenizer |
| `DEBUG` | `False` | Must be `False` in production |
| `ALLOWED_HOSTS` | `lora.yourcompany.com` | Comma-separated; add the Railway-provided URL too while testing |
| `CSRF_TRUSTED_ORIGINS` | `https://lora.yourcompany.com` | Comma-separated; MUST include your real domain (with `https://`) or login/forms get a 403. Add the Railway `*.up.railway.app` URL too while testing. |
| `MEDIA_ROOT` | `/app/media` | Set this to the mount path of a Railway Volume so uploaded evidence images persist across redeploys (see "Persistent media" below). |
| `AI_API_BASE` | `https://api.deepseek.com/v1` | Or your chosen provider |
| `AI_API_KEY` | (your DeepSeek/Qwen API key) | Treat as a secret |
| `AI_API_MODEL` | `deepseek-chat` | Or your chosen model |
| `AI_VALIDATION_STRICT` | `True` | Production should fail-loud on bad LLM output |
| `AI_TOKENIZER_BACKEND` | `regex` | Default |
| `AI_PHONE_DEFAULT_REGION` | `US` | |
| `AI_PHONE_FALLBACK_REGIONS` | `GB,FR,DE,IT,ES,JP` | Comma-separated |
| `IMAP_HOST` | (your IMAP host) | |
| `IMAP_USER` | (your IMAP user) | |
| `IMAP_PASS` | (your IMAP password / app password) | Treat as a secret |
| `ZENDESK_SUBDOMAIN` | (your subdomain, e.g., `mycompany`) | |
| `ZENDESK_TOKEN` | (your Zendesk API token) | Treat as a secret |
| `ZENDESK_EMAIL` | (your Zendesk admin email) | |
| `PAYPAL_CLIENT_ID` | (your PayPal client ID) | |
| `PAYPAL_SECRET` | (your PayPal secret) | Treat as a secret |
| `PAYPAL_MODE` | `live` | Or `sandbox` during testing |

After saving, Railway redeploys automatically.

---

## 4. First successful deploy

1. Watch the **Deploy Logs** in Railway. Expect ~5–8 minutes for the first build (Playwright browser download is slow).
2. Look for these signs of health near the end of the log:
   - `Applying ... OK` (Django migrations ran cleanly)
   - `Listening at: http://0.0.0.0:XXXX` (gunicorn started)
   - No tracebacks
3. Click the auto-generated Railway URL (`*.up.railway.app`) — you should see your LORA login page.
4. Log in as a manager (you may need to create the first user via Railway's "Run a command" shell: `python manage.py createsuperuser`, then update the user's role to `MANAGER` via the admin).

---

## 5. Pointing your subdomain

1. In Railway → web service → **Settings** → **Domains** → **+ Custom Domain**.
2. Enter `lora.yourcompany.com`. Railway shows you a CNAME record to add.
3. In your DNS provider (Cloudflare, Namecheap, GoDaddy, whatever), add the CNAME record exactly as shown.
4. Wait 1–10 minutes for DNS propagation. Railway issues a free Let's Encrypt SSL cert automatically once propagation completes.
5. Update Railway's `ALLOWED_HOSTS` env var to include only the subdomain (you can remove the temporary `*.up.railway.app` entry).

---

## 6. Webhook configuration

Once the subdomain is live with HTTPS, update the external services:

### Zendesk

1. Admin → Apps and Integrations → Webhooks → **+ Create webhook**
2. Endpoint URL: `https://lora.yourcompany.com/api/integrations/zd/claim-webhook/`
3. Add an `X-Webhook-Secret` header set to the value of `SystemSettings.sidebar_secret_token` (set via `/admin/`).
4. Trigger condition: ticket status changes to "Investigation Initiated" (custom status ID `11688538967068`).
5. Repeat for the refund-status and refund-webhook endpoints (see `docs/API.md`).

### PayPal

1. PayPal developer dashboard → Webhooks → **Add webhook**.
2. URL: `https://lora.yourcompany.com/api/integrations/paypal/dispute-webhook/`
3. Subscribe to `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, `PAYMENT.CAPTURE.REFUNDED`.
4. Copy the webhook ID into `SystemSettings.paypal_webhook_id` via `/admin/`.

---

## 7. Verifying the scheduler runs

The IMAP poller runs in-process every 3 minutes. To confirm it's running:

1. Railway → web service → **Logs** (live tail).
2. Wait 3 minutes after a fresh deploy. You should see log lines like:
   ```
   INFO apps.communications.services Email scheduler tick — fetched N new messages
   ```
3. If you don't see them, check `/admin/config/servicestatus/` in the running app — there's a `scheduler` row that shows status + last run.
4. The scheduler can be paused/started from Manager → Configuration → Scheduler Control.

**Important:** if you ever scale Railway to more than 1 replica, the scheduler will run multiple times per tick and you'll fetch each email N times. Don't scale up without moving the scheduler to a dedicated worker process.

---

## 7b. Persistent media (uploaded evidence images)

Railway's container disk is **ephemeral** — files written by the app are wiped on every redeploy. Uploaded claim-evidence images must live on a **Railway Volume** (a persistent disk) or they'll vanish.

1. Railway → your **app** service → **Settings** → **Volumes** → **+ New Volume**.
2. Set the **mount path** to `/app/media`.
3. Add the env var `MEDIA_ROOT=/app/media` (matches the mount path). Save → redeploy.

The app serves media through a **login-protected** route in production (`lora_app/urls.py`), so evidence images are only viewable by authenticated staff — `django.views.static.serve` wrapped in `login_required`. This is adequate for an internal, low-traffic tool.

**Scale-up path (later):** for high traffic or multi-replica, move media to object storage (Cloudflare R2 or AWS S3) via `django-storages`. A Volume binds you to a single replica (same constraint as the scheduler above), which is fine at current scale.

---

## 8. Monitoring usage and cost

- Railway dashboard → your project → **Metrics** shows CPU, RAM, network usage.
- Railway dashboard → top right → **Usage** shows your month-to-date spend.
- **Set a usage alert** at ~$30/month so a runaway loop doesn't surprise-bill you. Settings → Notifications → Spending alerts.

Realistic monthly cost for this app at low traffic: **$15–25**.

---

## 9. CI gating (recommended)

Once GitHub Actions is green on `main`:

1. Railway → web service → **Settings** → **Service** → toggle **"Wait for GitHub status checks"**.
2. Now if CI fails, Railway holds the deploy. Bad code never reaches production.

---

## 10. Day-2 operations

### Deploy a code change
Push to `main` → CI runs → Railway deploys → live in ~5 minutes. No manual steps.

### Rollback a bad deploy
Railway dashboard → **Deployments** → pick a previous deploy → **Redeploy**. ~2 minutes back to the prior version.

### Run a one-off command (migrations, createsuperuser, etc.)
Railway dashboard → web service → **Shell** tab → `python manage.py <command>`.

### Add an env var
Variables tab → add → Railway redeploys with the new value automatically.

### Database backups
Railway's managed Postgres takes daily backups automatically. Restore via dashboard → Postgres add-on → Backups.

---

## 11. Zendesk sidebar app

The agent sidebar app (AI briefing + drafts + claim-scoped chat) lives in [`zendesk_app/`](../zendesk_app/) and **deploys separately** from this Django backend. The backend ships to Railway on `git push`; the app ships to Zendesk via `zcli`. They are independent — changing the app does not redeploy LORA, and vice versa.

> **Installed 2026-06-11** as a private app on `airportlf.zendesk.com`, app_id `1260824` (kept in `zendesk_app/zcli.apps.config.json`). Day-to-day: backend changes need only `git push`; app-shell changes need `zcli apps:update`.

**One-time install** (needs a Zendesk plan that allows private apps — Support **Team** and up):
```bash
npm install -g @zendesk/zcli
cd zendesk_app
zcli login -i        # authenticate to your Zendesk subdomain
zcli apps:create     # packages + uploads the private app
```
Then in Zendesk **Admin → Apps**, set the two settings: `lora_base_url` (`https://lora.airportlostfound.com`) and `sidebar_secret_token` (the same value as `SystemSettings.sidebar_secret_token` in LORA).

**Updating after a code change** (the default — manual and deliberate):
```bash
cd zendesk_app && zcli apps:update
```
⚠️ `zcli apps:update` is **immediate for all agents** — there is no staging environment. Run it intentionally.

**Optional automation (later):** a GitHub Actions workflow can run `zcli apps:update` on push using repo secrets (`ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN`). Because updates hit every agent instantly, gate it behind a release tag (e.g. run only on `v*` tags), not on every commit to `main`. See [`zendesk_app/README.md`](../zendesk_app/README.md) for the full dev/install/update workflow.

---

## Troubleshooting

**Build fails on Playwright install**
- Increase the build timeout in Railway settings (default is usually enough; first build is the longest).
- Check the build log for "out of memory" — Playwright needs ~2 GB during install. Hobby plan should handle it; if not, temporarily bump RAM.

**App boots but the scheduler doesn't run**
- Check `/admin/config/servicestatus/` for the `scheduler` row.
- Confirm `IMAP_HOST`, `IMAP_USER`, `IMAP_PASS` env vars are set correctly.
- Manager → Configuration → Scheduler Control → click "Start".

**Migrations fail at startup**
- Most often: a new migration depends on a column from a prior migration that didn't apply. Check Railway's deploy log for the exact migration name that failed.
- Roll back deployment, fix the migration locally, push again.

**HTTPS not working on custom domain**
- Wait longer for DNS propagation (up to 24h in worst case, usually <10 min).
- Use `dig CNAME lora.yourcompany.com` to confirm the CNAME points at Railway.
- Railway's Domains tab shows cert issuance status.

**500 errors with no detail in logs**
- `DEBUG` is probably set to `False` (correct for prod) — set up Django logging to send tracebacks to a service like Sentry, or temporarily set `DEBUG=True` to see them (NEVER leave on long-term).
