# LORA Security Audit - Fixes Applied

## Summary

This document lists all security and code quality fixes applied to the LORA application following a comprehensive security audit.

**Date:** 2026-03-12  
**Total Issues Fixed:** 14 (6 Critical + 8 High)

---

## CRITICAL Issues Fixed (6)

### 1. ✅ Encrypt Secrets in Database

**File:** `apps/config/models.py`, `apps/config/encrypted_fields.py`

**Issue:** All API tokens, passwords, and secrets stored as plaintext in database.

**Fix:**
- Created custom `EncryptedCharField` and `EncryptedTextField` using `cryptography.fernet`
- Applied encryption to sensitive fields:
  - `imap_pass`
  - `zd_token`
  - `paypal_secret`
  - `sidebar_secret_token`
  - `paypal_client_id`

**Dependencies Added:**
```
cryptography>=41.0.0
```

---

### 2. ✅ Settings Form Pre-fills Secrets

**File:** `templates/manager/settings.html`, `apps/users/views.py`

**Issue:** Password/token fields exposed in HTML source via `value="{{ settings.field }}"`.

**Fix:**
- Changed password fields to `type="password"` with `autocomplete="off"`
- Use placeholder text `••••••••••••` instead of actual values
- Added helper text: "Current value is set. Enter new value to change."
- Updated view to only update sensitive fields if new value provided (non-empty)

---

### 3. ✅ Timing Attack on Sidebar Token

**File:** `apps/integrations/views.py`

**Issue:** `provided_token == expected_token` vulnerable to timing attacks.

**Fix:**
```python
import hmac

# Use constant-time comparison
return hmac.compare_digest(
    provided_token.encode('utf-8'),
    expected_token.encode('utf-8')
)
```

---

### 4. ✅ BasicAuthentication Enabled

**File:** `lora_app/settings.py`

**Issue:** BasicAuth sends plaintext credentials in HTTP headers on every request.

**Fix:**
- Removed `'rest_framework.authentication.BasicAuthentication'` from `REST_FRAMEWORK` settings
- Kept only `SessionAuthentication`
- Added rate limiting configuration

**Additional Security Settings Added:**
```python
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 3600  # 1 hour
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = 'Lax'
```

---

### 5. ✅ XSS: Unescaped Email Body

**File:** `templates/agent/claim_detail.html`

**Issue:** `<pre>{{ email.body }}</pre>` renders raw email content without escaping.

**Fix:**
```django
<pre>{{ email.body|escape }}</pre>
```

---

### 6. ✅ XSS: Unescaped Zendesk Comments in PDF

**File:** `apps/payments/templates/proof_of_work.html`

**Issue:** `{{ comment.body|linebreaks }}` doesn't escape HTML before applying linebreaks.

**Fix:**
```django
{{ comment.body|escape|linebreaks }}
```

---

## HIGH Issues Fixed (8)

### 7. ✅ Weak Permission Class

**Files:** `apps/communications/views.py`, `apps/claims/views.py`

**Issue:** `IsAgentOrManager` only checks `hasattr(request.user, 'role')` without validating value.

**Fix:**
```python
def has_permission(self, request, view):
    if not request.user.is_authenticated:
        return False
    if not hasattr(request.user, 'role'):
        return False
    return request.user.role in ['AGENT', 'MANAGER']  # Explicit validation
```

---

### 8. ✅ Demo Credentials in Login Template

**File:** `templates/login.html`, `apps/users/views.py`

**Issue:** Hardcoded demo usernames/passwords visible to all users.

**Fix:**
- Wrapped demo credentials section in `{% if debug %}`
- Only shows when DEBUG mode is enabled
- Updated view to pass `debug` context variable

---

### 9. ✅ Missing ProofOfWorkPDFView URL

**File:** `apps/payments/urls.py`

**Issue:** `ProofOfWorkPDFView` defined but not registered in URLs.

**Fix:**
```python
from apps.payments.views import PayPalWebhookView, ProofOfWorkPDFView

urlpatterns = [
    path('paypal/webhook/', PayPalWebhookView.as_view(), name='paypal-webhook'),
    path('proof-of-work/<int:claim_id>/', ProofOfWorkPDFView.as_view(), name='proof-of-work-pdf'),
]
```

---

### 10. ✅ No File Upload Validation

**File:** `apps/users/views.py` (agent_upload_evidence)

**Issue:** No file type validation, size limits, or content scanning on evidence uploads.

