# LORA Claim Assistant — Zendesk App

A ticket-sidebar app: AI briefing + claim-scoped chat, backed by LORA.

## Prerequisites
- Zendesk plan that allows **private apps** (Support **Team** plan and up).
- Node + Zendesk CLI: `npm install -g @zendesk/zcli`
- LORA running with `sidebar_secret_token` set in SystemSettings.

## Local development (live, no upload)
```bash
cd zendesk_app
zcli apps:server
# then in Zendesk: append ?zcli_apps=true to a ticket URL to load the local app
```
Set the two settings when prompted: `lora_base_url` (your LORA URL) and `sidebar_secret_token`.

## First install (upload as a private app)
```bash
cd zendesk_app
zcli login -i        # authenticate to your Zendesk subdomain
zcli apps:create     # packages + uploads, creates the private app
```
Then set the app settings (LORA URL + secret token) in Admin → Apps.

## Updating after changes
```bash
cd zendesk_app
zcli apps:update     # re-packages and pushes the new version to the installed app
```
Note: updates are **immediate for all agents** — there is no staging. Run this deliberately.

## What it talks to
The app sends the ticket's content (subject, description, comments, requester email) to LORA;
LORA enriches it with its own data, scrubs client PII before any AI call, and returns the result.

- `POST /api/integrations/zd/briefing/` → `{ summary, next_steps[], facts{} }`
- `POST /api/integrations/zd/chat/` → `{ answer, sources[] }` (scoped to this ticket's claim)

The `sidebar_secret_token` is a **secure** app setting: it is injected server-side by Zendesk
into the `Authorization` header via `{{setting.sidebar_secret_token}}` and never appears in
client JavaScript.
