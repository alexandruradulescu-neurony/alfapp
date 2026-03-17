# Service Monitoring System

**Version:** 1.0  
**Last Updated:** March 17, 2026

---

## Overview

The Service Monitoring System provides real-time visibility into the health and status of all external service integrations and background processes in LORA.

---

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│                  UI Layer (Dashboard)                    │
│  - Status indicators  - Toggle switches  - Test buttons  │
├─────────────────────────────────────────────────────────┤
│                   API Layer                              │
│  - ServiceStatusViewSet  - Control endpoints             │
├─────────────────────────────────────────────────────────┤
│                Service Layer                             │
│  - ConnectionTester  - SchedulerController               │
├─────────────────────────────────────────────────────────┤
│                  Data Layer                              │
│  - ServiceStatus model (database)                        │
└─────────────────────────────────────────────────────────┘
```

### Services Monitored

| Service | Type | Check Method | Status Values |
|---------|------|--------------|---------------|
| **AI Provider** | External API | Endpoint reachability | connected, disconnected, error |
| **IMAP Email** | External Server | Login authentication | connected, disconnected, error |
| **Zendesk** | External API | API access test | connected, disconnected, error |
| **PayPal** | External API | OAuth2 token request | connected, disconnected, error |
| **Email Scheduler** | Background Process | APScheduler state | running, stopped, error |
| **Screenshot Service** | Local Service | Playwright installation | connected, disconnected, error |

---

## Database Model

### ServiceStatus Model

**Location:** `apps/config/models.py`

```python
class ServiceStatus(models.Model):
    """Track status of external service connections and background services."""
    
    SERVICE_CHOICES = [
        ('AI', 'AI Provider'),
        ('IMAP', 'IMAP Email'),
        ('ZENDESK', 'Zendesk'),
        ('PAYPAL', 'PayPal'),
        ('SCHEDULER', 'Email Scheduler'),
        ('SCREENSHOT', 'Screenshot Service'),
    ]
    
    STATUS_CHOICES = [
        ('connected', 'Connected'),
        ('disconnected', 'Disconnected'),
        ('error', 'Error'),
        ('running', 'Running'),
        ('stopped', 'Stopped'),
    ]
    
    service = models.CharField(
        max_length=20,
        choices=SERVICE_CHOICES,
        unique=True
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='disconnected'
    )
    is_enabled = models.BooleanField(default=True)
    last_checked = models.DateTimeField(auto_now_add=True)
    last_error = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
```

### Methods

```python
# Mark service as connected
status.mark_connected()

# Mark service as disconnected
status.mark_disconnected()

# Mark service as having an error
status.mark_error('Connection timeout')

# Get DaisyUI color class
color = status.get_status_color()  # 'success', 'neutral', 'error', 'primary', 'warning'
```

---

## Service Layer

### ConnectionTester

**Location:** `apps/config/services/connection_tester.py`

Tests connections to external services.

```python
from apps.config.services.connection_tester import ConnectionTester

tester = ConnectionTester()

# Test individual services
result = tester.test_ai()
result = tester.test_imap()
result = tester.test_zendesk()
result = tester.test_paypal()
result = tester.get_scheduler_status()
result = tester.get_screenshot_status()

# Test all services
results = tester.test_all_services()
```

**Return Format:**
```python
{
    'success': True,
    'status': 'connected',
    'message': 'AI provider is reachable',
    'service': 'AI'
}
```

### SchedulerController

**Location:** `apps/config/services/scheduler_controller.py`

Controls the email processing scheduler.

```python
from apps.config.services.scheduler_controller import SchedulerController

controller = SchedulerController()

# Start scheduler
result = controller.start()

# Stop scheduler
result = controller.stop()

# Toggle enabled state
result = controller.toggle_enabled(False)

# Get scheduler info
info = controller.get_info()
```

---

## API Endpoints

### List All Services

```http
GET /api/services/status/
Authorization: Session

Response: 200 OK
{
    "services": [
        {
            "service": "AI",
            "service_name": "AI Provider",
            "status": "connected",
            "status_name": "Connected",
            "status_color": "success",
            "is_enabled": true,
            "last_checked": "2026-03-17T10:30:00Z",
            "last_error": "",
            "metadata": {}
        },
        ...
    ]
}
```

### Get Single Service

```http
GET /api/services/status/{service}/
Authorization: Session

