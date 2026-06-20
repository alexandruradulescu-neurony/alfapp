# Client-reply risk webhook — design

Date: 2026-06-21
Status: Approved (brainstorm) → implementation

## Problem
LORA re-reads a ticket and re-assesses client sentiment/risk only on: a status change (webhook), a Check-Email, a manual Regenerate, or claim creation. When a client replies **inside Zendesk** (a public comment, not an email to LORA's alias), none of those fire — so the reply is never assessed, the at-risk flag never rises, and the at-a-glance summary goes stale. Client sentiment/risk on in-Zendesk replies is invisible.

## Goal
When the client posts a public reply in Zendesk, LORA re-reads the live thread and re-runs the **existing** summary + risk assessment, so a hostile / refund-demand / chargeback / negative reply raises the at-risk flag (which already pauses automated client updates) and the summary reflects the reply. **No new data is stored** — we do not duplicate Zendesk.

## Design
- **Trigger:** a Zendesk trigger fires a webhook the moment the requester (client) adds a public comment, POSTing `{"ticket_id": "<id>"}` to a new LORA endpoint. Auth: the existing `X-Webhook-Secret` shared-secret pattern (`verify_webhook_secret`).
- **Endpoint:** `POST /api/integrations/zd/client-reply-webhook/` → `ZendeskClientReplyWebhookView` (thin: verify secret, parse `ticket_id`, find claim, delegate). Mirrors the existing claim-webhook view.
- **Core (module fn `assess_client_reply(claim)`):**
  1. Fetch the live ticket + comments (`fetch_zendesk_ticket` + `fetch_zendesk_comments`).
  2. **Guard:** confirm the latest PUBLIC comment is from the client (author email == `claim.client_email`). If it is clearly an agent/institution comment, no-op — this assessment is for client sentiment, and the Zendesk trigger is the primary filter; the guard is defense-in-depth against a misconfigured trigger.
  3. Call `refresh_claim_summary(claim, ticket_data)` — the existing engine that rewrites `claim.ai_summary` AND re-scores risk via `register_risk` (`hostile_language` / `refund_demanded` / `dispute_risk` / `negative_sentiment`, with the chargeback/lawyer/BBB keyword backstop in `merge_risk`).
- **Consequences (existing behavior, unchanged):** an at-risk flag raises the badge/banner and HOLDS automated client updates (the due update is left `SCHEDULED`, not skipped). Acknowledging the risk (`acknowledge_risk`) flips `risk_active` off and the held update sends on the next hourly cron run; a fresh concerning reply re-raises the flag (`register_risk` clears the acknowledgement on a new signal).
- **No new storage:** the reply is read live and assessed; nothing is mirrored into the DB.
- **Resilience:** unknown ticket / no matching claim → 200 no-op; fetch/assess failure → logged, return 200 (no Zendesk retry storm).

## Zendesk trigger recipe (one-time setup by the user)
- **Webhook (connection):** URL `https://alfapp-production.up.railway.app/api/integrations/zd/client-reply-webhook/`, method POST, JSON, header `X-Webhook-Secret: <SystemSettings.sidebar_secret_token>`.
- **Trigger:** Meet ALL conditions — *Ticket is Updated*; *Comment is Public*; *Current user is (End user)*. Action — *Notify active webhook* (above) with body `{"ticket_id": "{{ticket.id}}"}`.

## Files
- `apps/integrations/views/webhooks.py` — new `ZendeskClientReplyWebhookView`, module fn `assess_client_reply()`, helper `_latest_public_comment_author()`.
- `apps/integrations/urls.py` — new route.
- `apps/integrations/tests/` — tests.

## Out of scope (deliberate)
- Mirroring comments or other ticket activity into LORA (we do not duplicate Zendesk).
- Any change to the risk model or the summary engine (reused as-is).

## Testing
- Client public reply → `assess_client_reply` calls `refresh_claim_summary`; risk registered when the reply is negative.
- Latest public comment from an agent → no-op (refresh not called).
- Unknown ticket id → 200 no-op.
- Webhook secret enforced (missing/wrong secret rejected).
