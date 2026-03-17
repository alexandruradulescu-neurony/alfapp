# Changelog

All notable changes to the LORA project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] - 2026-03-17

### 🎉 Added

#### Email System Improvements
- **Detailed Logging**: Success/failure icons for Zendesk posting visibility
- **Explicit Match Logging**: Clear messages when emails will/won't be posted to Zendesk
- **Enhanced Debugging**: Step-by-step logging of email matching and posting process

### 🔧 Changed

#### Email System Simplification
- **Removed Sentiment Analysis**: No longer needed (emails only from airports/TSA/platforms)
- **Simplified Zendesk Matching**: Uses ONLY custom field `13606076120860` (hardcoded)
- **Removed from_email Fallback**: Only alias-based matching via Zendesk custom field
- **Full Email Posting**: Posts complete email body + AI summary (not just summary)
- **New Zendesk Format**: Shows email headers, full body, then AI analysis

#### Email Categories (Unchanged)
- OBJECT_FOUND - Item located
- OBJECT_NOT_FOUND - Search completed, not found (auto-resolvable)
- RESUBMISSION_REQUIRED - Need more information
- SUBMISSION_CONFIRMATION - Form submission acknowledgment (auto-resolvable)
- GENERAL_CORRESPONDENCE - Other communication
- UNKNOWN - Cannot categorize

### 🗑️ Removed

- **EmailLog.sentiment** field and database index
- **SENTIMENT_CHOICES** from model
- **Sentiment filter** from email list template
- **Sentiment display** from email detail template
- **Sentiment extraction** from AI parsing
- **from_email fallback matching** in email processing

### 📦 Dependencies

No new dependencies added.

### ⚠️ Database Changes

- **communications.EmailLog**: Removed `sentiment` field
- **communications.EmailLog**: Removed `sentiment` index

### 🚀 Migration

```bash
python manage.py migrate
```

This will remove the sentiment field and index from the EmailLog table.

### 📝 Documentation

- **README.md**: Added comprehensive Email System section
- **README.md**: Updated version to 1.3.0
- **README.md**: Added email flow diagram
- **README.md**: Added Zendesk comment format example
- **README.md**: Added configuration table for email settings
- **README.md**: Updated Email Processing features list

---

## [1.2.0] - 2026-03-17

### 🎉 Added

#### Refund Management System
- **Refund Model** - Track refunds with status workflow (REQUESTED → PENDING → PROCESSING → COMPLETED/FAILED/CANCELLED)
- **Claim Status Updates** - Added REFUND_REQUESTED, REFUNDED, PARTIALLY_REFUNDED statuses
- **RefundService** - Process refunds via PayPal API with idempotency protection
- **RefundViewSet** - Full CRUD API for refunds
- **Refund Statistics** - GET /api/payments/refunds/stats/ endpoint
- **Manager UI** - Refunds list page with filters, search, and statistics
- **Process Refund Modal** - Create and process refunds from UI
- **Sidebar Navigation** - "Refunds" menu item for MANAGER role

#### Zendesk Integration for Refunds
- **ZendeskStatusWebhookView** - Handle Zendesk ticket status changes
- **Automatic Status Sync** - Zendesk "refund requested" → LORA REFUND_REQUESTED
- **tag_zendesk_ticket_as_refunded()** - Add 'refunded' tag to tickets
- **add_refund_comment_to_zendesk()** - Post refund details as comments
- **Webhook Endpoints**:
  - POST /api/integrations/zd/refund-webhook/ - PayPal/WooCommerce notifications
  - POST /api/integrations/zd/status-webhook/ - Zendesk status changes

#### API Endpoints
- `GET /api/payments/refunds/` - List refunds
- `POST /api/payments/refunds/` - Create manual refund
- `POST /api/payments/refunds/process/` - Process refund via PayPal
- `GET /api/payments/refunds/stats/` - Get statistics
- `POST /api/payments/refunds/{id}/update_status/` - Update status
- `POST /api/integrations/zd/status-webhook/` - Zendesk status webhook

