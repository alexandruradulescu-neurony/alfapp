# LORA Project Context

## Project Overview

**LORA (Lost Object Recovery Automation)** is a comprehensive Django-based platform for automating lost object recovery claims, dispute management, customer communications, and refund management.

**Version:** 1.6.0
**Framework:** Django 5.2.11 | Python 3.14
**UI:** Tailwind CSS 4 + DaisyUI 5

### Core Features

1. **Claims Management** - Automated workflow for lost object claims with status tracking, agent assignment, and evidence management
2. **Email Processing** - IMAP integration with AI analysis, Zendesk integration, and alias-based email routing
3. **Dispute Management** - PayPal dispute handling with evidence submission and document generation
4. **Service Monitoring** - Real-time status monitoring for all external services (AI, IMAP, Zendesk, PayPal)
5. **Refund Management** - Complete refund workflow with PayPal API integration and Zendesk sync
6. **Zendesk Integration** - Ticket management, comment posting, and browser automation for screenshots
7. **AI Agent Chat** ⭐ NEW - ChatGPT-like interface for natural language claim queries with conversation context

## Tech Stack

### Backend
- **Django 5.2.11** - Web framework
- **Django REST Framework** - API endpoints
- **APScheduler** - Background task scheduling (email fetching every 3 minutes)
- **OpenAI SDK** - AI provider integration (DeepSeek, Qwen compatible)
- **Playwright** - Browser automation for screenshots
- **WeasyPrint** - PDF generation
- **cryptography** - Field-level encryption for sensitive data
- **django-auditlog** - Automatic audit trail

### Frontend
- **Tailwind CSS 4** - Utility-first CSS framework
- **DaisyUI 5** - Component library
- **Bootstrap Icons** - Icon library
- **Vanilla JavaScript** - AJAX interactions

### Database
- **SQLite** (development)
- **PostgreSQL** (production recommended)

### External Services
- **AI Providers**: DeepSeek, Qwen (OpenAI-compatible APIs)
- **Email**: IMAP server (Gmail, Outlook, etc.)
- **Zendesk**: Customer support ticketing
- **PayPal**: Payment disputes and refunds

## Project Structure

```
alf-app/
├── apps/                          # Django applications
│   ├── agent/                     # ⭐ NEW: AI Agent Chat
│   ├── claims/                    # Claims management
│   ├── communications/            # Email processing and logging
│   ├── config/                    # System configuration and service monitoring
│   ├── integrations/              # Zendesk integration
│   ├── payments/                  # PayPal disputes and refunds
│   └── users/                     # User authentication and dashboards
├── lora_app/                      # Django project settings
├── templates/                     # HTML templates
│   ├── agent/                     # Agent-facing views
│   ├── manager/                   # Manager-facing views
│   └── base.html                  # Base template
├── static/                        # Static files (CSS, JS, images)
├── scripts/                       # Utility scripts
├── deploy/                        # Deployment configurations
└── docs/                          # Documentation (AGENT_CHAT.md, API.md)
```

## Building and Running

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd alf-app

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Install Python dependencies
pip install -r requirements.txt

# Install Node.js dependencies (for Tailwind CSS)
npm install

# Install Playwright browsers
playwright install

# Set up environment variables
cp .env.example .env
# Edit .env with your configuration

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Build static files (Tailwind CSS)
npm run build
```

### Running the Development Server

```bash
python manage.py runserver
```

Access the application at `http://127.0.0.1:8000/`

### Background Tasks

The email processing scheduler runs automatically when enabled:
- **Interval**: Every 3 minutes
- **Management**: Via Manager → Configuration page
- **API**: `/api/services/scheduler/start/`, `/api/services/scheduler/stop/`

### Running Tests

```bash
python manage.py test
```

## Configuration

### Environment Variables (.env)

**Required:**
- `SECRET_KEY` - Django secret key
- `ENCRYPTION_KEY` - Key for encrypting sensitive database fields
- `DEBUG` - Debug mode (True/False)
- `ALLOWED_HOSTS` - Allowed hostnames

**Email (IMAP):**
- `IMAP_HOST` - IMAP server (e.g., `imap.gmail.com`)
- `IMAP_USER` - IMAP username
- `IMAP_PASS` - IMAP password (app-specific password recommended)

**Zendesk:**
- `ZENDESK_SUBDOMAIN` - Zendesk subdomain
- `ZENDESK_TOKEN` - Zendesk API token
- `ZENDESK_EMAIL` - Zendesk email

**PayPal:**
- `PAYPAL_CLIENT_ID` - PayPal client ID
- `PAYPAL_SECRET` - PayPal secret
- `PAYPAL_MODE` - `sandbox` or `live`

