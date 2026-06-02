import pytest
from apps.ai.tokenizer import generate_placeholder


SALT = b"deterministic-test-salt-do-not-use-in-prod"


def test_placeholder_format():
    token = generate_placeholder("EMAIL", "alice@example.com", salt=SALT)
    assert token.startswith("<EMAIL_")
    assert token.endswith(">")
    # Body between tag delimiters: 8 hex chars
    body = token[len("<EMAIL_"):-1]
    assert len(body) == 8
    assert all(c in "0123456789abcdef" for c in body)


def test_placeholder_deterministic_for_same_input():
    """Same kind + value + salt → same placeholder, every time."""
    a = generate_placeholder("EMAIL", "alice@example.com", salt=SALT)
    b = generate_placeholder("EMAIL", "alice@example.com", salt=SALT)
    assert a == b


def test_placeholder_changes_with_different_value():
    a = generate_placeholder("EMAIL", "alice@example.com", salt=SALT)
    b = generate_placeholder("EMAIL", "bob@example.com", salt=SALT)
    assert a != b


def test_placeholder_changes_with_different_kind():
    """Different kinds produce different tokens even for the same value."""
    a = generate_placeholder("EMAIL", "12345", salt=SALT)
    b = generate_placeholder("PHONE", "12345", salt=SALT)
    assert a != b
    assert a.startswith("<EMAIL_")
    assert b.startswith("<PHONE_")


def test_placeholder_changes_with_different_salt():
    """Different salts produce different placeholders for the same value (security: provider can't rainbow-table)."""
    a = generate_placeholder("EMAIL", "alice@example.com", salt=b"salt-one")
    b = generate_placeholder("EMAIL", "alice@example.com", salt=b"salt-two")
    assert a != b


def test_placeholder_rejects_lowercase_kind():
    """Kind must be uppercase to enforce convention."""
    with pytest.raises(ValueError, match="kind must be uppercase"):
        generate_placeholder("email", "alice@example.com", salt=SALT)


def test_placeholder_rejects_empty_salt():
    """Salt cannot be empty — would defeat the security purpose."""
    with pytest.raises(ValueError, match="salt"):
        generate_placeholder("EMAIL", "alice@example.com", salt=b"")
