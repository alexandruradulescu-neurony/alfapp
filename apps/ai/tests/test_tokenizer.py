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


def test_untokenize_round_trip():
    tok = make_tokenizer_with_phones()
    mapping = {}
    original = "Contact alice@example.com about claim ALF1234567"
    tokenized = tok.tokenize(original, mapping)
    assert tokenized != original
    restored = tok.untokenize(tokenized, mapping)
    # Note: untokenize restores normalized PII values (email lowercased, ALF ID
    # uppercased) but non-PII surrounding text is left as-is.
    assert "alice@example.com" in restored
    assert "ALF1234567" in restored
    assert "Contact" in restored
    assert "about claim" in restored


def test_untokenize_normalized_form_preserved():
    """untokenize replaces placeholders with the NORMALIZED real value (per the mapping),
    not the original pre-normalization text."""
    tok = make_tokenizer_with_phones()
    mapping = {}
    tokenized = tok.tokenize("Alice@Example.com is the contact", mapping)
    restored = tok.untokenize(tokenized, mapping)
    assert restored == "alice@example.com is the contact"


def test_untokenize_unknown_placeholder_left_as_is():
    """If the LLM hallucinates a placeholder that isn't in our mapping,
    leave it visible in the output rather than silently mapping to a wrong value."""
    tok = make_tokenizer_with_phones()
    mapping = {"<EMAIL_aaaaaaaa>": "real@example.com"}
    out = tok.untokenize(
        "Reply was sent to <EMAIL_aaaaaaaa> and CC'd to <EMAIL_deadbeef>.",
        mapping,
    )
    assert "real@example.com" in out
    assert "<EMAIL_deadbeef>" in out  # unknown — left as-is


def test_untokenize_no_placeholders_unchanged():
    tok = make_tokenizer_with_phones()
    out = tok.untokenize("Plain text with nothing tokenized.", {})
    assert out == "Plain text with nothing tokenized."


# --- known client names (NAME kind) ---

def make_tokenizer_with_names(names):
    return RegexTokenizer(salt=SALT, known_aliases=[], known_names=names)


def test_known_name_full_match_replaced():
    tok = make_tokenizer_with_names(["Charles Copeland"])
    mapping = {}
    out = tok.tokenize("Item belongs to Charles Copeland, please ship it.", mapping)
    assert "Charles Copeland" not in out
    assert "<NAME_" in out


def test_known_name_full_match_case_insensitive():
    tok = make_tokenizer_with_names(["Charles Copeland"])
    mapping = {}
    out = tok.tokenize("Cards for CHARLES COPELAND were found.", mapping)
    assert "CHARLES COPELAND" not in out
    assert "<NAME_" in out


def test_known_name_capitalized_part_replaced():
    """Single name parts are replaced when they look like a name
    (Capitalized or ALL-CAPS), e.g. greetings: 'Dear Charles,'."""
    tok = make_tokenizer_with_names(["Charles Copeland"])
    mapping = {}
    out = tok.tokenize("Dear Charles, your wallet was located. COPELAND confirmed.", mapping)
    assert "Charles" not in out
    assert "COPELAND" not in out
    assert out.count("<NAME_") >= 2


def test_known_name_lowercase_part_left_alone():
    """Lowercase occurrences of a single name part are NOT replaced — protects
    common words from being nuked when a client is named e.g. 'May' or 'Will'.
    (The FULL name is replaced in any casing — that's tested above.)"""
    tok = make_tokenizer_with_names(["Will Turner"])
    mapping = {}
    out = tok.tokenize("we will arrange pickup at the turner counter", mapping)
    assert "will" in out
    assert "turner" in out
    assert "<NAME_" not in out


def test_known_name_short_parts_not_replaced_alone():
    """1-2 char parts (middle initials) are not replaced on their own."""
    tok = make_tokenizer_with_names(["Charles M Copeland"])
    mapping = {}
    out = tok.tokenize("Section M of the terminal", mapping)
    assert "Section M of the terminal" == out


def test_known_name_untokenize_restores():
    tok = make_tokenizer_with_names(["Charles Copeland"])
    mapping = {}
    tokenized = tok.tokenize("Charles Copeland asked for UPS.", mapping)
    restored = tok.untokenize(tokenized, mapping)
    assert "Charles Copeland" in restored


def test_no_known_names_is_noop():
    tok = make_tokenizer_with_names([])
    mapping = {}
    text = "Charles Copeland asked for UPS."
    assert tok.tokenize(text, mapping) == text


def test_untokenize_restores_bracketless_placeholders():
    """LLMs often strip the angle brackets when echoing placeholders
    (NAME_27f8b391 instead of <NAME_27f8b391>); the restore pass must
    recognize both forms."""
    tok = make_tokenizer_with_phones()
    mapping = {
        "<NAME_27f8b391>": "Dan",
        "<NAME_a03ca3d8>": "Costello",
        "<ALF_ID_94550153>": "ALF9455015",
    }
    out = tok.untokenize(
        "Case ALF_ID_94550153 for NAME_27f8b391 NAME_a03ca3d8.", mapping)
    assert out == "Case ALF9455015 for Dan Costello."


def test_untokenize_bracketless_unknown_left_as_is():
    tok = make_tokenizer_with_phones()
    out = tok.untokenize("Random CODE_deadbeef stays.", {})
    assert "CODE_deadbeef" in out