### 🔧 Changed

- **Refund Default Status** - Changed from PENDING to REQUESTED
- **Documentation** - Updated README with refund management features
- **Version** - Bumped to 1.2.0

### 📦 Dependencies

No new dependencies added.

### ⚠️ Database Changes

- **claims.Claim** - Added REFUND_REQUESTED, REFUNDED, PARTIALLY_REFUNDED status choices
- **payments.Refund** - Added REQUESTED status, changed default to REQUESTED

### 🚀 Migration

```bash
python manage.py migrate
```

This will update status choices for existing models.

---

## [1.1.0] - 2026-03-17

### 🎉 Added

#### Service Monitoring System
- **ServiceStatus model** - Track connection status for 6 external services
- **ConnectionTester service** - Test connectivity to AI, IMAP, Zendesk, PayPal
- **SchedulerController service** - Control email processing scheduler (start/stop)
- **API endpoints** for service control:
  - `GET /api/services/status/` - List all service statuses
  - `POST /api/services/<service>/test/` - Test connection
  - `POST /api/services/<service>/toggle/` - Toggle enabled state
  - `POST /api/services/scheduler/start/` - Start scheduler
  - `POST /api/services/scheduler/stop/` - Stop scheduler
- **UI components** in Manager → Configuration:
  - Status indicator badges with color coding
  - Enable/disable toggle switches for each service
  - Test buttons for connectivity verification
  - Start/Stop buttons for email scheduler
  - Auto-refresh every 2 minutes
  - Toast notifications for user feedback

#### New Configuration Sections
- **Email Scheduler** - Dedicated section with scheduler controls and status
- **Screenshot Service** - Playwright availability monitoring

#### Documentation
- Comprehensive README with full feature documentation
- Development guide with code examples and best practices
- Service monitoring system documentation
- API reference with usage examples

### 🎨 Changed

#### Frontend Migration (Bootstrap → Tailwind/DaisyUI)
- **Complete UI overhaul** from Bootstrap 5 to Tailwind CSS 4 + DaisyUI 5
- **Custom theme** (`lora`) with indigo/slate color palette
- **New utility classes:**
  - `.glass-panel` - Backdrop blur sidebar
  - `.card-modern` - Elevated cards with hover effects
  - `.stat-modern` - Statistics cards
  - `.table-modern` - Sticky header tables
  - `.input-modern` - Styled form inputs
- **Mesh gradient background** instead of solid colors
- **Inter font** from Google Fonts
- **Custom status badge colors** for claims and disputes
- **Smooth animations** (fade-in, hover-lift)

#### Settings Page Enhancement
- **Integrated service status** into each configuration section
- **Removed separate dashboard widget** - status now shown alongside config
- **Added status legend** explaining all status colors
- **Improved layout** with inline controls

### 🔧 Improved

#### Performance
- Optimized database queries with `select_related` and `prefetch_related`
- Single-query aggregates for dashboard statistics
- Auto-refresh throttling (2-minute interval)
- Reduced JavaScript bundle size

#### Security
- Field-level encryption for all sensitive credentials
- CSRF protection on all AJAX endpoints
- Rate limiting on login (5 attempts/minute/IP)
- Session security enhancements (HTTPOnly, secure cookies)
- Content Security Policy headers (production)

#### Code Quality
- Service layer pattern for business logic
- Consistent error handling across services
- Comprehensive logging
- Type hints in service functions

### 🐛 Fixed

- CSRF token handling in JavaScript AJAX calls
- Service status initialization on dashboard load
- Template tag library registration
- Migration conflicts with existing indexes
- Encryption key derivation performance (PBKDF2 iterations)

### 📦 Dependencies

#### Added
- `tailwindcss` v4.2.1
- `daisyui` v5.5.19
- `@tailwindcss/cli` v4.2.1

#### Updated
- Django 5.2.11 (from 4.x)
- All Python dependencies to latest compatible versions

### ⚠️ Deprecated