**AI Provider:**
- `AI_API_BASE` - AI API endpoint (e.g., `https://api.deepseek.com/v1`)
- `AI_API_KEY` - AI API key (DeepSeek, Qwen, etc.)
- `AI_API_MODEL` - Model name (e.g., `deepseek-chat`)

### SystemSettings (Database)

Configure via Django admin (`/admin/`) or Manager → Configuration:

- **AI Configuration**: Provider, API base, API key, model, prompts
- **IMAP Settings**: Host, username, password
- **Zendesk Settings**: Subdomain, token, email, custom field IDs
- **PayPal Settings**: Client ID, secret, webhook ID
- **Email Domain**: Domain for alias matching (e.g., `yourdomain.com`)

## Key Workflows

### Email Processing Flow

1. Email sent to alias (e.g., `client-123@yourdomain.com`)
2. APScheduler fetches emails from IMAP every 3 minutes
3. System extracts alias from `Delivered-To` header
4. Searches Zendesk for ticket where `custom_fields_13606076120860 = alias`
5. If match found:
   - Posts full email + AI analysis to Zendesk as internal note
   - Creates EmailLog record linked to claim
6. If no match: Logs email only (no Zendesk posting)
7. Auto-resolves simple emails based on category

### Refund Request Flow

1. Agent moves Zendesk ticket to "Refund Requested" custom status
2. Zendesk webhook notifies LORA (`POST /api/integrations/zd/claim-webhook/`) — the claim webhook handles all status changes
3. LORA mirrors the status on the claim (`Claim.status = 'Refund Requested'`) and records a timeline entry
4. Manager creates a refund record and processes it via PayPal API
5. Refund status updated to `COMPLETED`
6. Zendesk ticket tagged as "refunded"

### Dispute Evidence Flow

1. PayPal dispute webhook received
2. System matches dispute to claim by buyer email
3. AI generates response letter and evidence report
4. Evidence package submitted to PayPal via API
5. Zendesk ticket screenshot captured via Playwright
6. All documents stored in database with audit trail

## API Endpoints

### Claims
```
GET    /api/claims/              # List claims
POST   /api/claims/              # Create claim
GET    /api/claims/{id}/         # Get claim detail
PUT    /api/claims/{id}/         # Update claim
DELETE /api/claims/{id}/         # Delete claim
```

### Communications
```
GET    /api/communications/email-logs/     # List email logs
GET    /api/communications/email-logs/{id}/ # Get email detail
```

### Services
```
GET    /api/services/status/            # List service statuses
POST   /api/services/{service}/test/    # Test connection
POST   /api/services/{service}/toggle/  # Toggle enabled
POST   /api/services/scheduler/start/   # Start scheduler
POST   /api/services/scheduler/stop/    # Stop scheduler
```

### Refunds
```
GET    /api/payments/refunds/              # List refunds
POST   /api/payments/refunds/              # Create refund
POST   /api/payments/refunds/process/      # Process via PayPal
GET    /api/payments/refunds/stats/        # Get statistics
```

### Integrations
```
POST   /api/integrations/zd/claim-webhook/    # Zendesk claim creation + status mirror (X-Webhook-Secret required)
POST   /api/integrations/zd/refund-webhook/   # Refund notifications (X-Webhook-Secret required)
```

### AI Agent Chat ⭐ NEW

```
GET    /agent/chat/                     # Chat page
POST   /api/agent/chat/                 # Chat API
```

**Request:**
```json
{
    "message": "any ticket for emma williamson?",
    "conversationHistory": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hello! How can I help?"}
    ]
}
```

**Response:**
```json
{
    "answer": "Yes, we have a claim for Emma Williams (ALF1000004)...",
    "sources": ["LORA", "EmailLog", "Refund"],
    "claims": [{"alf_claim_id": "ALF1000004", "status": "Refunded"}],
    "success": true
}
```

**Features:**
- Auto-detect claim IDs (ALF1234567 format)
- Search by customer name or email
- Conversation context persistence (last 10 messages)
- Fetches claim data, emails, refunds, timeline, Zendesk tickets
- LLM-powered natural language responses
- Hallucination prevention (uses ONLY provided data)

## Development Conventions

### Code Style
- **Python**: PEP 8 compliant
- **JavaScript**: Vanilla ES6+
- **HTML**: Django templates with Tailwind CSS classes

### Testing Practices
- Model tests for all models
- Service tests for external API integrations
- API endpoint tests for DRF views
- Test coverage for critical business logic

### Git Workflow
- **Main branch**: `main` (production-ready)
- **Feature branches**: `feature/description`
- **Commit messages**: Conventional Commits format
  - `feat:` New feature
  - `fix:` Bug fix
  - `docs:` Documentation changes
  - `refactor:` Code refactoring

