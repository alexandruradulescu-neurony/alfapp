# Changelog

All notable changes to the LORA project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.4.0] - 2026-03-18

### 🎉 Added

#### Phase 1: Refund Management System

**New Model: `payments.Refund`**
- `id` - Auto-increment primary key
- `claim` - ForeignKey to claims.Claim (required)
- `amount` - Decimal (max_digits=10, decimal_places=2)
- `currency` - CharField (default: "USD")
- `refund_type` - CharField: FULL, PARTIAL
- `reason` - TextField
- `status` - CharField: REQUESTED, PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED
- `source` - CharField: LORA, WOOCOMMERCE, MANUAL
- `paypal_transaction_id` - CharField (nullable, unique)
- `processed_by` - ForeignKey to users.User (nullable)
- `processed_at` - DateTimeField (nullable)
- `created_at` - DateTimeField (auto_now_add)
- `updated_at` - DateTimeField (auto_now)
- `metadata` - JSONField (nullable, for storing API responses)

**Claim Model Updates**
- Added status choices: `REFUND_REQUESTED`, `REFUNDED`, `PARTIALLY_REFUNDED`
- Added `refund` reverse relation for accessing associated refunds

**Admin Interface**
- RefundAdmin with list_display, list_filter, search_fields
- Read-only fields for processed_at, processed_by
- Inline display of refunds on Claim admin page

**Model Tests (11 tests)**
- Refund model creation and validation
- Status transition tests
- Unique constraint on paypal_transaction_id
- ForeignKey relationships
- Default values
- Field type validation

#### Phase 2: Refund Service Layer

**RefundService Class**
- `process_refund(refund_id)` - Process refund via PayPal API
- `_create_paypal_payout(refund)` - Create PayPal payout item
- `_handle_paypal_response(response, refund)` - Process API response
- `_validate_refund_for_processing(refund)` - Pre-processing validation
- Idempotency protection via unique paypal_transaction_id

**Zendesk Integration Functions**
- `tag_zendesk_ticket_as_refunded(ticket_id, refund)` - Add 'refunded' tag
- `add_refund_comment_to_zendesk(ticket_id, refund)` - Post refund details as comment
- Automatic status sync from Zendesk to LORA

**Webhook Handlers**
- `ZendeskRefundWebhookView` - Handle PayPal/WooCommerce refund notifications
- `ZendeskStatusWebhookView` - Handle Zendesk ticket status changes
- Webhook secret verification using `SIDEBAR_SECRET_TOKEN`
- Idempotency checks to prevent duplicate processing

**Logging**
- Comprehensive logging for all refund operations
- Success/failure tracking
- API request/response logging (sanitized)

#### Phase 3: Refund UI and API

**DRF ViewSet: `RefundViewSet`**
- `GET /api/payments/refunds/` - List all refunds (filterable)
- `POST /api/payments/refunds/` - Create manual refund
- `GET /api/payments/refunds/{id}/` - Get refund details
- `PUT /api/payments/refunds/{id}/` - Update refund
- `PATCH /api/payments/refunds/{id}/` - Partial update
- `DELETE /api/payments/refunds/{id}/` - Delete refund
- `POST /api/payments/refunds/process/` - Process refund via PayPal
- `GET /api/payments/refunds/stats/` - Get refund statistics
- `POST /api/payments/refunds/{id}/update_status/` - Update status

**Refund List Page**
- URL: `/manager/refunds/`
- Filter by status, source, refund_type
- Search by claim ID, PayPal transaction ID
- Statistics cards (total refunds, total amount, by status)
- Responsive table with sorting

**Process Refund Modal**
- Create new refund from UI
- Select claim (searchable dropdown)
- Enter amount, type, reason
- Process directly via PayPal or save as pending

**Sidebar Navigation Update**
- Added "Refunds" menu item for MANAGER role
- Icon: currency-dollar
- Position: Between "Disputes" and "Configuration"

#### Phase 4: Email System Simplification

**Removed Features**
- Sentiment analysis from email processing pipeline
- `EmailLog.sentiment` field and database index
- `SENTIMENT_CHOICES` from model
- Sentiment filter from email list template
- Sentiment display from email detail template
- `from_email` fallback matching logic

**Simplified Zendesk Matching**
- Uses ONLY custom field `13606076120860` (hardcoded)
- Removed configurable field ID setting
- Direct alias matching without fallbacks

**Full Email Posting**
- Posts complete email body to Zendesk (not just AI summary)
- Format: Headers → Full Body → AI Analysis
- Preserves original formatting and attachments references

