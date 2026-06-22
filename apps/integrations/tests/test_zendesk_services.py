"""
Comprehensive tests for Zendesk integration services.

Tests all functions in apps/integrations/services.py including:
- Authentication and base URL helpers
- Ticket operations (fetch, create, update, search)
- Comment operations (post, fetch)
- Alias matching
- Dispute-related search
- Tagging and refund comments
- LLM extraction
- ALF claim ID parsing

All external API calls are mocked using unittest.mock.
"""

import pytest
import json
import base64
from unittest.mock import patch, MagicMock, Mock
from urllib.error import HTTPError, URLError
from io import BytesIO

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from apps.config.models import SystemSettings
from apps.integrations import services


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_system_settings():
    """
    Creates SystemSettings with Zendesk credentials.
    
    Note: We use get_or_create and then update fields to avoid
    encryption issues during test setup. The encrypted fields will
    use SECRET_KEY as fallback if ENCRYPTION_KEY is not set.
    """
    # Use get_or_create since SystemSettings is a singleton (pk=1)
    settings_obj, created = SystemSettings.objects.get_or_create(
        pk=1,
        defaults={
            'zd_subdomain': 'testcompany',
            'zd_token': 'test_zendesk_token_12345',
            'zd_email': 'test@testcompany.com',
            'ai_api_key': 'test_ai_key',
            'ai_api_base': 'https://api.example.com/v1',
            'ai_api_model': 'qwen-turbo',
            'sidebar_secret_token': 'test_secret',
        }
    )
    
    # If it already existed, update the fields to ensure correct values
    if not created:
        settings_obj.zd_subdomain = 'testcompany'
        settings_obj.zd_token = 'test_zendesk_token_12345'
        settings_obj.zd_email = 'test@testcompany.com'
        settings_obj.save()
    
    return settings_obj


@pytest.fixture
def mock_urlopen_response():
    """Creates a mock urlopen context manager response."""
    mock_response = MagicMock()
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)
    return mock_response


# =============================================================================
# Test _get_zendesk_auth_headers
# =============================================================================


@pytest.mark.django_db
class TestGetZendeskAuthHeaders:
    """Tests for _get_zendesk_auth_headers helper function."""

    def test_returns_correct_auth_headers(self, mock_system_settings):
        """Auth headers generated correctly from credentials."""
        headers = services._get_zendesk_auth_headers()

        expected_credentials = f"test@testcompany.com/token:test_zendesk_token_12345"
        expected_encoded = base64.b64encode(expected_credentials.encode('utf-8')).decode('utf-8')

        assert headers['Authorization'] == f'Basic {expected_encoded}'
        assert headers['Content-Type'] == 'application/json'

    def test_raises_when_credentials_missing(self):
        """ValueError raised when Zendesk credentials not configured."""
        # Delete existing settings
        SystemSettings.objects.all().delete()

        with pytest.raises(ValueError, match="Zendesk credentials not configured"):
            services._get_zendesk_auth_headers()

    def test_raises_when_subdomain_missing(self, mock_system_settings):
        """ValueError raised when subdomain is empty."""
        mock_system_settings.zd_subdomain = ''
        mock_system_settings.save()

        with pytest.raises(ValueError, match="Zendesk credentials not configured"):
            services._get_zendesk_auth_headers()

    def test_raises_when_token_missing(self, mock_system_settings):
        """ValueError raised when token is empty."""
        mock_system_settings.zd_token = ''
        mock_system_settings.save()

        with pytest.raises(ValueError, match="Zendesk credentials not configured"):
            services._get_zendesk_auth_headers()

    def test_raises_when_email_missing(self, mock_system_settings):
        """ValueError raised when email is empty."""
        mock_system_settings.zd_email = ''
        mock_system_settings.save()

        with pytest.raises(ValueError, match="Zendesk credentials not configured"):
            services._get_zendesk_auth_headers()


# =============================================================================
# Test _get_zendesk_base_url
# =============================================================================


@pytest.mark.django_db
class TestGetZendeskBaseUrl:
    """Tests for _get_zendesk_base_url helper function."""

    def test_returns_correct_base_url(self, mock_system_settings):
        """Base URL constructed correctly from subdomain."""
        url = services._get_zendesk_base_url()
        assert url == 'https://testcompany.zendesk.com/api/v2'

    def test_raises_when_subdomain_missing(self):
        """ValueError raised when subdomain not configured."""
        SystemSettings.objects.all().delete()

        with pytest.raises(ValueError, match="Zendesk subdomain not configured"):
            services._get_zendesk_base_url()

    def test_raises_when_subdomain_empty(self, mock_system_settings):
        """ValueError raised when subdomain is empty string."""
        mock_system_settings.zd_subdomain = ''
        mock_system_settings.save()

        with pytest.raises(ValueError, match="Zendesk subdomain not configured"):
            services._get_zendesk_base_url()


# =============================================================================
# Test post_zendesk_comment
# =============================================================================


