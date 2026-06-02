"""PII tokenizer for the AI client layer.

Replaces real PII values with deterministic placeholders before sending text
to the LLM provider, and reverses the substitution on the response.
"""

from __future__ import annotations

import hashlib
import hmac


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
