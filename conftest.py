"""
Pytest configuration and shared fixtures for the ALF application.

This file configures pytest-django and provides shared fixtures for all tests.
"""

import os
import pytest
import django
from django.conf import settings

# Configure Django settings before importing Django modules
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')


@pytest.fixture(scope='session')
def django_db_setup():
    """
    Set up the test database for the test session.
    
    This fixture is automatically used by pytest-django.
    It creates a test database and runs migrations.
    """
    pass


@pytest.fixture
def test_user(django_user_model):
    """
    Creates a test user for authentication tests.
    
    Usage:
        def test_something(test_user):
            assert test_user.username == 'testuser'
    """
    return django_user_model.objects.create_user(
        username='testuser',
        email='testuser@example.com',
        password='testpass123',
    )


@pytest.fixture
def test_manager_user(django_user_model):
    """
    Creates a test manager user for permission tests.
    
    Usage:
        def test_manager_action(test_manager_user):
            assert test_manager_user.role == 'MANAGER'
    """
    return django_user_model.objects.create_user(
        username='testmanager',
        email='manager@example.com',
        password='testpass123',
        role='MANAGER',
    )


@pytest.fixture
def test_agent_user(django_user_model):
    """
    Creates a test agent user for assignment tests.
    
    Usage:
        def test_agent_assignment(test_agent_user):
            claim.assigned_to = test_agent_user
    """
    return django_user_model.objects.create_user(
        username='testagent',
        email='agent@example.com',
        password='testpass123',
        role='AGENT',
    )


@pytest.fixture
def sample_claim():
    """
    Creates a sample Claim object for testing.
    
    Usage:
        def test_claim_operation(sample_claim):
            assert sample_claim.client_email == 'sample@example.com'
    """
    from apps.claims.models import Claim
    
    return Claim.objects.create(
        alf_claim_id='ALF0000001',
        zd_ticket_id='11111',
        client_email='sample@example.com',
        flight_details='Flight AA100 from JFK to LAX',
        object_description='Black suitcase',
        status='Received',
    )


@pytest.fixture
def sample_claims():
    """
    Creates multiple sample Claim objects for testing queries.
    
    Usage:
        def test_claim_queries(sample_claims):
            assert Claim.objects.count() == 5
    """
    from apps.claims.models import Claim
    
    claims = []
    for i in range(5):
        claim = Claim.objects.create(
            alf_claim_id=f'ALF000000{i}',
            zd_ticket_id=f'{10000 + i}',
            client_email=f'user{i}@example.com',
            status='Received',
        )
        claims.append(claim)
    
    return claims


@pytest.fixture
def mock_zendesk_credentials():
    """
    Mocks Zendesk credentials in SystemSettings.

    Usage:
        def test_zendesk_integration(mock_zendesk_credentials):
            # SystemSettings is configured with test credentials
            pass
    """
    from apps.config.models import SystemSettings

    # Use get_or_create since SystemSettings is a singleton (pk=1)
    settings, created = SystemSettings.objects.get_or_create(
        pk=1,
        defaults={
            'zd_email': 'test@company.com',
            'zd_token': 'test_token_12345',
            'zd_subdomain': 'testcompany',
            'sidebar_secret_token': 'test_secret_abc123',
            'ai_api_key': 'test_ai_key',
            'ai_api_base': 'https://api.example.com/v1',
            'ai_api_model': 'qwen-turbo',
            'email_domain': 'mydomain.com',
        }
    )
    
    return settings


@pytest.fixture
def mock_email_log():
    """
    Creates a sample EmailLog object for testing.
    
    Usage:
        def test_email_processing(mock_email_log):
            assert mock_email_log.category == 'OBJECT_FOUND'
    """
    from apps.communications.models import EmailLog
    from apps.claims.models import Claim
    
    claim = Claim.objects.create(
        client_email='email-test@example.com',
    )
    
    return EmailLog.objects.create(
        claim=claim,
        subject='Test Email Subject',
        body='Test email body content',
        ai_summary='AI summary of the email',
        action_required=True,
        from_email='sender@example.com',
        to_email='recipient@mydomain.com',
        alias_matched='client-123@mydomain.com',
        zd_ticket_id='12345',
        category='OBJECT_FOUND',
        auto_resolved=False,
    )


@pytest.fixture
def mock_dispute():
    """
    Creates a sample Dispute object for testing.
    
    Usage:
        def test_dispute_workflow(mock_dispute):
            assert mock_dispute.status == 'RECEIVED'
    """
    from apps.payments.models import Dispute
    from apps.claims.models import Claim
    
    claim = Claim.objects.create(
        client_email='dispute-test@example.com',
    )
    
    return Dispute.objects.create(
        paypal_dispute_id='DISPUTE-12345',
        claim=claim,
        zd_ticket_id='12345',
        status='RECEIVED',
        dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
        dispute_amount=100.00,
        dispute_currency='USD',
        buyer_email='buyer@example.com',
        buyer_name='Test Buyer',
        transaction_id='TXN-12345',
        transaction_date='2026-03-15T10:00:00Z',
    )


@pytest.fixture
def mock_refund():
    """
    Creates a sample Refund object for testing.
    
    Usage:
        def test_refund_processing(mock_refund):
            assert mock_refund.status == 'PENDING'
    """
    from apps.payments.models import Refund
    from apps.claims.models import Claim
    
    claim = Claim.objects.create(
        client_email='refund-test@example.com',
    )
    
    return Refund.objects.create(
        claim=claim,
        paypal_refund_id='REFUND-12345',
        amount=50.00,
        currency='USD',
        refund_type='FULL',
        status='PENDING',
        external_source='LORA',
        reason='Customer request',
    )


@pytest.fixture
def api_client():
    """
    Provides DRF API client for testing.
    
    Usage:
        def test_api_endpoint(api_client):
            response = api_client.get('/api/endpoint/')
            assert response.status_code == 200
    """
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def authenticated_api_client(test_user):
    """
    Provides authenticated DRF API client for testing.
    
    Usage:
        def test_authenticated_endpoint(authenticated_api_client):
            response = authenticated_api_client.get('/api/protected/')
            assert response.status_code == 200
    """
    from rest_framework.test import APIClient
    from rest_framework.authtoken.models import Token
    
    client = APIClient()
    token = Token.objects.create(user=test_user)
    client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return client


# pytest-django configuration
pytest_plugins = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
]
