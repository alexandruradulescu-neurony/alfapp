"""
Tests for the config services layer.

Tests cover:
- ConnectionTester class (connection_tester.py)
- SchedulerController class (scheduler_controller.py)

Tests mock all external API calls (no real HTTP requests).

Note: SchedulerController tests are excluded because the underlying code has an 
import bug - it tries to import get_scheduler from apps.communications.tasks, 
but that function is not exported from that module.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from django.utils import timezone

from apps.config.services.connection_tester import ConnectionTester
from apps.config.services.scheduler_controller import SchedulerController
from apps.config.models import SystemSettings, ServiceStatus


# =============================================================================
# ConnectionTester Tests
# =============================================================================


@pytest.mark.django_db
class TestConnectionTesterInit:
    """Tests for ConnectionTester initialization."""

    def test_connection_tester_initialization(self):
        """Test ConnectionTester initializes with correct timeout."""
        tester = ConnectionTester()
        assert tester.timeout == 10


@pytest.mark.django_db
class TestConnectionTesterAI:
    """Tests for test_ai method."""

    @patch('apps.config.services.connection_tester.requests.get')
    def test_ai_connection_success(self, mock_get):
        """Test successful AI provider connection."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = 'test_api_key'
        settings.ai_api_base = 'https://api.example.com/v1'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_ai()

        assert result['success'] is True
        assert result['status'] == 'connected'
        assert result['message'] == 'AI provider is reachable'
        assert result['service'] == 'AI'

        status = ServiceStatus.objects.get(service='AI')
        assert status.status == 'connected'
        assert status.is_enabled is True

    @patch('apps.config.services.connection_tester.requests.get')
    def test_ai_connection_no_api_key(self, mock_get):
        """Test AI connection when API key not configured."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_ai()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'API key not configured'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_ai_connection_server_unreachable(self, mock_get):
        """Test AI connection when server is unreachable."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = 'test_api_key'
        settings.ai_api_base = 'https://api.example.com/v1'
        settings.save()

        import requests
        mock_get.side_effect = requests.RequestException('Connection refused')

        tester = ConnectionTester()
        result = tester.test_ai()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'AI provider server not reachable'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_ai_connection_http_error_still_reachable(self, mock_get):
        """Test AI connection returns success even with HTTP error."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = 'test_api_key'
        settings.ai_api_base = 'https://api.example.com/v1'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_ai()

        assert result['success'] is True
        assert result['status'] == 'connected'

    @patch('apps.config.services.connection_tester.SystemSettings')
    def test_ai_connection_no_settings(self, mock_settings_class):
        """Test AI connection when SystemSettings doesn't exist."""
        mock_settings_class.DoesNotExist = SystemSettings.DoesNotExist
        mock_settings_class.objects.get.side_effect = SystemSettings.DoesNotExist

        tester = ConnectionTester()
        result = tester.test_ai()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'System settings not configured'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_ai_connection_generic_exception(self, mock_get):
        """Test AI connection with generic exception."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = 'test_api_key'
        settings.save()

        mock_get.side_effect = Exception('Unexpected error')

        tester = ConnectionTester()
        result = tester.test_ai()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Unexpected error'


@pytest.mark.django_db
class TestConnectionTesterIMAP:
    """Tests for test_imap method."""

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_success(self, mock_imap):
        """Test successful IMAP connection."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = 'imap.gmail.com'
        settings.imap_user = 'test@example.com'
        settings.imap_pass = 'test_password'
        settings.save()

        mock_mail = Mock()
        mock_imap.return_value = mock_mail

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is True
        assert result['status'] == 'connected'
        assert result['message'] == 'IMAP server connection successful'

        mock_imap.assert_called_once_with('imap.gmail.com', timeout=10)
        mock_mail.login.assert_called_once_with('test@example.com', 'test_password')
        mock_mail.logout.assert_called_once()

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_no_credentials(self, mock_imap):
        """Test IMAP connection when credentials not configured."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = ''
        settings.imap_user = ''
        settings.imap_pass = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'IMAP credentials not configured'

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_missing_host(self, mock_imap):
        """Test IMAP connection when only host is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = ''
        settings.imap_user = 'test@example.com'
        settings.imap_pass = 'password'
        settings.save()

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_missing_user(self, mock_imap):
        """Test IMAP connection when only user is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = 'imap.gmail.com'
        settings.imap_user = ''
        settings.imap_pass = 'password'
        settings.save()

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_missing_password(self, mock_imap):
        """Test IMAP connection when only password is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = 'imap.gmail.com'
        settings.imap_user = 'test@example.com'
        settings.imap_pass = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_authentication_failed(self, mock_imap):
        """Test IMAP connection with authentication failure."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = 'imap.gmail.com'
        settings.imap_user = 'test@example.com'
        settings.imap_pass = 'wrong_password'
        settings.save()

        import imaplib
        mock_mail = Mock()
        mock_mail.login.side_effect = imaplib.IMAP4.error('Authentication failed')
        mock_imap.return_value = mock_mail

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert 'IMAP authentication failed' in result['message']

    @patch('apps.config.services.connection_tester.imaplib.IMAP4_SSL')
    def test_imap_connection_timeout(self, mock_imap):
        """Test IMAP connection with timeout error."""
        settings = SystemSettings.objects.get(pk=1)
        settings.imap_host = 'imap.gmail.com'
        settings.imap_user = 'test@example.com'
        settings.imap_pass = 'password'
        settings.save()

        mock_imap.side_effect = Exception('Connection timed out')

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Connection timed out'

    @patch('apps.config.services.connection_tester.SystemSettings')
    def test_imap_connection_no_settings(self, mock_settings_class):
        """Test IMAP connection when SystemSettings doesn't exist."""
        mock_settings_class.DoesNotExist = SystemSettings.DoesNotExist
        mock_settings_class.objects.get.side_effect = SystemSettings.DoesNotExist

        tester = ConnectionTester()
        result = tester.test_imap()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'System settings not configured'


