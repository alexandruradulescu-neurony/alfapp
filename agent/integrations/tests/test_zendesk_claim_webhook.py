"""
Tests for ZendeskClaimWebhookView - Zendesk-first claims flow with LLM extraction.

Tests the webhook endpoint that creates claims from Zendesk tickets when
status changes to 'investigation_initiated'.
"""

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
            'sidebar_secret_token': 'test_webhook_secret_abc123',
            'ai_api_key': 'test_ai_key',
            'ai_api_base': 'https://api.example.com/v1',
            'ai_api_model': 'qwen-turbo',
        }
    )
    return settings


@pytest.fixture
def valid_webhook_payload():
    """Returns a valid webhook payload for testing."""
    return {
        'ticket_id': '12345',
        'subject': 'Lost Item - ALF1234567',
        'requester': {
            'email': 'customer@example.com',
            'name': 'John Doe',
        },
        'status': 'investigation_initiated',
    }


@pytest.mark.django_db
class TestZendeskClaimWebhookView:
    """Test cases for ZendeskClaimWebhookView."""

    def test_webhook_creates_claim_successfully(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Valid webhook creates claim with all fields."""
        # Mock Zendesk API calls
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

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': 'Flight AA123 from JFK to LAX on March 15, 2026',
            'object_description': 'Black MacBook Pro laptop, 15-inch',
            'phone': '+1-555-123-4567',
            'alternate_email': 'john.doe.backup@gmail.com',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=mock_comments), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

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
        assert claim.status == 'Received'
        assert claim.llm_extraction_failed is False

    def test_webhook_idempotency(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Duplicate webhook for same ticket returns existing claim."""
        # Create existing claim
        existing_claim = Claim.objects.create(
            alf_claim_id='ALF1234567',
            zd_ticket_id='12345',
            client_email='customer@example.com',
            status='Received',
        )

        # Mock should not be called since we return early
        with patch('apps.integrations.services.fetch_zendesk_ticket') as mock_fetch:
            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        # Should return 200 OK (not 201 Created) for existing claim
        assert response.status_code == status.HTTP_200_OK
        assert response.data['message'] == 'Claim already exists'
        assert response.data['claim_id'] == existing_claim.id
        assert response.data['alf_claim_id'] == 'ALF1234567'

        # Verify fetch_zendesk_ticket was NOT called (early return)
        mock_fetch.assert_not_called()

        # Verify no duplicate claim was created
        assert Claim.objects.filter(zd_ticket_id='12345').count() == 1

    def test_webhook_invalid_secret(self, api_client, system_settings, valid_webhook_payload):
        """Invalid webhook secret returns 401."""
        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=valid_webhook_payload,
            format='json',
            HTTP_X_WEBHOOK_SECRET='invalid_secret_wrong',
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.data['error'] == 'Invalid webhook secret'

    def test_webhook_missing_ticket_id(self, api_client, system_settings):
        """Missing ticket_id returns 400."""
        payload = {
            'subject': 'Lost Item - ALF1234567',
            'requester': {'email': 'customer@example.com'},
        }

        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=payload,
            format='json',
            HTTP_X_WEBHOOK_SECRET='test_webhook_secret_abc123',
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Missing required field: ticket_id'

    def test_webhook_alf_id_parsing(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """ALF claim ID correctly parsed from subject."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost luggage',
            'status': 'investigation_initiated',
        }

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['alf_claim_id'] == 'ALF1234567'

        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.alf_claim_id == 'ALF1234567'

    def test_webhook_alf_id_not_found(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Missing ALF ID generates placeholder."""
        # Subject without ALF ID
        payload = {
            'ticket_id': '67890',
            'subject': 'Lost Item Report',
            'requester': {'email': 'customer@example.com'},
            'status': 'investigation_initiated',
        }

        mock_ticket_data = {
            'id': '67890',
            'subject': 'Lost Item Report',
            'description': 'Lost luggage',
            'status': 'investigation_initiated',
        }

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        # parse_alf_claim_id_from_subject returns None when no ALF ID found
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value=None):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        # Should generate placeholder: ALF + ticket_id zero-padded to 7 digits
        assert response.data['alf_claim_id'] == 'ALF0067890'

        claim = Claim.objects.get(zd_ticket_id='67890')
        assert claim.alf_claim_id == 'ALF0067890'

    def test_webhook_llm_extraction_success(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """LLM extracts all fields correctly."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
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

        # LLM successfully extracts all fields
        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': 'Flight AA123 from JFK to LAX',
            'object_description': 'Black suitcase with wheels',
            'phone': '+1-555-987-6543',
            'alternate_email': 'backup@example.com',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=mock_comments), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
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

    def test_webhook_llm_extraction_failed(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """LLM failure sets flag and uses fallback email."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
        }

        # LLM returns empty data (extraction failed)
        mock_extracted_data = {
            'client_email': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['llm_extraction_failed'] is True

        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.llm_extraction_failed is True
        # Should use requester email as fallback
        assert claim.client_email == 'customer@example.com'

    def test_webhook_requester_email_fallback(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Uses requester email when LLM fails to extract email."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item details',
            'status': 'investigation_initiated',
        }

        # LLM extracts other fields but not email
        mock_extracted_data = {
            'client_email': '',  # Empty - LLM couldn't find email
            'flight_details': 'Flight AA123 from JFK to LAX',
            'object_description': 'Black suitcase',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        # Should use requester email from payload as fallback
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.client_email == 'customer@example.com'
        # Other fields should still be populated
        assert claim.flight_details == 'Flight AA123 from JFK to LAX'
        assert claim.object_description == 'Black suitcase'

    def test_webhook_fetch_ticket_fails(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Returns 500 when Zendesk API fails to fetch ticket."""
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=None):
            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.data['error'] == 'Failed to fetch Zendesk ticket'

    def test_webhook_no_secret_header(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Webhook without secret header still processes (secret is optional)."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
        }

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            # No HTTP_X_WEBHOOK_SECRET header
            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
            )

        # Should still succeed (secret verification is conditional)
        assert response.status_code == status.HTTP_201_CREATED

    def test_webhook_system_settings_error(
        self, api_client, valid_webhook_payload
    ):
        """Returns 500 when SystemSettings cannot be loaded."""
        # Delete system settings to trigger error
        SystemSettings.objects.all().delete()

        # Don't send secret header - SystemSettings error happens before secret validation
        response = api_client.post(
            reverse('zendesk-claim-webhook'),
            data=valid_webhook_payload,
            format='json',
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


@pytest.mark.django_db
class TestZendeskClaimWebhookEdgeCases:
    """Edge case tests for ZendeskClaimWebhookView."""

    def test_webhook_empty_subject(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Handles webhook with empty subject."""
        payload = {
            'ticket_id': '12345',
            'subject': '',
            'requester': {'email': 'customer@example.com'},
            'status': 'investigation_initiated',
        }

        mock_ticket_data = {
            'id': '12345',
            'subject': '',
            'description': 'Lost item',
            'status': 'investigation_initiated',
        }

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value=None):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        # Should generate placeholder ALF ID from ticket_id
        assert response.data['alf_claim_id'] == 'ALF0012345'

    def test_webhook_special_characters_in_subject(
        self, api_client, system_settings
    ):
        """Handles webhook with special characters in subject."""
        payload = {
            'ticket_id': '12345',
            'subject': 'Lost Item - ALF1234567 - Special chars: <>&"\'',
            'requester': {'email': 'customer@example.com'},
            'status': 'investigation_initiated',
        }

        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567 - Special chars: <>&"\'',
            'description': 'Lost item',
            'status': 'investigation_initiated',
        }

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['alf_claim_id'] == 'ALF1234567'

    def test_webhook_missing_requester_email(
        self, api_client, system_settings
    ):
        """Handles webhook when requester email is missing."""
        payload = {
            'ticket_id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'requester': {},  # No email
            'status': 'investigation_initiated',
        }

        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
        }

        # LLM also fails to extract email
        mock_extracted_data = {
            'client_email': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_201_CREATED
        # Claim created with empty email (LLM failed flag set)
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.client_email == ''
        assert claim.llm_extraction_failed is True

    def test_webhook_llm_exception(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Handles exception during LLM extraction."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
        }

        # LLM extraction raises exception
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', side_effect=Exception('LLM API error')), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        # Should still create claim with fallback (LLM failed)
        assert response.status_code == status.HTTP_201_CREATED
        claim = Claim.objects.get(zd_ticket_id='12345')
        assert claim.llm_extraction_failed is True
        assert claim.client_email == 'customer@example.com'  # Fallback to requester email

    def test_webhook_database_error(
        self, api_client, system_settings, valid_webhook_payload
    ):
        """Handles database error during claim creation."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost item',
            'status': 'investigation_initiated',
        }

        mock_extracted_data = {
            'client_email': 'customer@example.com',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

        # Simulate database error on claim creation
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=mock_ticket_data), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim', return_value=mock_extracted_data), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value='ALF1234567'), \
             patch('apps.claims.models.Claim.objects.create', side_effect=Exception('DB error')):

            response = api_client.post(
                reverse('zendesk-claim-webhook'),
                data=valid_webhook_payload,
                format='json',
                HTTP_X_WEBHOOK_SECRET=system_settings.sidebar_secret_token,
            )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.data['error'] == 'Internal server error'
