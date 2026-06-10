# Zendesk Agent Sidebar App — Design Spec

**Status:** Draft for review
**Date:** 2026-06-10
**Owner:** Alex (alexandru.radulescu@neurony.ro)
**Related:** [[project-roadmap]] (sidebar app was a planned mid-term feature), [[project-llm-trust-boundary]], [[project-terminology]] ("agent" = human call-center worker)

---

## 1. Context

Call-center agents work tickets inside Zendesk. When an agent picks up a ticket they haven't touched in days (e.g., a client calls for an update), they need to get up to speed in seconds. This feature adds a **panel in the Zendesk ticket sidebar** that:

1. Shows an **AI-written briefing** — a 2-3 sentence "where this claim stands" plus suggested next steps.
2. Shows **key facts** — status, deadline, unresolved emails, disputes.
3. Provides an **ask-the-AI chat box**, scoped to the current ticket's claim.

**What already exists in the repo (verified 2026-06-10):**
- A backend endpoint `GET /api/integrations/zd/info/` ([apps/integrations/views.py](../../apps/integrations/views.py) `ZendeskSidebarView`) that returns enriched claim data (status, email stats, disputes, submission tracking).
- `ZendeskSidebarAuth` — validates an `Authorization` header against `SystemSettings.sidebar_secret_token`, with IP-based rate limiting on failures.
- `AgentChatService` ([apps/agent/services.py](../../apps/agent/services.py)) — answers natural-language questions about claims, already migrated to the `AIClient` security layer.
- The `apps/ai/` AI security layer — PII tokenization + prompt fencing + schema validation.

**What does NOT exist:** any Zendesk App Framework (ZAF) frontend — no `manifest.json`, no iframe app. The panel itself was never built.

---

## 2. Architecture principle (the key decision)

**LORA = secure AI gateway + action engine + cross-system data. Zendesk app = UI + ticket-data source.**

The Zendesk app already has the full ticket (subject, description, comments, custom fields, requester) client-side, so LORA does **not** re-fetch ticket content. Instead:

- The app **reads the ticket locally** and **sends its content to LORA** for AI work.
- LORA's distinct value is what Zendesk can't do: (a) hold the AI provider key, (b) **tokenize PII before the AI provider sees it** (the trust-boundary requirement — a transparent proxy would leak PII and is explicitly rejected), (c) blend in **cross-system data** the ticket lacks (refund records, PayPal disputes, AI email-categorization), and (d) — in future — **take actions** (browser-use form-filling, document generation, PayPal submission, email).