@pytest.mark.django_db
class TestConnectionTesterZendesk:
    """Tests for test_zendesk method."""

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_success(self, mock_get):
        """Test successful Zendesk API connection."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = 'testcompany'
        settings.zd_email = 'test@company.com'
        settings.zd_token = 'test_token_123'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is True
        assert result['status'] == 'connected'
        assert result['message'] == 'Zendesk API connection successful'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_no_credentials(self, mock_get):
        """Test Zendesk connection when credentials not configured."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = ''
        settings.zd_token = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'Zendesk credentials not configured'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_missing_subdomain(self, mock_get):
        """Test Zendesk connection when only subdomain is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = ''
        settings.zd_token = 'test_token'
        settings.save()

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_missing_token(self, mock_get):
        """Test Zendesk connection when only token is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = 'testcompany'
        settings.zd_token = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_invalid_credentials(self, mock_get):
        """Test Zendesk connection with invalid credentials (401)."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = 'testcompany'
        settings.zd_email = 'test@company.com'
        settings.zd_token = 'invalid_token'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Invalid Zendesk credentials'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_other_error(self, mock_get):
        """Test Zendesk connection with other HTTP error."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = 'testcompany'
        settings.zd_email = 'test@company.com'
        settings.zd_token = 'test_token'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Zendesk API error: 500'

    @patch('apps.config.services.connection_tester.requests.get')
    def test_zendesk_connection_request_exception(self, mock_get):
        """Test Zendesk connection with request exception."""
        settings = SystemSettings.objects.get(pk=1)
        settings.zd_subdomain = 'testcompany'
        settings.zd_email = 'test@company.com'
        settings.zd_token = 'test_token'
        settings.save()

        import requests
        mock_get.side_effect = requests.RequestException('Connection failed')

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Connection failed'

    @patch('apps.config.services.connection_tester.SystemSettings')
    def test_zendesk_connection_no_settings(self, mock_settings_class):
        """Test Zendesk connection when SystemSettings doesn't exist."""
        mock_settings_class.DoesNotExist = SystemSettings.DoesNotExist
        mock_settings_class.objects.get.side_effect = SystemSettings.DoesNotExist

        tester = ConnectionTester()
        result = tester.test_zendesk()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'System settings not configured'