**Fix:**
- Added max file size check (10MB)
- Added MIME type validation (`image/jpeg`, `image/png`, `image/gif`, `image/webp`)
- Added file extension validation
- Proper error messages for each validation failure

---

### 11. ✅ Database Indexes Missing

**File:** `apps/claims/models.py`

**Issue:** `status` and `created_at` fields lack indexes despite frequent filtering/ordering.

**Fix:**
```python
client_email = models.EmailField(unique=True, db_index=True)
status = models.CharField(..., db_index=True)
zd_ticket_id = models.CharField(..., db_index=True)
created_at = models.DateTimeField(..., db_index=True)
updated_at = models.DateTimeField(..., db_index=True)

class Meta:
    indexes = [
        models.Index(fields=['-created_at']),
        models.Index(fields=['status', '-created_at']),
        models.Index(fields=['client_email']),
    ]
```

---

### 12. ✅ No Rate Limiting

**File:** `lora_app/settings.py`

**Issue:** No throttling on login, API, or webhook endpoints.

**Fix:**
```python
REST_FRAMEWORK = {
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        'login': '5/min',
        'paypal_webhook': '100/hour',
    },
}
```

---

### 13. ✅ Hardcoded Status Strings

**Issue:** Status values like 'Received', 'Searching' hardcoded throughout views.

**Fix:**
- Updated code to use `Claim.STATUS_CHOICES` constant
- Example: `valid_statuses = [choice[0] for choice in Claim.STATUS_CHOICES]`

---

### 14. ✅ Loose Dependency Versions

**File:** `requirements.txt`

**Issue:** All dependencies use `>=` without upper bounds.

**Fix:**
```txt
# Before
Django>=5.0,<6.0
cryptography>=41.0.0

# After (pinned versions for production)
Django==5.2.12
cryptography==46.0.5
weasyprint==68.1
djangorestframework==3.16.1
```

---

## Additional Improvements

### Session Security
- `SESSION_COOKIE_HTTPONLY = True` - Prevents JavaScript access
- `SESSION_COOKIE_AGE = 3600` - 1 hour session timeout
- `SESSION_EXPIRE_AT_BROWSER_CLOSE = True` - Sessions expire when browser closes

### CSRF Protection
- `CSRF_COOKIE_HTTPONLY = True` - Prevents JavaScript access
- `CSRF_COOKIE_SAMESITE = 'Lax'` - Prevents CSRF attacks

---

## Migration Required

After applying these fixes, run:

```bash
py -3.10 manage.py migrate
```

This will apply the encryption field changes and database indexes.

---

## Remaining Recommendations (Not Implemented)

### Medium Priority
- Implement object-level permissions for claims
- Add webhook idempotency (store processed event IDs)
- Use `transaction.atomic()` for multi-step DB operations
- Add pagination to claim list views
- Implement retry logic with exponential backoff for external APIs
- Cache PayPal OAuth token

### Low Priority
- Add audit logging (django-auditlog)
- Add custom error pages (404, 500)
- Migrate from `urllib` to `requests` library
- Add Content Security Policy headers
- Add structured logging with correlation IDs

---

## Testing

After applying fixes, verify:

1. **Encryption:**
   ```bash
   py -3.10 manage.py shell
   >>> from apps.config.models import SystemSettings
   >>> s = SystemSettings.get_instance()
   >>> s.imap_pass  # Should decrypt automatically
   ```

2. **Authentication:**
   - Login with demo credentials
   - Verify API access with session auth only
   - Verify BasicAuth is rejected

3. **File Upload:**
   - Try uploading file > 10MB (should fail)
   - Try uploading non-image file (should fail)

4. **XSS Protection:**
   - View email with HTML content
   - Verify PDF generation with special characters

---

## Security Checklist for Production

- [ ] Set `DEBUG = False`
- [ ] Set `SECRET_KEY` to random value
- [ ] Configure `ALLOWED_HOSTS`
- [ ] Enable HTTPS (`SECURE_SSL_REDIRECT = True`)
- [ ] Set up encrypted field encryption key (separate from SECRET_KEY)
- [ ] Configure proper email backend
- [ ] Set up PayPal webhook in PayPal dashboard
- [ ] Configure Zendesk sidebar widget URL
- [ ] Run `python manage.py check --deploy`
- [ ] Set up monitoring and alerting
- [ ] Configure log aggregation
- [ ] Set up database backups
- [ ] Document incident response procedure
