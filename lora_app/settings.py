"""
Django settings for LORA (Lost Object Recovery Automation) project.
"""

from pathlib import Path

import environ

# Build paths inside the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Initialize django-environ
env = environ.Env(
    DEBUG=(bool, False)
)

# Read .env file if it exists
ENV_FILE = BASE_DIR / '.env'
if ENV_FILE.exists():
    environ.Env.read_env(ENV_FILE)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env('SECRET_KEY')

# Separate encryption key for database field encryption (falls back to SECRET_KEY if not set)
ENCRYPTION_KEY = env('ENCRYPTION_KEY', default='')
# Previous encryption keys, retained so credentials encrypted under an old key stay
# decryptable after a key rotation (MultiFernet tries each in turn). Comma-separated.
ENCRYPTION_KEY_FALLBACKS = env.list('ENCRYPTION_KEY_FALLBACKS', default=[])

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env('DEBUG')

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# CSRF Trusted Origins. In production, set the CSRF_TRUSTED_ORIGINS env var to a
# comma-separated list that includes your real domain, e.g.
#   CSRF_TRUSTED_ORIGINS=https://lora.yourcompany.com
# Without your production domain here, login and config form POSTs are rejected
# with a 403 CSRF error. The env value REPLACES this default. The default covers
# the known production host + local dev only — no transient dev tunnel (ngrok) is
# trusted by default; add any tunnel you use via the env var while developing.
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[
    'https://alfapp-production.up.railway.app',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
])

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party apps
    'rest_framework',
    'django_filters',
    'auditlog',
    'csp',
    # Local apps
    'apps.users',
    'apps.claims',
    'apps.communications',
    'apps.payments',
    'apps.integrations',
    'apps.config',
    'apps.core',
    'apps.agent',
    'apps.ai',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves static files in production; must come right after
    # SecurityMiddleware and before everything else.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'auditlog.middleware.AuditlogMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Add CSP middleware only in production (when DEBUG is False)
if not DEBUG:
    MIDDLEWARE.append('csp.middleware.CSPMiddleware')

ROOT_URLCONF = 'lora_app.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'lora_app.wsgi.application'

# Database
DATABASES = {
    'default': env.db('DATABASE_URL', default='sqlite:///db.sqlite3')
}

# Cache — database-backed so per-IP login/sidebar throttle counters are SHARED
# across gunicorn workers. The default LocMemCache is per-process, so each worker
# keeps its own counter and the effective limit is multiplied by the worker count.
# The table is created by a migration (apps/config), so it exists in dev/test/prod
# after `migrate` (no separate `createcachetable` step needed).
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'lora_cache_table',
    }
}

# Custom User Model
AUTH_USER_MODEL = 'users.User'

# Test-speed: under pytest, use a fast password hasher. The test fixtures create
# many users, and the production PBKDF2 hasher (~260k iterations per hash)
# dominates the suite runtime. This is the standard Django test optimization and
# only applies when running under pytest — production keeps the secure default.
import sys as _sys
if 'pytest' in _sys.modules:
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

# Serve static files in production via WhiteNoise (no separate web server / CDN
# needed). CompressedStaticFilesStorage gzips/brotli-compresses files but does
# NOT use manifest hashing — chosen so a template referencing a not-yet-collected
# static file won't 500 the whole page. WhiteNoise middleware is added below.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

# Media files (user uploads, e.g. claim evidence images)
# Leading slash so {{ image.url }} produces a root-relative URL that works on
# nested pages (claim detail, etc.), not one relative to the current path.
MEDIA_URL = '/media/'
# MEDIA_ROOT is env-driven so a persistent disk (e.g. a Railway Volume) can be
# mounted at a different path in production. Defaults to <project>/media for dev.
MEDIA_ROOT = env('MEDIA_ROOT', default=str(BASE_DIR / 'media'))

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email Configuration
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = env('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = env.int('EMAIL_PORT', default=587)
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=True)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')

