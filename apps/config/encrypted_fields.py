"""
Custom encrypted fields for SystemSettings.
Uses Django's cryptography backend for field-level encryption.

Key rotation is non-destructive: encryption always uses the PRIMARY key
(ENCRYPTION_KEY, or SECRET_KEY if unset), while decryption tries the primary
first and then every key in ENCRYPTION_KEY_FALLBACKS (plus SECRET_KEY as a
last resort). To rotate, set the new key as ENCRYPTION_KEY and move the old one
into ENCRYPTION_KEY_FALLBACKS — existing ciphertext stays readable.

If a value cannot be decrypted with ANY known key we return the DECRYPTION_FAILED
sentinel rather than '' so a failed read can never be silently re-encrypted as
empty (which would permanently destroy the original ciphertext). get_prep_value
refuses to persist the sentinel and raises instead — fail loud, lose nothing.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 100_000
_KEY_LENGTH = 32

# Returned when stored ciphertext cannot be decrypted with any known key.
# Intentionally not a plausible real value (NUL-wrapped); get_prep_value refuses
# to write it back, so a decrypt failure can never overwrite good ciphertext.
DECRYPTION_FAILED = "\x00__LORA_DECRYPTION_FAILED__\x00"


def _derive_fernet(key: str) -> Fernet:
    """Derive a Fernet from a raw key string via PBKDF2.

    The salt is derived from the key itself (as in the original single-key
    implementation), so the PRIMARY key produces a byte-identical Fernet to the
    pre-rotation code — existing ciphertext keeps decrypting unchanged.
    """
    secret = key.encode("utf-8")
    salt = hashlib.sha256(b"lora_field_encryption_" + secret[:16]).digest()[:16]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


def _encryption_keys() -> list[str]:
    """Ordered keys: primary first (used to encrypt), then decrypt-only fallbacks."""
    primary = getattr(settings, "ENCRYPTION_KEY", "") or settings.SECRET_KEY
    keys: list[str] = [primary]
    for fallback in getattr(settings, "ENCRYPTION_KEY_FALLBACKS", []) or []:
        if fallback and fallback not in keys:
            keys.append(fallback)
    # SECRET_KEY as a last-resort decryptor (covers data written before a
    # dedicated ENCRYPTION_KEY was introduced).
    if settings.SECRET_KEY and settings.SECRET_KEY not in keys:
        keys.append(settings.SECRET_KEY)
    return keys


def _get_fernet() -> MultiFernet:
    """MultiFernet: encrypts with the primary key, decrypts by trying each key."""
    return MultiFernet([_derive_fernet(k) for k in _encryption_keys()])


def _decrypt(value):
    """Decrypt a stored value, or return DECRYPTION_FAILED if no key works."""
    if value is None or value == "":
        return value
    try:
        return _get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(
            "Failed to decrypt encrypted field (key rotation needed, or "
            "corruption?): %s", e,
        )
        return DECRYPTION_FAILED


def _encrypt(value):
    """Encrypt a value with the primary key, refusing to persist the sentinel."""
    if value is None:
        return None
    if value == DECRYPTION_FAILED:
        # Never persist the failure sentinel — that would clobber the original
        # ciphertext. Surface loudly so the operator fixes the key first.
        raise ImproperlyConfigured(
            "Refusing to save an encrypted field that failed to decrypt — set "
            "ENCRYPTION_KEY / ENCRYPTION_KEY_FALLBACKS correctly before saving."
        )
    try:
        return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to encrypt field value: {e}")
        raise


class EncryptedCharField(models.CharField):
    """
    A CharField that encrypts data before saving to the database.
    """

    def __init__(self, *args, **kwargs):
        # Remember the user-supplied (logical) max_length so deconstruct() can
        # return it verbatim. Without this, Django's migration framework reads
        # back the INFLATED runtime value and re-applies the inflation on every
        # replay — `(N*4+100)*4+100` and so on — bloating the DB column on
        # every regeneration.
        self._user_max_length = kwargs.get('max_length')
        # Increase max_length to accommodate encrypted data overhead
        if 'max_length' in kwargs:
            # Encrypted data is larger than plaintext
            kwargs['max_length'] = kwargs['max_length'] * 4 + 100
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Return the user-supplied max_length, not the inflated runtime value,
        # so migrations are stable across replays.
        if self._user_max_length is not None and 'max_length' in kwargs:
            kwargs['max_length'] = self._user_max_length
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        """Decrypt when loading from database."""
        return _decrypt(value)

    def get_prep_value(self, value):
        """Encrypt before saving to database."""
        return _encrypt(value)


class EncryptedTextField(models.TextField):
    """
    A TextField that encrypts data before saving to the database.
    """

    def from_db_value(self, value, expression, connection):
        """Decrypt when loading from database."""
        return _decrypt(value)

    def get_prep_value(self, value):
        """Encrypt before saving to database."""
        return _encrypt(value)
