# LORA Claim Assistant — Zendesk App

A ticket-sidebar app for ALF agents: AI briefing, on-demand next steps, one-click
email drafts, and a claim-scoped chat — all backed by LORA.

**Status: INSTALLED in production** on `airportlf.zendesk.com` as a private app
(app_id `1260824`, tracked in `zcli.apps.config.json`, first installed 2026-06-11).

## What the panel does

**Briefing tab**
- AI summary of where the case stands (leads with lifecycle stage: searching / found / retrieval / delivered)
- LORA facts: claim status + status family (new/open/pending/hold/solved), deadline (with urgency coloring), email counts, disputes, next client-update milestone (day 2/5/11/20 cadence — stops once the case reaches a solved/closed status)
- ⚠️ Needs attention: up to 5 unresolved action-required institution emails
- Buttons: **Regenerate** · **Next steps** (generated on demand) · **Client update** / **Institution reply** (AI drafts the email and inserts it into the ticket reply box — the agent reviews and sends; nothing sends automatically) · **Find flight info** (looks the flight up on AeroDataBox, AI-cross-checks it against the client's report — wrong-airport detection, search-focus advice — saves it on the claim and posts an internal note; on not-found it lists likely candidate departures from the stated airport)

**Chat tab**
- Ask anything about the ticket/claim; scoped to THIS ticket's claim only
- Works on tickets without a LORA claim too (answers from the ticket content)
- Can translate institution emails on request ("translate the last email")
- Empty state offers tap-to-ask suggestion chips

**Email tab**
- **Check email now**: checks the shared mailbox for new mail addressed to THIS
  ticket's email alias only — unread, last 2 days, each email processed at most
  once ever (Message-ID dedup). The rest of the inbox is untouched.
- New mail is AI-categorized, logged in LORA, posted on the ticket as an
  internal note, and the ticket gets additive AI tags: `ai_object_found`,
  `ai_object_not_found`, `ai_resubmission_required`, `ai_attention_needed`.
- Works on tickets without a LORA claim too (alias is read from the ticket's
  Email Alias custom field; with a claim the alias is cached on the claim).

## Privacy model (do not break this)

All ticket content goes to LORA, which **tokenizes client PII (names, emails,
phones, claim ids, flights) before any AI provider sees it** and swaps real
values back into what the agent reads. The app sends `requester_name` so LORA
knows which name to protect. AI calls MUST go through `apps/ai/AIClient` on the
LORA side — never a passthrough.

## Endpoints the app calls (all on LORA, sidebar-token auth)

| Endpoint | Purpose | Notes |
|---|---|---|
| `POST /api/integrations/zd/briefing/` | Briefing | default mode returns `{summary, next_steps[], facts{}, attention[]}`; `mode: "next_steps"` returns `{next_steps[]}` only |
| `POST /api/integrations/zd/chat/` | Chat | `{answer, sources[]}`; claim-linked tickets use LORA's AgentChatService, unlinked tickets answer from ticket content |
| `POST /api/integrations/zd/draft/` | Email drafts | `draft_type: "client_update" \| "institution_reply"` → `{body}` |
| `POST /api/integrations/zd/flight-lookup/` | Flight lookup (action button) | `{ticket_id, refresh?}` → `{flight, analysis, cached, note_posted}` or `{error_message, candidates?}`; needs `aerodatabox_api_key` in SystemSettings; posts an internal note on the ticket |
| `POST /api/integrations/zd/email-check/` | Email tab | `{ticket_id}` → `{processed[], already_processed, tags_added[], alias, claimless, capped}`; needs IMAP credentials in SystemSettings; posts internal notes + adds ai_* tags on the ticket |

**Payload the app sends** (built in `assets/app.js` → `ticketContext()`):
`ticket_id`, `subject`, `description`, `requester_email`, `requester_name`,
`ticket_created_at`, and `comments` as `[{author, created_at, public, text}]`
(30 newest, chronological, fetched via the Zendesk REST API with the agent's
session — ZAF's own `ticket.comments` has no timestamps/visibility). Chat adds
`message` + `history`; drafts add `draft_type`; briefing accepts `mode`.

## Auth

`sidebar_secret_token` is a **secure** app setting. Installed apps send it via
Zendesk's proxy (`{{setting.sidebar_secret_token}}` + `secure: true` +
`domainWhitelist` in manifest.json). The value MUST equal
`SystemSettings.sidebar_secret_token` in LORA — change one, change both.
LORA rate-limits failed attempts (~5 min lockout per caller).

## Prerequisites
- Zendesk plan that allows **private apps** (Support **Team** plan and up).
- Node + Zendesk CLI: `npm install -g @zendesk/zcli`
- LORA running with `sidebar_secret_token` set in SystemSettings.

## Local development (live preview, no upload)
```bash
cd zendesk_app
zcli apps:server
# then open a ticket with ?zcli_apps=true appended to the URL
```
Enter the two settings when prompted: `lora_base_url`
(`https://alfapp-production.up.railway.app`) and `sidebar_secret_token`.

> **Note:** the zcli local server does not support Zendesk's secure-settings
> substitution. The app detects this and sends the token you typed at the zcli
> prompt directly instead. The installed app uses the proper secure path.
> Browser gotchas for local preview: allow Local Network Access in
> macOS System Settings → Privacy & Security; Safari blocks localhost apps —
> use Chrome or Firefox.

## Shipping changes to the installed app
```bash
cd zendesk_app
zcli apps:update     # pushes a new version to app_id in zcli.apps.config.json
```
⚠️ Updates are **immediate for all agents** — no staging. Test locally first.
(`zcli login -i` first if the session expired: subdomain `airportlf`, admin
email, Zendesk API token from Admin Center → Apps and integrations → APIs.)

Backend changes (LORA endpoints/prompts) deploy separately: `git push` →
Railway. Most behavior improvements are backend-only and need **no** app update.

## Known gaps / next increments
- Linked-claim chat answers from LORA data only (not the raw ticket text) — fine
  in practice since institutional email lives in LORA, but agent-typed Zendesk
  comments aren't visible to it.
- Update cadence shows the next milestone from claim age; LORA doesn't yet track
  which updates were actually SENT.
- The stored claim summary (`ai_summary`) now refreshes automatically on every
  Zendesk status change (via the claim webhook) — no agent action required.
- Unbuilt (discussed, not picked): open-in-LORA button + copy case summary;
  GitHub Action to auto-run `zcli apps:update` on release tags.
- Action buttons (browser-use form fill, dispute docs, PayPal) extend the same
  pattern: new LORA endpoint + a button here.
