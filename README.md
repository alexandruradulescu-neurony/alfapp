# LORA - Lost Object Recovery Automation

## Complete Documentation

**Version:** 1.1.0
**Django:** 5.x (Python 3.10+)
**Frontend:** Bootstrap 5 (CDN)
**API:** Django REST Framework

---

## Table of Contents

1. [Overview](#overview)
2. [Business Workflow](#business-workflow)
3. [Quick Start](#quick-start)
4. [Project Structure](#project-structure)
5. [Configuration](#configuration)
6. [User Roles & Authorization](#user-roles--authorization)
7. [Frontend Views](#frontend-views)
8. [API Reference](#api-reference)
9. [Services](#services)
10. [Database Models](#database-models)
11. [Dispute Management](#dispute-management)
12. [Scheduled Tasks](#scheduled-tasks)
13. [Security Notes](#security-notes)
14. [Troubleshooting](#troubleshooting)

---

## Overview

LORA (Lost Object Recovery Automation) manages lost luggage claims end-to-end, integrating with:

- **Email (IMAP)** — Automatic inbox processing with AI-powered analysis
- **Zendesk** — Ticket creation, comment syncing, sidebar widget for agents
- **PayPal** — Dispute webhook handling, evidence submission, claim acceptance
- **Qwen AI** — Email categorization, sentiment analysis, suggested actions
- **WeasyPrint** — Proof of Work PDF generation with evidence gallery
- **Playwright** — Zendesk ticket screenshot capture for dispute evidence

---

## Business Workflow

1. **Client submits claim** via website form or external channel
2. **Zendesk ticket created** (manually or via API) and linked to claim
3. **Agent works the case** — updates status, uploads evidence, generates email aliases
4. **Shared inbox monitored** — IMAP service fetches emails, AI analyzes them, posts summaries to Zendesk
5. **Dispute arrives** (PayPal webhook) — matched to claim, Zendesk ticket linked automatically
6. **Manager handles dispute** — captures screenshots, generates documents, submits evidence to PayPal
7. **Resolution** — claim status updated based on outcome

---

## Quick Start

### Prerequisites

- Python 3.10+
- pip
- GTK+ libraries (for WeasyPrint PDF generation, optional)

### Installation

```bash
# 1. Create virtual environment
py -3.10 -m venv venv
venv\Scripts\activate       # Windows
source venv/bin/activate    # Linux/Mac

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and configure environment
copy .env.example .env      # Windows
cp .env.example .env        # Linux/Mac
# Edit .env with your credentials

# 4. Run migrations
py -3.10 manage.py migrate

# 5. Create sample data (optional)
py -3.10 manage.py shell < scripts/create_sample_data.py

# 6. Start development server
py -3.10 manage.py runserver
```

### Default Login Credentials (development only)

| Username | Password | Role |
|----------|----------|------|
| admin | admin123 | MANAGER (Superuser) |
| manager1 | password123 | MANAGER |
| agent1 | password123 | AGENT |

### Access Points

- **Frontend:** http://127.0.0.1:8000/login/
- **Django Admin:** http://127.0.0.1:8000/admin/
- **API Base:** http://127.0.0.1:8000/api/

---

## Project Structure

```
alf-app/
├── manage.py
├── requirements.txt
├── .env.example
├── .gitignore
│
├── lora_app/                        # Project configuration
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
│
├── templates/                       # Django templates
│   ├── base.html                   # Base layout with navbar
│   ├── base_auth.html              # Auth pages layout
│   ├── login.html
│   ├── agent/                      # Agent role templates
│   │   ├── dashboard.html
│   │   ├── claims.html
│   │   ├── claim_detail.html
│   │   ├── emails.html
│   │   └── email_detail.html
│   └── manager/                    # Manager role templates
│       ├── dashboard.html
│       ├── claims.html
│       ├── settings.html
│       ├── users.html
│       ├── disputes.html
│       ├── dispute_detail.html
│       └── dispute_edit_document.html
│
├── scripts/                         # One-time / utility scripts
│
├── media/                           # Uploaded files (gitignored)
│   └── evidence/
│
└── apps/
    ├── users/                       # Authentication & frontend views
    │   ├── models.py               # Custom User model (role field)
    │   ├── views.py                # All frontend dashboard views
    │   ├── urls.py                 # Frontend URL routing
    │   └── decorators.py           # @agent_required, @manager_required
    │
    ├── claims/                      # Claims data & API
    │   ├── models.py               # Claim, ClaimEvidence
    │   ├── serializers.py          # DRF serializers
    │   ├── views.py                # DRF ViewSets
    │   └── urls.py                 # API URLs
    │
    ├── communications/              # Email processing
    │   ├── models.py               # EmailLog
    │   ├── services.py             # IMAP + AI processing pipeline
    │   ├── tasks.py                # APScheduler job registration
    │   ├── serializers.py          # DRF serializers
    │   ├── views.py                # DRF ViewSets
    │   └── urls.py
    │
    ├── integrations/                # Zendesk integration
    │   ├── services.py             # All Zendesk API functions
    │   ├── views.py                # Sidebar widget endpoint
    │   └── urls.py
    │
    ├── payments/                    # PayPal, disputes, PDF
    │   ├── models.py               # Dispute, DisputeDocument, DisputeScreenshot, DisputeActivityLog
    │   ├── views.py                # PayPal webhook + PDF download API
    │   ├── frontend_views.py       # Dispute management UI (manager)
    │   ├── frontend_urls.py        # Dispute UI URL routing
    │   ├── utils.py                # WeasyPrint PDF generation
    │   ├── document_service.py     # AI document generation for disputes
    │   ├── paypal_disputes_service.py  # PayPal Disputes API client
    │   ├── screenshot_service.py   # Playwright screenshot capture
    │   └── templates/
    │       └── proof_of_work.html  # PDF template
    │
    └── config/                      # System configuration
        ├── models.py               # SystemSettings singleton
        ├── encrypted_fields.py     # Fernet encryption for DB fields
        └── admin.py
```

---

## Configuration

### Environment Variables (.env)

See `.env.example` for all available settings. Key groups:

| Group | Variables | Purpose |
|-------|-----------|---------|
| Django | `DEBUG`, `SECRET_KEY`, `ENCRYPTION_KEY`, `ALLOWED_HOSTS` | Core Django settings |
| Database | `DATABASE_URL` | SQLite (dev) or PostgreSQL (prod) |
| Email/IMAP | `EMAIL_HOST`, `IMAP_HOST`, `IMAP_USER`, `IMAP_PASS` | Email sending and fetching |
| Zendesk | `ZENDESK_SUBDOMAIN`, `ZENDESK_TOKEN`, `ZENDESK_EMAIL` | Zendesk API access |
| PayPal | `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_MODE` | PayPal integration |
| AI | `QWEN_API_BASE`, `QWEN_API_KEY`, `QWEN_MODEL` | Qwen AI for email analysis |
| Timeouts | `API_TIMEOUT`, `IMAP_TIMEOUT`, `ZENDESK_TIMEOUT`, `PAYPAL_TIMEOUT` | Optional, default 30s each |

### SystemSettings (Runtime Configuration)

Accessible via `/manager/settings/` or Django Admin. Stores credentials with Fernet encryption.

**Important:** SystemSettings is the authoritative source for service credentials at runtime. Environment variables in `.env` are used for Django-level settings only (SECRET_KEY, DEBUG, etc.).

Sensitive fields (`imap_pass`, `zd_token`, `paypal_secret`, `paypal_client_id`, `paypal_webhook_id`, `sidebar_secret_token`) are encrypted at rest using a key derived from `ENCRYPTION_KEY` in `.env`.

---

## User Roles & Authorization

### MANAGER

Full system access including:
- All agent capabilities
- Dispute management (view, generate documents, send evidence, accept claims)
- SystemSettings configuration
- User management (create/edit users)
- PDF proof-of-work download

### AGENT

Claims and email access:
- View and work claims **assigned to them** (ownership enforced)
- Update claim status, upload evidence
- View email logs with AI analysis
- Unassigned claims are accessible to all agents

### Authorization Model

- `@agent_required` — allows both AGENT and MANAGER roles
- `@manager_required` — MANAGER only
- Agent views enforce ownership: agents can only modify claims where `assigned_to` is null or matches the current user

---

## Frontend Views

### Authentication

| URL | Description |
|-----|-------------|
| `/login/` | Login with role-based redirect |
| `/logout/` | Logout |

### Agent Views (AGENT + MANAGER)

| URL | Description |
|-----|-------------|
| `/agent/` | Dashboard with claim stats |
| `/agent/claims/` | Claims list with search/filter |
| `/agent/claims/<id>/` | Claim detail with evidence, emails, Zendesk link |
| `/agent/claims/<id>/status/` | Update claim status (POST) |
| `/agent/claims/<id>/upload/` | Upload evidence image (POST) |
| `/agent/emails/` | Email logs with AI analysis |
| `/agent/emails/<id>/` | Email detail with full AI breakdown |

### Manager Views (MANAGER only)

| URL | Description |
|-----|-------------|
| `/manager/` | Manager dashboard with aggregate stats |
| `/manager/claims/` | All claims with PDF download |
| `/manager/settings/` | Edit SystemSettings |
| `/manager/users/` | User management |
| `/manager/disputes/` | Dispute list with status filters |
| `/manager/disputes/<id>/` | Dispute detail with documents, screenshots, activity log |
| `/manager/disputes/<id>/generate-documents/` | Generate response letter + evidence report (POST) |
| `/manager/disputes/<id>/send-evidence/` | Submit evidence to PayPal (POST) |
| `/manager/disputes/<id>/accept-claim/` | Accept dispute / issue refund (POST) |
| `/manager/disputes/<id>/capture-screenshots/` | Capture Zendesk screenshots (POST) |
| `/manager/documents/<id>/edit/` | Edit document content |
| `/manager/documents/<id>/accept/` | Mark document as accepted (POST) |
| `/manager/documents/<id>/delete/` | Delete document (POST) |

---

## API Reference

### Claims API (`/api/claims/`)

| Method | Endpoint | Permission | Description |
|--------|----------|------------|-------------|
| GET | `/api/claims/` | Any auth | List claims (paginated) |
| POST | `/api/claims/` | MANAGER | Create claim |
| GET | `/api/claims/{id}/` | Any auth | Claim detail |
| PUT/PATCH | `/api/claims/{id}/` | MANAGER | Update claim |
| DELETE | `/api/claims/{id}/` | MANAGER | Delete claim |
| PATCH | `/api/claims/{id}/update_status/` | Any auth | Update status only |
| GET | `/api/claims/{id}/proof-of-work/` | MANAGER | Download PDF |

### Evidence API (`/api/evidence/`)

| Method | Endpoint | Permission | Description |
|--------|----------|------------|-------------|
| GET | `/api/evidence/` | Any auth | List evidence |
| POST | `/api/evidence/` | Any auth | Upload evidence |
| DELETE | `/api/evidence/{id}/` | MANAGER | Delete evidence |

### Communications API (`/api/communications/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/communications/email-logs/` | List email logs |
| GET | `/api/communications/email-logs/{id}/` | Email log detail |

### Integrations API (`/api/integrations/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/integrations/zd/info/?email=` | Zendesk sidebar data |
| POST | `/api/integrations/zd/sync/` | Create Zendesk ticket for claim |

### Payments API (`/api/payments/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/payments/paypal/webhook/` | PayPal webhook receiver |

---

## Services

### Email Processing Pipeline (`apps/communications/services.py`)

1. Connects to IMAP using SystemSettings credentials
2. Fetches up to 20 UNSEEN emails
3. For each email:
   - Extracts alias from To/CC headers for claim matching
   - Falls back to sender email matching against `Claim.client_email`
   - Calls Qwen AI for categorization, sentiment, summary, and suggested action
   - Saves `EmailLog` record linked to claim
   - Posts AI summary to Zendesk as internal note (only for alias-matched emails)
   - Marks email as SEEN in IMAP

**AI Analysis** uses structured message roles to prevent prompt injection — email content is passed in the `user` role, separate from system instructions.

### Zendesk Integration (`apps/integrations/services.py`)

- `post_zendesk_comment()` — Add internal/public note to ticket (uses PUT on tickets endpoint)
- `fetch_zendesk_comments()` — Get ticket comments
- `fetch_zendesk_ticket()` / `fetch_zendesk_ticket_full()` — Get ticket data (full version includes custom fields)
- `create_zendesk_ticket()` — Create new ticket
- `update_zendesk_ticket_status()` — Change ticket status
- `search_zendesk_tickets()` — Search tickets by query
- `search_zendesk_ticket_for_dispute()` — Find matching ticket for a PayPal dispute (sorted by recency)

### PayPal Integration

**Webhook** (`apps/payments/views.py`):
- Verifies PayPal webhook signature
- Handles `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, `CUSTOMER.DISPUTE.RESOLVED`
- Matches disputes to claims by buyer email
- Links to Zendesk tickets automatically

**Disputes API** (`apps/payments/paypal_disputes_service.py`):
- `provide_evidence()` — Submit documents to PayPal
- `accept_claim()` — Accept dispute and issue refund

**Screenshots** (`apps/payments/screenshot_service.py`):
- Uses Playwright to capture Zendesk ticket screenshots for dispute evidence

**Documents** (`apps/payments/document_service.py`):
- AI-generated response letters and evidence reports
- PDF output via WeasyPrint

---

## Database Models

### User (`apps/users/models.py`)

Extends `AbstractUser` with `role` field: `MANAGER` or `AGENT`.

### Claim (`apps/claims/models.py`)

| Field | Type | Notes |
|-------|------|-------|
| `client_email` | EmailField | Indexed, not unique (same person can have multiple claims) |
| `status` | CharField | Received, Searching, Found, Shipped, Disputed |
| `zd_ticket_id` | CharField | Linked Zendesk ticket |
| `flight_details` | TextField | Flight information |
| `assigned_to` | ForeignKey(User) | Nullable, agent assignment |

### ClaimEvidence (`apps/claims/models.py`)

Evidence images linked to claims. Stored in `media/evidence/`.

### EmailLog (`apps/communications/models.py`)

| Field | Type | Notes |
|-------|------|-------|
| `claim` | ForeignKey(Claim) | Nullable |
| `from_email` | EmailField | Sender |
| `subject` | CharField | |
| `body` | TextField | |
| `ai_summary` | TextField | Qwen AI output |
| `ai_category` | CharField | OBJECT_FOUND, STATUS_UPDATE, etc. |
| `sentiment` | CharField | Positive, Neutral, Frustrated, Urgent |
| `action_required` | BooleanField | |

### Dispute (`apps/payments/models.py`)

| Field | Type | Notes |
|-------|------|-------|
| `paypal_dispute_id` | CharField | Unique PayPal ID |
| `claim` | ForeignKey(Claim) | Nullable — disputes may arrive before claim match |
| `zd_ticket_id` | CharField | Auto-linked Zendesk ticket |
| `status` | CharField | RECEIVED through RESOLVED_WON/LOST |
| `reason` | CharField | ITEM_NOT_RECEIVED, UNAUTHORIZED, etc. |
| `buyer_email` | EmailField | |
| `amount` | DecimalField | Disputed amount |

### DisputeDocument, DisputeScreenshot, DisputeActivityLog

Supporting models for dispute evidence management. Documents have versioning and acceptance workflow (DRAFT → ACCEPTED). Activity log tracks all dispute actions.

### SystemSettings (`apps/config/models.py`)

Singleton (pk=1) storing all runtime configuration. Sensitive fields use Fernet encryption via custom `EncryptedCharField`. Retrieved with `SystemSettings.get_instance()`.

---

## Scheduled Tasks

**Location:** `apps/communications/tasks.py`

| Task | Schedule | Description |
|------|----------|-------------|
| `process_incoming_emails` | Every 3 minutes | Fetch and process IMAP inbox |

Tasks are registered via APScheduler. Enable in `apps/communications/apps.py` by uncommenting the `ready()` method.

---

## Security Notes

- **ENCRYPTION_KEY**: Separate from SECRET_KEY. Used for encrypting sensitive DB fields (credentials in SystemSettings). Changing this key requires re-entering all credentials via the settings UI.
- **Prompt injection protection**: AI email analysis uses structured message roles — email content is never interpolated into system prompts.
- **Agent authorization**: Agents can only modify claims assigned to them or unassigned claims.
- **CSRF protection**: All POST forms use Django's CSRF middleware.
- Set `DEBUG=False` and use HTTPS in production.
- Restrict `ALLOWED_HOSTS` to actual domain(s).
- Use strong, unique values for `SECRET_KEY` and `ENCRYPTION_KEY`.
- Rotate API keys and tokens regularly.
- Never commit `.env` (included in `.gitignore`).

---

## Troubleshooting

### WeasyPrint: "cannot load library 'libgobject-2.0-0'"

Install GTK+ libraries:
- **Windows:** https://github.com/nickvdyck/weasyprint-win/releases
- **macOS:** `brew install pango glib`
- **Linux:** `apt-get install libpango-1.0-0 libpangocairo-1.0-0`

### IMAP Connection Failed

1. Verify credentials in SystemSettings (`/manager/settings/`)
2. Use app-specific password for Gmail (not regular password)
3. Ensure IMAP is enabled in email provider settings

### PayPal Webhook Returns 400

1. Check PayPal credentials in SystemSettings
2. Verify webhook ID matches PayPal dashboard configuration
3. Confirm mode matches (sandbox vs live)

### Encrypted Fields Show Garbage

If `ENCRYPTION_KEY` was changed, existing encrypted values can't be decrypted. Re-enter credentials via `/manager/settings/` or Django Admin.

### Zendesk Comments Not Posting

Verify `zd_subdomain`, `zd_token`, and `zd_email` in SystemSettings. The token must belong to an admin or agent with ticket update permissions.

---

## License

Internal use only - LORA Project
