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


from apps.ai.tokenizer import RegexTokenizer


def make_tokenizer():
    return RegexTokenizer(salt=SALT, known_aliases=[])


def test_tokenize_email_replaces_with_placeholder():
    tok = make_tokenizer()
    mapping = {}
    out = tok.tokenize("Contact alice@example.com about it", mapping)
    assert "alice@example.com" not in out
    assert "<EMAIL_" in out
    # The placeholder is mapped to the real value
    placeholder = next(k for k in mapping if k.startswith("<EMAIL_"))
    assert mapping[placeholder] == "alice@example.com"


def test_tokenize_email_lowercase_normalization():
    """Different cases of the same email get the same placeholder."""
    tok = make_tokenizer()
    map_a = {}
    tok.tokenize("Alice@Example.com", map_a)
    map_b = {}
    tok.tokenize("alice@example.com", map_b)
    placeholder_a = next(iter(map_a))
    placeholder_b = next(iter(map_b))
    assert placeholder_a == placeholder_b


def test_tokenize_email_multiple_in_one_text():
    tok = make_tokenizer()
    mapping = {}
    out = tok.tokenize("a@x.com and b@x.com and a@x.com again", mapping)
    # Two distinct emails → two placeholders; same email → same placeholder
    assert "a@x.com" not in out
    assert "b@x.com" not in out
    assert len(mapping) == 2  # a@x.com, b@x.com
    # Same email appears twice → mapped to same placeholder, both replaced
    assert out.count("<EMAIL_") == 3


def test_tokenize_no_pii_leaves_text_unchanged():
    tok = make_tokenizer()
    mapping = {}
    out = tok.tokenize("This text contains no personal data.", mapping)
    assert out == "This text contains no personal data."
    assert mapping == {}
