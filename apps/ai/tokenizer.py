"""PII tokenizer for the AI client layer.

Replaces real PII values with deterministic placeholders before sending text
to the LLM provider, and reverses the substitution on the response.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Protocol

import phonenumbers


def generate_placeholder(kind: str, value: str, *, salt: bytes) -> str:
    """Generate a deterministic placeholder for a PII value.

    Format: `<KIND_HHHHHHHH>` where `HHHHHHHH` is the first 8 hex chars of
    HMAC-SHA256(salt, value). Deterministic — same inputs always produce the
    same placeholder, enabling cross-request consistency without storage.

    The salt makes the mapping non-reversible by the LLM provider (no rainbow
    tables against common values).

    Args:
        kind: PII kind in UPPERCASE (e.g., "EMAIL", "PHONE", "ALF_ID").
        value: The normalized real value. Caller is responsible for normalization
            (lowercase email, E.164 phone, etc.) so that equivalent inputs map
            to the same placeholder.
        salt: HMAC key — long random bytes. Empty salt rejected.

    Returns:
        Placeholder string like `<EMAIL_a3f9b2c1>`.

    Raises:
        ValueError: If kind is not uppercase or salt is empty.
    """
    if not kind.isupper() or not kind:
        raise ValueError(f"kind must be uppercase non-empty, got {kind!r}")
    if not salt:
        raise ValueError("salt must be non-empty bytes")

    digest = hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"<{kind}_{digest[:8]}>"


_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

_PLACEHOLDER_PATTERN = re.compile(r"<[A-Z_]+_[a-f0-9]{8}>")

_ALF_ID_PATTERN = re.compile(r"\bALF\d{7}\b")
_FLIGHT_PATTERN = re.compile(r"\b[A-Z]{2}\d{2,4}\b")


class Tokenizer(Protocol):
    """Interface for PII tokenizers. RegexTokenizer is the v1 implementation;
    a future PresidioTokenizer can implement the same Protocol."""

    def tokenize(self, text: str, mapping: dict[str, str]) -> str:
        """Return `text` with PII replaced by placeholders.

        `mapping` is mutated in place: new {placeholder: real_value} entries
        are added for every distinct PII value found.
        """
        ...

    def untokenize(self, text: str, mapping: dict[str, str]) -> str:
        """Return `text` with placeholders replaced by real values from `mapping`.

        Placeholders not in `mapping` (the LLM invented one) are left as-is.
        """
        ...


class RegexTokenizer:
    """Regex-based PII detector. Detects emails, ALF IDs, flight numbers,
    phone numbers (via phonenumbers library), known aliases, and known client
    names passed in by the caller.

    UNKNOWN names, street addresses, and other unstructured PII are NOT
    detected by this implementation — see the spec for the Presidio upgrade
    path. Known names (e.g. the ticket requester) ARE tokenized: the full name
    case-insensitively, and each name part (3+ chars) when it appears
    Capitalized or ALL-CAPS — the capitalization guard keeps common words
    intact for clients named e.g. "May" or "Will".
    """

    def __init__(
        self,
        salt: bytes,
        known_aliases: list[str],
        phone_default_region: str = "US",
        phone_fallback_regions: list[str] | None = None,
        known_names: list[str] | None = None,
    ) -> None:
        if not salt:
            raise ValueError("salt must be non-empty bytes")
        self._salt = salt
        self._known_aliases = {a.lower() for a in known_aliases if a}
        self._known_names = [n.strip() for n in (known_names or []) if n and n.strip()]
        self._phone_default_region = phone_default_region
        self._phone_fallback_regions = list(phone_fallback_regions or [])

    def tokenize(self, text: str, mapping: dict[str, str]) -> str:
        if not text:
            return text
        text = self._tokenize_aliases(text, mapping)
        text = self._tokenize_names(text, mapping)
        text = self._tokenize_phones(text, mapping)
        text = self._tokenize_pattern(
            text, mapping, _EMAIL_PATTERN, kind="EMAIL", normalize=str.lower
        )
        text = self._tokenize_pattern(
            text, mapping, _ALF_ID_PATTERN, kind="ALF_ID", normalize=str.upper
        )
        text = self._tokenize_pattern(
            text, mapping, _FLIGHT_PATTERN, kind="FLIGHT",
            normalize=lambda v: v.upper().replace(" ", ""),
        )
        return text

    def _tokenize_phones(self, text: str, mapping: dict[str, str]) -> str:
        # phonenumbers.PhoneNumberMatcher finds phone-shaped substrings and
        # validates them as real numbers in the given region. Try default first,
        # then fallbacks. Collect all unique matches before substituting so we
        # don't replace already-tokenized text mid-pass.
        regions = [self._phone_default_region, *self._phone_fallback_regions]
        all_matches: dict[tuple[int, int], str] = {}  # (start, end) -> E.164

        for region in regions:
            try:
                matcher = phonenumbers.PhoneNumberMatcher(text, region)
                for match in matcher:
                    span = (match.start, match.end)
                    if span in all_matches:
                        continue
                    e164 = phonenumbers.format_number(
                        match.number, phonenumbers.PhoneNumberFormat.E164
                    )
                    all_matches[span] = e164
            except Exception:
                # Region code not recognized by phonenumbers — skip silently.
                continue

        if not all_matches:
            return text

        # Apply substitutions in reverse order of position so earlier indices stay valid.
        result_parts = []
        cursor = 0
        for (start, end) in sorted(all_matches.keys()):
            if start < cursor:
                # Overlapping match from a different region — skip
                continue
            e164 = all_matches[(start, end)]
            placeholder = generate_placeholder("PHONE", e164, salt=self._salt)
            mapping[placeholder] = e164
            result_parts.append(text[cursor:start])
            result_parts.append(placeholder)
            cursor = end
        result_parts.append(text[cursor:])
        return "".join(result_parts)

    def _tokenize_aliases(self, text: str, mapping: dict[str, str]) -> str:
        if not self._known_aliases:
            return text
        # Case-insensitive literal replacement of each known alias
        for alias in self._known_aliases:
            pattern = re.compile(re.escape(alias), re.IGNORECASE)

            def sub(match: re.Match, *, _alias=alias) -> str:
                placeholder = generate_placeholder("ALIAS", _alias, salt=self._salt)
                mapping[placeholder] = _alias
                return placeholder

            text = pattern.sub(sub, text)
        return text

    def _tokenize_names(self, text: str, mapping: dict[str, str]) -> str:
        if not self._known_names:
            return text
        for name in self._known_names:
            # Full name: case-insensitive, word-bounded (a multi-word client
            # name is distinctive enough to replace in any casing).
            full_pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)

            def sub_full(match: re.Match, *, _name=name) -> str:
                placeholder = generate_placeholder("NAME", _name.lower(), salt=self._salt)
                mapping[placeholder] = _name
                return placeholder

            text = full_pattern.sub(sub_full, text)

            # Individual parts (3+ chars): only when Capitalized or ALL-CAPS,
            # so ordinary lowercase words survive for clients named "May"/"Will".
            for part in name.split():
                if len(part) < 3:
                    continue
                part_pattern = re.compile(
                    r"\b(?:" + re.escape(part.capitalize()) + "|"
                    + re.escape(part.upper()) + r")\b"
                )

                def sub_part(match: re.Match, *, _part=part) -> str:
                    placeholder = generate_placeholder(
                        "NAME", _part.lower(), salt=self._salt
                    )
                    mapping[placeholder] = _part.capitalize()
                    return placeholder

                text = part_pattern.sub(sub_part, text)
        return text

    def _tokenize_pattern(
        self,
        text: str,
        mapping: dict[str, str],
        pattern: re.Pattern,
        *,
        kind: str,
        normalize,
    ) -> str:
        def sub(match: re.Match) -> str:
            value = match.group(0)
            normalized = normalize(value)
            placeholder = generate_placeholder(kind, normalized, salt=self._salt)
            mapping[placeholder] = normalized
            return placeholder

        return pattern.sub(sub, text)

    def untokenize(self, text: str, mapping: dict[str, str]) -> str:
        if not text:
            return text

        def sub(match: re.Match) -> str:
            placeholder = match.group(0)
            return mapping.get(placeholder, placeholder)  # unknown → leave as-is

        return _PLACEHOLDER_PATTERN.sub(sub, text)