**Enhanced Logging**
- Success/failure icons for Zendesk posting visibility
- Explicit match logging (why emails will/won't be posted)
- Step-by-step debugging of email matching process
- Detailed error messages for failed operations

#### Phase 5: Zendesk-First Claims Flow

**Webhook-Driven Claim Creation**
- Claims automatically created from Zendesk tickets
- Endpoint: `POST /api/integrations/zd/claim-webhook/`
- Webhook secret verification using `SIDEBAR_SECRET_TOKEN`
- Comprehensive error handling with detailed messages

**LLM-Powered Ticket Analysis**
- Qwen AI extracts claim data from ticket content and comments
- Fetches up to 5 comments for context
- Structured extraction prompt for consistent output
- Fallback to requester email if LLM extraction fails

**ALF Claim ID Parsing**
- Automatic extraction from subject line using regex `ALF(\d{7})`
- Example: "Lost Item - ALF1234567" → `ALF1234567`
- Generates placeholder `ALF{ticket_id}` if not found

**Idempotency Protection**
- Check for existing claim with same `zd_ticket_id` before creation
- Returns existing claim ID if duplicate detected
- Safe for multiple webhook deliveries and manual re-plays

**New Claim Model Fields**
- `alf_claim_id` - CharField (unique, indexed, max_length=20)
- `zd_ticket_id` - CharField (indexed, max_length=20)
- `phone` - CharField (max_length=50, nullable)
- `alternate_email` - EmailField (nullable)
- `object_description` - TextField (nullable)
- `llm_extraction_failed` - BooleanField (default=False)

**Extracted Data Fields**
- `client_email` - Primary customer email
- `flight_details` - Flight number, date, and route
- `object_description` - Description of lost item
- `phone` - Phone number (if available)
- `alternate_email` - Alternate contact email (if available)

#### Phase 6: Claim Detail Enhancement

**ALF ID Prominent Display**
- Large badge in claim header
- Copy-to-clipboard functionality
- Visible in list views and detail pages

**AI Summary Section**
- Displays LLM-extracted claim data
- Shows extraction confidence/failure status
- Manual edit capability for corrected data

**Zendesk Update Timeline**
- Shows ticket status changes over time
- Links to Zendesk ticket
- Displays webhook receipt timestamps

**Update from Zendesk Feature**
- Manual sync button to refresh claim data from Zendesk
- Fetches latest ticket status and comments
- Updates claim fields if changed

### 🔧 Changed

#### Claim Creation Flow
- **Before**: Manual entry via form or external submission
- **After**: Automatic creation from Zendesk webhook with LLM extraction

#### Data Extraction
- **Before**: Manual data entry by agents
- **After**: LLM-powered automatic extraction from ticket content

#### Email Processing
- **Before**: Sentiment analysis + configurable field matching + summary-only posting
- **After**: No sentiment + hardcoded field matching + full email posting

#### Claim Statuses
- Added: `REFUND_REQUESTED`, `REFUNDED`, `PARTIALLY_REFUNDED`
- Integrated with refund workflow

#### Claim Origin
- **Before**: External forms, manual entry
- **After**: Zendesk tickets (webhook-driven)

### 🗑️ Removed

- `EmailLog.sentiment` field and database index
- `SENTIMENT_CHOICES` from EmailLog model
- Sentiment extraction from AI parsing pipeline
- Sentiment filter from email list UI
- Sentiment display from email detail UI
- `from_email` fallback matching in email processing
- Configurable Zendesk custom field ID (now hardcoded)

### 📦 Dependencies

No new dependencies added.

### ⚠️ Database Changes

#### payments.Refund (New Table)
| Field | Type | Constraints |
|-------|------|-------------|
| id | AutoField | Primary Key |
| claim | ForeignKey | claims.Claim, NOT NULL |
| amount | DecimalField | max_digits=10, decimal_places=2 |
| currency | CharField | max_length=3, default="USD" |
| refund_type | CharField | FULL, PARTIAL |
| reason | TextField | |
| status | CharField | REQUESTED, PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED |
| source | CharField | LORA, WOOCOMMERCE, MANUAL |
| paypal_transaction_id | CharField | max_length=100, unique, nullable |
| processed_by | ForeignKey | users.User, nullable |
| processed_at | DateTimeField | nullable |
| created_at | DateTimeField | auto_now_add |
| updated_at | DateTimeField | auto_now |
| metadata | JSONField | nullable |

#### claims.Claim (Modified)
| Field | Type | Constraints |
|-------|------|-------------|
| alf_claim_id | CharField | max_length=20, unique, indexed |
| zd_ticket_id | CharField | max_length=20, indexed |
| phone | CharField | max_length=50, nullable |
| alternate_email | EmailField | nullable |
| object_description | TextField | nullable |
| llm_extraction_failed | BooleanField | default=False |

#### claims.Claim (Status Choices)
- Added: `REFUND_REQUESTED`, `REFUNDED`, `PARTIALLY_REFUNDED`

#### communications.EmailLog (Removed)
- Removed: `sentiment` field
- Removed: `sentiment` database index

### 🚀 Migrations

```bash
# Apply all migrations
python manage.py migrate

# Expected migrations:
# - payments.0004_refund_initial (creates Refund model)
# - claims.0010_claim_alf_claim_id_claim_alternate_email_and_more (claim fields)
# - communications.0008_remove_emaillog_sentiment (remove sentiment)
```

### 📝 Configuration Requirements

#### Environment Variables (.env)

| Variable | Description | Required |
|----------|-------------|----------|
| `PAYPAL_CLIENT_ID` | PayPal API client ID | Yes (for refunds) |
| `PAYPAL_SECRET` | PayPal API secret | Yes (for refunds) |
| `PAYPAL_WEBHOOK_ID` | PayPal webhook identifier | Yes (for webhooks) |
| `PAYPAL_MODE` | sandbox or live | Yes |
| `SIDEBAR_SECRET_TOKEN` | Webhook authentication token | Yes (for Zendesk webhooks) |
| `ZENDESK_SUBDOMAIN` | Zendesk subdomain | Yes |
| `ZENDESK_TOKEN` | Zendesk API token | Yes |
| `ZENDESK_EMAIL` | Zendesk admin email | Yes |
| `AI_API_KEY` | Qwen/DeepSeek API key | Yes (for LLM extraction) |
| `AI_API_BASE` | AI API endpoint URL | Yes |
| `AI_API_MODEL` | Model name (e.g., qwen-plus) | Yes |

#### Zendesk Setup

**1. Create Custom Field for Email Aliases**
- Type: Text field
- Field ID: `13606076120860` (hardcoded in code)
- Purpose: Store email alias for matching (e.g., `client-123@yourdomain.com`)

**2. Create Trigger for Claim Creation**
- **Name**: `LORA - Create Claim on Investigation`
- **Conditions**:
  - `Status` is `Investigation Initiated`
  - `Type` is `Ticket`
- **Actions**:
  - Notify webhook: `https://your-lora.com/api/integrations/zd/claim-webhook/`
  - Headers:
    - `X-Webhook-Secret`: `your-sidebar-secret-token`
    - `Content-Type`: `application/json`
  - Payload:
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

**3. Create Trigger for Refund Status Sync**
- **Name**: `LORA - Sync Refund Status`
- **Conditions**:
  - `Status` is `Refund Requested`
- **Actions**:
  - Notify webhook: `https://your-lora.com/api/integrations/zd/status-webhook/`
  - Headers:
    - `X-Webhook-Secret`: `your-sidebar-secret-token`
    - `Content-Type`: `application/json`
  - Payload:
  ```json
  {
    "ticket_id": "{{ticket.id}}",
    "status": "refund_requested",
    "claim_id": "{{ticket.custom_fields.claim_id}}"
  }
  ```

### ⚠️ Breaking Changes

#### Email System
- **Sentiment field removed**: Any code referencing `EmailLog.sentiment` will fail
- **Custom field ID hardcoded**: If using a different Zendesk field for alias matching, update code or migrate data
- **Full email posting**: Zendesk tickets will now contain full email bodies (more content than before)

#### Claim Creation
- **Claims originate from Zendesk**: Manual claim creation still possible but primary flow is now webhook-driven
- **New required fields**: `alf_claim_id` is unique and indexed - ensure no duplicates during migration
- **LLM dependency**: Claim creation now requires AI provider connectivity

#### Refund System
- **New table**: `payments.Refund` must be migrated before use
- **PayPal credentials required**: Refund processing requires valid PayPal API credentials
- **Status workflow**: Claims with refund-related statuses need migration logic if existing data uses different statuses

### 🧪 Testing

**Run All Tests**
```bash
pytest apps/payments/tests/test_refund_model.py -v
pytest apps/payments/tests/test_refund_service.py -v
pytest apps/integrations/tests/test_webhooks.py -v
pytest apps/claims/tests/test_claim_webhook.py -v
```

**Test Coverage**
- Refund model: 11 tests
- RefundService: Integration tests for PayPal API
- Webhook handlers: Request validation, idempotency, error handling
- LLM extraction: Prompt validation, fallback behavior

### 📊 API Changes Summary

#### New Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/payments/refunds/` | GET, POST | List/Create refunds |
| `/api/payments/refunds/{id}/` | GET, PUT, PATCH, DELETE | CRUD single refund |
| `/api/payments/refunds/process/` | POST | Process refund via PayPal |
| `/api/payments/refunds/stats/` | GET | Get refund statistics |
| `/api/payments/refunds/{id}/update_status/` | POST | Update refund status |
| `/api/integrations/zd/claim-webhook/` | POST | Zendesk claim creation |
| `/api/integrations/zd/refund-webhook/` | POST | Refund notifications |
| `/api/integrations/zd/status-webhook/` | POST | Status change notifications |

### 📈 Statistics Endpoints

**Refund Statistics**
```bash
GET /api/payments/refunds/stats/

Response:
{
    "total_refunds": 25,
    "total_amount": 1250.00,
    "by_status": {
        "REQUESTED": 5,
        "PENDING": 3,
        "COMPLETED": 15,
        "FAILED": 2
    },
    "by_type": {
        "FULL": 20,
        "PARTIAL": 5
    },
    "by_source": {
        "LORA": 10,
        "WOOCOMMERCE": 10,
        "MANUAL": 5
    }
}
```

---

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
| 1.4.0 | 2026-03-18 | Zendesk-first claims, LLM extraction |
| 1.3.0 | 2026-03-17 | Email system improvements, simplification |
| 1.2.0 | 2026-03-17 | Refund management system |
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
