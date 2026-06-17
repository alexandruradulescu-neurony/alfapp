"""Fail-closed invariant for the three auth entry points (RED phase).

The stored sidebar_secret_token is encrypted at rest. When it cannot be
decrypted with any known key, the decryption layer returns the
DECRYPTION_FAILED sentinel instead of the real plaintext.

INVARIANT (what SHOULD be true): a decrypt failure must NEVER authenticate
anyone — not even a caller who supplies the exact sentinel string as their own
secret. Both entry points must fail closed:

    1. apps.integrations.views.auth.verify_webhook_secret  -> 401 Response
    2. apps.integrations.views.auth.ZendeskSidebarAuth.authenticate -> False

The current code compares the caller's secret against the stored value with
hmac.compare_digest WITHOUT first rejecting the sentinel, so when both sides
equal the sentinel the comparison succeeds and the caller is wrongly
authenticated. The "sentinel" tests below therefore FAIL against current
code (that failure IS the bug, captured as a spec). The happy-path guard tests
PASS now and must keep passing after the fix.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.config.encrypted_fields import DECRYPTION_FAILED
from apps.config.models import SystemSettings
from apps.integrations.views.auth import (
    ZendeskSidebarAuth,
    verify_webhook_secret,
)


def _stub_settings(token):
    """A patch target so SystemSettings.get_instance() yields a chosen token
    without going through the encryption layer (which refuses to persist the
    sentinel)."""
    return patch.object(
        SystemSettings, 'get_instance',
        return_value=SimpleNamespace(sidebar_secret_token=token),
    )


# --------------------------------------------------------------------------
# 1. verify_webhook_secret
# --------------------------------------------------------------------------

class VerifyWebhookSecretFailClosedTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_sentinel_stored_and_supplied_is_rejected(self):
        """Stored token == sentinel, caller's X-Webhook-Secret == sentinel:
        must return a 401 Response (fail closed), NOT None."""
        request = self.factory.post('/x', HTTP_X_WEBHOOK_SECRET=DECRYPTION_FAILED)
        with _stub_settings(DECRYPTION_FAILED):
            result = verify_webhook_secret(request, context='test')
        self.assertIsInstance(result, Response)
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_real_secret_match_returns_none(self):
        """Happy path: a normal stored secret matched by the caller returns
        None (authenticated). Must keep passing after the fix."""
        request = self.factory.post('/x', HTTP_X_WEBHOOK_SECRET='a-real-secret')
        with _stub_settings('a-real-secret'):
            result = verify_webhook_secret(request, context='test')
        self.assertIsNone(result)


# --------------------------------------------------------------------------
# 2. ZendeskSidebarAuth.authenticate
# --------------------------------------------------------------------------

class ZendeskSidebarAuthFailClosedTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_sentinel_raw_authorization_is_rejected(self):
        """Stored token == sentinel, raw Authorization header == sentinel:
        authenticate() must return False (fail closed)."""
        request = self.factory.get('/x', HTTP_AUTHORIZATION=DECRYPTION_FAILED)
        with _stub_settings(DECRYPTION_FAILED):
            self.assertFalse(ZendeskSidebarAuth.authenticate(request))

    def test_sentinel_bearer_authorization_is_rejected(self):
        """Stored token == sentinel, Authorization == 'Bearer <sentinel>':
        authenticate() must return False (fail closed)."""
        request = self.factory.get('/x',
                                   HTTP_AUTHORIZATION=f'Bearer {DECRYPTION_FAILED}')
        with _stub_settings(DECRYPTION_FAILED):
            self.assertFalse(ZendeskSidebarAuth.authenticate(request))

    def test_real_secret_match_returns_true(self):
        """Happy path: a normal stored token matched by a Bearer header
        authenticates. Must keep passing after the fix."""
        request = self.factory.get('/x', HTTP_AUTHORIZATION='Bearer a-real-secret')
        with _stub_settings('a-real-secret'):
            self.assertTrue(ZendeskSidebarAuth.authenticate(request))