Response: 200 OK
{
    "service": "IMAP",
    "status": "connected",
    ...
}
```

### Test Connection

```http
POST /api/services/{service}/test/
Authorization: Session
X-CSRFToken: <token>

Response: 200 OK
{
    "success": true,
    "status": "connected",
    "message": "IMAP server connection successful"
}
```

### Toggle Service

```http
POST /api/services/{service}/toggle/
Authorization: Session
X-CSRFToken: <token>
Content-Type: application/json

{
    "enabled": false
}

Response: 200 OK
{
    "success": true,
    "service": "IMAP",
    "is_enabled": false,
    "message": "Service IMAP disabled"
}
```

### Scheduler Control

```http
# Start scheduler
POST /api/services/scheduler/start/
Authorization: Session
X-CSRFToken: <token>

Response: 200 OK
{
    "success": true,
    "status": "running",
    "message": "Email scheduler started successfully"
}

# Stop scheduler
POST /api/services/scheduler/stop/
Authorization: Session
X-CSRFToken: <token>

Response: 200 OK
{
    "success": true,
    "status": "stopped",
    "message": "Email scheduler stopped successfully"
}

# Toggle scheduler enabled
POST /api/services/scheduler/toggle/
Authorization: Session
X-CSRFToken: <token>
Content-Type: application/json

{
    "enabled": false
}

Response: 200 OK
{
    "success": true,
    "status": "disabled",
    "message": "Scheduler disabled",
    "previously": "enabled"
}

# Get scheduler info
GET /api/services/scheduler/info/
Authorization: Session

Response: 200 OK
{
    "success": true,
    "running": true,
    "enabled": true,
    "status": "running",
    "jobs": [
        {
            "id": "process_incoming_emails",
            "next_run": "2026-03-17T10:33:00Z"
        }
    ]
}
```

---

## UI Components

### Services Dashboard Widget

**Location:** `templates/config/services_dashboard.html`

Displays status cards for all services.

**Features:**
- Responsive grid layout (3 columns on large screens)
- Service-specific icons and colors
- Enable/disable toggle switches
- Test buttons for each service
- Start/Stop buttons for scheduler
- Error message display
- Auto-refresh every 2 minutes

### Status Indicators

**Color Coding:**

| Status | Color | Badge Class | Meaning |
|--------|-------|-------------|---------|
| Connected | Green | `badge-success` | Service is operational |
| Disconnected | Gray | `badge-neutral` | Not configured or unreachable |
| Error | Red | `badge-error` | Configuration or authentication error |
| Running | Blue | `badge-primary` | Background service active |
| Stopped | Yellow | `badge-warning` | Background service stopped |

### JavaScript Controls

**Location:** `static/js/service-controls.js`

**Functions:**

```javascript
// Test a service connection
testService('AI')

// Toggle service enabled state
toggleService('IMAP', true)

// Control scheduler
controlScheduler('start')
controlScheduler('stop')
toggleSchedulerEnabled(false)

// Refresh statuses
refreshServiceStatus('AI')
refreshAllStatuses()

// Toast notifications
showToast('Test successful', 'success')
hideToast()
```

---

## Usage Guide

### Accessing Service Monitoring

1. Log in as MANAGER
2. Navigate to **Manager → Configuration**
3. Scroll to service sections

### Testing Connections

**Manual Test:**
1. Click "Test" button for the service
2. Wait for response (up to 10 seconds)
3. View result in toast notification
4. Status badge updates automatically

**Auto-Refresh:**
- Statuses refresh every 2 minutes
- Click "Refresh All" for immediate update

### Enabling/Disabling Services

1. Toggle the "Enabled" switch
2. Confirmation toast appears
3. Service won't be used when disabled

### Controlling Scheduler

**Start:**
1. Click play button (▶)
2. Status changes to "Running"
3. Email processing begins (every 3 minutes)

**Stop:**
1. Click stop button (⬛)
2. Status changes to "Stopped"
3. Email processing pauses

**Disable:**
1. Toggle "Enabled" switch off
2. Scheduler won't start even if "Start" is clicked

---

## Troubleshooting

### Common Issues

#### AI Provider Shows "Disconnected"

**Causes:**
- API key not configured
- API base URL incorrect
- Network connectivity issue

**Resolution:**
1. Check AI configuration in SystemSettings
2. Verify API key is set
3. Test API base URL in browser
4. Check firewall/proxy settings

#### IMAP Shows "Error"

**Causes:**
- Incorrect credentials
- IMAP server unreachable
- Authentication failed

**Resolution:**
1. Verify IMAP host, username, password
2. Check if IMAP is enabled on email account
3. Test connection with email client
4. Check app-specific password requirement

#### Scheduler Won't Start

**Causes:**
- APScheduler not initialized
- Database lock
- Code error in tasks.py

**Resolution:**
1. Check Django logs for errors
2. Verify `register_scheduler_jobs()` is called
3. Restart Django server
4. Check database file permissions

#### Screenshot Service Shows "Error"

**Causes:**
- Playwright not installed
- Chromium browser missing
- PATH not configured

**Resolution:**
```bash
# Install Playwright
pip install playwright

