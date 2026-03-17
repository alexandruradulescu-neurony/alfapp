# LORA - Lost Object Recovery Automation

**Version:** 1.1.0  
**Framework:** Django 5.2.11 | Python 3.10+  
**UI:** Tailwind CSS 4 + DaisyUI 5

A comprehensive platform for automating lost object recovery claims, dispute management, and customer communications.

---

## 📋 Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Service Monitoring](#service-monitoring)
- [Security](#security)
- [Development](#development)
- [Deployment](#deployment)

---

## ✨ Features

### Claims Management
- **Automated Workflow**: Status tracking from Received → Searching → Found → Shipped → Disputed
- **Agent Assignment**: Role-based claim ownership with enforcement
- **Evidence Management**: Image upload with validation (MIME type, size, path traversal protection)
- **Email Integration**: Automatic email-to-claim linking via alias matching
- **PDF Generation**: Automated proof-of-work documents

### Email Processing
- **IMAP Integration**: Automatic fetching of unread emails every 3 minutes
- **AI Analysis**: Automatic categorization, sentiment analysis, and action detection
- **Zendesk Integration**: Auto-posting AI summaries to tickets
- **Alias Matching**: Route emails to correct claims/tickets via custom email aliases

### Dispute Management (PayPal)
- **Webhook Integration**: Real-time dispute notifications from PayPal
- **Auto-Matching**: Link disputes to claims by buyer email
- **Evidence Submission**: Generate and submit evidence packages to PayPal
- **Screenshot Capture**: Automated Zendesk ticket screenshots via Playwright
- **Document Generation**: AI-powered response letters and evidence reports

### Service Monitoring
- **Real-time Status**: Monitor connection status for all external services
- **Health Checks**: Test connectivity to AI, IMAP, Zendesk, PayPal
- **Scheduler Control**: Start/stop email processing scheduler
- **Enable/Disable**: Toggle individual services without configuration changes

### Zendesk Integration
- **Ticket Management**: Create, update, and fetch tickets via API
- **Comment Posting**: Internal notes and public replies
- **Custom Fields**: Store email aliases for routing
- **Browser Automation**: Screenshot capture for evidence

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
│   ├── claims/            # Claims management
│   ├── communications/    # Email processing & AI analysis
│   ├── payments/          # PayPal disputes & documents
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
| **claims** | Lost object claims | Claim, ClaimEvidence |
| **communications** | Email processing, AI analysis | EmailLog |
| **payments** | PayPal disputes, documents | Dispute, DisputeDocument, DisputeScreenshot |
| **integrations** | Zendesk API | (service layer only) |
| **config** | System settings, monitoring | SystemSettings, ServiceStatus |

### Data Flow

```
Email Received (IMAP)
    ↓
AI Analysis (DeepSeek/Qwen)
    ↓
Match to Claim/Ticket
    ↓
Log to EmailLog
    ↓
Post to Zendesk (if matched)
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
ZENDESK_ALIAS_CUSTOM_FIELD_ID=360001234567
```

### System Settings (Database)

Configure via Django admin (`/admin/`) or Manager → Configuration page:

- **AI Configuration**: Provider, API base URL, API key, model name, prompt templates
- **IMAP Settings**: Host, username, password
- **Zendesk Settings**: Subdomain, token, email, agent credentials
- **PayPal Settings**: Client ID, secret, webhook ID
- **Sidebar Authentication**: Secret token for Zendesk widget

### Encrypted Fields

The following fields are encrypted at rest using Fernet symmetric encryption:

- AI API key
- IMAP password
- Zendesk API token
- PayPal client ID and secret
- Sidebar secret token
- Zendesk agent password

**Encryption Key**: Set `ENCRYPTION_KEY` in `.env` (separate from `SECRET_KEY`).

---

## 🚀 Usage

### User Roles

#### Manager
- Access to all features
- System configuration
- User management
- Dispute management
- Service monitoring

#### Agent
- View assigned claims
- Process emails
- Add evidence to claims
- View dispute status

### Dashboard Views

#### Manager Dashboard
- Claims statistics (total, by status)
- Email overview (total, auto-resolved, requires attention)
- Dispute overview (by status)
- Recent activity (claims, emails, disputes)
- Quick actions

#### Agent Dashboard
- Assigned claims
- Email queue
- Claim statistics

### Service Monitoring

Access via **Manager → Configuration** page.

**Services Monitored:**
1. **AI Provider** - API connectivity test
2. **IMAP Email** - Server login test
3. **Zendesk** - API access test
4. **PayPal** - OAuth2 token test
5. **Email Scheduler** - APScheduler running status
6. **Screenshot Service** - Playwright availability

**Controls:**
- **Test Button** - Test connection immediately
- **Enable/Disable Toggle** - Enable or disable service
- **Start/Stop** (Scheduler only) - Control email processing

**Status Colors:**
- 🟢 **Connected** - Service is operational
- ⚪ **Disconnected** - Not configured or unreachable
- 🔴 **Error** - Configuration error or authentication failed
- 🔵 **Running** - Background service active
- 🟡 **Stopped** - Background service stopped

---

## 📡 API Reference

### Authentication

All API endpoints require authentication. Use session authentication (logged-in user) or token authentication.

### Claims API

```
GET    /api/claims/              # List claims
POST   /api/claims/              # Create claim
GET    /api/claims/{id}/         # Get claim detail
PUT    /api/claims/{id}/         # Update claim
DELETE /api/claims/{id}/         # Delete claim

GET    /api/claims/evidence/     # List evidence
POST   /api/claims/evidence/     # Upload evidence
```

### Communications API

```
GET    /api/communications/email-logs/  # List email logs
POST   /api/communications/email-logs/  # Create email log
```

### Service Control API

```
GET    /api/services/status/            # List all service statuses
GET    /api/services/status/{service}/  # Get single service status
POST   /api/services/{service}/test/    # Test connection
POST   /api/services/{service}/toggle/  # Toggle enabled state

POST   /api/services/scheduler/start/   # Start email scheduler
POST   /api/services/scheduler/stop/    # Stop email scheduler
POST   /api/services/scheduler/toggle/  # Toggle scheduler enabled
GET    /api/services/scheduler/info/    # Get scheduler info
```

### PayPal Webhook

```
POST   /api/payments/paypal/webhook/  # PayPal webhook endpoint
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

---

## 🧑‍💻 Development

### Running Tests

```bash
python manage.py test
```

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
1. Check logs: `apps/*/logs/` or console output
2. Review error messages in Django admin
3. Verify service status in Manager → Configuration
4. Check external service status pages

---

## 📄 License

[Your License Here]