# Django REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        # Note: BasicAuthentication removed for security - use Session or Token auth
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
    # Rate limiting
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        # No custom DRF scopes are kept here: login throttling is enforced by
        # apps.users.views.rate_limit_logins (a plain Django view), and the
        # PayPal/Zendesk webhooks are AllowAny + HMAC-secret-verified, relying on
        # the global anon throttle. A scope defined but never wired (no
        # ScopedRateThrottle references them) is a misleading safety net.
        # NOTE: webhook deliveries share the global anon 100/hour bucket — fine at
        # current volume; give them a dedicated ScopedRateThrottle if it grows.
    },
}

# Scheduled jobs run via a Railway cron job: `python manage.py run_scheduled_jobs`
# (apps/core/management/commands). No in-process scheduler — see that command.

# AI API Configuration (DeepSeek, Qwen, or other OpenAI-compatible providers)
# Note: Runtime configuration is done via SystemSettings model
# These env vars serve as defaults/fallbacks
AI_PROVIDER = env('AI_PROVIDER', default='DeepSeek')
AI_API_BASE = env('AI_API_BASE', default='https://api.deepseek.com/v1')
AI_API_KEY = env('AI_API_KEY', default='')
AI_API_MODEL = env('AI_API_MODEL', default='deepseek-chat')

# PII tokenization
# Used as HMAC-SHA256 key for deterministic placeholder generation in apps.ai.
# SystemSettings.pii_tokenization_salt overrides this if set (allows runtime rotation).
PII_TOKENIZATION_SALT = env('PII_TOKENIZATION_SALT', default='')

# AI client behavior
AI_VALIDATION_STRICT = env.bool('AI_VALIDATION_STRICT', default=True)
AI_TOKENIZER_BACKEND = env('AI_TOKENIZER_BACKEND', default='regex')
AI_PHONE_DEFAULT_REGION = env('AI_PHONE_DEFAULT_REGION', default='US')
AI_PHONE_FALLBACK_REGIONS = env.list('AI_PHONE_FALLBACK_REGIONS', default=['GB', 'FR', 'DE', 'IT', 'ES', 'JP'])

# Bound the synchronous LLM call so a slow/hung provider can't tie up a gunicorn
# worker for the OpenAI SDK default (~600s). complete() runs on the request path
# (Zendesk briefing/chat), so this caps worst-case per-request worker occupancy.
AI_TIMEOUT = env.int('AI_TIMEOUT', default=30)  # seconds, per LLM request
AI_MAX_RETRIES = env.int('AI_MAX_RETRIES', default=1)  # OpenAI SDK auto-retry count

# Legacy Qwen settings (for backward compatibility)
QWEN_API_BASE = env('QWEN_API_BASE', default='https://dashscope.aliyuncs.com/compatible-mode/v1')
QWEN_API_KEY = env('QWEN_API_KEY', default='')
QWEN_MODEL = env('QWEN_MODEL', default='qwen-plus')

# Zendesk Configuration
ZENDESK_SUBDOMAIN = env('ZENDESK_SUBDOMAIN', default='')
ZENDESK_TOKEN = env('ZENDESK_TOKEN', default='')
ZENDESK_EMAIL = env('ZENDESK_EMAIL', default='')
# Zendesk custom-status id that signals "Investigation Initiated" — a webhook at
# this status on an unknown ticket triggers full claim creation. Deploy/tenant
# specific; the default preserves the historical hardcoded value.
ZENDESK_INVESTIGATION_STATUS_ID = env('ZENDESK_INVESTIGATION_STATUS_ID', default='11688538967068')

# PayPal Configuration
PAYPAL_CLIENT_ID = env('PAYPAL_CLIENT_ID', default='')
PAYPAL_SECRET = env('PAYPAL_SECRET', default='')
PAYPAL_WEBHOOK_ID = env('PAYPAL_WEBHOOK_ID', default='')
PAYPAL_MODE = env('PAYPAL_MODE', default='sandbox')