# Install browsers
playwright install chromium

# Verify installation
playwright --version
```

### Checking Logs

**Django Logs:**
```bash
# Console output (development)
python manage.py runserver

# Or check log files if configured
tail -f logs/django.log
```

**Service Status in Database:**
```python
# Django shell
python manage.py shell

>>> from apps.config.models import ServiceStatus
>>> status = ServiceStatus.objects.get(service='AI')
>>> print(f"Status: {status.status}")
>>> print(f"Error: {status.last_error}")
>>> print(f"Checked: {status.last_checked}")
```

---

## Best Practices

### Monitoring

- Check service status daily in production
- Set up alerts for error states
- Review `last_error` messages for debugging
- Monitor scheduler run frequency

### Testing

- Test connections after configuration changes
- Verify all services before deployment
- Use test buttons for troubleshooting
- Document known issues in error messages

### Security

- Keep API keys and credentials secure
- Rotate credentials periodically
- Monitor for unauthorized access attempts
- Use HTTPS in production

### Performance

- Auto-refresh interval: 2 minutes (default)
- Test timeout: 10 seconds
- Scheduler max instances: 1 (prevents overlap)
- Database indexes on `service` field

---

## API Reference (JavaScript)

### Example: Service Status Dashboard

```javascript
// Fetch all service statuses
async function fetchServiceStatuses() {
    const response = await fetch('/api/services/status/');
    const data = await response.json();
    return data.services;
}

// Test AI connection
async function testAI() {
    const response = await fetch('/api/services/AI/test/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        }
    });
    return await response.json();
}

// Start scheduler
async function startScheduler() {
    const response = await fetch('/api/services/scheduler/start/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': getCookie('csrftoken')
        }
    });
    return await response.json();
}
```

---

## Migration Guide

### Adding New Service

1. **Add to ServiceStatus model:**
```python
# Add to SERVICE_CHOICES
('NEW_SERVICE', 'New Service Name')
```

2. **Create database migration:**
```bash
python manage.py makemigrations
python manage.py migrate
```

3. **Add test method to ConnectionTester:**
```python
def test_new_service(self):
    try:
        # Test logic here
        return self._update_status(
            'NEW_SERVICE',
            'connected',
            success=True,
            message='Connection successful'
        )
    except Exception as e:
        return self._update_status(
            'NEW_SERVICE',
            'error',
            success=False,
            message=str(e)
        )
```

4. **Add to test_all_services:**
```python
def test_all_services(self):
    results = {
        # ... existing services ...
        'NEW_SERVICE': self.test_new_service(),
    }
    return results
```

5. **Add API endpoint (if needed):**
```python
# apps/config/api/views.py
test_methods['NEW_SERVICE'] = tester.test_new_service
```

6. **Add UI component:**
```html
<!-- templates/config/services_dashboard.html -->
<!-- Add service card with status badge, toggle, test button -->
```

---

## Resources

- **Code Locations:**
  - Model: `apps/config/models.py`
  - Services: `apps/config/services/`
  - API: `apps/config/api/`
  - Templates: `templates/config/`
  - JavaScript: `static/js/service-controls.js`

- **Related Documentation:**
  - [Development Guide](DEVELOPMENT.md)
  - [README](../README.md)
  - [API Reference](../README.md#api-reference)
