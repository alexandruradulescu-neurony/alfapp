# LORA Development Guide

This guide covers development workflows, architecture details, and best practices for working on the LORA application.

---

## Table of Contents

- [Development Setup](#development-setup)
- [Architecture Overview](#architecture-overview)
- [Code Organization](#code-organization)
- [Database Models](#database-models)
- [API Development](#api-development)
- [Frontend Development](#frontend-development)
- [Testing](#testing)
- [Debugging](#debugging)
- [Common Tasks](#common-tasks)

---

## Development Setup

### 1. Initial Setup

```bash
# Clone repository
git clone <repository-url>
cd alf-app

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Install Node dependencies
npm install

# Build CSS
npm run dev  # Development watch mode

# Create .env file
cp .env.example .env
# Edit .env with your credentials

# Initialize database
python manage.py migrate

# Create superuser
python manage.py createsuperuser
```

### 2. Development Server

```bash
# Run Django development server
python manage.py runserver

# Run CSS watch (separate terminal)
npm run dev
```

### 3. Pre-commit Setup (Recommended)

```bash
# Install pre-commit hooks
pip install pre-commit
pre-commit install
```

---

## Architecture Overview

### Layered Architecture

```
┌─────────────────────────────────────┐
│         Presentation Layer          │
│  (Templates, Static Files, JS)      │
├─────────────────────────────────────┤
│         Application Layer           │
│  (Views, Services, API Endpoints)   │
├─────────────────────────────────────┤
│           Domain Layer              │
│  (Models, Business Logic)           │
├─────────────────────────────────────┤
│        Infrastructure Layer         │
│  (Database, External APIs, Email)   │
└─────────────────────────────────────┘
```

### Request Flow

```
User Request
    ↓
URL Routing (urls.py)
    ↓
View Function/Class
    ↓
[Authentication/Permission Checks]
    ↓
Service Layer (Business Logic)
    ↓
Model Layer (Database Operations)
    ↓
External APIs (if needed)
    ↓
Response (HTML/JSON)
```

---

## Code Organization

### App Structure

Each app follows this structure:

```
apps/<app_name>/
├── __init__.py
├── apps.py              # App configuration
├── models.py            # Database models
├── views.py             # View functions/classes
├── serializers.py       # DRF serializers (if API)
├── urls.py              # URL routing (if API)
├── services.py          # Business logic layer
├── forms.py             # Django forms (if needed)
├── permissions.py       # DRF permissions (if needed)
├── decorators.py        # Custom decorators (if needed)
├── migrations/          # Database migrations
└── templates/<app>/     # App-specific templates
```

### Model Organization

**Location Pattern:**
- Simple apps: Single `models.py` file
- Complex apps: `models/` package with multiple files

**Example:**
```python
# apps/config/models.py
from django.db import models
from .encrypted_fields import EncryptedCharField

class SystemSettings(models.Model):
    """Singleton model for system configuration."""
    ai_api_key = EncryptedCharField(max_length=255)
    
    class Meta:
        verbose_name = 'System Settings'
    
    def save(self, *args, **kwargs):
        self.pk = 1  # Enforce singleton
        super().save(*args, **kwargs)
    
    @classmethod
    def get_instance(cls):
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance
```

### Service Layer Pattern

Services encapsulate business logic and external API interactions:

```python
# apps/communications/services.py
import imaplib
import logging
from typing import Dict, Any
from openai import OpenAI
from apps.config.models import SystemSettings
from apps.communications.models import EmailLog

logger = logging.getLogger(__name__)

def process_incoming_emails() -> Dict[str, Any]:
    """
    Main email processing function.
    
    Returns:
        Dict with statistics: processed, matched, auto_resolved, etc.
    """
    settings = SystemSettings.get_instance()
    
    # Connect to IMAP
    mail = imaplib.IMAP4_SSL(settings.imap_host)
    mail.login(settings.imap_user, settings.imap_pass)
    
    # Fetch emails...
    # Process with AI...
    # Create EmailLog records...
    
    return stats
```

---

## Database Models

### Common Patterns

#### Singleton Model

```python
class SystemSettings(models.Model):
    """Only one instance should exist (pk=1)."""
    
    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
    
    @classmethod
    def get_instance(cls):
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance
```

#### Encrypted Fields

```python
from apps.config.encrypted_fields import EncryptedCharField

class MyModel(models.Model):
    api_key = EncryptedCharField(max_length=255)
    # Automatically encrypted on save, decrypted on load
```

#### Status Choices

```python
class Claim(models.Model):
    STATUS_CHOICES = [
        ('Received', 'Received'),
        ('Searching', 'Searching'),
        ('Found', 'Found'),
        ('Shipped', 'Shipped'),
        ('Disputed', 'Disputed'),
    ]
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
```

### Query Optimization

**Use `select_related` for foreign keys:**
```python
# Bad: N+1 queries
claims = Claim.objects.all()
for claim in claims:
    print(claim.assigned_to.username)

# Good: 2 queries total
claims = Claim.objects.select_related('assigned_to').all()
```

**Use `prefetch_related` for many-to-many/reverse foreign keys:**
```python
claims = Claim.objects.prefetch_related('evidence').all()
```

**Use `annotate` for aggregates:**
```python
from django.db.models import Count

claims = Claim.objects.annotate(
    evidence_count=Count('evidence'),
    email_count=Count('emails')
)
```

---

## API Development

### Creating a ViewSet

```python
# apps/claims/views.py
from rest_framework import viewsets, permissions
from .models import Claim
from .serializers import ClaimSerializer
from apps.users.permissions import IsAgentOrManager

class ClaimViewSet(viewsets.ModelViewSet):
    queryset = Claim.objects.all()
    serializer_class = ClaimSerializer
    permission_classes = [IsAgentOrManager]
    
    def get_queryset(self):
        # Filter claims by assigned agent
        user = self.request.user
        if user.role == 'AGENT':
            return Claim.objects.filter(assigned_to=user)
        return super().get_queryset()
```

### Creating a Serializer

```python
# apps/claims/serializers.py
from rest_framework import serializers
from .models import Claim

class ClaimSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(
        source='get_status_display',
        read_only=True
    )
    
    class Meta:
        model = Claim
        fields = [
            'id', 'client_email', 'status', 'status_display',
            'item_description', 'location', 'created_at'
        ]
        read_only_fields = ['created_at']
```

### API URL Routing

```python
# apps/claims/urls.py
from rest_framework.routers import DefaultRouter
from .views import ClaimViewSet

router = DefaultRouter()
router.register(r'claims', ClaimViewSet, basename='claim')

urlpatterns = router.urls
```

---

## Frontend Development

### Template Structure

**Base Template (`base.html`):**
```django
{% extends 'base.html' %}
{% load static %}

{% block title %}Page Title{% endblock %}

{% block content %}
<!-- Your content here -->
{% endblock %}

{% block extra_js %}
<script src="{% static 'js/custom.js' %}"></script>
{% endblock %}
```

### Tailwind CSS Usage

**Utility Classes:**
```html
<!-- Button -->
<button class="btn btn-primary rounded-xl transition-all duration-200 hover:scale-[1.02]">
    <i class="bi bi-check-circle"></i> Save
</button>

<!-- Card -->
<div class="card-modern p-6 mb-6 animate-fade-in">
    <h3 class="text-lg font-semibold">Title</h3>
    <p class="text-base-content/70">Content</p>
</div>

<!-- Status Badge -->
<span class="badge badge-success gap-1">
    <i class="bi bi-circle-fill text-[0.4rem]"></i>
    Connected
</span>
```

**Custom Components:**
See `static/src/css/tailwind.css` for custom classes like `.card-modern`, `.stat-modern`, etc.

### JavaScript Patterns

**AJAX with Fetch:**
```javascript
// Get CSRF token
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

const csrftoken = getCookie('csrftoken');

// POST request
async function testData(url, data) {
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrftoken
        },
        body: JSON.stringify(data)
    });
    
    const result = await response.json();
    return result;
}
```

---

## Testing

### Running Tests

```bash
# Run all tests
python manage.py test

# Run specific app tests
python manage.py test apps.claims

# Run with verbosity
python manage.py test -v 2

# Run with coverage (if configured)
coverage run manage.py test
coverage report
```

### Writing Tests

**Model Test:**
```python
# tests/test_models.py
from django.test import TestCase
from apps.claims.models import Claim

class ClaimModelTest(TestCase):
    def test_claim_creation(self):
        claim = Claim.objects.create(
            client_email='test@example.com',
            item_description='Lost wallet',
            location='Flight AA123'
        )
        
        self.assertEqual(claim.status, 'Received')
        self.assertIsNotNone(claim.created_at)
```

**View Test:**
```python
# tests/test_views.py
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()

class ClaimViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass',
            role='MANAGER'
        )
        self.client.force_login(self.user)
    
    def test_claim_list(self):
        response = self.client.get(reverse('api:claims:claim-list'))
        self.assertEqual(response.status_code, 200)
```

**Service Test:**
```python
# tests/test_services.py
from django.test import TestCase
from unittest.mock import patch, MagicMock
from apps.communications.services import process_incoming_emails

class EmailServiceTest(TestCase):
    @patch('apps.communications.services.imaplib.IMAP4_SSL')
    def test_process_emails(self, mock_imap):
        # Mock IMAP connection
        mock_mail = MagicMock()
        mock_imap.return_value = mock_mail
        
        result = process_incoming_emails()
        
        self.assertIn('processed', result)
        mock_mail.login.assert_called_once()
```

---

## Debugging

### Django Debug Toolbar

Install and enable in development:

```bash
pip install django-debug-toolbar
```

Add to `INSTALLED_APPS` and `MIDDLEWARE` in `settings.py`.

Access at `http://localhost:8000/__debug__/`

### Logging

**Configure logging:**
```python
# lora_app/settings.py
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'apps.communications': {
            'handlers': ['console'],
            'level': 'DEBUG',
        },
    },
}
```

**Use logging in code:**
```python
import logging

logger = logging.getLogger(__name__)

def my_function():
    logger.debug('Debug message')
    logger.info('Info message')
    logger.warning('Warning message')
    logger.error('Error message')
```

### Python Debugger

```python
import pdb

def my_function():
    pdb.set_trace()  # Breakpoint
    # ... code ...
```

**Commands:**
- `n` - Next line
- `s` - Step into
- `c` - Continue
- `q` - Quit
- `p variable` - Print variable

---

## Common Tasks

### Create New App

```bash
python manage.py startapp apps/myapp
```

Update `lora_app/settings.py`:
```python
INSTALLED_APPS = [
    # ...
    'apps.myapp',
]
```

### Create Migration

```bash
# After model changes
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Create migration with name
python manage.py makemigrations --name add_new_field
```

### Create Superuser

```bash
python manage.py createsuperuser
```

### Collect Static Files

```bash
python manage.py collectstatic
```

### Build CSS for Production

```bash
npm run build
```

### Test Email Processing

```python
# Django shell
python manage.py shell

>>> from apps.communications.services import process_incoming_emails
>>> result = process_incoming_emails()
>>> print(result)
```

### Test AI Connection

```python
# Django shell
python manage.py shell

>>> from apps.config.models import SystemSettings
>>> from openai import OpenAI
>>> settings = SystemSettings.get_instance()
>>> client = OpenAI(api_key=settings.ai_api_key, base_url=settings.ai_api_base)
>>> response = client.chat.completions.create(
...     model=settings.ai_api_model,
...     messages=[{"role": "user", "content": "Hello"}]
... )
>>> print(response.choices[0].message.content)
```

### View Service Status

```python
# Django shell
python manage.py shell

>>> from apps.config.models import ServiceStatus
>>> for status in ServiceStatus.objects.all():
...     print(f"{status.service}: {status.status}")
```

### Trigger Service Test via API

```bash
curl -X POST http://localhost:8000/api/services/AI/test/ \
  -H "X-CSRFToken: <token>" \
  -H "Cookie: sessionid=<session>"
```

---

## Git Workflow

### Branch Naming

- `feature/description` - New features
- `fix/description` - Bug fixes
- `docs/description` - Documentation
- `refactor/description` - Code refactoring

### Commit Messages

Follow Conventional Commits:

```
feat: add service status monitoring
fix: resolve IMAP connection timeout
docs: update README with API examples
refactor: simplify email processing logic
```

### Pull Request Process

1. Create feature branch from `main`
2. Make changes and commit
3. Run tests
4. Push to remote
5. Create pull request
6. Code review
7. Merge to `main`

---

## Performance Tips

### Database Queries

- Use `select_related` and `prefetch_related`
- Use `only()` and `defer()` for large text fields
- Add indexes for frequently queried fields
- Use `annotate` for aggregates instead of Python loops

### Caching

```python
from django.core.cache import cache

# Get from cache
data = cache.get('my_key')

# Set cache (5 minutes)
cache.set('my_key', data, 300)

# Decorator caching
from django.views.decorators.cache import cache_page

@cache_page(60 * 15)  # 15 minutes
def my_view(request):
    ...
```

### Template Optimization

- Use `{% cache %}` template tag for expensive operations
- Avoid complex logic in templates
- Use `select_related` in template queries

---

## Troubleshooting

### Common Issues

**Migration conflicts:**
```bash
# Delete migration files (except __init__.py)
# Delete database
python manage.py migrate
```

**Static files not loading:**
```bash
python manage.py collectstatic --clear
npm run build
```

**CSS changes not appearing:**
- Hard refresh browser (Ctrl+Shift+R)
- Check browser dev tools for CSS file path
- Verify `npm run dev` is running

**Service status not updating:**
- Check service is enabled in SystemSettings
- Verify credentials are correct
- Check logs for error messages

---

## Resources

- [Django Docs](https://docs.djangoproject.com/)
- [DRF Docs](https://www.django-rest-framework.org/)
- [Tailwind Docs](https://tailwindcss.com/docs)
- [DaisyUI Docs](https://daisyui.com/)