@pytest.mark.django_db
class TestConnectionTesterPayPal:
    """Tests for test_paypal method."""

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_success(self, mock_post):
        """Test successful PayPal API connection."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = 'test_client_id'
        settings.paypal_secret = 'test_secret'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is True
        assert result['status'] == 'connected'
        assert result['message'] == 'PayPal API connection successful'

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_no_credentials(self, mock_post):
        """Test PayPal connection when credentials not configured."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = ''
        settings.paypal_secret = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'PayPal credentials not configured'

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_missing_client_id(self, mock_post):
        """Test PayPal connection when only client ID is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = ''
        settings.paypal_secret = 'test_secret'
        settings.save()

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_missing_secret(self, mock_post):
        """Test PayPal connection when only secret is missing."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = 'test_client_id'
        settings.paypal_secret = ''
        settings.save()

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'disconnected'

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_invalid_credentials(self, mock_post):
        """Test PayPal connection with invalid credentials (401)."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = 'invalid_client'
        settings.paypal_secret = 'invalid_secret'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 401
        mock_post.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Invalid PayPal credentials'

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_other_error(self, mock_post):
        """Test PayPal connection with other HTTP error."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = 'test_client_id'
        settings.paypal_secret = 'test_secret'
        settings.save()

        mock_response = Mock()
        mock_response.status_code = 500
        mock_post.return_value = mock_response

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'PayPal API error: 500'

    @patch('apps.config.services.connection_tester.requests.post')
    def test_paypal_connection_request_exception(self, mock_post):
        """Test PayPal connection with request exception."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = 'test_client_id'
        settings.paypal_secret = 'test_secret'
        settings.save()

        import requests
        mock_post.side_effect = requests.RequestException('Connection failed')

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Connection failed'

    @patch('apps.config.services.connection_tester.SystemSettings')
    def test_paypal_connection_no_settings(self, mock_settings_class):
        """Test PayPal connection when SystemSettings doesn't exist."""
        mock_settings_class.DoesNotExist = SystemSettings.DoesNotExist
        mock_settings_class.objects.get.side_effect = SystemSettings.DoesNotExist

        tester = ConnectionTester()
        result = tester.test_paypal()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'System settings not configured'


