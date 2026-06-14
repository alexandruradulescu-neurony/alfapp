"""Deploy-time system checks for the config app."""

from django.conf import settings
from django.core.checks import Warning, register


@register()
def encryption_key_configured(app_configs, **kwargs):
    """Warn (in production) when no dedicated ENCRYPTION_KEY is set.

    Encrypted credentials (PayPal/Zendesk/IMAP secrets, etc.) are derived from
    ENCRYPTION_KEY, or SECRET_KEY when that is unset (see encrypted_fields). The
    fallback couples credential decryption to the app signing key: rotating
    SECRET_KEY would make every stored credential undecryptable. We do NOT change
    the derivation here (doing so would break already-encrypted data) — we just
    surface the coupling so a dedicated, stable key gets set deliberately."""
    if settings.DEBUG:
        return []
    if (getattr(settings, 'ENCRYPTION_KEY', '') or '').strip():
        return []
    return [Warning(
        "No dedicated ENCRYPTION_KEY is set; encrypted credentials fall back to "
        "SECRET_KEY. Rotating SECRET_KEY would make stored credentials undecryptable.",
        hint="Set the ENCRYPTION_KEY env var to a stable secret (distinct from "
             "SECRET_KEY) BEFORE any credentials are saved, and never change it after.",
        id="config.W001",
    )]
