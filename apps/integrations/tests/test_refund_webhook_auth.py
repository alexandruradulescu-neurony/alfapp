"""
Tests for RefundWebhookView authentication.

Verifies that X-Webhook-Secret is mandatory — unauthenticated callers are
rejected before any refund logic runs.
"""

import json
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from apps.config.models import SystemSettings


class RefundWebhookAuthTests(TestCase):
    """Auth gate tests for the refund webhook endpoint."""

    def setUp(self):
        self.settings_obj, _ = SystemSettings.objects.get_or_create(pk=1)
        self.settings_obj.sidebar_secret_token = 'test-webhook-secret'
        self.settings_obj.save()
        self.webhook_url = reverse('zendesk-refund-webhook')

    def _post(self, payload=None, **extra):
        body = json.dumps(payload or {})
        return self.client.post(
            self.webhook_url, body, content_type='application/json', **extra
        )

    def test_missing_secret_returns_401(self):
        """A POST with no X-Webhook-Secret header is rejected with 401."""
        response = self._post({'claim_number': '1', 'refund_id': 'R1', 'refund_amount': '10.00'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_wrong_secret_returns_401(self):
        """A POST with an incorrect X-Webhook-Secret header is rejected with 401."""
        response = self._post(
            {'claim_number': '1', 'refund_id': 'R1', 'refund_amount': '10.00'},
            HTTP_X_WEBHOOK_SECRET='wrong-secret',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_correct_secret_missing_field_returns_400(self):
        """A correctly-authenticated request with a missing required field returns 400.

        This proves auth passes and the view proceeds to field validation.
        """
        response = self._post(
            {'refund_id': 'R1', 'refund_amount': '10.00'},  # claim_number missing
            HTTP_X_WEBHOOK_SECRET='test-webhook-secret',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
