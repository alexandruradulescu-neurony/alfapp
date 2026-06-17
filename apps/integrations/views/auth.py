"""Shared authentication helper for the Zendesk sidebar endpoints.

Extracted verbatim from views.py so individual view modules (e.g. flight.py) can
import it without importing the views package itself and creating a circular
import. Behaviour is unchanged.
"""

import hmac
import logging

from django.core.cache import cache
from rest_framework.response import Response
from rest_framework import status

from apps.config.models import SystemSettings
from apps.core.utils import get_client_ip

logger = logging.getLogger(__name__)


class ZendeskSidebarAuth:
    """
    Custom authentication for Zendesk sidebar widget.
    Validates the Authorization header against the sidebar_secret_token.
    Uses constant-time comparison to prevent timing attacks.
    """

    # Failed-auth brute-force throttle: after this many failures from one client
    # IP inside the window we return 429 instead of 403.
    AUTH_FAIL_LIMIT = 5
    AUTH_FAIL_WINDOW_SECONDS = 300  # 5 minutes

    @classmethod
    def reject_if_unauthenticated(cls, request, *, context: str = ''):
        """Authenticate the sidebar token; on failure record the attempt against
        the caller's real client IP and return the right error Response (429 once
        AUTH_FAIL_LIMIT is exceeded in the window, else 403). Returns None when
        authenticated so the caller just proceeds. Centralises the per-IP
        brute-force throttle that was copy-pasted across every sidebar endpoint.

        IP comes from get_client_ip (the entry added by the outermost trusted
        proxy — the right-most TRUSTED_PROXY_DEPTH hop of X-Forwarded-For, NOT the
        spoofable left-most client-supplied entry), not REMOTE_ADDR — behind
        Railway's proxy the latter collapses every caller into one bucket and
        makes the per-IP throttle effectively global."""
        if cls.authenticate(request):
            return None
        ip = get_client_ip(request)
        cache_key = f'sidebar_auth_fail_{ip}'
        failed_attempts = cache.get(cache_key, 0)
        cache.set(cache_key, failed_attempts + 1, cls.AUTH_FAIL_WINDOW_SECONDS)
        logger.warning(
            "Failed sidebar auth attempt%s, IP: %s, attempt: %s",
            f' ({context})' if context else '', ip, failed_attempts + 1)
        if failed_attempts >= cls.AUTH_FAIL_LIMIT:
            return Response({'error': 'Too many failed attempts. Please try again later.'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

    @staticmethod
    def authenticate(request) -> bool:
        """
        Check if the Authorization header matches the sidebar secret token.
        Returns True if authenticated, False otherwise.
        Uses hmac.compare_digest for constant-time comparison.
        """
        auth_header = request.headers.get('Authorization', '')

        # Get the expected token from SystemSettings
        try:
            system_settings = SystemSettings.get_instance()
            expected_token = system_settings.sidebar_secret_token
        except Exception as e:
            logger.error("Error loading SystemSettings for sidebar auth: %s", e)
            return False

        if not expected_token:
            logger.warning("Sidebar secret token not configured in SystemSettings")
            return False

        # Support both "Bearer <token>" and raw token formats
        if auth_header.startswith('Bearer '):
            provided_token = auth_header[7:]  # Remove "Bearer " prefix
        else:
            provided_token = auth_header

        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(
            provided_token.encode('utf-8'),
            expected_token.encode('utf-8')
        )
