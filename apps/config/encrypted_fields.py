"""
Custom encrypted fields for SystemSettings.
Uses Django's cryptography backend for field-level encryption.
"""

import base64
import logging
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


def _get_fernet():
    """
    Create a Fernet instance for field-level encryption.
    Uses ENCRYPTION_KEY env var if set, otherwise falls back to SECRET_KEY.
    """
    # Prefer dedicated ENCRYPTION_KEY over SECRET_KEY
    encryption_key = getattr(settings, 'ENCRYPTION_KEY', '') or settings.SECRET_KEY
    secret = encryption_key.encode('utf-8')

    # Use a salt derived from the key itself to avoid fully static salt
    import hashlib
    salt = hashlib.sha256(b'lora_field_encryption_' + secret[:16]).digest()[:16]

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )

    key = base64.urlsafe_b64encode(kdf.derive(secret))
    return Fernet(key)


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
        if value is None or value == '':
            return value
        try:
            fernet = _get_fernet()
            return fernet.decrypt(value.encode('utf-8')).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to decrypt CharField value: {e}")
            return ''  # Return empty string, not ciphertext

    def get_prep_value(self, value):
        """Encrypt before saving to database."""
        if value is None:
            return None
        try:
            fernet = _get_fernet()
            encrypted = fernet.encrypt(value.encode('utf-8'))
            return encrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encrypt field value: {e}")
            raise


class EncryptedTextField(models.TextField):
    """
    A TextField that encrypts data before saving to the database.
    """

    def from_db_value(self, value, expression, connection):
        """Decrypt when loading from database."""
        if value is None or value == '':
            return value
        try:
            fernet = _get_fernet()
            return fernet.decrypt(value.encode('utf-8')).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to decrypt TextField value: {e}")
            return ''  # Return empty string, not ciphertext
    
    def get_prep_value(self, value):
        """Encrypt before saving to database."""
        if value is None:
            return None
        try:
            fernet = _get_fernet()
            encrypted = fernet.encrypt(value.encode('utf-8'))
            return encrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encrypt field value: {e}")
            raise