### Security Practices
- **Field-level encryption** for sensitive credentials (IMAP password, API keys, etc.)
- **CSRF protection** on all POST forms
- **Role-based access control** (MANAGER, AGENT)
- **Input validation** for file uploads (MIME type, size, path traversal protection)
- **Prompt injection protection** (user content separated from system prompts)

## Common Tasks

### Add a New Email Alias
1. Create email forward/alias: `client-123@yourdomain.com` → main inbox
2. In Zendesk, update ticket custom field `13606076120860` with `client-123@yourdomain.com`
3. System will automatically match future emails to this ticket

### Configure Zendesk Webhook
1. Admin → Apps and Integrations → Webhooks → Create webhook
2. URL: `https://your-lora.com/api/integrations/zd/claim-webhook/`
3. Add header `X-Webhook-Secret: your-sidebar-secret-token` (mandatory — absent header returns 401)
4. Trigger condition: ticket custom status changes (fires on all custom-status transitions including "Investigation initiated" which creates the claim)

Note: the old `zd/status-webhook/` endpoint has been removed. Delete any Zendesk trigger pointing to it.

### Enable Email Processing
1. Go to Manager → Configuration
2. Test IMAP connection
3. Click "Start Scheduler"
4. Verify status shows "Running"

### Generate Proof-of-Work PDF
1. Navigate to a claim detail page
2. Click "Generate PDF" button
3. System creates PDF with claim details and evidence
4. PDF available for download

## Troubleshooting

### Email Not Posting to Zendesk
- Check logs for: `✓ Matched alias {email} to Zendesk ticket {id}`
- Verify Zendesk custom field `13606076120860` is populated on tickets
- Check Zendesk credentials in SystemSettings
- Test Zendesk connection via Manager → Configuration

### Scheduler Not Running
- Check ServiceStatus in Django admin
- Verify APScheduler is enabled
- Check logs for scheduler errors
- Restart scheduler via API or UI

### AI Analysis Not Working
- Verify AI API key in SystemSettings
- Test AI connection via Manager → Configuration
- Check AI provider endpoint is accessible
- Review logs for API errors

## Recent Changes

### v1.6.0 (Latest) - AI Agent Chat

**New Feature: AI Agent Chat**
- ChatGPT-like interface at `/agent/chat`
- Natural language claim queries
- Conversation context persistence
- Multi-source data integration

**Claim Detection:**
- Auto-detect ALF claim IDs from messages
- Search by customer name (e.g., "emma williamson")
- Search by email address
- Context maintained across follow-up questions

**Data Sources:**
- Complete claim details from database
- Email history with full body content
- Refund history and status
- Timeline updates from Zendesk sync
- Zendesk ticket data and comments

**LLM Integration:**
- Uses DeepSeek AI API
- Conversational system prompt
- Hallucination prevention (uses ONLY provided data)
- JSON output prevention (natural language only)

**Files Created:**
- `apps/agent/` - New Django app
- `apps/agent/services.py` - AgentChatService
- `apps/agent/views.py` - API and page views
- `templates/agent/chat.html` - Chat UI
- `docs/AGENT_CHAT.md` - Complete documentation
- `docs/API.md` - API reference

**Documentation:**
- Updated README.md with AI Agent Chat section
- Updated CHANGELOG.md with v1.6.0 release notes
- Created comprehensive agent chat documentation
- Created API documentation

### v1.5.0 - Status Separation & Refund UI

- Separated fulfillment_status, financial_status, dispute_status
- Added grant refund functionality to claim page
- Enhanced claim detail UI with prominent status display
- Refund history section on claim pages
- All 101 tests passing

### v1.4.0 - Zendesk-First Claims Flow

- Claims created from Zendesk webhooks
- LLM extraction of claim data
- ALF claim ID parsing from subject
- Idempotency protection
- Email system simplification (removed sentiment analysis)

### v1.3.0 - Refund Management

- **Removed sentiment analysis** from email processing (not needed for B2B emails)
- **Simplified Zendesk matching** to use only custom field `13606076120860`
- **Full email posting** to Zendesk (not just AI summary)
- **Enhanced logging** with success/failure indicators
- **Added Refund Management** system with PayPal integration
- **Added Zendesk webhooks** for refund status sync

## Additional Resources

- **Full README**: See `README.md` for comprehensive documentation
- **Changelog**: See `CHANGELOG.md` for version history
- **AI Agent Chat Docs**: See `docs/AGENT_CHAT.md` for detailed chat documentation
- **API Documentation**: See `docs/API.md` for complete API reference
- **Deployment**: See `deploy/` directory for deployment configurations
