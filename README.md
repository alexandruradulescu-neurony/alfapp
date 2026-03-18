# LORA - Lost Object Recovery Automation

**Version:** 1.5.0  
**Framework:** Django 5.2.11 | Python 3.10+  
**UI:** Tailwind CSS 4 + DaisyUI 5

A comprehensive platform for automating lost object recovery claims, dispute management, customer communications, refund management, and Zendesk integration.

---

## 📋 Table of Contents

- [Features](#-features)
- [Tech Stack](#-tech-stack)
- [Architecture](#-architecture)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [API Reference](#-api-reference)
- [Service Monitoring](#-service-monitoring)
- [Security](#-security)
- [Development](#-development)
- [Testing](#-testing)
- [Deployment](#-deployment)

---

## ✨ Features

### Claims Management
- **Automated Workflow**: Status tracking from Received → Searching → Found → Shipped → Disputed
- **Agent Assignment**: Role-based claim ownership with enforcement
- **Evidence Management**: Image upload with validation (MIME type, size, path traversal protection)
- **Email Integration**: Automatic email-to-claim linking via alias matching
- **PDF Generation**: Automated proof-of-work documents
- **AI Summary**: LLM-generated summaries from Zendesk ticket analysis
- **Zendesk Sync**: Update claims directly from Zendesk tickets with LLM-powered data extraction

### Status Separation Architecture ✅
- **Fulfillment Status**: Track physical claim progress (Received → Searching → Found → Shipped)
- **Financial Status**: Track monetary state (Pending Payment → Paid → Refunded)
- **Dispute Status**: Track PayPal dispute lifecycle (Received → Evidence Gathering → Resolved)
- **Legacy Compatibility**: Original `status` field maintained for backward compatibility
- **Claim Update Timeline**: Historical tracking of all Zendesk-sourced updates

### Refund Management System ✅
- **Status Workflow**: REQUESTED → PENDING → PROCESSING → COMPLETED/FAILED/CANCELLED
- **Claim Statuses**: REFUND_REQUESTED, REFUNDED, PARTIALLY_REFUNDED
- **PayPal Integration**: Process refunds via PayPal API with full transaction tracking
- **Zendesk Integration**: Automatic status sync from Zendesk tickets
- **Webhook Support**:
  - PayPal refund webhooks (PAYMENT.CAPTURE.REFUNDED)
  - Zendesk status change webhooks
  - WooCommerce refund notifications
- **Manual Entry**: Create refund records manually with audit trail
- **Grant Refund from Claim Page**: One-click refund initiation from claim detail view
- **Idempotency**: Prevent duplicate refunds via unique constraints
- **Audit Trail**: Track who initiated each refund and when

### Email Processing
- **IMAP Integration**: Automatic fetching of unread emails every 3 minutes
- **AI Analysis**: Automatic categorization and action detection
- **Zendesk Integration**: Auto-posting full emails + AI summaries to tickets
- **Alias Matching**: Route emails to tickets via custom field `13606076120860`
- **Auto-Resolution**: Smart categorization marks simple emails as processed
- **Full Email Posting**: Complete email content posted to Zendesk as internal notes
- **Enhanced Logging**: Detailed logging for debugging and audit

### Dispute Management (PayPal)
- **Webhook Integration**: Real-time dispute notifications from PayPal
- **Auto-Matching**: Link disputes to claims by buyer email
- **Evidence Submission**: Generate and submit evidence packages to PayPal
- **Screenshot Capture**: Automated Zendesk ticket screenshots via Playwright
- **Document Generation**: AI-powered response letters and evidence reports

### Zendesk Integration ✅
- **Ticket Management**: Create, update, and fetch tickets via API
- **Comment Posting**: Internal notes and public replies
- **Custom Fields**: Store email aliases for routing
- **Browser Automation**: Screenshot capture for evidence
- **Claim Creation from Webhooks**: Claims auto-created from Zendesk tickets
- **LLM-Powered Extraction**: Qwen AI extracts claim data from ticket content
- **ALF Claim ID Parsing**: Automatic extraction from subject line (format: `ALF1234567`)
- **Idempotency Protection**: Duplicate webhooks for same ticket are skipped
- **Extraction Failure Flag**: `llm_extraction_failed` marks claims needing manual review
- **Update from Zendesk**: Sync claim data from updated Zendesk tickets
- **Zendesk Update Timeline**: Visual timeline of all Zendesk-sourced updates on claim detail page

### Service Monitoring
- **Real-time Status**: Monitor connection status for all external services
- **Health Checks**: Test connectivity to AI, IMAP, Zendesk, PayPal
- **Scheduler Control**: Start/stop email processing scheduler
- **Enable/Disable**: Toggle individual services without configuration changes

---

## 🛠️ Tech Stack

### Backend
- **Django 5.2.11** - Web framework
- **Django REST Framework** - API endpoints
- **APScheduler** - Background task scheduling
- **OpenAI SDK** - AI provider integration (DeepSeek, Qwen compatible)
- **Playwright** - Browser automation for screenshots
- **WeasyPrint** - PDF generation
- **django-auditlog** - Automatic audit trail
- **django-csp** - Content Security Policy headers
- **django-filter** - Advanced filtering for API endpoints

### Frontend
- **Tailwind CSS 4** - Utility-first CSS framework
- **DaisyUI 5** - Component library
- **Bootstrap Icons** - Icon library
- **Vanilla JavaScript** - AJAX interactions

### Database
- **SQLite** (default) - Development
- **PostgreSQL** (recommended) - Production

### External Services
- **AI Providers**: DeepSeek, Qwen (OpenAI-compatible APIs)
- **Email**: IMAP server (Gmail, Outlook, etc.)
- **Helpdesk**: Zendesk
- **Payments**: PayPal Disputes API

---

## 🏗️ Architecture

### Project Structure

```
alf-app/
├── lora_app/              # Django project settings
│   ├── settings.py        # Configuration
│   ├── urls.py            # Root URL routing
│   └── views.py           # Error handlers
├── apps/
│   ├── users/             # Authentication & user management
│   ├── claims/            # Claims management & Zendesk sync
│   ├── communications/    # Email processing & AI analysis
│   ├── payments/          # PayPal disputes, refunds & documents
│   ├── integrations/      # Zendesk API integration
│   └── config/            # System settings & service monitoring
├── templates/             # HTML templates
│   ├── base.html          # Main layout
│   ├── manager/           # Manager views
│   ├── agent/             # Agent views
│   └── config/            # Service monitoring
├── static/
│   ├── src/css/           # Tailwind source
│   ├── css/               # Compiled CSS
│   └── js/                # JavaScript files
└── manage.py              # Django CLI
```

### Apps Overview

| App | Purpose | Key Models |
|-----|---------|------------|
| **users** | Authentication, roles, permissions | User (custom) |
| **claims** | Lost object claims, Zendesk sync | Claim, ClaimUpdateTimeline, ClaimEvidence |
| **communications** | Email processing, AI analysis | EmailLog |
| **payments** | PayPal disputes, refunds, documents | Dispute, Refund, DisputeDocument, DisputeScreenshot |
| **integrations** | Zendesk API | (service layer only) |
| **config** | System settings, monitoring | SystemSettings, ServiceStatus |

### Status Separation Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLAIM ENTITY                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐ │
│  │ FULFILLMENT      │  │ FINANCIAL        │  │ DISPUTE       │ │
│  │ STATUS           │  │ STATUS           │  │ STATUS        │ │
│  ├──────────────────┤  ├──────────────────┤  ├───────────────┤ │
│  │ • Received       │  │ • Pending        │  │ • Received    │ │
│  │ • Searching      │  │ • Paid           │  │ • Matched     │ │
│  │ • Found          │  │ • Refunded       │  │ • Gathering   │ │
│  │ • Shipped        │  │ • Partial Refund │  │ • Documents   │ │
│  │ • Disputed       │  │                  │  │ • Evidence    │ │
│  └──────────────────┘  └──────────────────┘  │ • Resolved    │ │
│                                              └───────────────┘ │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              LEGACY STATUS (BACKWARD COMPATIBILITY)       │   │
│  │  Received, Searching, Found, Shipped, Disputed,           │   │
│  │  REFUND_REQUESTED, REFUNDED, PARTIALLY_REFUNDED           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Refund Granting Data Flow

```
Agent clicks "Grant Refund" on claim detail page
                    ↓
            Modal opens with:
            • Amount (pre-filled)
            • Refund type (Full/Partial)
            • Reason (required)
                    ↓
        Agent submits form (POST /api/payments/refunds/process/)
                    ↓
        RefundService.initiate_refund()
                    ↓
    ┌───────────────┴───────────────┐
    ↓                               ↓
Create Refund record          Call PayPal API
(status: REQUESTED)           (create refund)
    ↓                               ↓
Store paypal_refund_id        PayPal processes
    ↓                               ↓
    └───────────────┬───────────────┘
                    ↓
        Update Refund status:
        PROCESSING → COMPLETED/FAILED
                    ↓
        Update Claim status:
        REFUND_REQUESTED → REFUNDED
                    ↓
        Log to audit trail
```

### Zendesk Sync Flow

```
Zendesk Ticket Updated
         ↓
Zendesk Trigger fires webhook
         ↓
LORA receives webhook (POST /api/claims/{id}/update-from-zendesk/)
         ↓
Fetch full ticket data + comments from Zendesk API
         ↓
LLM (Qwen AI) analyzes changes:
• Compares existing claim data with ticket
• Identifies NEW information only
• Generates summary of changes
         ↓
Update claim fields (only if empty):
• flight_details
• object_description
• phone
• alternate_email
         ↓
Create ClaimUpdateTimeline entry:
• update_type: STATUS_CHANGE, NEW_COMMENT, INFO_UPDATED, LLM_ANALYSIS
• changes_summary: JSON of what changed
• llm_summary: AI-generated summary
         ↓
Display timeline on claim detail page
```

### Email Processing Flow

```
Email Received (IMAP)
    ↓
AI Analysis (DeepSeek/Qwen)
    ↓
Match to Claim/Ticket via custom field 13606076120860
    ↓
Log to EmailLog
    ↓
Post to Zendesk (if matched) - Full email + AI summary
    ↓
Dispute Created (PayPal Webhook)
    ↓
Generate Documents (AI + Templates)
    ↓
Capture Screenshots (Playwright)
    ↓
Submit Evidence (PayPal API)
```

---

## 📦 Installation

### Prerequisites

- Python 3.10 or higher
- Node.js 18+ (for Tailwind CSS build)
- GTK+ 3 (for WeasyPrint PDF generation)
- Playwright (for screenshot capture)

### 1. Clone Repository

```bash
git clone <repository-url>
cd alf-app
```

### 2. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Node Dependencies

```bash
npm install
```

### 4. Build CSS

```bash
# Development (watch mode)
npm run dev

# Production (minified)
npm run build
```

### 5. Install Playwright

```bash
pip install playwright
playwright install chromium
```

### 6. Initialize Database

```bash
python manage.py migrate
```

### 7. Create Superuser

```bash
python manage.py createsuperuser
```

### 8. Create .env File

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 9. Run Development Server

```bash
python manage.py runserver
```

Visit `http://127.0.0.1:8000/login/`

---

## ⚙️ Configuration

### Environment Variables (.env)

```bash
# Django Settings
DEBUG=True
SECRET_KEY=your-secret-key-here
ENCRYPTION_KEY=your-encryption-key-here
ALLOWED_HOSTS=localhost,127.0.0.1

# Database
DATABASE_URL=sqlite:///db.sqlite3

# Email (Outbound SMTP)
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password

# IMAP (Inbound Email)
IMAP_HOST=imap.gmail.com
IMAP_USER=your-email@gmail.com
IMAP_PASS=your-app-password

# Zendesk
ZENDESK_SUBDOMAIN=your-company
ZENDESK_TOKEN=your-api-token
ZENDESK_EMAIL=your-email@company.com
ZENDESK_AGENT_EMAIL=agent-email@company.com
ZENDESK_AGENT_PASSWORD=agent-password

# PayPal
PAYPAL_CLIENT_ID=your-client-id
PAYPAL_SECRET=your-secret
PAYPAL_WEBHOOK_ID=your-webhook-id
PAYPAL_MODE=sandbox

# AI Provider
AI_PROVIDER=DeepSeek
AI_API_BASE=https://api.deepseek.com/v1
AI_API_KEY=your-api-key
AI_API_MODEL=deepseek-chat

# Zendesk Sidebar
SIDEBAR_SECRET_TOKEN=your-secret-token

# Email Domain (for alias matching)
EMAIL_DOMAIN=yourdomain.com
ZENDESK_ALIAS_CUSTOM_FIELD_ID=13606076120860
```

### System Settings (Database)

Configure via Django admin (`/admin/`) or Manager → Configuration page:

| Setting | Description | Example | Default |
|---------|-------------|---------|---------|
| `imap_host` | IMAP server hostname | `imap.gmail.com` | - |
| `imap_user` | IMAP username | `support@yourdomain.com` | - |
| `imap_pass` | IMAP password (encrypted) | App-specific password | - |
| `email_domain` | Domain for alias matching | `yourdomain.com` | - |
| `ai_api_key` | AI API key (encrypted) | Your API key | - |
| `ai_api_base` | AI API endpoint | `https://api.deepseek.com/v1` | - |
| `zendesk_alias_custom_field_id` | Zendesk custom field for email aliases | `13606076120860` | `13606076120860` |

### Encrypted Fields

The following fields are encrypted at rest using Fernet symmetric encryption:

- AI API key
- IMAP password
- Zendesk API token
- PayPal client ID and secret
- Sidebar secret token
- Zendesk agent password

**Encryption Key**: Set `ENCRYPTION_KEY` in `.env` (separate from `SECRET_KEY`).

### Zendesk Configuration

#### Custom Field Setup

1. **Create Email Alias Field** (if not exists):
   - Go to **Admin → Ticket Fields → Add Field**
   - Type: **Text**
   - Title: `Email Alias`
   - Note the field ID (e.g., `13606076120860`)
   - Populate on tickets with values like `client-123@yourdomain.com`

2. **Create Status Field for Refunds**:
   - Go to **Admin → Ticket Fields → Add Field**
   - Type: **Dropdown**
   - Title: `Refund Status`
   - Options: `Requested`, `Pending`, `Processing`, `Completed`, `Failed`, `Cancelled`

#### Webhook Setup

**1. Claim Creation Webhook:**

| Setting | Value |
|---------|-------|
| **Name** | `LORA - Create Claim on Investigation` |
| **Conditions** | `Status` is `Investigation Initiated` |
| **URL** | `https://your-lora.com/api/integrations/zd/claim-webhook/` |
| **Method** | POST |
| **Headers** | `X-Webhook-Secret: your-sidebar-secret-token` |
| **Payload** | See below |

**Payload:**
```json
{
  "ticket_id": "{{ticket.id}}",
  "subject": "{{ticket.title}}",
  "requester": {
    "email": "{{ticket.requester.email}}"
  },
  "status": "{{ticket.status}}"
}
```

**2. Status Change Webhook:**

| Setting | Value |
|---------|-------|
| **Name** | `LORA - Notify Status Change` |
| **Conditions** | `Status` is `Refund Requested` |
| **URL** | `https://your-lora.com/api/integrations/zd/status-webhook/` |
| **Method** | POST |
| **Headers** | `X-Webhook-Secret: your-sidebar-secret-token` |
| **Payload** | See below |

**Payload:**
```json
{
  "ticket_id": "{{ticket.id}}",
  "status": "refund_requested",
  "claim_id": "{{ticket.custom_fields.claim_id}}"
}
```

### PayPal Configuration

1. **Create PayPal App**:
   - Go to [PayPal Developer Dashboard](https://developer.paypal.com/)
   - Create new app with `Disputes` scope
   - Note Client ID and Secret

2. **Configure Webhook**:
   - Go to **Webhooks → Add Webhook**
   - URL: `https://your-lora.com/api/payments/paypal/webhook/`
   - Events: `PAYMENT.CAPTURE.REFUNDED`, `PAYMENT.CAPTURE.REVERSED`
   - Note Webhook ID

3. **Test Mode**:
   - Use `PAYPAL_MODE=sandbox` for testing
   - Switch to `live` for production

---

## 🚀 Usage

### User Roles

#### Manager
- Access to all features
- System configuration
- User management
- Dispute management
- Service monitoring
- Refund approval

#### Agent
- View assigned claims
- Process emails
- Add evidence to claims
- View dispute status
- **Grant refunds** from claim detail page
- **Update claims from Zendesk**

### Dashboard Views

#### Manager Dashboard
- Claims statistics (total, by status)
- Email overview (total, auto-resolved, requires attention)
- Dispute overview (by status)
- Refund statistics
- Recent activity (claims, emails, disputes, refunds)
- Quick actions

#### Agent Dashboard
- Assigned claims
- Email queue
- Claim statistics
- Recent refunds granted

### Creating Claims from Zendesk

Claims are automatically created when a Zendesk ticket status changes to `Investigation Initiated`.

**Manual Creation (if needed):**

```bash
POST /api/integrations/zd/claim-webhook/
Content-Type: application/json
X-Webhook-Secret: your-sidebar-secret-token

{
  "ticket_id": "12345",
  "subject": "Lost Item - ALF1234567",
  "requester": {
    "email": "customer@example.com"
  },
  "status": "investigation_initiated"
}
```

**Response:**
```json
{
  "message": "Claim created successfully",
  "claim_id": 42,
  "alf_claim_id": "ALF1234567",
  "zd_ticket_id": "12345",
  "llm_extraction_failed": false
}
```

### Granting Refunds

**From Claim Detail Page:**

1. Navigate to claim detail page (Agent or Manager role required)
2. Click **Grant Refund** button (top right or in refund section)
3. Fill in the form:
   - **Amount**: Pre-filled with claim amount (editable)
   - **Refund Type**: Full or Partial
   - **Reason**: Required explanation
4. Click **Process Refund**
5. Refund is processed via PayPal API
6. Claim status updates to `REFUNDED` or `PARTIALLY_REFUNDED`

**Via API:**

```bash
POST /api/payments/refunds/process/
Content-Type: application/json
X-CSRFToken: <token>

{
  "claim_id": 123,
  "amount": "50.00",
  "currency": "USD",
  "refund_type": "FULL",
  "reason": "Customer request - item not found"
}
```

**Response:**
```json
{
  "message": "Refund initiated successfully",
  "refund": {
    "id": 42,
    "paypal_refund_id": "REF-123456789",
    "amount": "50.00",
    "currency": "USD",
    "status": "REQUESTED",
    "refund_type": "FULL",
    "created_at": "2026-03-18T10:30:00Z"
  }
}
```

### Updating Claims from Zendesk

**From Claim Detail Page:**

1. Navigate to claim detail page
2. Ensure Zendesk ticket is linked (`zd_ticket_id` present)
3. Click **Update from Zendesk** button
4. System fetches latest ticket data and comments
5. LLM analyzes changes and identifies new information
6. Claim fields updated (only if currently empty)
7. New entry added to Zendesk Update Timeline

**Via API:**

```bash
POST /api/claims/42/update-from-zendesk/
Content-Type: application/json
X-CSRFToken: <token>

{}
```

**Response:**
```json
{
  "message": "Claim updated successfully",
  "updates": {
    "phone": "+1-555-123-4567",
    "alternate_email": "alt@example.com"
  },
  "llm_summary": "Customer provided phone number and alternate email in recent comment",
  "timeline_entry_id": 15
}
```

### Status Management

**View Status on Claim Detail Page:**

The claim detail page displays three separate status indicators:

- 📦 **Fulfillment**: Current physical progress
- 💰 **Financial**: Payment/refund state
- ⚖️ **Dispute**: PayPal dispute status (if applicable)

**Update Status:**

```bash
PATCH /api/claims/42/
Content-Type: application/json
X-CSRFToken: <token>

{
  "status": "Found"
}
```

**Note**: The `status` field is the legacy field. For fine-grained control, use:
- `fulfillment_status`
- `financial_status`
- `dispute_status`

### Zendesk Update Timeline

The claim detail page includes a **Zendesk Update Timeline** section showing:

- 📝 **Status Changes**: When Zendesk ticket status changed
- 💬 **New Comments**: Comments added to Zendesk ticket
- 🔄 **Info Updates**: Claim data updated from Zendesk
- 🤖 **LLM Analysis**: AI-generated summaries of changes

Each timeline entry shows:
- Timestamp
- Update type icon
- Summary of changes
- LLM-generated explanation

---

## 📡 API Reference

### Authentication

All API endpoints require authentication. Use session authentication (logged-in user) or token authentication.

### Claims API

```
GET    /api/claims/                    # List claims
POST   /api/claims/                    # Create claim
GET    /api/claims/{id}/               # Get claim detail
PUT    /api/claims/{id}/               # Update claim
DELETE /api/claims/{id}/               # Delete claim
POST   /api/claims/{id}/update-from-zendesk/  # Update from Zendesk ticket

GET    /api/claims/evidence/           # List evidence
POST   /api/claims/evidence/           # Upload evidence
```

**Claim Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Auto-increment primary key |
| `alf_claim_id` | string | ALF claim ID (format: `ALF1234567`) parsed from Zendesk subject |
| `zd_ticket_id` | string | Zendesk ticket ID |
| `client_email` | email | Primary customer email |
| `phone` | string | Phone number |
| `alternate_email` | email | Alternate contact email |
| `flight_details` | text | Flight information (number, date, route) |
| `object_description` | text | Description of lost item |
| `status` | string | Legacy status field |
| `assigned_to` | integer | User ID of assigned agent |
| `llm_extraction_failed` | boolean | True if LLM failed to extract data |
| `ai_summary` | text | AI-generated summary from ticket analysis |
| `created_at` | datetime | Creation timestamp |
| `updated_at` | datetime | Last update timestamp |

**Example: Update from Zendesk**
```bash
POST /api/claims/42/update-from-zendesk/
Content-Type: application/json
X-CSRFToken: <token>

Response:
{
  "message": "Claim updated successfully",
  "updates": {
    "phone": "+1-555-123-4567"
  },
  "llm_summary": "Customer provided phone number in recent comment",
  "timeline_entry_id": 15
}
```

### Refund API

```
GET    /api/payments/refunds/              # List all refunds
POST   /api/payments/refunds/              # Create manual refund
GET    /api/payments/refunds/{id}/         # Get refund details
PUT    /api/payments/refunds/{id}/         # Update refund
PATCH  /api/payments/refunds/{id}/         # Partial update refund
DELETE /api/payments/refunds/{id}/         # Delete refund
POST   /api/payments/refunds/process/      # Process refund via PayPal
GET    /api/payments/refunds/stats/        # Get refund statistics
POST   /api/payments/refunds/{id}/update_status/  # Update refund status
```

**Refund Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Auto-increment primary key |
| `claim` | integer | Linked claim ID |
| `paypal_refund_id` | string | PayPal refund transaction ID (unique) |
| `paypal_capture_id` | string | Original PayPal capture ID |
| `amount` | decimal | Refund amount |
| `currency` | string | Currency code (e.g., USD, EUR) |
| `status` | string | REQUESTED, PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED |
| `refund_type` | string | FULL or PARTIAL |
| `external_source` | string | LORA, WOOCOMMERCE, MANUAL |
| `reason` | text | Reason for refund |
| `metadata` | JSON | PayPal API response and additional data |
| `created_by` | integer | User who initiated refund |
| `created_at` | datetime | Creation timestamp |
| `processed_at` | datetime | When refund completed |

**Example: Process Refund**
```bash
POST /api/payments/refunds/process/
Content-Type: application/json
X-CSRFToken: <token>

{
  "claim_id": 123,
  "amount": "50.00",
  "currency": "USD",
  "refund_type": "FULL",
  "reason": "Customer request"
}

Response:
{
  "message": "Refund initiated successfully",
  "refund": {
    "id": 42,
    "paypal_refund_id": "REF-123456789",
    "amount": "50.00",
    "status": "REQUESTED"
  }
}
```

**Example: Get Statistics**
```bash
GET /api/payments/refunds/stats/

Response:
{
  "total_refunds": 25,
  "total_amount": 1250.00,
  "by_status": {
    "REQUESTED": 5,
    "PENDING": 3,
    "PROCESSING": 2,
    "COMPLETED": 15,
    "FAILED": 2,
    "CANCELLED": 1
  },
  "by_type": {
    "FULL": 20,
    "PARTIAL": 5
  },
  "by_source": {
    "LORA": 10,
    "WOOCOMMERCE": 10,
    "MANUAL": 5
  },
  "recent_refunds": [...]
}
```

### Communications API

```
GET    /api/communications/email-logs/     # List email logs
POST   /api/communications/email-logs/     # Create email log
GET    /api/communications/email-logs/{id}/ # Get email detail
```

### Service Control API

```
GET    /api/services/status/               # List all service statuses
GET    /api/services/status/{service}/     # Get single service status
POST   /api/services/{service}/test/       # Test connection
POST   /api/services/{service}/toggle/     # Toggle enabled state

POST   /api/services/scheduler/start/      # Start email scheduler
POST   /api/services/scheduler/stop/       # Stop email scheduler
POST   /api/services/scheduler/toggle/     # Toggle scheduler enabled
GET    /api/services/scheduler/info/       # Get scheduler info
```

### Webhook Endpoints

```
POST   /api/integrations/zd/claim-webhook/       # Zendesk claim creation
POST   /api/integrations/zd/refund-webhook/      # PayPal/WooCommerce refund notifications
POST   /api/integrations/zd/status-webhook/      # Zendesk status changes
POST   /api/payments/paypal/webhook/             # PayPal webhook endpoint
```

**Zendesk Claim Webhook Payload:**
```json
{
  "ticket_id": "12345",
  "subject": "Lost Item - ALF1234567",
  "requester": {
    "email": "customer@example.com"
  },
  "status": "investigation_initiated"
}
```

**Zendesk Status Webhook Payload:**
```json
{
  "ticket_id": "12345",
  "status": "refund_requested",
  "claim_id": "678"
}
```

**PayPal Webhook Payload:**
```json
{
  "event_type": "PAYMENT.CAPTURE.REFUNDED",
  "resource": {
    "id": "REF-123456789",
    "amount": {
      "currency_code": "USD",
      "value": "50.00"
    },
    "invoice_id": "CLAIM-123"
  }
}
```

### PDF Generation

```
GET    /api/payments/proof-of-work/{claim_id}/  # Generate proof-of-work PDF
```

### Zendesk Integration

```
GET    /api/zd/info/  # Get Zendesk sidebar widget data
POST   /api/zd/sync/  # Sync claim to Zendesk (create ticket)
```

---

## 🔍 Service Monitoring

### Architecture

The service monitoring system consists of three components:

1. **ServiceStatus Model** - Database storage for service states
2. **ConnectionTester Service** - Connection testing logic
3. **SchedulerController Service** - Background task control

### ServiceStatus Model

```python
class ServiceStatus(models.Model):
    service = CharField(unique=True)  # AI, IMAP, ZENDESK, PAYPAL, SCHEDULER, SCREENSHOT
    status = CharField()              # connected, disconnected, error, running, stopped
    is_enabled = BooleanField()       # Service enabled flag
    last_checked = DateTimeField()    # Last test timestamp
    last_error = TextField()          # Last error message
    metadata = JSONField()            # Additional data
```

### Connection Tests

Each service has a dedicated test method:

- **AI**: Tests API endpoint reachability
- **IMAP**: Tests server login with credentials
- **Zendesk**: Tests API `/tickets` endpoint
- **PayPal**: Tests OAuth2 token endpoint
- **Scheduler**: Checks APScheduler running state
- **Screenshot**: Checks Playwright installation

### API Usage Example

```javascript
// Test AI connection
fetch('/api/services/AI/test/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrftoken
    }
})
.then(response => response.json())
.then(data => {
    console.log(data.status);  // 'connected', 'disconnected', or 'error'
    console.log(data.message); // Human-readable message
});

// Toggle service
fetch('/api/services/IMAP/toggle/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrftoken
    },
    body: JSON.stringify({ enabled: false })
});

// Control scheduler
fetch('/api/services/scheduler/start/', {
    method: 'POST',
    headers: {
        'X-CSRFToken': csrftoken
    }
});
```

### Auto-Refresh

The service status widget auto-refreshes every 2 minutes via JavaScript. Manual refresh available via "Refresh All" button.

---

## 🔒 Security

### Authentication & Authorization

- **Role-based access control**: MANAGER and AGENT roles
- **Session security**: HTTPOnly cookies, 1-hour expiry
- **Rate limiting**: 5 login attempts per minute per IP
- **CSRF protection**: Enabled on all POST forms

### Data Encryption

- **Field-level encryption**: Sensitive credentials encrypted at rest
- **Algorithm**: Fernet symmetric encryption
- **Key derivation**: PBKDF2HMAC with 100,000 iterations
- **Storage**: Encrypted values stored in database

### Input Validation

- **File uploads**: MIME type, extension, size validation
- **Path traversal protection**: Secure filename handling
- **Prompt injection protection**: User content in user role, not system prompt

### Headers (Production)

- **Content Security Policy**: Via django-csp
- **X-Frame-Options**: Prevent clickjacking
- **Secure cookies**: HTTPS-only in production

### Audit Logging

- **django-auditlog**: Automatic audit trail for all model changes
- **DisputeActivityLog**: Manual activity tracking for disputes
- **Refund Audit**: Track who initiated each refund and when

---

## 🧑‍💻 Development

### Running Tests

```bash
# Run all tests
python manage.py test

# Run with coverage
pytest --cov=apps --cov-report=html

# Run specific app tests
python manage.py test apps.payments.tests.test_refund_model

# Run with verbose output
python manage.py test -v 2
```

### Coverage Summary

```bash
# Generate coverage report
pytest --cov=apps --cov-report=term-missing

# HTML report (open in browser)
pytest --cov=apps --cov-report=html
# Open: htmlcov/index.html
```

### Test Suites

| Suite | Command | Description |
|-------|---------|-------------|
| **All Tests** | `python manage.py test` | Run entire test suite |
| **Refund Tests** | `python manage.py test apps.payments.tests.test_refund_model` | Refund model and service tests |
| **Claim Tests** | `python manage.py test apps.claims.tests` | Claim management tests |
| **Zendesk Tests** | `python manage.py test apps.integrations.tests` | Zendesk integration tests |
| **Email Tests** | `python manage.py test apps.communications.tests` | Email processing tests |

### Code Style

- **Python**: PEP 8
- **HTML/Django Templates**: Django template best practices
- **CSS**: Tailwind utility classes with custom components in `tailwind.css`
- **JavaScript**: ES6+ with async/await

### Building CSS

```bash
# Development (watch mode)
npm run dev

# Production build
npm run build
```

### Database Migrations

```bash
# Create migrations after model changes
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Create migration for specific app
python manage.py makemigrations claims
```

### Debugging

Enable debug mode in `.env`:

```bash
DEBUG=True
```

Access Django debug toolbar (if installed) at `/__debug__/`

### Logging

Configure logging in `lora_app/settings.py`:

```python
LOGGING = {
    'version': 1,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'apps.communications': {'level': 'DEBUG'},
        'apps.payments': {'level': 'DEBUG'},
        'apps.claims': {'level': 'DEBUG'},
    },
}
```

---

## 🌐 Deployment

### Prerequisites

- Production web server (Gunicorn, uWSGI)
- Reverse proxy (Nginx, Apache)
- PostgreSQL database
- Redis (for caching and rate limiting)
- SSL/TLS certificate

### Environment Variables (Production)

```bash
DEBUG=False
SECRET_KEY=<strong-random-key>
ENCRYPTION_KEY=<strong-random-key>
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
DATABASE_URL=postgresql://user:pass@host:5432/dbname
EMAIL_HOST=smtp.yourdomain.com
# ... other settings
```

### Static Files

```bash
# Collect static files
python manage.py collectstatic

# Build minified CSS
npm run build
```

### Web Server Configuration

**Gunicorn example:**

```bash
gunicorn lora_app.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --threads 2 \
    --worker-class gthread \
    --timeout 120
```

**Nginx example:**

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location /static/ {
        alias /path/to/staticfiles/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Background Tasks

Start APScheduler in production:

```python
# lora_app/wsgi.py
from apps.communications.tasks import register_scheduler_jobs

# Call after Django setup
register_scheduler_jobs()
```

Or use a separate worker process:

```bash
python manage.py rqscheduler  # If using django-rq
```

### Security Checklist

- [ ] `DEBUG=False`
- [ ] Strong `SECRET_KEY` and `ENCRYPTION_KEY`
- [ ] HTTPS enabled
- [ ] CSP headers configured
- [ ] Database backups scheduled
- [ ] Error monitoring (Sentry, etc.)
- [ ] Rate limiting configured
- [ ] Session cookies secure
- [ ] PayPal webhook verified
- [ ] Zendesk webhooks configured

---

## 📝 Additional Resources

- [Django Documentation](https://docs.djangoproject.com/)
- [Django REST Framework](https://www.django-rest-framework.org/)
- [Tailwind CSS](https://tailwindcss.com/)
- [DaisyUI](https://daisyui.com/)
- [PayPal Disputes API](https://developer.paypal.com/docs/api/customer-disputes/)
- [Zendesk API](https://developer.zendesk.com/api-reference/)
- [Playwright](https://playwright.dev/)

---

## 🆘 Support

For issues or questions:

1. **Check logs**: `apps/*/logs/` or console output
2. **Review error messages**: Django admin error logs
3. **Verify service status**: Manager → Configuration page
4. **Check external service status**:
   - [PayPal Status](https://www.paypal-status.com/)
   - [Zendesk Status](https://status.zendesk.com/)
5. **Search documentation**: Use this README's table of contents

### Common Issues

| Issue | Solution |
|-------|----------|
| Claim not created from Zendesk | Verify webhook trigger conditions and secret token |
| Refund fails | Check PayPal credentials and webhook ID |
| Email not posting to Zendesk | Verify custom field ID `13606076120860` is correct |
| LLM extraction failed | Review ticket content quality and AI API connectivity |
| Status not syncing | Check Zendesk webhook configuration |

---

## 📄 License

[Your License Here]