@pytest.mark.django_db
class TestPostZendeskComment:
    """Tests for post_zendesk_comment function."""

    def test_posts_comment_successfully(self, mock_system_settings, mock_urlopen_response):
        """Comment posted successfully to Zendesk."""
        mock_urlopen_response.read.return_value = json.dumps({
            'ticket': {'id': '12345', 'status': 'open'}
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            result = services.post_zendesk_comment('12345', 'Test comment body', is_internal=True)

        assert result is not None
        assert result['ticket']['id'] == '12345'

        # Verify request was made correctly
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.method == 'PUT'
        assert 'tickets/12345.json' in call_args.full_url

        # Verify payload
        payload = json.loads(call_args.data.decode('utf-8'))
        assert payload['ticket']['comment']['body'] == 'Test comment body'
        assert payload['ticket']['comment']['public'] is False  # Internal note

    def test_posts_public_comment(self, mock_system_settings, mock_urlopen_response):
        """Public comment posted when is_internal=False."""
        mock_urlopen_response.read.return_value = json.dumps({
            'ticket': {'id': '12345'}
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            services.post_zendesk_comment('12345', 'Public comment', is_internal=False)

        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data.decode('utf-8'))
        assert payload['ticket']['comment']['public'] is True

    def test_returns_none_on_http_error(self, mock_system_settings):
        """Returns None when HTTP error occurs."""
        mock_response = MagicMock()
        mock_response.fp = None

        with patch('urllib.request.urlopen', side_effect=HTTPError(
            url='https://test.zendesk.com',
            code=404,
            msg='Not Found',
            hdrs={},
            fp=mock_response
        )):
            result = services.post_zendesk_comment('12345', 'Test comment')

        assert result is None

    def test_returns_none_on_url_error(self, mock_system_settings):
        """Returns None when URL error occurs."""
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            result = services.post_zendesk_comment('12345', 'Test comment')

        assert result is None

    def test_returns_none_on_value_error(self):
        """Returns None when configuration error occurs."""
        SystemSettings.objects.all().delete()

        result = services.post_zendesk_comment('12345', 'Test comment')
        assert result is None

    def test_returns_none_on_generic_exception(self, mock_system_settings):
        """Returns None when unexpected exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Unexpected error')):
            result = services.post_zendesk_comment('12345', 'Test comment')

        assert result is None

    def test_uses_configurable_timeout(self, mock_system_settings, mock_urlopen_response):
        """Uses ZENDESK_TIMEOUT setting if available."""
        mock_urlopen_response.read.return_value = json.dumps({'ticket': {}}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            services.post_zendesk_comment('12345', 'Test comment')

        # Verify timeout was passed (default 30)
        call_kwargs = mock_urlopen.call_args[1]
        assert call_kwargs['timeout'] == 30

    def test_logs_success_message(self, mock_system_settings, mock_urlopen_response, caplog):
        """Success message logged when comment posted."""
        mock_urlopen_response.read.return_value = json.dumps({'ticket': {}}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            services.post_zendesk_comment('12345', 'Test comment')

        assert 'Successfully posted comment to ticket 12345' in caplog.text


# =============================================================================
# Test fetch_zendesk_comments
# =============================================================================


@pytest.mark.django_db
class TestFetchZendeskComments:
    """Tests for fetch_zendesk_comments function."""

    def test_fetches_comments_successfully(self, mock_system_settings, mock_urlopen_response):
        """Comments fetched and transformed correctly."""
        mock_urlopen_response.read.return_value = json.dumps({
            'comments': [
                {
                    'id': 1,
                    'author': {'id': 100, 'name': 'John Doe', 'email': 'john@example.com'},
                    'body': 'First comment',
                    'public': True,
                    'created_at': '2026-03-15T10:00:00Z',
                },
                {
                    'id': 2,
                    'author': {'id': 101, 'name': 'Jane Smith', 'email': 'jane@example.com'},
                    'body': 'Second comment',
                    'public': False,
                    'created_at': '2026-03-15T11:00:00Z',
                },
            ]
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            comments = services.fetch_zendesk_comments('12345')

        assert len(comments) == 2
        assert comments[0]['id'] == 1
        assert comments[0]['author']['name'] == 'John Doe'
        assert comments[0]['body'] == 'First comment'
        assert comments[0]['public'] is True
        assert comments[1]['public'] is False

    def test_returns_empty_list_on_http_error(self, mock_system_settings):
        """Returns empty list when HTTP error occurs."""
        mock_response = MagicMock()
        mock_response.fp = None

        with patch('urllib.request.urlopen', side_effect=HTTPError(
            url='https://test.zendesk.com',
            code=404,
            msg='Not Found',
            hdrs={},
            fp=mock_response
        )):
            comments = services.fetch_zendesk_comments('12345')

        assert comments == []

    def test_returns_empty_list_on_url_error(self, mock_system_settings):
        """Returns empty list when URL error occurs."""
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            comments = services.fetch_zendesk_comments('12345')

        assert comments == []

    def test_returns_empty_list_on_value_error(self):
        """Returns empty list when configuration error occurs."""
        SystemSettings.objects.all().delete()

        comments = services.fetch_zendesk_comments('12345')
        assert comments == []

    def test_returns_empty_list_on_generic_exception(self, mock_system_settings):
        """Returns empty list when unexpected exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Unexpected error')):
            comments = services.fetch_zendesk_comments('12345')

        assert comments == []

    def test_handles_missing_author_fields(self, mock_system_settings, mock_urlopen_response):
        """Handles comments with missing author fields gracefully."""
        mock_urlopen_response.read.return_value = json.dumps({
            'comments': [
                {
                    'id': 1,
                    'author': {},  # Empty author
                    'body': 'Comment without author',
                    'public': True,
                    'created_at': '2026-03-15T10:00:00Z',
                }
            ]
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            comments = services.fetch_zendesk_comments('12345')

        assert len(comments) == 1
        assert comments[0]['author']['name'] == 'Unknown'
        assert comments[0]['author']['email'] == ''

    def test_handles_empty_comments_response(self, mock_system_settings, mock_urlopen_response):
        """Handles response with no comments."""
        mock_urlopen_response.read.return_value = json.dumps({
            'comments': []
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            comments = services.fetch_zendesk_comments('12345')

        assert comments == []


# =============================================================================
# Test fetch_zendesk_ticket
# =============================================================================


@pytest.mark.django_db
class TestFetchZendeskTicket:
    """Tests for fetch_zendesk_ticket function."""

    def test_fetches_ticket_successfully(self, mock_system_settings, mock_urlopen_response):
        """Ticket fetched and transformed correctly."""
        mock_urlopen_response.read.return_value = json.dumps({
            'ticket': {
                'id': '12345',
                'subject': 'Lost Item Report',
                'status': 'open',
                'priority': 'normal',
                'requester_id': 98765,
                'assignee_id': 11111,
                'created_at': '2026-03-15T10:00:00Z',
                'updated_at': '2026-03-15T12:00:00Z',
            }
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            ticket = services.fetch_zendesk_ticket('12345')

        assert ticket is not None
        assert ticket['id'] == '12345'
        assert ticket['subject'] == 'Lost Item Report'
        assert ticket['status'] == 'open'
        assert ticket['priority'] == 'normal'
        assert ticket['requester_id'] == 98765

    def test_returns_none_on_http_error(self, mock_system_settings):
        """Returns None when HTTP error occurs."""
        mock_response = MagicMock()
        mock_response.fp = None

        with patch('urllib.request.urlopen', side_effect=HTTPError(
            url='https://test.zendesk.com',
            code=404,
            msg='Not Found',
            hdrs={},
            fp=mock_response
        )):
            ticket = services.fetch_zendesk_ticket('12345')

        assert ticket is None

    def test_returns_none_on_url_error(self, mock_system_settings):
        """Returns None when URL error occurs."""
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            ticket = services.fetch_zendesk_ticket('12345')

        assert ticket is None

    def test_returns_none_on_generic_exception(self, mock_system_settings):
        """Returns None when unexpected exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Unexpected error')):
            ticket = services.fetch_zendesk_ticket('12345')

        assert ticket is None


# =============================================================================
# Test create_zendesk_ticket
# =============================================================================


@pytest.mark.django_db
class TestCreateZendeskTicket:
    """Tests for create_zendesk_ticket function."""

    def test_creates_ticket_successfully(self, mock_system_settings, mock_urlopen_response):
        """Ticket created successfully."""
        mock_urlopen_response.read.return_value = json.dumps({
            'ticket': {
                'id': '12345',
                'subject': 'New Ticket',
                'status': 'open',
                'url': 'https://testcompany.zendesk.com/api/v2/tickets/12345.json',
            }
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            result = services.create_zendesk_ticket(
                subject='New Ticket',
                comment_body='Initial comment',
                requester_email='customer@example.com',
                tags=['lora', 'lost-object'],
            )

        assert result is not None
        assert result['id'] == '12345'

        # Verify POST method
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.method == 'POST'

        # Verify payload
        payload = json.loads(call_args.data.decode('utf-8'))
        assert payload['ticket']['subject'] == 'New Ticket'
        assert payload['ticket']['comment']['body'] == 'Initial comment'
        assert payload['ticket']['requester']['email'] == 'customer@example.com'
        assert payload['ticket']['tags'] == ['lora', 'lost-object']

    def test_creates_ticket_with_default_tags(self, mock_system_settings, mock_urlopen_response):
        """Default tags used when none provided."""
        mock_urlopen_response.read.return_value = json.dumps({
            'ticket': {'id': '12345', 'tags': ['lora', 'lost-object']}
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            services.create_zendesk_ticket(
                subject='New Ticket',
                comment_body='Comment',
                requester_email='customer@example.com',
            )

        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data.decode('utf-8'))
        assert payload['ticket']['tags'] == ['lora', 'lost-object']

    def test_returns_none_on_http_error(self, mock_system_settings):
        """Returns None when HTTP error occurs."""
        mock_response = MagicMock()
        mock_response.fp = None

        with patch('urllib.request.urlopen', side_effect=HTTPError(
            url='https://test.zendesk.com',
            code=400,
            msg='Bad Request',
            hdrs={},
            fp=mock_response
        )):
            result = services.create_zendesk_ticket(
                subject='Test',
                comment_body='Test',
                requester_email='test@example.com',
            )

        assert result is None

    def test_returns_none_on_url_error(self, mock_system_settings):
        """Returns None when URL error occurs."""
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            result = services.create_zendesk_ticket(
                subject='Test',
                comment_body='Test',
                requester_email='test@example.com',
            )

        assert result is None

    def test_returns_none_on_generic_exception(self, mock_system_settings):
        """Returns None when unexpected exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Unexpected error')):
            result = services.create_zendesk_ticket(
                subject='Test',
                comment_body='Test',
                requester_email='test@example.com',
            )

        assert result is None



# =============================================================================
# Test search_zendesk_tickets
# =============================================================================


@pytest.mark.django_db
class TestSearchZendeskTickets:
    """Tests for search_zendesk_tickets function."""

    def test_searches_successfully(self, mock_system_settings, mock_urlopen_response):
        """Search returns results."""
        mock_urlopen_response.read.return_value = json.dumps({
            'results': [
                {'id': '12345', 'subject': 'Lost Item'},
                {'id': '12346', 'subject': 'Another Item'},
            ]
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            results = services.search_zendesk_tickets('requester:test@example.com')

        assert len(results) == 2
        assert results[0]['id'] == '12345'

        # Verify URL encoding
        call_args = mock_urlopen.call_args[0][0]
        assert 'query=requester%3Atest%40example.com' in call_args.full_url
        assert 'type=ticket' in call_args.full_url

    def test_returns_empty_list_on_empty_query(self, mock_system_settings):
        """Returns empty list when query is empty."""
        results = services.search_zendesk_tickets('')
        assert results == []

    def test_returns_empty_list_on_whitespace_query(self, mock_system_settings):
        """Returns empty list when query is only whitespace."""
        results = services.search_zendesk_tickets('   ')
        assert results == []

    def test_truncates_long_query(self, mock_system_settings, mock_urlopen_response):
        """Truncates query longer than 1000 characters."""
        mock_urlopen_response.read.return_value = json.dumps({'results': []}).encode('utf-8')

        long_query = 'a' * 1500

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            services.search_zendesk_tickets(long_query)

        call_args = mock_urlopen.call_args[0][0]
        # Query should be truncated to 1000 chars
        assert 'query=' + 'a' * 1000 in call_args.full_url

    def test_returns_empty_list_on_http_error(self, mock_system_settings):
        """Returns empty list when HTTP error occurs."""
        mock_response = MagicMock()
        mock_response.fp = None

        with patch('urllib.request.urlopen', side_effect=HTTPError(
            url='https://test.zendesk.com',
            code=400,
            msg='Bad Request',
            hdrs={},
            fp=mock_response
        )):
            results = services.search_zendesk_tickets('test')

        assert results == []

    def test_returns_empty_list_on_url_error(self, mock_system_settings):
        """Returns empty list when URL error occurs."""
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            results = services.search_zendesk_tickets('test')

        assert results == []

    def test_returns_empty_list_on_generic_exception(self, mock_system_settings):
        """Returns empty list when unexpected exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Unexpected error')):
            results = services.search_zendesk_tickets('test')

        assert results == []


# =============================================================================
# Test fetch_zendesk_ticket_full
# =============================================================================


@pytest.mark.django_db
class TestFetchZendeskTicketFull:
    """Tests for fetch_zendesk_ticket_full function."""

    def test_fetches_full_ticket_successfully(self, mock_system_settings, mock_urlopen_response):
        """Full ticket with custom fields fetched correctly."""
        mock_urlopen_response.read.return_value = json.dumps({
            'ticket': {
                'id': '12345',
                'subject': 'Lost Item',
                'description': 'I lost my bag',
                'status': 'open',
                'priority': 'high',
                'requester_id': 98765,
                'assignee_id': 11111,
                'custom_fields': [
                    {'id': 13606076120860, 'value': 'client-123@example.com'},
                ],
                'tags': ['lora', 'lost-object'],
                'created_at': '2026-03-15T10:00:00Z',
                'updated_at': '2026-03-15T12:00:00Z',
                'url': 'https://testcompany.zendesk.com/api/v2/tickets/12345.json',
            }
        }).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            ticket = services.fetch_zendesk_ticket_full('12345')

        assert ticket is not None
        assert ticket['id'] == '12345'
        assert ticket['description'] == 'I lost my bag'
        assert len(ticket['custom_fields']) == 1
        assert ticket['tags'] == ['lora', 'lost-object']

    def test_returns_none_on_http_error(self, mock_system_settings):
        """Returns None when HTTP error occurs."""
        mock_response = MagicMock()
        mock_response.fp = None

        with patch('urllib.request.urlopen', side_effect=HTTPError(
            url='https://test.zendesk.com',
            code=404,
            msg='Not Found',
            hdrs={},
            fp=mock_response
        )):
            ticket = services.fetch_zendesk_ticket_full('12345')

        assert ticket is None

    def test_returns_none_on_url_error(self, mock_system_settings):
        """Returns None when URL error occurs."""
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            ticket = services.fetch_zendesk_ticket_full('12345')

        assert ticket is None

    def test_returns_none_on_generic_exception(self, mock_system_settings):
        """Returns None when unexpected exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Unexpected error')):
            ticket = services.fetch_zendesk_ticket_full('12345')

        assert ticket is None


# =============================================================================
# Test search_zendesk_ticket_for_dispute
# =============================================================================


@pytest.mark.django_db
class TestSearchZendeskTicketForDispute:
    """Tests for search_zendesk_ticket_for_dispute function."""

    def test_finds_by_email_search(self, mock_system_settings):
        """Finds ticket by buyer email search."""
        mock_search_result = [
            {
                'id': '12345',
                'subject': 'Dispute Ticket',
                'created_at': '2026-03-15T10:00:00Z',
            }
        ]

        with patch('apps.integrations.services.search_zendesk_tickets', return_value=mock_search_result):
            ticket = services.search_zendesk_ticket_for_dispute(
                buyer_email='buyer@example.com',
                buyer_name='John Doe',
            )

        assert ticket is not None
        assert ticket['id'] == '12345'

    def test_finds_by_transaction_id(self, mock_system_settings):
        """Finds ticket by transaction ID when email search fails."""
        mock_search_result = [
            {'id': '12345', 'created_at': '2026-03-15T10:00:00Z'}
        ]

        with patch('apps.integrations.services.search_zendesk_tickets', side_effect=[
            [],  # Email search fails
            mock_search_result,  # Transaction ID search succeeds
        ]):
            ticket = services.search_zendesk_ticket_for_dispute(
                buyer_email='buyer@example.com',
                transaction_id='TXN-12345',
            )

        assert ticket is not None
        assert ticket['id'] == '12345'

    def test_finds_by_name_and_date(self, mock_system_settings):
        """Finds ticket by name and date when other searches fail."""
        mock_search_result = [
            {'id': '12345', 'created_at': '2026-03-15T10:00:00Z'}
        ]

        with patch('apps.integrations.services.search_zendesk_tickets', side_effect=[
            [],  # Email search fails
            [],  # Transaction ID search fails
            mock_search_result,  # Name+date search succeeds
        ]):
            ticket = services.search_zendesk_ticket_for_dispute(
                buyer_email='buyer@example.com',
                buyer_name='John Doe',
                transaction_date='2026-03-15',
            )

        assert ticket is not None
        assert ticket['id'] == '12345'

    def test_finds_by_name_only(self, mock_system_settings):
        """Finds ticket by name only when other searches fail."""
        mock_search_result = [
            {'id': '12345', 'created_at': '2026-03-15T10:00:00Z'}
        ]

        # When buyer_email is provided but returns empty, and no transaction_id,
        # the function tries: 1) email search, 2) name+date (skipped - no date), 3) name only
        # Note: buyer_email is provided so email search runs first
        with patch('apps.integrations.services.search_zendesk_tickets', side_effect=[
            [],  # Email search fails (buyer_email provided)
            mock_search_result,  # Name only search succeeds (strategy 4)
        ]):
            ticket = services.search_zendesk_ticket_for_dispute(
                buyer_email='buyer@example.com',
                buyer_name='John Doe',
            )

        assert ticket is not None
        assert ticket['id'] == '12345'

    def test_returns_none_when_no_match(self, mock_system_settings):
        """Returns None when no searches match."""
        with patch('apps.integrations.services.search_zendesk_tickets', return_value=[]):
            ticket = services.search_zendesk_ticket_for_dispute(
                buyer_email='buyer@example.com',
                buyer_name='John Doe',
            )

        assert ticket is None

    def test_picks_most_recent_ticket(self, mock_system_settings):
        """Picks most recent ticket when multiple results."""
        mock_search_result = [
            {'id': '12345', 'created_at': '2026-03-10T10:00:00Z'},
            {'id': '12346', 'created_at': '2026-03-15T10:00:00Z'},  # Most recent
            {'id': '12347', 'created_at': '2026-03-12T10:00:00Z'},
        ]

        with patch('apps.integrations.services.search_zendesk_tickets', return_value=mock_search_result):
            ticket = services.search_zendesk_ticket_for_dispute(buyer_email='buyer@example.com')

        assert ticket['id'] == '12346'  # Most recent

    def test_handles_empty_email(self, mock_system_settings):
        """Handles empty buyer email gracefully."""
        with patch('apps.integrations.services.search_zendesk_tickets', return_value=[]):
            ticket = services.search_zendesk_ticket_for_dispute(
                buyer_email='',
                buyer_name='John Doe',
            )

        assert ticket is None


# =============================================================================
# Test match_alias_to_zendesk_ticket
# =============================================================================


@pytest.mark.django_db
class TestMatchAliasToZendeskTicket:
    """Tests for match_alias_to_zendesk_ticket function."""

    def test_matches_alias_successfully(self, mock_system_settings):
        """Alias matched to ticket via custom field."""
        mock_search_result = [
            {
                'id': '12345',
                'subject': 'Ticket for client-123',
            }
        ]

        with patch('apps.integrations.services.search_zendesk_tickets', return_value=mock_search_result):
            ticket = services.match_alias_to_zendesk_ticket('client-123@mydomain.com')

        assert ticket is not None
        assert ticket['id'] == '12345'

    def test_uses_correct_custom_field_id(self, mock_system_settings):
        """Uses hard-coded custom field ID 13606076120860."""
        with patch('apps.integrations.services.search_zendesk_tickets', return_value=[]) as mock_search:
            services.match_alias_to_zendesk_ticket('client-123@mydomain.com')

        # Verify search query uses correct custom field ID
        mock_search.assert_called_once()
        query = mock_search.call_args[0][0]
        # SINGULAR custom_field_ — the Zendesk Search API form (plural matches nothing).
        assert 'custom_field_13606076120860:"client-123@mydomain.com"' == query

    def test_returns_none_when_no_match(self, mock_system_settings):
        """Returns None when no ticket matches alias."""
        with patch('apps.integrations.services.search_zendesk_tickets', return_value=[]):
            ticket = services.match_alias_to_zendesk_ticket('nonexistent@mydomain.com')

        assert ticket is None

    def test_returns_none_on_exception(self, mock_system_settings):
        """Returns None when exception occurs."""
        with patch('apps.integrations.services.search_zendesk_tickets', side_effect=Exception('Search error')):
            ticket = services.match_alias_to_zendesk_ticket('client-123@mydomain.com')

        assert ticket is None


# =============================================================================
# Test tag_zendesk_ticket_as_refunded
# =============================================================================


@pytest.mark.django_db
class TestTagZendeskTicketAsRefunded:
    """Tests for tag_zendesk_ticket_as_refunded function."""

    def test_tags_ticket_successfully(self, mock_system_settings, mock_urlopen_response):
        """'refunded' is ADDED via PUT on /tags.json — never the ticket-update
        endpoint, whose tags array REPLACES the whole set (wiped all other
        tags on every refund until 2026-06-12)."""
        mock_urlopen_response.read.return_value = json.dumps({'tags': ['refunded']}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            result = services.tag_zendesk_ticket_as_refunded('12345')

        assert result is True

        # Verify the additive endpoint, verb and payload
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.full_url.endswith('/tickets/12345/tags.json')
        assert call_args.get_method() == 'PUT'
        payload = json.loads(call_args.data.decode('utf-8'))
        assert payload == {'tags': ['refunded']}

    def test_returns_false_on_exception(self, mock_system_settings):
        """Returns False when exception occurs."""
        with patch('urllib.request.urlopen', side_effect=Exception('Tag error')):
            result = services.tag_zendesk_ticket_as_refunded('12345')

        assert result is False


# =============================================================================
# Test add_refund_comment_to_zendesk
# =============================================================================


@pytest.mark.django_db
class TestAddRefundCommentToZendesk:
    """Tests for add_refund_comment_to_zendesk function."""

    def test_adds_refund_comment_successfully(self, mock_system_settings):
        """Refund comment added with correct format."""
        mock_comment_result = {'ticket': {'id': '12345'}}

        with patch('apps.integrations.services.post_zendesk_comment', return_value=mock_comment_result) as mock_post:
            result = services.add_refund_comment_to_zendesk(
                zd_ticket_id='12345',
                refund_amount='$100.00 USD',
                refund_id='REFUND-12345',
                reason='Customer request',
                is_internal=True,
            )

        assert result is not None
        assert result['ticket']['id'] == '12345'

        # Verify comment format
        mock_post.assert_called_once()
        comment_body = mock_post.call_args[0][1]
        assert '💰 **Refund Processed**' in comment_body
        assert '**Amount**: $100.00 USD' in comment_body
        assert '**Refund ID**: REFUND-12345' in comment_body
        assert '**Reason**: Customer request' in comment_body
        assert 'processed via PayPal' in comment_body

    def test_returns_none_on_exception(self, mock_system_settings):
        """Returns None when exception occurs."""
        with patch('apps.integrations.services.post_zendesk_comment', side_effect=Exception('Comment error')):
            result = services.add_refund_comment_to_zendesk(
                zd_ticket_id='12345',
                refund_amount='$100.00',
                refund_id='REFUND-12345',
                reason='Test',
            )

        assert result is None


# =============================================================================
# Test analyze_zendesk_ticket_for_claim
# =============================================================================


@pytest.mark.django_db
class TestAnalyzeZendeskTicketForClaim:
    """Tests for analyze_zendesk_ticket_for_claim function.

    After the structured-fields-first migration:
    - email, phone, flight are read from Zendesk custom fields (not LLM).
    - LLM (call_qwen_ai_for_ticket_extraction) only extracts object_description
      and additional_context from the free-text description.
    - The alias custom field (13606076120860) is passed as known_aliases so the
      tokenizer tags it as ALIAS rather than EMAIL.
    """

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_extracts_object_description_from_llm(self, mock_extract, mock_system_settings):
        """LLM result populates object_description; structured fields stay empty when absent."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'I lost my black suitcase',
            'comments': [],
            'custom_fields': [],
        }
        mock_extract.return_value = {
            'object_description': 'Black suitcase',
            'additional_context': '',
        }

        result = services.analyze_zendesk_ticket_for_claim(mock_ticket_data)

        assert result['object_description'] == 'Black suitcase'
        # Structured fields not present in custom_fields — all empty
        assert result['client_email'] == ''
        assert result['flight_details'] == ''
        assert result['phone'] == ''
        assert result['alternate_email'] == ''

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_structured_fields_win_over_llm(self, mock_extract, mock_system_settings):
        """Confirmed structured custom fields are used directly, not from LLM."""
        from apps.integrations.services import (
            ZENDESK_FIELD_CLIENT_EMAIL, ZENDESK_FIELD_PHONE, ZENDESK_FIELD_FLIGHT,
        )
        # Patch field IDs to known values for this test
        with patch('apps.integrations.services.ZENDESK_FIELD_CLIENT_EMAIL', 9001), \
             patch('apps.integrations.services.ZENDESK_FIELD_PHONE', 9002), \
             patch('apps.integrations.services.ZENDESK_FIELD_FLIGHT', 9003):
            mock_ticket_data = {
                'id': '12345',
                'subject': 'Lost Item',
                'description': 'Test',
                'comments': [],
                'custom_fields': [
                    {'id': 9001, 'value': 'structured@example.com'},
                    {'id': 9002, 'value': '+1-555-000-1234'},
                    {'id': 9003, 'value': 'AA123 JFK-LAX'},
                ],
            }
            mock_extract.return_value = {'object_description': 'Red bag', 'additional_context': ''}

            result = services.analyze_zendesk_ticket_for_claim(mock_ticket_data)

        assert result['client_email'] == 'structured@example.com'
        assert result['phone'] == '+1-555-000-1234'
        # flight_details is now a labeled composition; only the Flight field is set here
        assert result['flight_details'] == 'Flight: AA123 JFK-LAX'
        assert result['object_description'] == 'Red bag'

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_returns_empty_fields_on_llm_exception(self, mock_extract, mock_system_settings):
        """Returns empty fields when the LLM call raises."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Test',
            'comments': [],
            'custom_fields': [],
        }
        mock_extract.side_effect = Exception('LLM error')

        result = services.analyze_zendesk_ticket_for_claim(mock_ticket_data)

        assert result['client_email'] == ''
        assert result['flight_details'] == ''
        assert result['object_description'] == ''
        assert result['phone'] == ''
        assert result['alternate_email'] == ''

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_alias_passed_as_known_aliases(self, mock_extract, mock_system_settings):
        """Alias from custom field 13606076120860 is passed as known_aliases to LLM call."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Test',
            'comments': [],
            'custom_fields': [
                {'id': 13606076120860, 'value': 'alias-99@example.com'},
            ],
        }
        mock_extract.return_value = {'object_description': 'Laptop', 'additional_context': ''}

        services.analyze_zendesk_ticket_for_claim(mock_ticket_data)

        mock_extract.assert_called_once()
        call_kwargs = mock_extract.call_args.kwargs
        assert call_kwargs.get('known_aliases') == ['alias-99@example.com']

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_limits_comments_to_five(self, mock_extract, mock_system_settings):
        """Limits comments to first 5 for LLM context."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Test',
            'comments': [{'body': f'Comment {i}'} for i in range(10)],
            'custom_fields': [],
        }
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}

        services.analyze_zendesk_ticket_for_claim(mock_ticket_data)

        mock_extract.assert_called_once()
        context = mock_extract.call_args.kwargs['ticket_context']
        import re
        comment_matches = re.findall(r'Comment \d+', context)
        assert len(comment_matches) == 5  # Exactly 5 comments included

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_handles_empty_comments(self, mock_extract, mock_system_settings):
        """Handles ticket with no comments without error."""
        mock_ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Test description',
            'comments': [],
            'custom_fields': [],
        }
        mock_extract.return_value = {'object_description': 'Bag', 'additional_context': ''}

        result = services.analyze_zendesk_ticket_for_claim(mock_ticket_data)

        assert result['object_description'] == 'Bag'
        mock_extract.assert_called_once()


# =============================================================================
# Test parse_alf_claim_id_from_subject
# =============================================================================


class TestParseAlfClaimIdFromSubject:
    """Tests for parse_alf_claim_id_from_subject function."""

    def test_parses_standard_format(self):
        """Parses ALF followed by 7 digits."""
        result = services.parse_alf_claim_id_from_subject('Lost Item - ALF1234567')
        assert result == 'ALF1234567'

    def test_parses_with_hyphen(self):
        """Parses ALF-1234567 format."""
        result = services.parse_alf_claim_id_from_subject('ALF-1234567 - Lost Item')
        assert result == 'ALF1234567'

    def test_parses_with_underscore(self):
        """Parses ALF_1234567 format."""
        result = services.parse_alf_claim_id_from_subject('ALF_1234567 Lost Item')
        assert result == 'ALF1234567'

    def test_parses_case_insensitive(self):
        """Parses alf in lowercase."""
        result = services.parse_alf_claim_id_from_subject('alf1234567 - Lost Item')
        assert result == 'ALF1234567'

    def test_parses_from_middle_of_subject(self):
        """Parses ALF ID from middle of subject."""
        result = services.parse_alf_claim_id_from_subject('Re: Lost Item ALF1234567 Please Help')
        assert result == 'ALF1234567'

    def test_returns_none_when_no_alf_id(self):
        """Returns None when no ALF ID in subject."""
        result = services.parse_alf_claim_id_from_subject('Lost Item Report')
        assert result is None

    def test_returns_none_when_empty_subject(self):
        """Returns None when subject is empty."""
        result = services.parse_alf_claim_id_from_subject('')
        assert result is None

    def test_returns_none_when_none_subject(self):
        """Returns None when subject is None."""
        result = services.parse_alf_claim_id_from_subject(None)
        assert result is None

    def test_returns_none_when_fewer_digits(self):
        """Returns None when ALF followed by fewer than 7 digits."""
        result = services.parse_alf_claim_id_from_subject('ALF123456')  # Only 6 digits
        assert result is None

    def test_parses_exactly_seven_digits(self):
        """Parses when exactly 7 digits follow ALF (even if more digits follow)."""
        # The regex matches ALF followed by exactly 7 digits
        # If there are 8 digits, it will match the first 7
        result = services.parse_alf_claim_id_from_subject('ALF12345678')  # 8 digits
        assert result == 'ALF1234567'  # Matches first 7

    def test_parses_first_alf_id_in_subject(self):
        """Parses first ALF ID when multiple present."""
        result = services.parse_alf_claim_id_from_subject('ALF1111111 and ALF2222222')
        assert result == 'ALF1111111'


# =============================================================================
# Test structured-fields-first behaviour in analyze_zendesk_ticket_for_claim
# =============================================================================


@pytest.mark.django_db
def test_analyze_ticket_reads_structured_alias_from_custom_field(db):
    """The extractor reads the alias from custom field 13606076120860 and passes
    it as known_aliases to the LLM call, so it gets ALIAS-tagged (not EMAIL-tagged)."""
    from apps.integrations.services import analyze_zendesk_ticket_for_claim
    from apps.config.models import SystemSettings

    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={
        'ai_api_key': 'test',
        'ai_api_base': 'https://api.example.com/v1',
        'ai_api_model': 'test-model',
        'pii_tokenization_salt': 'test_salt_long_enough_for_real_use',
    })
    ss.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
    ss.ai_api_key = 'test'
    ss.ai_api_base = 'https://api.example.com/v1'
    ss.ai_api_model = 'test-model'
    ss.save()

    ticket_payload = {
        'id': '88001',
        'subject': 'Lost item - ALF8800001',
        'description': 'I lost my black backpack at JFK terminal 4',
        'custom_fields': [
            {'id': 13606076120860, 'value': 'client-77@aliasdomain.example'},
        ],
        'comments': [],
    }

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"object_description": "black backpack", "additional_context": null}'
            )))],
        )
        result = analyze_zendesk_ticket_for_claim(ticket_payload)

    # Confirm the LLM was called and the alias was tokenized (not sent raw)
    sent_messages = MockOpenAI.return_value.chat.completions.create.call_args.kwargs["messages"]
    user_content = sent_messages[1]["content"]
    assert "client-77@aliasdomain.example" not in user_content, (
        "alias should be tokenized before reaching the LLM"
    )

    # Confirm structured extraction produced the right object_description
    assert result.get('object_description') == "black backpack"


# Real confirmed Zendesk custom field IDs (see apps/integrations/services.py)
_FIELD_CUSTOMER_NAME = 13737514170140
_FIELD_LOST_OBJECT = 11761123532444
_FIELD_OBJECT_DETAILS = 13737436477852
_FIELD_FLIGHT_NUMBER = 13737630819996
_FIELD_AIRLINE = 11761080032028
_FIELD_AIRPORT = 11761104069276
_FIELD_SEAT = 13737646294940
_FIELD_DATETIME = 13737598795292
_FIELD_CLAIM_NUMBER = 11688794648732
# Additional fields wired 2026-06-10
_FIELD_BILLING_ADDRESS = 13737449416988
_FIELD_SHIPPING_ADDRESS = 11949784750236
_FIELD_INCIDENT_DETAILS = 13737603591964
_FIELD_LOST_LOCATION = 16314445118492
_FIELD_DEADLINE_DATE = 14394267216668
_FIELD_DEADLINE_TIME = 14394267218972
_FIELD_DEADLINE_TZ = 14394267222684
_FIELD_PRICE_PAID = 19736734259996
_FIELD_PAYMENT_METHOD = 14495509913244
_FIELD_PAYMENT_STATUS = 11761180893980
_FIELD_WOOCOMMERCE_ID = 13484164181916
_FIELD_TRACKING_INFO = 11949753094556


@pytest.mark.django_db
class TestStructuredFieldComposition:
    """Tests for the enriched structured-field reads added 2026-06-10:
    composed flight_details, composed object_description, customer name, and
    the Claim # field."""

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_composes_flight_details_from_multiple_fields(self, mock_extract, mock_system_settings):
        """flight_details combines Flight Number + Airline + Airport + Seat + Date/Time
        into one labeled string."""
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [
                {'id': _FIELD_FLIGHT_NUMBER, 'value': 'AA123'},
                {'id': _FIELD_AIRLINE, 'value': 'American Airlines'},
                {'id': _FIELD_AIRPORT, 'value': 'JFK'},
                {'id': _FIELD_SEAT, 'value': '14C'},
                {'id': _FIELD_DATETIME, 'value': '2026-01-15 10:30'},
            ],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        fd = result['flight_details']
        assert 'AA123' in fd
        assert 'American Airlines' in fd
        assert 'JFK' in fd
        assert '14C' in fd
        assert '2026-01-15 10:30' in fd

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_flight_details_omits_absent_fields(self, mock_extract, mock_system_settings):
        """Only present flight fields appear; no empty labels for missing ones."""
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [
                {'id': _FIELD_FLIGHT_NUMBER, 'value': 'BA456'},
                {'id': _FIELD_AIRPORT, 'value': 'LHR'},
            ],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        fd = result['flight_details']
        assert 'BA456' in fd
        assert 'LHR' in fd
        assert 'Airline' not in fd  # absent field not labeled
        assert 'Seat' not in fd

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_composes_object_description_from_structured_fields(self, mock_extract, mock_system_settings):
        """object_description combines Lost Object + Object Details, and structured
        data wins over the LLM."""
        mock_extract.return_value = {'object_description': 'LLM GUESS', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [
                {'id': _FIELD_LOST_OBJECT, 'value': 'Black leather wallet'},
                {'id': _FIELD_OBJECT_DETAILS, 'value': 'Contains driver license and 2 cards'},
            ],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        od = result['object_description']
        assert 'Black leather wallet' in od
        assert 'Contains driver license and 2 cards' in od
        assert 'LLM GUESS' not in od  # structured fields win over the LLM

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_object_description_falls_back_to_llm_when_no_structured_fields(self, mock_extract, mock_system_settings):
        """When neither Lost Object nor Object Details is present, the LLM value is used."""
        mock_extract.return_value = {'object_description': 'A blue umbrella', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'lost my umbrella', 'comments': [],
            'custom_fields': [],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        assert result['object_description'] == 'A blue umbrella'

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_extracts_customer_name(self, mock_extract, mock_system_settings):
        """client_name is read from the Customer Name field."""
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [{'id': _FIELD_CUSTOMER_NAME, 'value': 'Jane Doe'}],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        assert result['client_name'] == 'Jane Doe'

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_returns_claim_number_from_field(self, mock_extract, mock_system_settings):
        """The Claim # field value is surfaced in the result dict for the view to use."""
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [{'id': _FIELD_CLAIM_NUMBER, 'value': 'ALF7654321'}],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        assert result['claim_number'] == 'ALF7654321'

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_reads_extended_structured_fields(self, mock_extract, mock_system_settings):
        """The extractor surfaces the extended fields wired 2026-06-10:
        addresses, incident details, lost location, deadline, price, payment,
        WooCommerce id, and tracking — all as raw string values."""
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [
                {'id': _FIELD_BILLING_ADDRESS, 'value': '1 Bill St, Berlin'},
                {'id': _FIELD_SHIPPING_ADDRESS, 'value': '2 Ship Ave, Munich'},
                {'id': _FIELD_INCIDENT_DETAILS, 'value': 'Left it at security'},
                {'id': _FIELD_LOST_LOCATION, 'value': 'Terminal 2, Gate B12'},
                {'id': _FIELD_DEADLINE_DATE, 'value': '2026-07-01'},
                {'id': _FIELD_DEADLINE_TIME, 'value': '17:00'},
                {'id': _FIELD_DEADLINE_TZ, 'value': 'Europe/Berlin'},
                {'id': _FIELD_PRICE_PAID, 'value': '149.99'},
                {'id': _FIELD_PAYMENT_METHOD, 'value': 'PayPal'},
                {'id': _FIELD_PAYMENT_STATUS, 'value': 'Paid'},
                {'id': _FIELD_WOOCOMMERCE_ID, 'value': 'WC-55012'},
                {'id': _FIELD_TRACKING_INFO, 'value': 'DHL 1234567890'},
            ],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        assert result['billing_address'] == '1 Bill St, Berlin'
        assert result['shipping_address'] == '2 Ship Ave, Munich'
        assert result['incident_details'] == 'Left it at security'
        assert result['lost_location'] == 'Terminal 2, Gate B12'
        assert result['deadline_date'] == '2026-07-01'
        assert result['deadline_time'] == '17:00'
        assert result['deadline_timezone'] == 'Europe/Berlin'
        assert result['price_paid'] == '149.99'
        assert result['payment_method'] == 'PayPal'
        assert result['payment_status'] == 'Paid'
        assert result['woocommerce_id'] == 'WC-55012'
        assert result['tracking_info'] == 'DHL 1234567890'

    @patch('apps.communications.services.call_qwen_ai_for_ticket_extraction')
    def test_extended_fields_default_empty_when_absent(self, mock_extract, mock_system_settings):
        """Extended fields default to '' when the ticket has no custom fields."""
        mock_extract.return_value = {'object_description': '', 'additional_context': ''}
        ticket = {
            'id': '12345', 'subject': 'Lost', 'description': 'x', 'comments': [],
            'custom_fields': [],
        }
        result = services.analyze_zendesk_ticket_for_claim(ticket)
        for key in (
            'billing_address', 'shipping_address', 'incident_details', 'lost_location',
            'deadline_date', 'deadline_time', 'deadline_timezone', 'price_paid',
            'payment_method', 'payment_status', 'woocommerce_id', 'tracking_info',
        ):
            assert result[key] == '', f"{key} should default to empty string"


# =============================================================================
# Test build_claim_facts
# =============================================================================


@pytest.mark.django_db
def test_build_claim_facts_returns_panel_facts():
    from apps.integrations.services import build_claim_facts
    from apps.claims.models import Claim
    from apps.communications.models import EmailLog
    from datetime import date

    claim = Claim.objects.create(
        alf_claim_id='ALF7000001', zd_ticket_id='70001',
        client_email='c@example.com', status='Claim submitted',
        deadline_date=date(2026, 7, 1),
    )
    EmailLog.objects.create(claim=claim, subject='a', body='', category='UNKNOWN',
                            action_required=True, auto_resolved=False)
    EmailLog.objects.create(claim=claim, subject='b', body='', category='OBJECT_FOUND',
                            action_required=False, auto_resolved=True)

    facts = build_claim_facts(claim)
    assert facts['status'] == 'Claim submitted'
    assert facts['deadline'] == '2026-07-01'
    assert facts['emails_total'] == 2
    assert facts['emails_unresolved'] == 1
    assert facts['disputes_total'] == 0


def test_build_ticket_thread_formats_dated_comments():
    from apps.integrations.services import build_ticket_thread
    data = {
        'subject': 'Lost wallet',
        'description': 'Left at TSA',
        'ticket_created_at': '2026-05-16T20:59:00Z',
        'comments': [
            {'author': 'Mark Johnson', 'created_at': '2026-05-16T20:59:00Z',
             'public': False, 'text': 'New abandoned cart created'},
            {'author': 'Gaby Smith', 'created_at': '2026-05-17T20:36:00Z',
             'public': True, 'text': 'We found your wallet'},
            'plain string comment',
        ],
    }
    untrusted = build_ticket_thread(data)
    assert untrusted['ticket_subject'] == 'Lost wallet'
    assert untrusted['ticket_created_at'].startswith('2026-05-16')
    lines = untrusted['zendesk_comment']
    assert lines[0] == '[2026-05-16T20:59:00Z | Mark Johnson | internal note] New abandoned cart created'
    assert lines[1] == '[2026-05-17T20:36:00Z | Gaby Smith | public] We found your wallet'
    assert lines[2] == 'plain string comment'


def test_build_ticket_thread_empty_input():
    from apps.integrations.services import build_ticket_thread
    untrusted = build_ticket_thread({})
    assert untrusted['ticket_subject'] == ''
    assert 'zendesk_comment' not in untrusted
    assert 'ticket_created_at' not in untrusted


@pytest.mark.django_db
def test_build_claim_facts_includes_next_update_due():
    """Client-update cadence: days 2/5/11/20 after claim creation; the facts
    carry the next milestone that is not yet past."""
    from apps.integrations.services import build_claim_facts
    from apps.claims.models import Claim

    claim = Claim.objects.create(
        alf_claim_id='ALF7000002', zd_ticket_id='70002',
        client_email='c2@example.com', status='Claim submitted',
    )
    facts = build_claim_facts(claim)
    # claim created just now -> day-2 update is the next one due
    assert facts['next_update_due']['day'] == 2
    assert 'date' in facts['next_update_due']


# =============================================================================
# Test resolve_custom_status
# =============================================================================


class ResolveCustomStatusTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch('apps.integrations.services._fetch_custom_statuses')
    def test_resolves_known_id_and_caches(self, mock_fetch):
        from apps.integrations.services import resolve_custom_status
        mock_fetch.return_value = {
            '111': {'name': 'Claim submitted', 'category': 'open'},
        }
        result = resolve_custom_status('111')
        self.assertEqual(result, {'name': 'Claim submitted', 'category': 'open'})
        resolve_custom_status('111')  # second call served from cache
        self.assertEqual(mock_fetch.call_count, 1)

    @patch('apps.integrations.services._fetch_custom_statuses')
    def test_unknown_id_refreshes_then_falls_back(self, mock_fetch):
        from apps.integrations.services import resolve_custom_status
        mock_fetch.return_value = {'111': {'name': 'Open', 'category': 'open'}}
        result = resolve_custom_status('999')
        self.assertEqual(result, {'name': '999', 'category': ''})
        self.assertEqual(mock_fetch.call_count, 1)

    @patch('apps.integrations.services._fetch_custom_statuses', side_effect=ValueError('no creds'))
    def test_fetch_failure_falls_back_to_id(self, mock_fetch):
        from apps.integrations.services import resolve_custom_status
        result = resolve_custom_status('123')
        self.assertEqual(result, {'name': '123', 'category': ''})


# =============================================================================
# Test build_claim_facts — family, cadence, deadline preference
# =============================================================================


class BuildClaimFactsFamilyTests(TestCase):
    def test_status_family_included_and_cadence_suppressed_when_solved(self):
        from apps.integrations.services import build_claim_facts
        from apps.claims.models import Claim
        claim = Claim.objects.create(
            client_email='facts@example.com',
            status='Closed - Refunded', status_category='solved')
        facts = build_claim_facts(claim)
        self.assertEqual(facts['status'], 'Closed - Refunded')
        self.assertEqual(facts['status_family'], 'solved')
        self.assertIsNone(facts['next_update_due'])

    def test_active_claim_keeps_cadence(self):
        from apps.integrations.services import build_claim_facts
        from apps.claims.models import Claim
        claim = Claim.objects.create(
            client_email='facts2@example.com',
            status='Claim submitted', status_category='open')
        facts = build_claim_facts(claim)
        self.assertIsNotNone(facts['next_update_due'])

    def test_deadline_displays_entered_date(self):
        """Human-entered deadline_date wins for display; deadline_at is urgency-math only."""
        from datetime import date, datetime
        from zoneinfo import ZoneInfo
        from apps.integrations.services import build_claim_facts
        from apps.claims.models import Claim
        # Both fields set: deadline_date must win
        claim = Claim.objects.create(
            client_email='facts3@example.com',
            status='Claim submitted', status_category='open',
            deadline_date=date(2026, 7, 2),
            deadline_at=datetime(2026, 7, 1, 23, 59, 59, tzinfo=ZoneInfo('UTC')))
        facts = build_claim_facts(claim)
        self.assertEqual(facts['deadline'], '2026-07-02')

        # Only deadline_at set: its localtime date is used as fallback
        claim2 = Claim.objects.create(
            client_email='facts3b@example.com',
            status='Claim submitted', status_category='open',
            deadline_date=None,
            deadline_at=datetime(2026, 7, 1, 23, 59, 59, tzinfo=ZoneInfo('UTC')))
        facts2 = build_claim_facts(claim2)
        self.assertEqual(facts2['deadline'], '2026-07-01')


@pytest.mark.django_db
class TestCreateClaimFromZendeskTicket:
    """M8: the single creation service shared by the webhook and the on-demand
    import. Returns a result dict; covers each outcome directly (no HTTP view)."""

    def test_fetch_failed_outcome(self, mock_system_settings):
        from apps.integrations.services import create_claim_from_zendesk_ticket
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=None):
            result = create_claim_from_zendesk_ticket('99001', status_id='X')
        assert result['outcome'] == 'fetch_failed'
        assert result['claim'] is None

    def test_ignored_when_no_alf_number(self, mock_system_settings):
        from apps.integrations.services import create_claim_from_zendesk_ticket
        from apps.claims.models import Claim
        ticket = {'id': '99002', 'subject': 'Incoming phone call', 'custom_fields': []}
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=ticket):
            result = create_claim_from_zendesk_ticket('99002', status_id='X')
        assert result['outcome'] == 'ignored'
        assert not Claim.objects.filter(zd_ticket_id='99002').exists()

    def test_created_uses_webhook_requester_email_fallback(self, mock_system_settings):
        from apps.integrations.services import create_claim_from_zendesk_ticket
        ticket = {'id': '99003', 'subject': 'Lost bag - ALF9009001', 'custom_fields': []}
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value={'client_email': '', 'flight_details': ''}), \
             patch('apps.integrations.services.resolve_custom_status',
                   return_value={'name': 'Investigation initiated', 'category': 'open'}), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):
            result = create_claim_from_zendesk_ticket(
                '99003', status_id='X', webhook_requester_email='fallback@e.com')
        assert result['outcome'] == 'created'
        claim = result['claim']
        assert claim.alf_claim_id == 'ALF9009001'
        assert claim.client_email == 'fallback@e.com'   # webhook-requester fallback used
        assert claim.llm_extraction_failed is True       # extractor returned no email/flight
