"""
Tests for the payments service layer.

Tests cover:
- Refund service (refund_service.py)
- PayPal disputes service (paypal_disputes_service.py)
- Document service (document_service.py)
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from decimal import Decimal
from django.test import TestCase

from apps.payments.refund_service import RefundService
from apps.payments.paypal_disputes_service import (
    get_paypal_access_token,
    fetch_dispute_details,
)
from apps.claims.models import Claim
from apps.payments.models import Refund, Dispute
from apps.config.models import SystemSettings
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
class TestRefundServiceInit:
    """Tests for RefundService initialization."""

    def test_service_initialization(self):
        """Service uses the mode-aware PayPal base URL (sandbox by default)."""
        from apps.payments.paypal_disputes_service import paypal_api_base
        service = RefundService()
        assert service.paypal_base_url == paypal_api_base()


@pytest.mark.django_db
class TestRefundServiceInitiateRefund:
    """Tests for initiate_refund method."""

    def test_initiate_refund_no_paypal_credentials(self):
        """Test refund initiation without PayPal credentials."""
        # Ensure SystemSettings has no PayPal credentials
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = ""
        settings.paypal_secret = ""
        settings.save()

        claim = Claim.objects.create(
            client_email="refund-test@example.com",
            alf_claim_id="ALF3100001",
        )

        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=Decimal("50.00"),
            reason="Test refund",
            user=None,
        )

        assert result["success"] is False
        assert "PayPal credentials not configured" in result["error"]

    @patch("apps.payments.refund_service.RefundService._process_paypal_refund")
    def test_initiate_refund_success_full(self, mock_process_refund):
        """Test successful full refund initiation."""
        # Setup PayPal credentials
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = "test_client_id"
        settings.paypal_secret = "test_secret"
        settings.save()

        claim = Claim.objects.create(
            client_email="full-refund@example.com",
            alf_claim_id="ALF3100002",
            status="Received",
        )

        # Mock PayPal API response
        mock_process_refund.return_value = {
            "success": True,
            "refund_id": "REFUND-TEST-123",
            "metadata": {"status": "COMPLETED"},
        }

        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=Decimal("100.00"),
            reason="Full refund test",
            user=None,
            refund_type="FULL",
            capture_id="CAPTURE-TEST",
        )

        assert result["success"] is True
        assert result["paypal_refund_id"] == "REFUND-TEST-123"
        assert "refund" in result

        # Claim status is NOT written by the refund service (Zendesk webhook owns status)
        claim.refresh_from_db()
        assert claim.status == "Received"  # status unchanged by refund

        # Verify refund record created
        refund = Refund.objects.filter(claim=claim).first()
        assert refund is not None
        assert refund.amount == Decimal("100.00")
        assert refund.refund_type == "FULL"

    @patch("apps.payments.refund_service.RefundService._process_paypal_refund")
    def test_initiate_refund_success_partial(self, mock_process_refund):
        """Test successful partial refund initiation."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = "test_client_id"
        settings.paypal_secret = "test_secret"
        settings.save()

        claim = Claim.objects.create(
            client_email="partial-refund@example.com",
            alf_claim_id="ALF3100003",
            status="Received",
        )

        mock_process_refund.return_value = {
            "success": True,
            "refund_id": "REFUND-PARTIAL-456",
            "metadata": {},
        }

        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=Decimal("50.00"),
            reason="Partial refund test",
            user=None,
            refund_type="PARTIAL",
            capture_id="CAPTURE-TEST",
        )

        assert result["success"] is True

        # Claim status is NOT written by the refund service (Zendesk webhook owns status)
        claim.refresh_from_db()
        assert claim.status == "Received"  # status unchanged by refund

    @patch("apps.payments.refund_service.RefundService._process_paypal_refund")
    def test_initiate_refund_paypal_error(self, mock_process_refund):
        """Test refund initiation with PayPal API error."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = "test_client_id"
        settings.paypal_secret = "test_secret"
        settings.save()

        claim = Claim.objects.create(
            client_email="paypal-error@example.com",
            alf_claim_id="ALF3100004",
        )

        mock_process_refund.return_value = {
            "success": False,
            "error": "PayPal API error",
        }

        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=Decimal("50.00"),
            reason="Test",
            user=None,
            capture_id="CAPTURE-TEST",
        )

        assert result["success"] is False
        assert result["error"] == "PayPal API error"

        # Verify refund record created but marked as failed
        refund = Refund.objects.filter(claim=claim).first()
        assert refund is not None
        assert refund.status == "FAILED"

    @patch("apps.payments.refund_service.RefundService._process_paypal_refund")
    def test_initiate_refund_no_refund_id(self, mock_process_refund):
        """Test refund initiation when PayPal returns no refund ID."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = "test_client_id"
        settings.paypal_secret = "test_secret"
        settings.save()

        claim = Claim.objects.create(
            client_email="no-refund-id@example.com",
            alf_claim_id="ALF1000005",
        )

        mock_process_refund.return_value = {
            "success": True,
            "refund_id": None,  # No refund ID
        }

        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=Decimal("50.00"),
            reason="Test",
            user=None,
            capture_id="CAPTURE-TEST",
        )

        assert result["success"] is False
        assert "No refund ID from PayPal" in result["error"]

    @patch("apps.payments.refund_service.RefundService._process_paypal_refund")
    def test_initiate_refund_exception(self, mock_process_refund):
        """Test refund initiation with unexpected exception."""
        settings = SystemSettings.objects.get(pk=1)
        settings.paypal_client_id = "test_client_id"
        settings.paypal_secret = "test_secret"
        settings.save()

        claim = Claim.objects.create(
            client_email="exception-test@example.com",
            alf_claim_id="ALF1000006",
        )

        mock_process_refund.side_effect = Exception("Unexpected error")

        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=Decimal("50.00"),
            reason="Test",
            user=None,
            capture_id="CAPTURE-TEST",
        )

        assert result["success"] is False
        assert "Unexpected error" in result["error"]


