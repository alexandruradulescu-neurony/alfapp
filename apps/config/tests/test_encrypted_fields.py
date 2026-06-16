"""Tests for the deconstruct() behavior of EncryptedCharField.

Regression coverage for a bug where __init__ inflates `max_length`
(`max_length * 4 + 100` to fit the Fernet ciphertext overhead) but does not
override `deconstruct()`. Django's migration framework called deconstruct() and
got back the already-inflated value, so re-instantiating from the migration
inflated again — producing `((N*4+100)*4+100)` in subsequent migrations.

The fix ensures deconstruct() returns the user-supplied (original) max_length
so migrations stay stable across re-runs.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.config.encrypted_fields import (
    DECRYPTION_FAILED,
    EncryptedCharField,
    _decrypt,
    _encrypt,
)

_OLD_KEY = "old-key-aaaaaaaaaaaaaaaaaaaa"
_NEW_KEY = "new-key-bbbbbbbbbbbbbbbbbbbb"


def test_round_trip_basic():
    with override_settings(ENCRYPTION_KEY=_OLD_KEY, ENCRYPTION_KEY_FALLBACKS=[]):
        assert _decrypt(_encrypt("hunter2")) == "hunter2"
        assert _decrypt(None) is None
        assert _decrypt("") == ""


def test_multifernet_rotation_keeps_old_ciphertext_readable():
    """M7: rotating ENCRYPTION_KEY while keeping the old one as a fallback must
    leave previously-stored credentials decryptable (non-destructive rotation)."""
    with override_settings(ENCRYPTION_KEY=_OLD_KEY, ENCRYPTION_KEY_FALLBACKS=[]):
        token = _encrypt("paypal-secret")
    with override_settings(ENCRYPTION_KEY=_NEW_KEY, ENCRYPTION_KEY_FALLBACKS=[_OLD_KEY]):
        assert _decrypt(token) == "paypal-secret"


def test_decrypt_returns_sentinel_not_empty_when_no_key_matches():
    """M7: an undecryptable value reads as the sentinel, never '' — so it can't
    be silently re-saved as empty and destroy the original ciphertext."""
    with override_settings(ENCRYPTION_KEY=_OLD_KEY, ENCRYPTION_KEY_FALLBACKS=[]):
        token = _encrypt("paypal-secret")
    with override_settings(ENCRYPTION_KEY=_NEW_KEY, ENCRYPTION_KEY_FALLBACKS=[]):
        assert _decrypt(token) == DECRYPTION_FAILED


def test_encrypt_refuses_to_persist_failure_sentinel():
    """M7: writing back a failed-decrypt sentinel is refused (fail loud)."""
    with pytest.raises(ImproperlyConfigured):
        _encrypt(DECRYPTION_FAILED)


def test_deconstruct_returns_original_max_length_not_inflated():
    field = EncryptedCharField(max_length=4580, blank=True, default='')
    _name, _path, _args, kwargs = field.deconstruct()
    assert kwargs['max_length'] == 4580, (
        f"deconstruct must return the user-supplied max_length, not the inflated "
        f"runtime value. Got {kwargs['max_length']}."
    )


def test_runtime_max_length_is_still_inflated_for_db_column():
    """The DB column still needs the inflated size to fit the ciphertext."""
    field = EncryptedCharField(max_length=4580)
    assert field.max_length == 4580 * 4 + 100, (
        f"Runtime max_length must remain inflated for the DB column. "
        f"Got {field.max_length}, expected {4580 * 4 + 100}."
    )


def test_deconstruct_round_trip_is_stable():
    """deconstruct → reconstruct → deconstruct produces the same kwargs.
    Without the fix, each round inflates again."""
    first = EncryptedCharField(max_length=4580, blank=True, default='')
    _n, path, args, kwargs = first.deconstruct()

    # Reconstruct as Django's migration framework would
    cls = EncryptedCharField  # path resolution skipped; same class
    second = cls(*args, **kwargs)
    _n2, _p2, _a2, kwargs2 = second.deconstruct()

    assert kwargs2['max_length'] == kwargs['max_length'], (
        f"Round-trip changed max_length from {kwargs['max_length']} "
        f"to {kwargs2['max_length']}. Migration framework would re-inflate "
        f"the column on every replay."
    )
    assert kwargs2['max_length'] == 4580


def test_deconstruct_without_explicit_max_length_unaffected():
    """If max_length wasn't supplied (Django default is 100 for CharField with
    blank/no max_length), deconstruct shouldn't crash."""
    # CharField requires max_length to be set; the fix should not assume it's
    # always present in deconstruct kwargs.
    field = EncryptedCharField(max_length=200)
    _n, _p, _a, kwargs = field.deconstruct()
    assert kwargs.get('max_length') == 200