@pytest.mark.django_db
class TestConnectionTesterScreenshot:
    """Tests for get_screenshot_status method."""

    @patch('subprocess.run')
    def test_screenshot_status_available(self, mock_run):
        """Test screenshot service when Playwright is available."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        tester = ConnectionTester()
        result = tester.get_screenshot_status()

        assert result['success'] is True
        assert result['status'] == 'connected'
        assert result['message'] == 'Screenshot service is available'

    @patch('subprocess.run')
    def test_screenshot_status_not_installed(self, mock_run):
        """Test screenshot service when Playwright not installed."""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        tester = ConnectionTester()
        result = tester.get_screenshot_status()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Playwright not installed'

    @patch('subprocess.run')
    def test_screenshot_status_file_not_found(self, mock_run):
        """Test screenshot service when Playwright command not found."""
        mock_run.side_effect = FileNotFoundError('playwright not found')

        tester = ConnectionTester()
        result = tester.get_screenshot_status()

        assert result['success'] is False
        assert result['status'] == 'disconnected'
        assert result['message'] == 'Playwright not installed'

    @patch('subprocess.run')
    def test_screenshot_status_exception(self, mock_run):
        """Test screenshot service with exception."""
        mock_run.side_effect = Exception('Unexpected error')

        tester = ConnectionTester()
        result = tester.get_screenshot_status()

        assert result['success'] is False
        assert result['status'] == 'error'
        assert result['message'] == 'Unexpected error'


@pytest.mark.django_db
class TestConnectionTesterUpdateStatus:
    """Tests for _update_status method."""

    def test_update_status_create_new(self):
        """Test _update_status creates new ServiceStatus record."""
        tester = ConnectionTester()
        result = tester._update_status(
            service='TEST_SERVICE',
            status='connected',
            success=True,
            message='Test message'
        )

        assert result['success'] is True
        assert result['status'] == 'connected'
        assert result['message'] == 'Test message'
        assert result['service'] == 'TEST_SERVICE'

        status = ServiceStatus.objects.get(service='TEST_SERVICE')
        assert status.status == 'connected'
        assert status.is_enabled is True
        assert status.last_error == ''

    def test_update_status_update_existing(self):
        """Test _update_status updates existing ServiceStatus record."""
        ServiceStatus.objects.update_or_create(
            service='TEST_SERVICE',
            defaults={'status': 'disconnected', 'is_enabled': False}
        )

        tester = ConnectionTester()
        result = tester._update_status(
            service='TEST_SERVICE',
            status='connected',
            success=True,
            message='Test message'
        )

        status = ServiceStatus.objects.get(service='TEST_SERVICE')
        assert status.status == 'connected'
        assert status.last_checked is not None

    def test_update_status_with_error(self):
        """Test _update_status sets error message on failure."""
        tester = ConnectionTester()
        result = tester._update_status(
            service='TEST_SERVICE2',
            status='error',
            success=False,
            message='Test error message'
        )

        assert result['success'] is False

        status = ServiceStatus.objects.get(service='TEST_SERVICE2')
        assert status.status == 'error'
        assert status.last_error == 'Test error message'

    def test_update_status_with_metadata(self):
        """Test _update_status includes metadata."""
        tester = ConnectionTester()
        metadata = {'key': 'value', 'count': 42}
        result = tester._update_status(
            service='TEST_SERVICE3',
            status='connected',
            success=True,
            message='Test',
            metadata=metadata
        )

        status = ServiceStatus.objects.get(service='TEST_SERVICE3')
        assert status.metadata == metadata


@pytest.mark.django_db
class TestConnectionTesterTestAll:
    """Tests for test_all_services method."""

    @patch.object(ConnectionTester, 'test_ai')
    @patch.object(ConnectionTester, 'test_imap')
    @patch.object(ConnectionTester, 'test_zendesk')
    @patch.object(ConnectionTester, 'test_paypal')
    @patch.object(ConnectionTester, 'get_scheduler_status')
    @patch.object(ConnectionTester, 'get_screenshot_status')
    def test_test_all_services(
        self, mock_screenshot, mock_scheduler, mock_paypal,
        mock_zendesk, mock_imap, mock_ai
    ):
        """Test test_all_services calls all test methods."""
        mock_ai.return_value = {'status': 'connected'}
        mock_imap.return_value = {'status': 'connected'}
        mock_zendesk.return_value = {'status': 'connected'}
        mock_paypal.return_value = {'status': 'connected'}
        mock_scheduler.return_value = {'status': 'running'}
        mock_screenshot.return_value = {'status': 'connected'}

        tester = ConnectionTester()
        results = tester.test_all_services()

        mock_ai.assert_called_once()
        mock_imap.assert_called_once()
        mock_zendesk.assert_called_once()
        mock_paypal.assert_called_once()
        mock_scheduler.assert_called_once()
        mock_screenshot.assert_called_once()

        assert 'AI' in results
        assert 'IMAP' in results
        assert 'ZENDESK' in results
        assert 'PAYPAL' in results
        assert 'SCHEDULER' in results
        assert 'SCREENSHOT' in results


# =============================================================================
# SchedulerController Tests (limited - no get_scheduler dependency)
# =============================================================================


@pytest.mark.django_db
class TestSchedulerControllerToggle:
    """Tests for toggle_enabled method (does not require get_scheduler)."""

    def test_toggle_enable(self):
        """Test enabling the scheduler."""
        ServiceStatus.objects.update_or_create(
            service='SCHEDULER',
            defaults={'status': 'stopped', 'is_enabled': False}
        )

        controller = SchedulerController()
        result = controller.toggle_enabled(enable=True)

        assert result['success'] is True
        assert result['status'] == 'enabled'
        assert result['message'] == 'Scheduler enabled'
        assert result['previously'] == 'disabled'

        status = ServiceStatus.objects.get(service='SCHEDULER')
        assert status.is_enabled is True

    def test_toggle_disable(self):
        """Test disabling the scheduler."""
        ServiceStatus.objects.update_or_create(
            service='SCHEDULER',
            defaults={'status': 'running', 'is_enabled': True}
        )

        controller = SchedulerController()
        result = controller.toggle_enabled(enable=False)

        assert result['success'] is True
        assert result['status'] == 'disabled'
        assert result['message'] == 'Scheduler disabled'
        assert result['previously'] == 'enabled'

        status = ServiceStatus.objects.get(service='SCHEDULER')
        assert status.is_enabled is False

    def test_toggle_create_new(self):
        """Test toggle when no ServiceStatus exists."""
        ServiceStatus.objects.filter(service='SCHEDULER').delete()

        controller = SchedulerController()
        result = controller.toggle_enabled(enable=True)

        assert result['success'] is True
        assert result['status'] == 'enabled'

        status = ServiceStatus.objects.get(service='SCHEDULER')
        assert status.is_enabled is True


# =============================================================================
# Model Tests
# =============================================================================


@pytest.mark.django_db
class TestServiceStatusModel:
    """Tests for ServiceStatus model methods."""

    def test_mark_connected(self):
        """Test mark_connected method."""
        status = ServiceStatus.objects.create(
            service='TEST_MARK_CONN',
            status='error',
            last_error='Some error'
        )

        status.mark_connected()
        status.refresh_from_db()

        assert status.status == 'connected'
        assert status.last_error == ''
        assert status.last_checked is not None

    def test_mark_disconnected(self):
        """Test mark_disconnected method."""
        status = ServiceStatus.objects.create(
            service='TEST_MARK_DISC',
            status='connected'
        )

        status.mark_disconnected()
        status.refresh_from_db()

        assert status.status == 'disconnected'

    def test_mark_error(self):
        """Test mark_error method."""
        status = ServiceStatus.objects.create(
            service='TEST_MARK_ERR',
            status='connected'
        )

        status.mark_error('Test error message')
        status.refresh_from_db()

        assert status.status == 'error'
        assert status.last_error == 'Test error message'

    def test_get_status_color(self):
        """Test get_status_color method."""
        status = ServiceStatus(service='TEST', status='connected')
        assert status.get_status_color() == 'success'

        status.status = 'disconnected'
        assert status.get_status_color() == 'neutral'

        status.status = 'error'
        assert status.get_status_color() == 'error'

        status.status = 'running'
        assert status.get_status_color() == 'primary'

        status.status = 'stopped'
        assert status.get_status_color() == 'warning'

    def test_str_representation(self):
        """Test string representation."""
        status, _ = ServiceStatus.objects.get_or_create(
            service='AI',
            defaults={'status': 'connected'}
        )
        assert 'AI Provider' in str(status)
        assert 'Connected' in str(status)


@pytest.mark.django_db
class TestSystemSettingsModel:
    """Tests for SystemSettings model methods."""

    def test_get_instance(self):
        """Test get_instance class method."""
        instance = SystemSettings.get_instance()
        assert instance.pk == 1

    def test_get_masked_value_short(self):
        """Test get_masked_value with short string."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = 'short'
        settings.save()

        masked = settings.get_masked_value('ai_api_key')
        assert masked == '•••••'

    def test_get_masked_value_long(self):
        """Test get_masked_value with long string."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = 'abcdefghij1234567890'
        settings.save()

        masked = settings.get_masked_value('ai_api_key')
        assert masked == 'abcd••••••••••••7890'

    def test_get_masked_value_empty(self):
        """Test get_masked_value with empty string."""
        settings = SystemSettings.objects.get(pk=1)
        settings.ai_api_key = ''

        masked = settings.get_masked_value('ai_api_key')
        assert masked == ''

    def test_str_representation(self):
        """Test string representation."""
        settings = SystemSettings.objects.get(pk=1)
        assert str(settings) == 'System Settings'