@pytest.mark.django_db
class TestRefundServiceProcessPaypalRefund:
    """Tests for _process_paypal_refund method."""

    @patch("apps.payments.refund_service.get_paypal_access_token")
    @patch("urllib.request.urlopen")
    def test_process_paypal_refund_success(
        self, mock_urlopen, mock_token
    ):
        """Test successful PayPal refund processing."""
        mock_token.return_value = "test_access_token"

        # Mock PayPal API response
        mock_response = Mock()
        mock_response.read.return_value = b'{"id": "REFUND-123", "status": "COMPLETED"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        service = RefundService()
        result = service._process_paypal_refund(
            capture_id="CAPTURE-123",
            amount=Decimal("50.00"),
            currency="USD",
            note_to_payer="Test refund",
        )

        assert result["success"] is True
        assert result["refund_id"] == "REFUND-123"

    @patch("apps.payments.refund_service.get_paypal_access_token")
    def test_process_paypal_refund_no_token(self, mock_token):
        """Test PayPal refund when no access token available."""
        mock_token.return_value = None

        service = RefundService()
        result = service._process_paypal_refund(
            capture_id="CAPTURE-123",
            amount=Decimal("50.00"),
            currency="USD",
            note_to_payer="Test",
        )

        assert result["success"] is False
        assert "Failed to get PayPal access token" in result["error"]

    @patch("apps.payments.refund_service.get_paypal_access_token")
    @patch("urllib.request.urlopen")
    def test_process_paypal_refund_api_error(self, mock_urlopen, mock_token):
        """Test PayPal refund with API error."""
        mock_token.return_value = "test_token"

        mock_urlopen.side_effect = Exception("API Error")

        service = RefundService()
        result = service._process_paypal_refund(
            capture_id="CAPTURE-123",
            amount=Decimal("50.00"),
            currency="USD",
            note_to_payer="Test",
        )

        assert result["success"] is False