# Zendesk Sidebar Authentication
SIDEBAR_SECRET_TOKEN = env('SIDEBAR_SECRET_TOKEN', default='')

# IMAP Configuration
IMAP_HOST = env('IMAP_HOST', default='imap.gmail.com')
IMAP_USER = env('IMAP_USER', default='')
IMAP_PASS = env('IMAP_PASS', default='')

# API Timeout Settings (seconds)
API_TIMEOUT = env.int('API_TIMEOUT', default=30)  # Default timeout for external API calls
IMAP_TIMEOUT = env.int('IMAP_TIMEOUT', default=30)  # IMAP connection timeout
ZENDESK_TIMEOUT = env.int('ZENDESK_TIMEOUT', default=30)  # Zendesk API timeout
PAYPAL_TIMEOUT = env.int('PAYPAL_TIMEOUT', default=30)  # PayPal API timeout

# Client-IP resolution for per-IP throttling (apps.core.utils.get_client_ip).
# In production LORA sits behind a single TLS-terminating proxy (Railway), so the
# real client IP comes from X-Forwarded-For, not REMOTE_ADDR. Off by default under
# DEBUG (no proxy locally). TRUSTED_PROXY_DEPTH = number of proxies in front.
USE_X_FORWARDED_FOR = env.bool('USE_X_FORWARDED_FOR', default=not DEBUG)
TRUSTED_PROXY_DEPTH = env.int('TRUSTED_PROXY_DEPTH', default=1)

# Session Security Settings
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG  # Only secure in production
SESSION_COOKIE_AGE = 3600  # 1 hour
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# CSRF Security
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = not DEBUG  # Only secure in production
CSRF_COOKIE_SAMESITE = 'Lax'

# CSP Configuration (updated format for django-csp 4.0+) - only in production
if not DEBUG:
    CONTENT_SECURITY_POLICY = {
        'DIRECTIVES': {
            'default-src': ["'self'"],
            'script-src': ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net", "https://code.jquery.com", "https://stackpath.bootstrapcdn.com"],
            'style-src': ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net", "https://stackpath.bootstrapcdn.com", "https://fonts.googleapis.com"],
            'img-src': ["'self'", "data:", "https:"],
            # font-src must allow the Bootstrap Icons font files (jsDelivr) and the
            # Google Fonts file host (gstatic). The matching stylesheets are
            # allowed in style-src above (jsDelivr + fonts.googleapis.com).
            'font-src': ["'self'", "https://fonts.gstatic.com", "https://cdn.jsdelivr.net"],
            'connect-src': ["'self'", "https://api.paypal.com", "https://api.sandbox.paypal.com"],
            'frame-ancestors': ["'none'"],
        }
    }

# Security settings for production
if not DEBUG:
    # Behind a TLS-terminating proxy (Railway, Render, Heroku, most PaaS), the
    # proxy handles HTTPS and forwards plain HTTP to the app, setting the
    # X-Forwarded-Proto header to 'https'. Without this, SECURE_SSL_REDIRECT
    # never sees the request as secure and redirects to https forever (infinite
    # 301 loop -> "too many redirects").
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Don't leak full claim/payment URLs (which embed identifiers) to the
    # third-party CDN origins allowed by the CSP above.
    SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'


# Logging: a console handler (Railway captures stdout/stderr) so unhandled 500s
# and integration failures (PayPal/Zendesk/IMAP/LLM) surface in the logs instead
# of being lost. Level is tunable via the LOG_LEVEL env var.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    # Configure the ROOT logger only: every logger (apps.*, django.request, …)
    # propagates here, so all records reach the console handler — and test log
    # capture (pytest caplog / assertLogs, which attach at the root) keeps working.
    # Do NOT add per-logger handlers with propagate=False: that detaches them from
    # the root and silently breaks caplog-based tests.
    'root': {'handlers': ['console'], 'level': env('LOG_LEVEL', default='INFO')},
}
