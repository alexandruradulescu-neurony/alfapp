"""Tests for the deconstruct() behavior of EncryptedCharField.

Regression coverage for a bug where __init__ inflates `max_length`
(`max_length * 4 + 100` to fit the Fernet ciphertext overhead) but does not
override `deconstruct()`. Django's migration framework called deconstruct() and
got back the already-inflated value, so re-instantiating from the migration
inflated again — producing `((N*4+100)*4+100)` in subsequent migrations.

The fix ensures deconstruct() returns the user-supplied (original) max_length
so migrations stay stable across re-runs.
"""

from apps.config.encrypted_fields import EncryptedCharField


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
