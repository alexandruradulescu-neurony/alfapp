"""PII tokenizer for the AI client layer.

Replaces real PII values with deterministic placeholders before sending text
to the LLM provider, and reverses the substitution on the response.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Protocol


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
    phone numbers (via phonenumbers library), and known aliases passed in by
    the caller.

    Names, street addresses, and other unstructured PII are NOT detected by
    this implementation — see the spec for the Presidio upgrade path.
    """

    def __init__(self, salt: bytes, known_aliases: list[str]) -> None:
        if not salt:
            raise ValueError("salt must be non-empty bytes")
        self._salt = salt
        # Aliases are matched as literal known strings (not regex), because
        # the caller knows which aliases are in scope for this request.
        self._known_aliases = {a.lower() for a in known_aliases if a}

    def tokenize(self, text: str, mapping: dict[str, str]) -> str:
        if not text:
            return text

        text = self._tokenize_aliases(text, mapping)
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
