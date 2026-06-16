"""Public entry point for every LLM call from LORA code.

`AIClient.complete()` is the only sanctioned path to the LLM provider. It
handles PII tokenization, prompt fencing + defense preamble, response
validation against a Pydantic schema, and reverse tokenization on the way
back out.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TypeVar, Type

from django.conf import settings
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from apps.config.models import SystemSettings

from .exceptions import AIClientError, AIResponseValidationError
from .prompt_fence import build_messages
from .tokenizer import RegexTokenizer


logger = logging.getLogger(__name__)


T = TypeVar("T", bound=BaseModel)


def _resolve_salt() -> bytes:
    """Salt resolution order: SystemSettings.pii_tokenization_salt → env var.
    Raises AIClientError if neither is set (refuse to operate without a salt)."""
    try:
        ss = SystemSettings.get_instance()
        if ss.pii_tokenization_salt:
            return ss.pii_tokenization_salt.encode("utf-8")
    except Exception:
        # If SystemSettings is unavailable, fall through to env var
        pass

    env_salt = settings.PII_TOKENIZATION_SALT
    if env_salt:
        return env_salt.encode("utf-8")

    raise AIClientError(
        "PII_TOKENIZATION_SALT is not configured (neither SystemSettings nor env var). "
        "Refusing to call the LLM without a tokenization salt."
    )


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?|\n?```\s*$")


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence (```json ... ```) that some
    models wrap JSON in — otherwise schema JSON-parsing fails on the backticks.
    No-op when no fence is present."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = _CODE_FENCE_RE.sub("", t)
        t = _CODE_FENCE_RE.sub("", t)  # second pass removes the closing fence
    return t.strip()


def _build_tokenizer(known_pii: dict | None) -> RegexTokenizer:
    return RegexTokenizer(
        salt=_resolve_salt(),
        known_aliases=(known_pii or {}).get("aliases", []),
        phone_default_region=settings.AI_PHONE_DEFAULT_REGION,
        phone_fallback_regions=settings.AI_PHONE_FALLBACK_REGIONS,
        known_names=(known_pii or {}).get("names", []),
    )


def _build_openai_client() -> OpenAI:
    ss = SystemSettings.get_instance()
    # Bound the call: complete() runs synchronously on the request path (Zendesk
    # briefing/chat), and the OpenAI SDK defaults to a ~600s timeout + 2 retries,
    # so without these a hung provider can tie up a gunicorn worker for minutes
    # and a burst can exhaust the pool. See settings.AI_TIMEOUT / AI_MAX_RETRIES.
    return OpenAI(
        api_key=ss.ai_api_key,
        base_url=ss.ai_api_base,
        timeout=settings.AI_TIMEOUT,
        max_retries=settings.AI_MAX_RETRIES,
    )


class AIClient:
    """Singleton-style public API. Use `AIClient.complete(...)` from any call site."""

    @staticmethod
    def complete(
        *,
        system_prompt: str,
        trusted: dict[str, str] | None = None,
        untrusted: dict[str, str | list[str]] | None = None,
        known_pii: dict | None = None,
        response_schema: Type[T],
        call_site: str,
        temperature: float = 0.3,
        max_tokens: int = 600,
    ) -> T:
        """Send a prompt to the LLM and return a validated Pydantic object.

        See the design spec at docs/superpowers/specs/2026-06-02-ai-client-security-layer-design.md
        for the full contract.
        """
        trusted = trusted or {}
        untrusted = untrusted or {}

        tokenizer = _build_tokenizer(known_pii)
        mapping: dict[str, str] = {}

        # Tokenize all string inputs (trusted + untrusted) BEFORE building messages.
        tokenized_trusted = {
            k: tokenizer.tokenize(str(v), mapping) for k, v in trusted.items()
        }
        tokenized_untrusted: dict[str, str | list[str]] = {}
        for tag, value in untrusted.items():
            if isinstance(value, list):
                tokenized_untrusted[tag] = [tokenizer.tokenize(item, mapping) for item in value]
            else:
                tokenized_untrusted[tag] = tokenizer.tokenize(value, mapping)

        # Render trusted dict as human-readable text for the user-role message.
        trusted_text = (
            "\n".join(f"{k}: {v}" for k, v in tokenized_trusted.items())
            if tokenized_trusted else None
        )

        messages = build_messages(
            system_prompt=system_prompt,
            trusted_text=trusted_text,
            untrusted=tokenized_untrusted,
        )

        ss = SystemSettings.get_instance()
        client = _build_openai_client()

        start = time.monotonic()
        try:
            completion = client.chat.completions.create(
                model=ss.ai_api_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.error(
                "AIClient[%s] LLM call failed after %dms: %s",
                call_site, int((time.monotonic() - start) * 1000), e,
            )
            raise AIClientError(f"LLM call failed: {e}") from e

        latency_ms = int((time.monotonic() - start) * 1000)
        raw_reply = _strip_code_fence(completion.choices[0].message.content or "")

        # Validate against the caller's schema.
        try:
            validated = response_schema.model_validate_json(raw_reply)
        except ValidationError as e:
            if settings.AI_VALIDATION_STRICT:
                logger.warning(
                    "AIClient[%s] schema validation failed (latency=%dms): %s",
                    call_site, latency_ms, e,
                )
                raise AIResponseValidationError(
                    call_site=call_site,
                    raw_reply=raw_reply,
                    message=str(e),
                ) from e
            else:
                logger.error(
                    "AIClient[%s] schema validation failed but STRICT=False; "
                    "attempting lenient parse. error=%s",
                    call_site, e,
                )
                import json
                try:
                    validated = response_schema.model_validate(json.loads(raw_reply))
                except Exception:
                    raise AIResponseValidationError(
                        call_site=call_site,
                        raw_reply=raw_reply,
                        message=f"non-strict mode also failed: {e}",
                    ) from e

        # Un-tokenize every string field in the validated response.
        untokenized = _untokenize_model(validated, tokenizer, mapping)

        logger.info(
            "AIClient[%s] OK latency=%dms tokens_in=%d tokens_out=%d",
            call_site,
            latency_ms,
            len(messages[1]["content"]) if len(messages) > 1 else 0,
            len(raw_reply),
        )
        return untokenized


def _untokenize_model(obj: T, tokenizer: RegexTokenizer, mapping: dict[str, str]) -> T:
    """Walk a Pydantic model and un-tokenize every string-typed field."""
    data = obj.model_dump()
    _untokenize_in_place(data, tokenizer, mapping)
    return type(obj).model_validate(data)


def _untokenize_in_place(node, tokenizer: RegexTokenizer, mapping: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, val in list(node.items()):
            if isinstance(val, str):
                node[key] = tokenizer.untokenize(val, mapping)
            elif isinstance(val, (dict, list)):
                _untokenize_in_place(val, tokenizer, mapping)
    elif isinstance(node, list):
        for i, val in enumerate(node):
            if isinstance(val, str):
                node[i] = tokenizer.untokenize(val, mapping)
            elif isinstance(val, (dict, list)):
                _untokenize_in_place(val, tokenizer, mapping)