- Bootstrap 5 CSS framework
- Bootstrap JavaScript components
- `tailwind.config.js` (using Tailwind v4 inline config)

### 🗑️ Removed

- Bootstrap CDN links from templates
- Legacy CSS files
- Unused JavaScript libraries

---

## [1.0.0] - 2025-XX-XX

### 🎉 Added

#### Core Features
- **Claims Management**
  - Status workflow (Received → Searching → Found → Shipped → Disputed)
  - Agent assignment with ownership enforcement
  - Evidence upload with validation
  - PDF proof-of-work generation

- **Email Processing**
  - IMAP integration with automatic fetching (every 3 minutes)
  - AI-powered email analysis (summary, sentiment, category)
  - Automatic claim/ticket matching via email aliases
  - Zendesk integration for auto-posting summaries

- **Dispute Management**
  - PayPal webhook integration
  - Auto-matching to claims by buyer email
  - AI-generated response letters
  - Evidence report generation
  - Zendesk screenshot capture (Playwright)
  - Evidence submission to PayPal API

- **Zendesk Integration**
  - Ticket CRUD operations
  - Comment posting (internal/public)
  - Custom field support for email aliases
  - Browser automation for screenshots

- **User Management**
  - Role-based access control (MANAGER, AGENT)
  - Session authentication
  - Rate-limited login
  - Password validation

#### Technical Infrastructure
- Django REST Framework API
- APScheduler for background tasks
- django-auditlog for automatic audit trail
- Encrypted field storage for credentials
- Custom user model with roles
- Comprehensive test suite

#### External Integrations
- AI providers (DeepSeek, Qwen, OpenAI-compatible)
- IMAP email servers
- Zendesk API
- PayPal Disputes API
- Playwright browser automation

---

## [Unreleased]

### 🚧 In Progress

- Real-time notifications for service status changes
- Email template customization UI
- Advanced reporting and analytics
- Multi-language support (i18n)
- Mobile-responsive improvements

### 📋 Planned

- Two-factor authentication
- API token authentication for third-party integrations
- Webhook event dashboard
- Automated backup system
- Performance monitoring dashboard
- Custom field builder for claims

---

## Version History

| Version | Release Date | Key Changes |
|---------|-------------|-------------|
| 1.1.0 | 2026-03-17 | Tailwind migration, Service monitoring |
| 1.0.0 | 2025-XX-XX | Initial release |

---

## Migration Notes

### Upgrading to 1.1.0

#### Database Migrations

```bash
python manage.py migrate
```

This will create the `ServiceStatus` model and related tables.

#### Frontend Build

```bash
# Install new dependencies
npm install

# Build CSS
npm run build
```

#### Initialize Service Statuses

```python
# Django shell
python manage.py shell

>>> from apps.config.models import ServiceStatus
>>> services = ['AI', 'IMAP', 'ZENDESK', 'PAYPAL', 'SCHEDULER', 'SCREENSHOT']
>>> for service in services:
...     ServiceStatus.objects.get_or_create(
...         service=service,
...         defaults={'status': 'disconnected', 'is_enabled': True}
...     )
```

#### Configuration Changes

1. **Update `.env`** - Review `.env.example` for new variables
2. **Update SystemSettings** - Configure new fields in admin
3. **Install Playwright** (for screenshot service):
   ```bash
   pip install playwright
   playwright install chromium
   ```

#### Breaking Changes

- **Bootstrap classes removed** - All templates now use Tailwind/DaisyUI classes
- **Custom CSS changes** - Review `static/src/css/tailwind.css` for new theme
- **JavaScript updates** - Service controls require new `service-controls.js`

### Rollback Procedure

If you need to rollback to 1.0.0:

```bash
# Revert code
git checkout <previous-tag>

# Revert database
python manage.py migrate config 0005
python manage.py migrate payments 0003

# Reinstall old dependencies
pip install -r requirements.old.txt
npm install
```

---

## Contributors

This project exists thanks to all the people who contribute.

---

## License

[Your License Here]