@pytest.mark.django_db
class TestGetPaypalAccessToken:
    """Tests for get_paypal_access_token function."""

    @patch("apps.payments.paypal_disputes_service.SystemSettings")
    @patch("apps.payments.paypal_disputes_service.cache")
    @patch("urllib.request.urlopen")
    def test_get_token_success(self, mock_urlopen, mock_cache, mock_settings_class):
        """Test successful token retrieval."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.paypal_client_id = "test_client"
        mock_settings.paypal_secret = "test_secret"
        mock_settings_class.get_instance.return_value = mock_settings

        # Mock cache miss
        mock_cache.get.return_value = None

        # Mock PayPal OAuth response
        mock_response = Mock()
        mock_response.read.return_value = b'{"access_token": "test_token_123"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        token = get_paypal_access_token()

        assert token == "test_token_123"
        mock_cache.set.assert_called()

    @patch("apps.payments.paypal_disputes_service.SystemSettings")
    @patch("apps.payments.paypal_disputes_service.cache")
    def test_get_token_from_cache(self, mock_cache, mock_settings_class):
        """Test token retrieved from cache."""
        mock_settings = Mock()
        mock_settings.paypal_client_id = "test_client"
        mock_settings.paypal_secret = "test_secret"
        mock_settings_class.get_instance.return_value = mock_settings

        mock_cache.get.return_value = "cached_token_456"

        token = get_paypal_access_token()

        assert token == "cached_token_456"

    @patch("apps.payments.paypal_disputes_service.SystemSettings")
    def test_get_token_no_credentials(self, mock_settings_class):
        """Test token retrieval without credentials."""
        mock_settings = Mock()
        mock_settings.paypal_client_id = ""
        mock_settings.paypal_secret = ""
        mock_settings_class.get_instance.return_value = mock_settings

        token = get_paypal_access_token()

        assert token is None

    @patch("apps.payments.paypal_disputes_service.SystemSettings")
    @patch("urllib.request.urlopen")
    def test_get_token_http_error(self, mock_urlopen, mock_settings_class):
        """Test token retrieval with HTTP error."""
        mock_settings = Mock()
        mock_settings.paypal_client_id = "test_client"
        mock_settings.paypal_secret = "test_secret"
        mock_settings_class.get_instance.return_value = mock_settings

        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://paypal.com",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

        token = get_paypal_access_token()

        assert token is None

    @patch("apps.payments.paypal_disputes_service.SystemSettings")
    @patch("urllib.request.urlopen")
    def test_get_token_url_error(self, mock_urlopen, mock_settings_class):
        """Test token retrieval with URL error."""
        mock_settings = Mock()
        mock_settings.paypal_client_id = "test_client"
        mock_settings.paypal_secret = "test_secret"
        mock_settings_class.get_instance.return_value = mock_settings

        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection failed")

        token = get_paypal_access_token()

        assert token is None

    @patch("apps.payments.paypal_disputes_service.SystemSettings")
    @patch("urllib.request.urlopen")
    def test_get_token_no_access_token_in_response(self, mock_urlopen, mock_settings_class):
        """Test token retrieval when response has no access_token."""
        mock_settings = Mock()
        mock_settings.paypal_client_id = "test_client"
        mock_settings.paypal_secret = "test_secret"
        mock_settings_class.get_instance.return_value = mock_settings

        mock_response = Mock()
        mock_response.read.return_value = b'{"error": "invalid_client"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        token = get_paypal_access_token()

        assert token is None


@pytest.mark.django_db
class TestFetchDisputeDetails:
    """Tests for fetch_dispute_details function."""

    @patch("apps.payments.paypal_disputes_service.get_paypal_access_token")
    @patch("urllib.request.urlopen")
    def test_fetch_dispute_success(self, mock_urlopen, mock_token):
        """Test successful dispute details fetch."""
        mock_token.return_value = "test_token"

        mock_response = Mock()
        mock_response.read.return_value = b'''
        {
            "dispute_id": "PP-D-12345",
            "reason": "MERCHANDISE_NOT_RECEIVED",
            "status": "OPEN",
            "amount": {"value": "100.00", "currency_code": "USD"}
        }
        '''
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = fetch_dispute_details("PP-D-12345")

        assert result is not None
        assert result["dispute_id"] == "PP-D-12345"
        assert result["reason"] == "MERCHANDISE_NOT_RECEIVED"
        assert result["status"] == "OPEN"

    @patch("apps.payments.paypal_disputes_service.get_paypal_access_token")
    def test_fetch_dispute_no_token(self, mock_token):
        """Test dispute fetch without token."""
        mock_token.return_value = None

        result = fetch_dispute_details("PP-D-12345")

        assert result is None

    @patch("apps.payments.paypal_disputes_service.get_paypal_access_token")
    @patch("urllib.request.urlopen")
    def test_fetch_dispute_http_error(self, mock_urlopen, mock_token):
        """Test dispute fetch with HTTP error."""
        mock_token.return_value = "test_token"

        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://paypal.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

        result = fetch_dispute_details("PP-D-12345")

        assert result is None


# Continue in part 2...