This build covers briefing + facts + chat. **Action buttons are out of scope** (roadmap's later items) but the architecture is designed to receive them as additional LORA endpoints + buttons in the same app.

---

## 3. Goals

1. A Zendesk ticket-sidebar app (ZAF v2) with a **tabbed** layout: **Briefing** tab (AI summary + next steps + key facts) and **Chat** tab (ask-the-AI, scoped to the current claim).
2. Two new LORA endpoints — **briefing** and **chat** — that accept ticket content, enrich with LORA's own claim data, run through the `AIClient` (PII-safe), and return validated results.
3. Reuse: `AgentChatService` for chat, the `/zd/info/` enrichment data, `ZendeskSidebarAuth` for auth, `AIClient` for PII protection.
4. Token never exposed in client JS; no CORS complexity (both via Zendesk's proxied requests).

## 4. Non-goals

- **Action buttons** (browser-use form-fill, dispute-doc generation, PayPal submission, sending email) — future work; architecture leaves room but this spec does not build them.
- Public/marketplace distribution — this is a **private app** for the company's own Zendesk.
- Per-agent identity/audit in the AI calls — a single shared `sidebar_secret_token` is used (internal tool; acceptable). Revisit if per-agent audit is needed.
- Rewriting `AgentChatService` or the AI layer — reused as-is.

---

## 5. Backend: two new endpoints

Both live in `apps/integrations/` alongside the existing sidebar code, both authed via `ZendeskSidebarAuth` (the `sidebar_secret_token`), both run their AI through `apps/ai/AIClient` so PII is tokenized.

### 5.1 Briefing — `POST /api/integrations/zd/briefing/`

**Request body** (sent by the app from Zendesk-side ticket data):
```json
{
  "ticket_id": "12345",
  "requester_email": "client@example.com",
  "subject": "...",
  "description": "...",
  "comments": ["...", "..."]
}
```

**Processing:**
1. Resolve the `Claim` by `ticket_id` (zd_ticket_id). If none exists, degrade gracefully — briefing uses ticket content only and notes "no linked claim record."
2. **Enrich**: gather LORA-only data for that claim — refund status, PayPal disputes, AI email-categorization stats, financial/dispute status, deadline (reuse the logic behind `/zd/info/`).
3. Build the AI context from ticket content + enrichment; call `AIClient.complete()` with a new `BriefingSummary` schema. Untrusted ticket text is fenced; PII tokenized.
4. Return everything the Briefing tab needs in one response.

**Response** (validated `BriefingSummary` + facts):
```json
{
  "summary": "Client's bag lost on UA123 (JFK->LAX). Searching 9 days; airport replied 'not located' 2 days ago. Refund not requested.",
  "next_steps": ["Chase airport lost & found", "Send the 11-day client update"],
  "facts": {
    "status": "Searching",
    "deadline": "2026-07-01",
    "emails_total": 4,
    "emails_unresolved": 1,
    "disputes_active": 0
  }
}
```

New Pydantic schema in `apps/ai/schemas.py`:
```python
class BriefingSummary(BaseModel):
    summary: str = Field(max_length=600)
    next_steps: list[str] = Field(default_factory=list, max_length=6)
```
(`facts` is assembled by the view from structured data, not the LLM, so it isn't part of the LLM-validated schema.)

### 5.2 Chat — `POST /api/integrations/zd/chat/`

**Request body:**
```json
{
  "ticket_id": "12345",
  "message": "what did the airline say?",
  "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
}
```

**Processing:**
1. Resolve the `Claim` by `ticket_id`. The chat is **locked to this claim** — it cannot query other claims (unlike the manager chat).
2. Call `AgentChatService` scoped to that claim (reuse existing service; pass the resolved claim so it does not do its own free-text claim detection).
3. Return the existing `ChatAnswer` shape (`{answer, sources}`), PII un-tokenized by `AIClient`.

If no claim is linked to the ticket, return a friendly message ("No LORA claim is linked to this ticket yet").

### 5.3 Reuse note

The existing `GET /zd/info/` endpoint stays as the **enrichment-data builder** — its claim/refund/dispute/email-stats logic is reused by the briefing view (extract into a shared helper if cleaner). It may also remain callable directly for debugging.

---

## 6. Frontend: the Zendesk app (ZAF v2)

New top-level folder `zendesk_app/` (not part of the Django app; packaged and uploaded to Zendesk separately).

```
zendesk_app/
├── manifest.json          # declares the app, ticket_sidebar location, settings
├── assets/
│   ├── iframe.html        # the panel shell (tabs)
│   ├── app.js             # ZAFClient logic + render + calls to LORA
│   ├── styles.css         # narrow-sidebar styling
│   └── logo.png
└── translations/
    └── en.json
```

**Behavior:**
- `ZAFClient.init()`; read `ticket.id`, `ticket.requester.email`, `ticket.subject`, `ticket.description`, `ticket.comments` via the ZAF API.
- On load: call the **briefing** endpoint, render the **Briefing tab** (summary + next steps + facts). Show a loading state, then the result; on error show a friendly "couldn't load briefing — retry" with the agent able to still use Chat.
- **Chat tab**: text box + send; each message calls the **chat** endpoint with accumulated history; render the conversation.
- **Tabbed layout** (chosen 2026-06-10): "Briefing" and "Chat" tabs; Briefing is default.

**Settings (manifest `parameters`):**
- `lora_base_url` (text) — e.g. `https://lora.airportlostfound.com`
- `sidebar_secret_token` (text, **`secure: true`**) — the shared secret.

**Calling LORA — `client.request()` (proxied), NOT `fetch`:**
- The app uses ZAF's `client.request()` so Zendesk makes the HTTP call **server-side**. This (a) injects the `secure` setting token via `{{setting.sidebar_secret_token}}` so it never appears in client JS, and (b) avoids browser CORS entirely (no cross-origin call from the browser).

---

## 7. End-to-end data flow

```
Agent opens ticket
  -> ZAF app reads ticket {id, requester email, subject, description, comments}
  -> app: client.request() POST {lora_base_url}/api/integrations/zd/briefing/
        (Zendesk proxies server-side, adds Authorization: {{secure token}})
  -> LORA: ZendeskSidebarAuth check -> resolve Claim by ticket_id
        -> enrich (refunds, disputes, email stats) -> AIClient (tokenize PII,
           fence ticket text) -> DeepSeek/Qwen -> validate BriefingSummary
           -> untokenize -> return {summary, next_steps, facts}
  -> app renders Briefing tab

Agent types a question in Chat tab
  -> app: client.request() POST .../zd/chat/ {ticket_id, message, history}
  -> LORA: auth -> resolve Claim -> AgentChatService (claim-scoped) via AIClient
        -> return {answer, sources}
  -> app appends to conversation
```

---

## 8. Security

- **Token**: `sidebar_secret_token` stored as a Zendesk **secure setting**; injected server-side by Zendesk; never in client JS. Validated by `ZendeskSidebarAuth`; failed attempts rate-limited by IP (existing behavior).
- **No CORS surface**: all calls go through `client.request()` (Zendesk server-side), so LORA does not need to allow a browser cross-origin; `X_FRAME_OPTIONS = 'DENY'` and the CSP stay untouched (the app is hosted by Zendesk, not embedding LORA in a frame).
- **PII**: briefing + chat run through `AIClient`; client name/email/phone/aliases tokenized before the AI provider sees them, un-tokenized on the way back. Consistent with [[project-llm-trust-boundary]].
- **Claim-scoping**: the chat endpoint resolves the claim from `ticket_id` and constrains `AgentChatService` to it — an agent viewing one ticket cannot surface another client's data.

---

## 9. Testing

- **Backend (pytest):** briefing + chat endpoints — auth required (401 without token), claim resolution + graceful no-claim path, claim-scoping (chat can't reach another claim), `BriefingSummary` schema validation, and that PII is tokenized before the (mocked) AI call. Follows the patterns in `apps/integrations/tests/`.
- **Frontend (manual):** small JS UI tested live via `zcli apps:server` against the real Zendesk. No JS test framework — not worth it for a panel this size.

---

## 10. Packaging, dev, and install

- **Local dev:** Zendesk's `zcli` — `zcli apps:server` runs the app against the real Zendesk in dev mode (live edit, no upload).
- **Install:** `zcli apps:create` packages + uploads as a **private app**. Configure the two settings (`lora_base_url`, `sidebar_secret_token`) in the app's admin page.
- **Prerequisite:** a Zendesk plan that allows private/custom apps — **Support Team plan and up** (all Suite plans). Confirm before install.

---

## 11. Prerequisites & open items

1. **Confirm Zendesk plan** supports private apps (Team+). Gating for install, not for building.
2. **Production LORA URL** is `https://lora.airportlostfound.com` (the app's `lora_base_url`). Until the custom domain is live, the Railway URL can be used for testing.
3. **Set `sidebar_secret_token`** in `SystemSettings` (and as the app's secure setting) — generate a strong shared secret.
4. Decide the briefing's tone/length in the prompt during implementation (the schema caps summary at 600 chars, ≤6 next steps).
