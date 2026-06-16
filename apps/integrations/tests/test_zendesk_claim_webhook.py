"""
Tests for ZendeskClaimWebhookView - Zendesk-first claims flow with LLM extraction.

Tests the webhook endpoint that creates claims from Zendesk tickets when
status changes to 'investigation_initiated'.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from django.test import TestCase, Client
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from django.contrib.auth import get_user_model

User = get_user_model()


# Zendesk custom status ID for "Investigation Initiated" (matches the view's constant)
_INVESTIGATION_STATUS_ID = '11688538967068'


@pytest.fixture
def api_client():
    """Provides DRF API client for testing."""
    return APIClient()


@pytest.fixture
def system_settings():
    """Creates SystemSettings with Zendesk credentials."""
    # Use get_or_create since SystemSettings is a singleton (pk=1)
    settings, created = SystemSettings.objects.get_or_create(
        pk=1,
        defaults={
            'zd_email': 'test@company.com',
            'zd_token': 'test_zendesk_token_12345',
            'zd_subdomain': 'testcompany',
            'sidebar_secret_token': 'test-webhook-secret',
            'ai_api_key': 'test_ai_key',
            'ai_api_base': 'https://api.example.com/v1',
            'ai_api_model': 'qwen-turbo',
        }
    )
    if not created:
        settings.sidebar_secret_token = 'test-webhook-secret'
        settings.save()
    return settings


@pytest.fixture
def valid_webhook_payload():
    """Returns a valid nested webhook payload matching the current Zendesk format.

    NOTE: The old flat payload (ticket_id/subject/requester/status at top level) is
    no longer accepted by ZendeskClaimWebhookView for status-gating. The view now
    reads event.current for the custom status ID. This fixture has been updated to
    the nested format so all tests exercise the real code path.
    """
    return _nested_webhook_payload(ticket_id='12345', subject='Lost Item - ALF1234567')


def _full_extracted_data(**overrides):
    """Return a complete extracted-data dict with sensible defaults.

    Providing a complete dict avoids KeyError when the view reads fields that
    older per-test mocks omitted (billing_address, shipping_address, etc.).
    """
    base = {
        'client_email': 'customer@example.com',
        'client_name': '',
        'flight_details': '',
        'object_description': '',
        'phone': '',
        'alternate_email': '',
        'claim_number': '',
        'billing_address': '',
        'shipping_address': '',
        'incident_details': '',
        'lost_location': '',
        'deadline_date': '',
        'deadline_time': '',
        'deadline_timezone': '',
        'price_paid': '',
        'payment_method': '',
        'payment_status': '',
        'woocommerce_id': '',
        'tracking_info': '',
    }
    base.update(overrides)
    return base


def _nested_webhook_payload(ticket_id='12345', subject='Lost Item - ALF1234567'):
    """Build a webhook payload matching the current nested Zendesk format."""
    return {
        'event': {'current': _INVESTIGATION_STATUS_ID},
        'detail': {
            'id': ticket_id,
            'subject': subject,
            'requester_id': 98765,
            'custom_status': _INVESTIGATION_STATUS_ID,
        },
    }


@pytest.mark.django_db
class TestZendeskClaimWebhookView:
    """Test cases for ZendeskClaimWebhookView."""

    def test_webhook_creates_claim_successfully(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Valid nested webhook creates claim with all fields populated from LLM."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'I lost my MacBook on flight AA123',
            'status': 'investigation_initiated',
            'requester_id': 98765,
            'assignee_id': None,
            'created_at': '2026-03-15T10:30:00Z',
            'updated_at': '2026-03-15T10:30:00Z',
        }

        mock_comments = [
            {
                'id': 1,
                'author': {'id': 98765, 'name': 'John Doe', 'email': 'customer@example.com'},
                'body': 'I lost my black MacBook Pro 15-inch on flight AA123 from JFK to LAX on March 15, 2026.',
                'public': True,
                'created_at': '2026-03-15T10:30:00Z',
            }
        ]

        mock_extracted_data = _full_extracted_data(
            client_email='customer@example.com',
            flight_details='Flight AA123 from JFK to LAX on March 15, 2026',
            object_description='Black MacBook Pro laptop, 15-inch',
            phone='+1-555-123-4567',
            alternate_email='john.doe.backup@gmail.com',
        )

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=mock_comments), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['message'] == 'Claim created successfully'
        assert response.data['alf_claim_id'] == 'ALF1234567'
        assert response.data['zd_ticket_id'] == '12345'
        assert response.data['llm_extraction_failed'] is False

        # Verify claim was created in database
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.alf_claim_id == 'ALF1234567'
        assert claim.client_email == 'customer@example.com'
        assert claim.flight_details == 'Flight AA123 from JFK to LAX on March 15, 2026'
        assert claim.object_description == 'Black MacBook Pro laptop, 15-inch'
        assert claim.phone == '+1-555-123-4567'
        assert claim.alternate_email == 'john.doe.backup@gmail.com'
        assert claim.status == 'Investigation initiated'
        assert claim.llm_extraction_failed is False

    def test_webhook_idempotency(self, api_client, system_settings):
        """Duplicate webhook for same ticket returns existing claim without re-fetching."""
        # Create existing claim
        existing_claim = Claim.objects.create(
            alf_claim_id='ALF1234567',
            zd_ticket_id='12345',
            client_email='customer@example.com',
            status='Investigation initiated',
        )

        payload = _nested_webhook_payload(ticket_id='12345')

        # The view's early existence check fires AFTER the secret check, so the
        # nested payload must carry the correct investigation status ID.
        with patch('apps.integrations.views.fetch_zendesk_ticket') as mock_fetch, \
             patch('apps.integrations.views.resolve_custom_status',
                   return_value={'name': 'Investigation initiated', 'category': 'open'}):
            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        # Existing claim → status-change path; same status → 200 no-op
        assert response.status_code == status.HTTP_200_OK

        # Verify fetch_zendesk_ticket was NOT called (status was same, no-op)
        mock_fetch.assert_not_called()

        # Verify no duplicate claim was created
        assert Claim.objects.filter(zd_ticket_id='12345').count() == 1

    def test_webhook_invalid_secret(self, api_client, system_settings):
        """Invalid webhook secret returns 401.

        Auth is checked before the body is parsed (Fix 2), so any payload with
        a wrong secret is rejected immediately regardless of ticket_id presence.
        """
        payload = _nested_webhook_payload(ticket_id='12345')
        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=payload,
            format='json',
            HTTP_X_WEBHOOK_SECRET='invalid_secret_wrong',
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.data['error'] == 'Invalid webhook secret'

    def test_webhook_missing_ticket_id(self, api_client, system_settings):
        """Nested payload with no detail.id and no ticket_id returns 400."""
        # A payload that has an event section but no id in detail and no ticket_id
        payload = {
            'event': {'current': _INVESTIGATION_STATUS_ID},
            'detail': {'subject': 'Lost Item - ALF1234567'},
        }

        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=payload,
            format='json',
            HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Missing required field: ticket_id'

    def test_webhook_alf_id_parsing(self, api_client, system_settings):
        """ALF claim ID correctly parsed from the nested payload's subject."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost luggage',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        mock_extracted_data = _full_extracted_data(client_email='customer@example.com')

        payload = _nested_webhook_payload(ticket_id='12345', subject='Lost Item - ALF1234567')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['alf_claim_id'] == 'ALF1234567'

        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.alf_claim_id == 'ALF1234567'

    def test_webhook_alf_id_not_found(self, api_client, system_settings):
        """No ALF id in subject or Claim # field → ignored, no claim.

        Contract changed 2026-06-12: the placeholder-id fallback admitted
        phone-call/email tickets as junk claims. Tickets without a real ALF
        claim number are not claim-form tickets and are ignored.
        """
        mock_ticket_data = {
            'id': '67890',
            'subject': 'Lost Item Report',
            'description': 'Lost luggage',
            'status': 'investigation_initiated',
            'requester_id': 98765,
            'custom_fields': [],
        }

        payload = _nested_webhook_payload(ticket_id='67890', subject='Lost Item Report')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value=None):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_200_OK
        assert 'not a claim form ticket' in response.data['message']
        assert not Claim.objects.filter(zd_ticket_id='67890').exists()

    def test_webhook_llm_extraction_success(self, api_client, system_settings):
        """LLM extracts all fields correctly — llm_extraction_failed is False."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        mock_comments = [
            {
                'id': 1,
                'author': {'id': 1, 'name': 'Customer', 'email': 'customer@example.com'},
                'body': 'Lost my bag on flight',
                'public': True,
                'created_at': '2026-03-15T10:30:00Z',
            }
        ]

        mock_extracted_data = _full_extracted_data(
            client_email='customer@example.com',
            flight_details='Flight AA123 from JFK to LAX',
            object_description='Black suitcase with wheels',
            phone='+1-555-987-6543',
            alternate_email='backup@example.com',
        )

        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=mock_comments), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['llm_extraction_failed'] is False

        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.llm_extraction_failed is False
        assert claim.client_email == 'customer@example.com'
        assert claim.flight_details == 'Flight AA123 from JFK to LAX'
        assert claim.object_description == 'Black suitcase with wheels'

    def test_webhook_llm_extraction_failed(self, api_client, system_settings):
        """When LLM returns empty data, llm_extraction_failed=True and email
        falls back to the Zendesk user API (requester_id is in detail)."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        # LLM returns empty data (extraction failed)
        mock_extracted_data = _full_extracted_data(
            client_email='',
            flight_details='',
        )

        # The nested payload has no top-level 'requester.email'; the view falls
        # through to fetch_zendesk_user with the requester_id from detail.
        mock_user_data = {'email': 'customer@example.com', 'name': 'John Doe'}

        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.services.fetch_zendesk_user', return_value=mock_user_data), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['llm_extraction_failed'] is True

        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.llm_extraction_failed is True
        # Email resolved via Zendesk user API fallback
        assert claim.client_email == 'customer@example.com'

    def test_webhook_requester_email_fallback(self, api_client, system_settings):
        """When LLM returns no email, the view falls back to fetch_zendesk_user
        using the requester_id from detail. Other LLM fields are still persisted."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        # LLM extracts other fields but not email
        mock_extracted_data = _full_extracted_data(
            client_email='',  # Empty - LLM couldn't find email
            flight_details='Flight AA123 from JFK to LAX',
            object_description='Black suitcase',
        )

        mock_user_data = {'email': 'customer@example.com', 'name': 'John Doe'}

        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.services.fetch_zendesk_user', return_value=mock_user_data), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        # Email resolved via Zendesk user API
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.client_email == 'customer@example.com'
        # Other LLM fields still populated
        assert claim.flight_details == 'Flight AA123 from JFK to LAX'
        assert claim.object_description == 'Black suitcase'

    def test_webhook_fetch_ticket_fails(self, api_client, system_settings):
        """Returns 500 when Zendesk API fails to fetch ticket."""
        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=None):
            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.data['error'] == 'Failed to fetch Zendesk ticket'

    def test_webhook_no_secret_header(self, api_client, system_settings):
        """Webhook without X-Webhook-Secret header is rejected with 401 (auth is mandatory)."""
        payload = _nested_webhook_payload(ticket_id='12345')

        # No HTTP_X_WEBHOOK_SECRET header
        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=payload,
            format='json',
        )

        # Auth is now mandatory — missing header returns 401
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_webhook_system_settings_error(self, api_client):
        """When SystemSettings has no token configured (blank row) and a secret
        header is present, the view returns 401.

        NOTE: The original test expected HTTP 500 on the assumption that
        SystemSettings.get_instance() would raise when no row exists. In
        practice get_instance() uses get_or_create, so it always succeeds —
        returning a blank-token row. hmac.compare_digest(provided, '') then
        returns False, which the view correctly surfaces as 401 Unauthorized
        rather than 500. The asserted behavior has been updated accordingly.
        """
        # Ensure no SystemSettings row exists; get_instance() will auto-create
        # one with an empty sidebar_secret_token.
        SystemSettings.objects.all().delete()

        # Send a nested payload WITH a secret header so the view reaches the
        # secret-comparison branch.
        payload = _nested_webhook_payload(ticket_id='12345')
        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=payload,
            format='json',
            HTTP_X_WEBHOOK_SECRET='any_secret',
        )

        # View loads settings (auto-created, empty token), compare_digest fails → 401
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.data['error'] == 'Invalid webhook secret'


@pytest.mark.django_db
class TestZendeskClaimWebhookEdgeCases:
    """Edge case tests for ZendeskClaimWebhookView."""

    def test_webhook_empty_subject(self, api_client, system_settings):
        """Empty subject and no Claim # field → ignored, no claim.

        Contract changed 2026-06-12: no more placeholder ALF ids — a ticket
        without a real claim number is not a claim-form ticket.
        """
        mock_ticket_data = {
            'id': '12345',
            'subject': '',
            'description': 'Lost item',
            'status': 'investigation_initiated',
            'requester_id': 98765,
            'custom_fields': [],
        }

        payload = _nested_webhook_payload(ticket_id='12345', subject='')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value=None):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_200_OK
        assert 'not a claim form ticket' in response.data['message']
        assert not Claim.objects.filter(zd_ticket_id='12345').exists()

    def test_webhook_special_characters_in_subject(self, api_client, system_settings):
        """Nested payload with special characters in subject is handled gracefully."""
        subject_with_specials = 'Lost Item - ALF1234567 - Special chars: <>&"\''
        mock_ticket_data = {
            'id': '12345',
            'subject': subject_with_specials,
            'description': 'Lost item',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        mock_extracted_data = _full_extracted_data(client_email='customer@example.com')

        payload = _nested_webhook_payload(ticket_id='12345', subject=subject_with_specials)

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['alf_claim_id'] == 'ALF1234567'

    def test_webhook_missing_requester_email(self, api_client, system_settings):
        """When LLM and Zendesk user API both return no email, claim is still created
        with empty client_email and llm_extraction_failed=True."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        # LLM also fails to extract email
        mock_extracted_data = _full_extracted_data(
            client_email='',
            flight_details='',
        )

        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.services.fetch_zendesk_user', return_value=None), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        # Claim created with empty email; llm_extraction_failed must be True
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.client_email == ''
        assert claim.llm_extraction_failed is True

    def test_webhook_llm_exception(self, api_client, system_settings):
        """Exception from analyze_zendesk_ticket_for_claim triggers fallback path.

        The view catches the exception, uses empty extracted_data, then resolves
        the email via fetch_zendesk_user. Claim is created with llm_extraction_failed=True.
        """
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        mock_user_data = {'email': 'customer@example.com', 'name': 'John Doe'}

        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', side_effect=Exception('LLM API error')), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.integrations.services.fetch_zendesk_user', return_value=mock_user_data), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        # Should still create claim (with fallback email from Zendesk user API)
        assert response.status_code == status.HTTP_201_CREATED
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.llm_extraction_failed is True
        assert claim.client_email == 'customer@example.com'

    def test_webhook_database_error(self, api_client, system_settings):
        """A non-IntegrityError exception from Claim.objects.create returns 500."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }

        mock_extracted_data = _full_extracted_data(client_email='customer@example.com')

        payload = _nested_webhook_payload(ticket_id='12345')

        # Simulate a generic (non-IntegrityError) database error
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.claims.models.Claim.objects.create', side_effect=Exception('DB error')):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.data['error'] == 'Internal server error'


# ---------------------------------------------------------------------------
# Regression tests for two bugs identified by code review on 2026-06-02.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestZendeskClaimRaceCondition:
    """Bug 1: concurrent webhooks for the same ticket must not create duplicate Claims."""

    def test_zd_ticket_id_unique_constraint_at_db_level(self):
        """After the constraint migration, two Claims with the same zd_ticket_id
        raise IntegrityError at the DB level. This is the foundation that makes
        the view-level IntegrityError handling work safely under real concurrency."""
        from django.db import IntegrityError

        Claim.objects.create(
            alf_claim_id='ALF0099001',
            zd_ticket_id='99001',
            client_email='first@example.com',
            status='Investigation initiated',
        )
        with pytest.raises(IntegrityError):
            Claim.objects.create(
                alf_claim_id='ALF0099002',
                zd_ticket_id='99001',  # duplicate
                client_email='second@example.com',
                status='Investigation initiated',
            )

    def test_null_zd_ticket_id_allowed_multiple_times(self):
        """The unique constraint does NOT apply to NULL values, so manually-created
        claims without a Zendesk ticket can still coexist."""
        Claim.objects.create(
            alf_claim_id='ALF0099003',
            zd_ticket_id=None,
            client_email='manual1@example.com',
            status='Investigation initiated',
        )
        Claim.objects.create(
            alf_claim_id='ALF0099004',
            zd_ticket_id=None,
            client_email='manual2@example.com',
            status='Investigation initiated',
        )
        # Scope to just the two claims we created (other test fixtures may
        # have unrelated NULL-ticket claims in the test DB).
        ours = Claim.objects.filter(alf_claim_id__in=['ALF0099003', 'ALF0099004'])
        assert ours.count() == 2
        assert all(c.zd_ticket_id is None for c in ours)

    def test_view_handles_race_via_integrity_error(self, api_client, system_settings):
        """Simulates a real race: the view's early existence check returns None,
        but during ticket-processing another concurrent webhook creates the same
        Claim. When the view reaches its own Claim.objects.create(), it hits the
        DB unique constraint (IntegrityError). The view must catch the error and
        return a graceful 'already exists' response, with no duplicate row."""
        mock_ticket = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }
        mock_extracted = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        # Simulate the race: create the conflicting Claim DURING the LLM extraction
        # call, AFTER the view's early-existence check has already returned None.
        def extraction_with_race_create(ticket_data):
            Claim.objects.create(
                alf_claim_id='ALF1234567',
                zd_ticket_id='12345',
                client_email='other-webhook@example.com',
                status='Investigation initiated',
            )
            return mock_extracted

        payload = _nested_webhook_payload(ticket_id='12345')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   side_effect=extraction_with_race_create), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject',
                   return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_200_OK
        assert 'already exists' in response.data['message'].lower()
        # Exactly one Claim must exist for this ticket — no duplicate from the race
        assert Claim.objects.filter(zd_ticket_id='12345').count() == 1


@pytest.mark.django_db
class TestZendeskClaimEmptyEmailHandling:
    """Bug 2: when every email-resolution path fails, operators must be notified
    via a WARNING log so the manual-review queue gets attention instead of the
    failure being silently lost."""

    def test_warning_logged_and_flag_forced_when_email_unresolvable(
        self, api_client, system_settings, caplog
    ):
        """When LLM extraction returns no email, the webhook has no requester.email,
        and the Zendesk user API returns no email either — the view emits a
        WARNING log mentioning the ticket_id and forces llm_extraction_failed=True
        so the manual-review queue picks the Claim up."""
        mock_ticket = {
            'id': '77001',
            'subject': 'Lost Item - ALF7700001',
            'description': 'Lost item',
            'status': 'investigation_initiated',
            'requester_id': 12345,  # present so fallback path is exercised
        }
        mock_extracted = {
            'client_email': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        # Payload has NO top-level 'requester' object, so webhook fallback fails.
        payload = _nested_webhook_payload(ticket_id='77001', subject='Lost Item - ALF7700001')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value=mock_extracted), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject',
                   return_value='ALF7700001'), \
             patch('apps.integrations.services.fetch_zendesk_user', return_value=None), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            with caplog.at_level('WARNING', logger='apps.integrations.views'):
                response = api_client.post(
                    reverse('zendesk-claim-webhook'),
                    data=payload,
                    format='json',
                    HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
                )

        assert response.status_code == status.HTTP_201_CREATED

        # Claim is saved so the manual-review queue surfaces it
        claim = Claim.objects.get(zd_ticket_id='77001')
        assert claim.llm_extraction_failed is True, \
            "When client_email cannot be resolved, llm_extraction_failed must be True"
        # client_email empty is acceptable — operator fills it in during review
        assert claim.client_email == ''

        # A WARNING-level log mentioning the ticket_id and the word 'email' was emitted
        warning_records = [r for r in caplog.records if r.levelname == 'WARNING']
        relevant = [
            r for r in warning_records
            if '77001' in r.message and 'email' in r.message.lower()
        ]
        assert len(relevant) >= 1, (
            f"Expected a WARNING log mentioning ticket 77001 and 'email'. "
            f"Got these records: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )


@pytest.mark.django_db
class TestZendeskClaimEnrichedFields:
    """Verifies the enriched structured-field wiring (2026-06-10): client_name
    persists on the Claim, and the 'Claim #' field drives the ALF id."""

    def test_client_name_persisted_and_claim_number_field_drives_alf_id(
        self, api_client, system_settings
    ):
        """When the extractor returns a client_name and a claim_number, the Claim
        is saved with the name and the ALF id comes from the Claim # field (not
        the subject)."""
        mock_ticket = {
            'id': '88123',
            'subject': 'Lost item - ALF0000001',  # subject has a DIFFERENT id
            'description': 'Lost a watch',
            'status': 'investigation_initiated',
            'requester_id': 98765,
        }
        # Extractor returns the Claim # field value distinct from the subject id
        mock_extracted = {
            'client_email': 'real.client@example.com',
            'client_name': 'Maria Schmidt',
            'flight_details': 'Flight: LH400 | Airport: FRA',
            'object_description': 'Silver wristwatch',
            'phone': '+49 30 1234567',
            'alternate_email': '',
            'claim_number': 'ALF9990001',  # authoritative
        }
        payload = _nested_webhook_payload(ticket_id='88123', subject='Lost item - ALF0000001')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value=mock_extracted), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject',
                   side_effect=lambda s: 'ALF9990001' if s == 'ALF9990001'
                   else ('ALF0000001' if 'ALF0000001' in (s or '') else None)), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        claim = Claim.objects.get(zd_ticket_id='88123')
        # client_name persisted from the Customer Name field
        assert claim.client_name == 'Maria Schmidt'
        # ALF id came from the Claim # field, NOT the subject's ALF0000001
        assert claim.alf_claim_id == 'ALF9990001'
        # ai_summary is empty at creation (real AI call is best-effort, mocked to False)
        assert claim.ai_summary == ''

    def test_extended_fields_persist_with_type_coercion(self, api_client, system_settings):
        """deadline_date coerces to a date, price_paid to Decimal, and the text
        fields persist; bad date/price values become None rather than raising."""
        from datetime import date
        from decimal import Decimal

        mock_ticket = {
            'id': '88200', 'subject': 'Lost item - ALF8820000',
            'description': 'x', 'status': 'investigation_initiated', 'requester_id': 1,
        }
        mock_extracted = {
            'client_email': 'c@example.com', 'client_name': 'Sam Lee',
            'flight_details': '', 'object_description': 'Bag', 'phone': '',
            'alternate_email': '', 'claim_number': '',
            'billing_address': '1 Bill St', 'shipping_address': '2 Ship Ave',
            'incident_details': 'Lost at gate', 'lost_location': 'Gate B12',
            'deadline_date': '2026-07-01', 'deadline_time': '17:00',
            'deadline_timezone': 'Europe/Berlin', 'price_paid': '149.99',
            'payment_method': 'PayPal', 'payment_status': 'Paid',
            'woocommerce_id': 'WC-55012', 'tracking_info': 'DHL 123',
        }
        payload = _nested_webhook_payload(ticket_id='88200', subject='Lost item - ALF8820000')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF8820000'), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):
            response = api_client.post(
                reverse('zendesk-claim-webhook'), data=payload, format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        claim = Claim.objects.get(zd_ticket_id='88200')
        assert claim.deadline_date == date(2026, 7, 1)
        assert claim.price_paid == Decimal('149.99')
        assert claim.shipping_address == '2 Ship Ave'
        assert claim.billing_address == '1 Bill St'
        assert claim.lost_location == 'Gate B12'
        assert claim.woocommerce_id == 'WC-55012'
        assert claim.tracking_info == 'DHL 123'
        assert claim.payment_method == 'PayPal'

    def test_bad_deadline_and_price_become_none(self, api_client, system_settings):
        """Malformed deadline_date / price_paid values do not crash; they store None."""
        mock_ticket = {
            'id': '88201', 'subject': 'Lost item - ALF8820001',
            'description': 'x', 'status': 'investigation_initiated', 'requester_id': 1,
        }
        mock_extracted = {
            'client_email': 'c@example.com', 'client_name': '', 'flight_details': '',
            'object_description': 'Bag', 'phone': '', 'alternate_email': '', 'claim_number': '',
            'billing_address': '', 'shipping_address': '', 'incident_details': '',
            'lost_location': '', 'deadline_date': 'not-a-date', 'deadline_time': '',
            'deadline_timezone': '', 'price_paid': 'free', 'payment_method': '',
            'payment_status': '', 'woocommerce_id': '', 'tracking_info': '',
        }
        payload = _nested_webhook_payload(ticket_id='88201', subject='Lost item - ALF8820001')

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF8820001'), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):
            response = api_client.post(
                reverse('zendesk-claim-webhook'), data=payload, format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        claim = Claim.objects.get(zd_ticket_id='88201')
        assert claim.deadline_date is None
        assert claim.price_paid is None


# ---------------------------------------------------------------------------
# New tests: mandatory auth + status-change mirroring (Task 7)
# ---------------------------------------------------------------------------


class WebhookTestBase(TestCase):
    """Base TestCase with SystemSettings and a posting helper."""

    def setUp(self):
        self.settings_obj, _ = SystemSettings.objects.get_or_create(pk=1)
        self.settings_obj.sidebar_secret_token = 'test-webhook-secret'
        self.settings_obj.save()
        self.webhook_url = reverse('zendesk-claim-webhook')

    def _post_webhook(self, payload):
        return self.client.post(
            self.webhook_url, payload, content_type='application/json',
            HTTP_X_WEBHOOK_SECRET='test-webhook-secret',
        )


class WebhookAuthRequiredTests(WebhookTestBase):
    def test_missing_secret_is_rejected(self):
        payload = json.dumps({'event': {'current': '11688538967068'}, 'detail': {'id': '50001'}})
        response = self.client.post(self.webhook_url, payload, content_type='application/json')
        self.assertEqual(response.status_code, 401)

    def test_wrong_secret_is_rejected(self):
        payload = json.dumps({'event': {'current': '11688538967068'}, 'detail': {'id': '50001'}})
        response = self.client.post(
            self.webhook_url, payload, content_type='application/json',
            HTTP_X_WEBHOOK_SECRET='wrong',
        )
        self.assertEqual(response.status_code, 401)


class WebhookStatusMirrorTests(WebhookTestBase):
    def setUp(self):
        super().setUp()
        self.claim = Claim.objects.create(
            client_email='mirror@example.com', zd_ticket_id='60001',
            status='Investigation initiated', status_category='open')

    def _payload(self, status_id='222'):
        return json.dumps({'event': {'current': status_id}, 'detail': {'id': '60001'}})

    @patch('apps.integrations.views.refresh_claim_summary', return_value=True)
    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Claim submitted', 'category': 'open'})
    @patch('apps.integrations.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.fetch_zendesk_ticket',
           return_value={'subject': 's', 'description': 'd', 'comments': []})
    def test_status_change_updates_claim_and_writes_timeline(self, *_mocks):
        response = self._post_webhook(self._payload())
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, 'Claim submitted')
        self.assertEqual(self.claim.status_category, 'open')
        self.assertIsNotNone(self.claim.status_changed_at)
        entry = self.claim.updates.first()
        self.assertEqual(entry.update_type, 'STATUS_CHANGE')
        self.assertIn('Investigation initiated', entry.changes_summary)

    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Investigation initiated', 'category': 'open'})
    def test_same_status_is_a_noop(self, _mock):
        response = self._post_webhook(self._payload('111'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.claim.updates.count(), 0)

    @patch('apps.integrations.views.refresh_claim_summary', return_value=False)
    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Object Found', 'category': 'open'})
    @patch('apps.integrations.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.fetch_zendesk_ticket', return_value=None)
    def test_summary_failure_does_not_block_status_update(self, *_mocks):
        response = self._post_webhook(self._payload())
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, 'Object Found')
        # Timeline entry is written atomically with the status save (Fix 3);
        # llm_summary stays '' when the AI call does not succeed.
        entry = self.claim.updates.first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.llm_summary, '')
        # ai_summary on the claim itself is unchanged from creation ('')
        self.assertEqual(self.claim.ai_summary, '')

    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Investigation initiated', 'category': 'open'})
    def test_creation_retry_is_noop(self, _mock_resolve):
        """Creation retry: claim already exists with the creation status name.

        When Zendesk retries the 'investigation initiated' webhook and the claim
        already exists with that exact status, _handle_status_change must return
        200 with no timeline entry written and the status unchanged.
        """
        # claim already has status 'Investigation initiated' (from setUp)
        response = self._post_webhook(
            json.dumps({'event': {'current': _INVESTIGATION_STATUS_ID},
                        'detail': {'id': '60001'}}))
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, 'Investigation initiated')
        self.assertEqual(self.claim.updates.count(), 0)

    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': '424242', 'category': ''})
    def test_unresolved_status_guard(self, _mock_resolve):
        """Unresolved status id: resolver returns the raw id as the name.

        The view must not overwrite a real named status with a numeric id.
        Returns 503 so Zendesk retries, status unchanged, zero timeline entries.
        """
        payload = json.dumps({'event': {'current': '424242'}, 'detail': {'id': '60001'}})
        response = self._post_webhook(payload)
        self.assertEqual(response.status_code, 503)
        self.assertIn('could not be resolved', response.json()['error'])
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, 'Investigation initiated')
        self.assertEqual(self.claim.updates.count(), 0)

    def test_unknown_ticket_with_non_creation_status_is_ignored(self):
        with patch('apps.integrations.views.resolve_custom_status',
                   return_value={'name': 'Pending', 'category': 'pending'}):
            response = self._post_webhook(
                json.dumps(
                    {'event': {'current': '333'}, 'detail': {'id': '99999'}}))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Ignored', response.json()['message'])


@pytest.mark.django_db
class TestFormTicketGate:
    """Only claim-form tickets become claims (added 2026-06-12).

    Phone calls and client emails auto-created as Zendesk tickets carry no
    ALF claim number — when Zendesk flips them to Investigation initiated
    (e.g. the Open category's default status), the webhook must ignore them
    BEFORE any AI extraction runs. The placeholder-id fallback
    (ALF + zero-padded ticket id) that used to admit them is gone.
    """

    def _post(self, api_client, system_settings, ticket_id, subject):
        return api_client.post(
            reverse('zendesk-claim-webhook'),
            data=_nested_webhook_payload(ticket_id=ticket_id, subject=subject),
            format='json',
            HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
        )

    def test_phone_ticket_without_alf_number_is_ignored_without_ai(
            self, api_client, system_settings):
        ticket = {'id': '55001', 'subject': 'Incoming call from +40 721 000 000',
                  'custom_fields': []}
        with patch('apps.integrations.services.fetch_zendesk_ticket',
                   return_value=ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments') as mock_comments, \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim') as mock_ai:
            response = self._post(api_client, system_settings,
                                  '55001', 'Incoming call from +40 721 000 000')

        assert response.status_code == 200
        assert 'not a claim form ticket' in response.data['message']
        assert not Claim.objects.filter(zd_ticket_id='55001').exists()
        mock_ai.assert_not_called()
        mock_comments.assert_not_called()

    def test_alf_number_in_claim_field_still_creates(
            self, api_client, system_settings):
        """Subject has no ALF id but the structured Claim # field does."""
        from apps.integrations.services import ZENDESK_FIELD_CLAIM_NUMBER
        ticket = {'id': '55002', 'subject': 'Lost laptop',
                  'custom_fields': [
                      {'id': ZENDESK_FIELD_CLAIM_NUMBER, 'value': 'ALF7654321'}]}
        with patch('apps.integrations.services.fetch_zendesk_ticket',
                   return_value=ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments',
                   return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value=_full_extracted_data()), \
             patch('apps.integrations.briefing.refresh_claim_summary',
                   return_value=False):
            response = self._post(api_client, system_settings, '55002', 'Lost laptop')

        assert response.status_code == 201
        claim = Claim.objects.get(zd_ticket_id='55002')
        assert claim.alf_claim_id == 'ALF7654321'

    def test_alf_number_in_subject_still_creates(
            self, api_client, system_settings):
        ticket = {'id': '55003', 'subject': 'Lost Item - ALF1112223',
                  'custom_fields': []}
        with patch('apps.integrations.services.fetch_zendesk_ticket',
                   return_value=ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments',
                   return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value=_full_extracted_data()), \
             patch('apps.integrations.briefing.refresh_claim_summary',
                   return_value=False):
            response = self._post(api_client, system_settings,
                                  '55003', 'Lost Item - ALF1112223')

        assert response.status_code == 201
        assert Claim.objects.get(zd_ticket_id='55003').alf_claim_id == 'ALF1112223'
