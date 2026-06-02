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


def test_tokenize_alf_id():
    tok = make_tokenizer()
    mapping = {}
    out = tok.tokenize("See claim ALF1234567 for details", mapping)
    assert "ALF1234567" not in out
    assert "<ALF_ID_" in out


def test_tokenize_flight_number():
    tok = make_tokenizer()
    mapping = {}
    out = tok.tokenize("Lost on flight UA1234", mapping)
    assert "UA1234" not in out
    assert "<FLIGHT_" in out


def test_tokenize_known_alias():
    """Aliases are not pattern-detected; they're matched as known strings passed in by the caller."""
    tok = RegexTokenizer(
        salt=SALT,
        known_aliases=["client-77@aliasdomain.example"],
    )
    mapping = {}
    out = tok.tokenize(
        "The reply was sent to client-77@aliasdomain.example yesterday.",
        mapping,
    )
    assert "client-77@aliasdomain.example" not in out
    assert "<ALIAS_" in out
    # Specifically NOT tagged as EMAIL — aliases are a distinct kind
    assert "<EMAIL_" not in out


def test_tokenize_alias_case_insensitive():
    tok = RegexTokenizer(
        salt=SALT,
        known_aliases=["Client-99@AliasDomain.example"],
    )
    mapping = {}
    out = tok.tokenize("Reply went to CLIENT-99@aliasdomain.example", mapping)
    assert "<ALIAS_" in out


def test_tokenize_unknown_alias_not_replaced():
    """An alias-shaped string the caller didn't declare is NOT replaced as an alias
    (it'll fall through to the EMAIL pattern if it matches)."""
    tok = RegexTokenizer(salt=SALT, known_aliases=[])
    mapping = {}
    out = tok.tokenize("client-42@otherdomain.com is in here", mapping)
    # Not tagged as ALIAS because the caller didn't declare it
    assert "<ALIAS_" not in out
    # But IS tagged as EMAIL because it's email-shaped
    assert "<EMAIL_" in out


def make_tokenizer_with_phones():
    return RegexTokenizer(
        salt=SALT,
        known_aliases=[],
        phone_default_region="US",
        phone_fallback_regions=["GB", "FR", "DE", "IT", "ES", "JP"],
    )


def test_tokenize_us_phone_various_formats():
    tok = make_tokenizer_with_phones()

    formats = [
        "Call (415) 555-1212 today",
        "Call 415-555-1212 today",
        "Call 415.555.1212 today",
        "Call +1 415 555 1212 today",
    ]
    placeholders = []
    for text in formats:
        mapping = {}
        out = tok.tokenize(text, mapping)
        assert "555" not in out, f"failed to tokenize: {text}"
        assert "<PHONE_" in out, f"no phone placeholder: {text}"
        placeholders.append(next(iter(mapping)))

    # All formats normalize to the same E.164 number → same placeholder
    assert len(set(placeholders)) == 1, f"formats produced different placeholders: {placeholders}"


def test_tokenize_uk_phone():
    tok = make_tokenizer_with_phones()
    mapping = {}
    out = tok.tokenize("Call +44 20 7946 0958 anytime", mapping)
    assert "7946" not in out
    assert "<PHONE_" in out


def test_tokenize_japanese_phone():
    tok = make_tokenizer_with_phones()
    mapping = {}
    out = tok.tokenize("Call +81-3-1234-5678 please", mapping)
    assert "1234" not in out
    assert "<PHONE_" in out


def test_tokenize_non_phone_digits_left_alone():
    """Random number sequences that aren't phones should not be tokenized as phones."""
    tok = make_tokenizer_with_phones()
    mapping = {}
    out = tok.tokenize("The order total was 1234567", mapping)
    # 1234567 is not a parseable phone in any region — leave as-is
    assert "1234567" in out
    assert "<PHONE_" not in out
