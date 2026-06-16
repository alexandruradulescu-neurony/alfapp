"""Shared request/HTTP helpers for LORA."""
from __future__ import annotations

from django.conf import settings


def get_client_ip(request) -> str:
    """Best-effort originating client IP for ``request``.

    LORA runs behind a single reverse proxy in production (Railway/gunicorn),
    so ``REMOTE_ADDR`` is the proxy address — every client collapses to one
    value, which silently breaks per-IP throttling (one global bucket). When a
    proxy is in front we instead read the client IP from ``X-Forwarded-For``.

    ``X-Forwarded-For`` is ``"<client>, <proxy1>, <proxy2>, ..."`` where each
    proxy *appends* the address it received the request from. The genuinely
    trustworthy client IP is therefore the entry added by the OUTERMOST trusted
    proxy — i.e. counting ``TRUSTED_PROXY_DEPTH`` entries from the right — not
    the left-most entry, which is fully client-controlled and spoofable.

    Settings:
        USE_X_FORWARDED_FOR (bool, default True): honour the header at all.
        TRUSTED_PROXY_DEPTH (int, default 1): number of trusted proxies in
            front of the app (Railway = 1).
    """
    if getattr(settings, "USE_X_FORWARDED_FOR", True):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            depth = max(1, int(getattr(settings, "TRUSTED_PROXY_DEPTH", 1)))
            # Entry added by the outermost trusted proxy; clamp if the chain is
            # shorter than expected (treat the left-most as the client).
            idx = -depth if depth <= len(parts) else 0
            return parts[idx]
    return request.META.get("REMOTE_ADDR", "") or ""
