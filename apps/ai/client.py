"""Public entry point for every LLM call from LORA code.

`AIClient.complete()` is the only sanctioned path to the LLM provider. It
handles PII tokenization, prompt fencing + defense preamble, response
validation against a Pydantic schema, and reverse tokenization on the way
back out.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TypeVar, Type

import requests
from django.conf import settings
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from apps.config.encrypted_fields import is_decryption_failure
from apps.config.models import SystemSettings

from .exceptions import AIClientError, AIResponseValidationError
from .prompt_fence import build_messages
from .tokenizer import RegexTokenizer


logger = logging.getLogger(__name__)


T = TypeVar("T", bound=BaseModel)

# Default LLM sampling knobs for AIClient.complete(). Tuned for the briefing/chat
# call sites: low temperature for deterministic structured output, a modest token
# ceiling sized for the JSON schemas these call sites return.
DEFAULT_TEMPERATURE: float = 0.3
DEFAULT_MAX_TOKENS: int = 600

# Disputes are resolved with Anthropic/Claude (better case understanding) when an
# Anthropic key is configured; every other call site stays on the default
# provider, and disputes fall back to it too when no Anthropic key is set.
DISPUTE_CALL_SITE_PREFIX = "dispute"
ANTHROPIC_VERSION = "2023-06-01"
# Claude models that reject sampling params (temperature/top_p/top_k) — Opus 4.7+
# and the Fable family. Sonnet 4.6 / Haiku 4.5 accept them.
_ANTHROPIC_NO_SAMPLING = ("claude-opus-4-7", "claude-opus-4-8", "claude-fable", "claude-mythos")


def _resolve_salt() -> bytes:
    """Salt resolution order: SystemSettings.pii_tokenization_salt → env var.
    Raises AIClientError if neither is set (refuse to operate without a salt)."""
    try:
        ss = SystemSettings.get_instance()
        if ss.pii_tokenization_salt:
            return ss.pii_tokenization_salt.encode("utf-8")
    except Exception as e:
        # Don't mask a real DB/config error — log it, then fall through to the
        # env-var salt so a transient SystemSettings failure stays observable.
        logger.warning(
            "AIClient: could not read salt from SystemSettings (%s); "
            "falling back to env var", e)

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
    api_key = ss.ai_api_key
    # Fail closed: refuse a missing key or the decrypt-failure sentinel rather
    # than send it to the provider as a Bearer token.
    if not api_key or is_decryption_failure(api_key):
        raise AIClientError(
            "AI API key is not configured or could not be decrypted — "
            "refusing to call the LLM without a usable key.")
    # Bound the call: complete() runs synchronously on the request path (Zendesk
    # briefing/chat), and the OpenAI SDK defaults to a ~600s timeout + 2 retries,
    # so without these a hung provider can tie up a gunicorn worker for minutes
    # and a burst can exhaust the pool. See settings.AI_TIMEOUT / AI_MAX_RETRIES.
    return OpenAI(
        api_key=api_key,
        base_url=ss.ai_api_base,
        timeout=settings.AI_TIMEOUT,
        max_retries=settings.AI_MAX_RETRIES,
    )


def _anthropic_enabled_for(call_site: str, ss: SystemSettings) -> bool:
    """Route the dispute zone to Claude when an Anthropic key is configured;
    every other call site (and disputes with no key) uses the default provider."""
    if not (call_site or "").startswith(DISPUTE_CALL_SITE_PREFIX):
        return False
    key = getattr(ss, "anthropic_api_key", "")
    return bool(key) and not is_decryption_failure(key)


def _anthropic_complete(ss: SystemSettings, messages: list[dict[str, str]],
                        temperature: float, max_tokens: int) -> tuple[str, dict]:
    """Call the Anthropic Messages API over raw HTTP (no new dependency) and
    return (reply_text, {'in','out'}). PII is already tokenized in `messages`.

    Mirrors the default path: the system prompt already instructs the model to
    return JSON, so we just return the text and let complete() validate it
    against the caller's schema — no provider-specific structured-output config."""
    api_key = ss.anthropic_api_key
    if not api_key or is_decryption_failure(api_key):
        raise AIClientError(
            "Anthropic API key is not configured or could not be decrypted — "
            "refusing to call Claude without a usable key.")
    # OpenAI-style [system, user, ...] -> Anthropic `system` field + `messages`.
    system_text = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    chat = [{"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") != "system"]
    model = ss.anthropic_model or "claude-sonnet-4-6"
    body: dict = {"model": model, "max_tokens": max_tokens, "messages": chat}
    if system_text:
        body["system"] = system_text
    # Opus 4.7+/Fable reject temperature; Sonnet 4.6 / Haiku 4.5 accept it.
    if not any(model.startswith(p) for p in _ANTHROPIC_NO_SAMPLING):
        body["temperature"] = temperature
    base = (ss.anthropic_api_base or "https://api.anthropic.com").rstrip("/")
    resp = requests.post(
        f"{base}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=body,
        timeout=settings.AI_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in (data.get("content") or [])
                   if b.get("type") == "text")
    usage = data.get("usage") or {}
    return text, {"in": usage.get("input_tokens"), "out": usage.get("output_tokens")}


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
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
        use_anthropic = _anthropic_enabled_for(call_site, ss)
        provider = "anthropic" if use_anthropic else "default"

        start = time.monotonic()
        usage_in = usage_out = None
        try:
            if use_anthropic:
                raw_text, usage = _anthropic_complete(ss, messages, temperature, max_tokens)
                usage_in, usage_out = usage.get("in"), usage.get("out")
            else:
                completion = _build_openai_client().chat.completions.create(
                    model=ss.ai_api_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                raw_text = completion.choices[0].message.content or ""
                u = getattr(completion, "usage", None)
                if u is not None:
                    usage_in = getattr(u, "prompt_tokens", None)
                    usage_out = getattr(u, "completion_tokens", None)
        except Exception as e:
            logger.error(
                "AIClient[%s] %s LLM call failed after %dms: %s",
                call_site, provider, int((time.monotonic() - start) * 1000), e,
            )
            raise AIClientError(f"LLM call failed: {e}") from e

        latency_ms = int((time.monotonic() - start) * 1000)
        raw_reply = _strip_code_fence(raw_text)

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

        # Provider token usage, normalised across providers (None -> "?").
        logger.info(
            "AIClient[%s] OK provider=%s latency=%dms tokens_in=%s tokens_out=%s",
            call_site, provider, latency_ms,
            usage_in if usage_in is not None else "?",
            usage_out if usage_out is not None else "?",
        )
        return untokenized


def _untokenize_model(obj: T, tokenizer: RegexTokenizer, mapping: dict[str, str]) -> T:
    """Walk a Pydantic model and un-tokenize every string-typed field.

    Re-validate with model_validate so nested models in the dumped data are
    rebuilt into their proper types: model_construct does NOT recurse and would
    leave nested objects as plain dicts, breaking callers that read their
    attributes (e.g. the dispute-evidence narrative's list of items). A side
    effect is that field validators (e.g. soft length caps) re-run on the
    restored values — accepted, since capping restored PII is rare and far less
    harmful than returning dict-shaped nested fields."""
    data = obj.model_dump()
    _untokenize_in_place(data, tokenizer, mapping)
    return type(obj).model_validate(data)


def _untokenize_in_place(node: object, tokenizer: RegexTokenizer, mapping: dict[str, str]) -> None:
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
