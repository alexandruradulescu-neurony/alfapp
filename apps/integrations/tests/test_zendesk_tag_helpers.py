"""
Tests for get_zendesk_ticket_tags and remove_zendesk_ticket_tags helpers.

Mirrors the HTTP-mocking approach used in TestTagZendeskTicketAsRefunded:
  - patch 'urllib.request.urlopen'
  - inspect call_args[0][0] for .full_url / .get_method() / .data
"""

import json
import pytest
from unittest.mock import patch, MagicMock, Mock

from apps.config.models import SystemSettings
from apps.integrations import services


# =============================================================================
# Fixtures (same pattern as test_zendesk_services.py)
# =============================================================================


@pytest.fixture
def mock_system_settings():
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
    if not created:
        settings_obj.zd_subdomain = 'testcompany'
        settings_obj.zd_token = 'test_zendesk_token_12345'
        settings_obj.zd_email = 'test@testcompany.com'
        settings_obj.save()
    return settings_obj


@pytest.fixture
def mock_urlopen_response():
    mock_response = MagicMock()
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)
    return mock_response


# =============================================================================
# get_zendesk_ticket_tags
# =============================================================================


@pytest.mark.django_db
class TestGetZendeskTicketTags:
    """Tests for get_zendesk_ticket_tags."""

    def test_returns_tags_list_on_success(self, mock_system_settings, mock_urlopen_response):
        """Returns the tags list when the API responds with {"tags": [...]}."""
        mock_urlopen_response.read.return_value = json.dumps(
            {'tags': ['lora', 'client_update_1', 'some_other_tag']}
        ).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            result = services.get_zendesk_ticket_tags('99999')

        assert result == ['lora', 'client_update_1', 'some_other_tag']

    def test_issues_get_to_correct_endpoint(self, mock_system_settings, mock_urlopen_response):
        """GET request is sent to /tickets/<id>/tags.json."""
        mock_urlopen_response.read.return_value = json.dumps({'tags': ['x']}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            services.get_zendesk_ticket_tags('12345')

        call_args = mock_urlopen.call_args[0][0]
        assert call_args.full_url.endswith('/tickets/12345/tags.json')
        assert call_args.get_method() == 'GET'

    def test_returns_empty_list_on_http_failure(self, mock_system_settings):
        """Returns [] when urlopen raises an exception."""
        with patch('urllib.request.urlopen', side_effect=Exception('network error')):
            result = services.get_zendesk_ticket_tags('12345')

        assert result == []

    def test_returns_empty_list_when_tags_key_missing(self, mock_system_settings, mock_urlopen_response):
        """Returns [] when the response JSON has no 'tags' key."""
        mock_urlopen_response.read.return_value = json.dumps({}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            result = services.get_zendesk_ticket_tags('12345')

        assert result == []

    def test_returns_empty_list_when_tags_is_none(self, mock_system_settings, mock_urlopen_response):
        """Returns [] when 'tags' is explicitly null in the response."""
        mock_urlopen_response.read.return_value = json.dumps({'tags': None}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response):
            result = services.get_zendesk_ticket_tags('12345')

        assert result == []


# =============================================================================
# remove_zendesk_ticket_tags
# =============================================================================


@pytest.mark.django_db
class TestRemoveZendeskTicketTags:
    """Tests for remove_zendesk_ticket_tags."""

    def test_issues_delete_with_correct_method_url_and_body(self, mock_system_settings, mock_urlopen_response):
        """DELETE request is sent to /tickets/<id>/tags.json with the right JSON body."""
        mock_urlopen_response.read.return_value = json.dumps({'tags': []}).encode('utf-8')

        with patch('urllib.request.urlopen', return_value=mock_urlopen_response) as mock_urlopen:
            result = services.remove_zendesk_ticket_tags('42', ['with_client_update', 'third_party_update'])

        assert result is True

        call_args = mock_urlopen.call_args[0][0]
        assert call_args.full_url.endswith('/tickets/42/tags.json')
        assert call_args.get_method() == 'DELETE'
        payload = json.loads(call_args.data.decode('utf-8'))
        assert payload == {'tags': ['with_client_update', 'third_party_update']}

    def test_returns_true_for_empty_tag_list_without_network(self, mock_system_settings):
        """Returns True immediately for an empty list — no network call made."""
        with patch('urllib.request.urlopen') as mock_urlopen:
            result = services.remove_zendesk_ticket_tags('42', [])

        assert result is True
        mock_urlopen.assert_not_called()

    def test_returns_false_on_exception_never_raises(self, mock_system_settings):
        """Returns False when the network call fails; never raises."""
        with patch('urllib.request.urlopen', side_effect=Exception('delete failed')):
            result = services.remove_zendesk_ticket_tags('42', ['some_tag'])

        assert result is False
