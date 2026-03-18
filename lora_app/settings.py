"""
Django settings for LORA (Lost Object Recovery Automation) project.
"""

import os
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

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env('DEBUG')

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

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
    'django_apscheduler',
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
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
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

# Custom User Model
AUTH_USER_MODEL = 'users.User'

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

# Media files (uploads)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

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
        'login': '5/min',
        'paypal_webhook': '100/hour',
        'zendesk_sidebar': '30/min',  # Rate limit for Zendesk sidebar widget
    },
}

# APScheduler Settings
APSCHEDULER_DATETIME_FORMAT = env('APSCHEDULER_DATETIME_FORMAT', default='N j, Y, f:s a')
APSCHEDULER_RUN_NOW_TIMEOUT = env.int('APSCHEDULER_RUN_NOW_TIMEOUT', default=25)
APSCHEDULER_DEFAULT_TIMEZONE = env('TIME_ZONE', default='UTC')

# AI API Configuration (DeepSeek, Qwen, or other OpenAI-compatible providers)
# Note: Runtime configuration is done via SystemSettings model
# These env vars serve as defaults/fallbacks
AI_PROVIDER = env('AI_PROVIDER', default='DeepSeek')
AI_API_BASE = env('AI_API_BASE', default='https://api.deepseek.com/v1')
AI_API_KEY = env('AI_API_KEY', default='')
AI_API_MODEL = env('AI_API_MODEL', default='deepseek-chat')

# Legacy Qwen settings (for backward compatibility)
QWEN_API_BASE = env('QWEN_API_BASE', default='https://dashscope.aliyuncs.com/compatible-mode/v1')
QWEN_API_KEY = env('QWEN_API_KEY', default='')
QWEN_MODEL = env('QWEN_MODEL', default='qwen-plus')

# Zendesk Configuration
ZENDESK_SUBDOMAIN = env('ZENDESK_SUBDOMAIN', default='')
ZENDESK_TOKEN = env('ZENDESK_TOKEN', default='')
ZENDESK_EMAIL = env('ZENDESK_EMAIL', default='')

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
            'style-src': ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net", "https://stackpath.bootstrapcdn.com"],
            'img-src': ["'self'", "data:", "https:"],
            'font-src': ["'self'", "https://fonts.gstatic.com"],
            'connect-src': ["'self'", "https://api.paypal.com", "https://api.sandbox.paypal.com"],
            'frame-ancestors': ["'none'"],
        }
    }

# Security settings for production
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
