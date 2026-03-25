"""
Tests for payments app utilities.

Tests cover:
- generate_proof_of_work_pdf function
- generate_dispute_notification_email function
- WeasyPrint integration
- File validation and security
"""

import pytest
from unittest.mock import Mock, patch, MagicMock, mock_open
from django.test import Client
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from io import BytesIO
from PIL import Image

from apps.claims.models import Claim, ClaimEvidence
from apps.payments.utils import (
    generate_proof_of_work_pdf,
    generate_dispute_notification_email,
    _get_weasyprint,
)


# ============== Helper Functions ==============

def create_test_image():
    """Create a test image file for evidence."""
    img = Image.new('RGB', (100, 100), color='red')
    buffer = BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return buffer


# ============== Test _get_weasyprint ==============

@pytest.mark.django_db
class TestGetWeasyPrint:
    """Tests for _get_weasyprint helper function."""

    def test_get_weasyprint_returns_tuple(self):
        """Test _get_weasyprint returns a tuple."""
        result = _get_weasyprint()
        assert isinstance(result, tuple)
        assert len(result) == 2


# ============== Test generate_dispute_notification_email ==============

@pytest.mark.django_db
class TestGenerateDisputeNotificationEmail:
    """Tests for generate_dispute_notification_email function."""

    def test_generates_email_with_all_fields(self):
        """Test email generation with complete claim data."""
        claim = Claim.objects.create(
            client_email='customer@example.com',
            status='Disputed',
            flight_details='Flight AA100 from JFK to LAX',
        )
        
        email_body = generate_dispute_notification_email(claim)
        
        assert 'Dear LORA Team' in email_body
        assert 'PayPal dispute has been opened' in email_body
        assert str(claim.id) in email_body
        assert 'customer@example.com' in email_body
        assert 'Disputed' in email_body
        assert 'Flight AA100 from JFK to LAX' in email_body
        assert 'automated notification' in email_body

    def test_generates_email_without_flight_details(self):
        """Test email generation when flight details not provided."""
        claim = Claim.objects.create(
            client_email='customer_noflight@example.com',
            status='Disputed',
            flight_details='',  # Empty flight details
        )
        
        email_body = generate_dispute_notification_email(claim)
        
        assert 'Not provided' in email_body


# ============== Test generate_proof_of_work_pdf ==============

@pytest.mark.django_db
class TestGenerateProofOfWorkPdf:
    """Tests for generate_proof_of_work_pdf function."""

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_no_evidence(self, mock_get_wp, mock_render):
        """Test PDF generation with no evidence attached."""
        claim = Claim.objects.create(
            client_email='test_pdf@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        # Mock WeasyPrint to avoid actual PDF generation
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-fake-pdf-content'
        mock_html_instance.write_pdf.assert_called_once()

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_with_evidence(self, mock_get_wp, mock_render):
        """Test PDF generation with evidence images."""
        claim = Claim.objects.create(
            client_email='test_pdf_ev@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        # Create evidence with image
        img_buffer = create_test_image()
        evidence = ClaimEvidence.objects.create(
            claim=claim,
            description='Test evidence image'
        )
        evidence.image.save('test_image.jpg', 
                           SimpleUploadedFile('test_image.jpg', 
                                            img_buffer.read(), 
                                            content_type='image/jpeg'))
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-fake-pdf-content'

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    @patch('apps.integrations.services.fetch_zendesk_comments')
    def test_pdf_generation_with_zendesk_comments(self, mock_fetch, mock_get_wp, mock_render):
        """Test PDF generation fetches Zendesk comments."""
        claim = Claim.objects.create(
            client_email='test_pdf_zd@example.com',
            zd_ticket_id='12345',
            status='Received',
            flight_details='Flight AA100',
        )
        
        mock_comments = [
            {'author': {'name': 'Agent', 'email': 'agent@example.com'}, 
             'body': 'Test comment', 
             'created_at': '2026-03-18T10:00:00Z',
             'public': True}
        ]
        mock_fetch.return_value = mock_comments
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-fake-pdf-content'
        mock_fetch.assert_called_once_with('12345')

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    @patch('apps.integrations.services.fetch_zendesk_comments')
    def test_pdf_generation_zendesk_error_handled(self, mock_fetch, mock_get_wp, mock_render):
        """Test PDF generation continues when Zendesk fetch fails."""
        claim = Claim.objects.create(
            client_email='test_pdf_zderr@example.com',
            zd_ticket_id='12345',
            status='Received',
            flight_details='Flight AA100',
        )
        
        mock_fetch.side_effect = Exception('Zendesk API error')
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        # Should still generate PDF despite Zendesk error
        assert result == b'%PDF-fake-pdf-content'

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_no_zd_ticket_id(self, mock_get_wp, mock_render):
        """Test PDF generation skips Zendesk when no ticket ID."""
        claim = Claim.objects.create(
            client_email='test_pdf_nozd@example.com',
            zd_ticket_id='',  # No Zendesk ticket
            status='Received',
            flight_details='Flight AA100',
        )
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-fake-pdf-content'

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_weasyprint_not_available(self, mock_get_wp, mock_render):
        """Test PDF generation returns None when WeasyPrint not available."""
        claim = Claim.objects.create(
            client_email='test_pdf_nowp@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        mock_get_wp.return_value = (None, None)
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result is None

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_weasyprint_error(self, mock_get_wp, mock_render):
        """Test PDF generation handles WeasyPrint errors."""
        claim = Claim.objects.create(
            client_email='test_pdf_wperr@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.side_effect = Exception('PDF generation error')
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result is None

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    @patch('apps.payments.utils.logger')
    def test_pdf_generation_evidence_processing_error(self, mock_logger, mock_get_wp, mock_render):
        """Test PDF generation continues when evidence processing fails."""
        claim = Claim.objects.create(
            client_email='test_pdf_everr@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        # Create evidence with invalid path
        evidence = ClaimEvidence.objects.create(
            claim=claim,
            description='Invalid evidence'
        )
        # Set invalid image path
        evidence.image.name = '/nonexistent/path/image.jpg'
        evidence.save()
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        # Should still generate PDF, skipping invalid evidence
        assert result == b'%PDF-fake-pdf-content'
        mock_logger.warning.assert_called()

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    @patch('apps.payments.utils.logger')
    def test_pdf_generation_path_traversal_protection(self, mock_logger, mock_get_wp, mock_render):
        """Test PDF generation rejects evidence files outside MEDIA_ROOT."""
        claim = Claim.objects.create(
            client_email='test_pdf_traversal@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        evidence = ClaimEvidence.objects.create(
            claim=claim,
            description='Suspicious evidence'
        )
        # Set path outside MEDIA_ROOT
        evidence.image.name = '../../../etc/passwd'
        evidence.save()
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        # Should still generate PDF, skipping suspicious file
        assert result == b'%PDF-fake-pdf-content'
        mock_logger.warning.assert_called()
        # Check that warning mentions path outside MEDIA_ROOT or similar security issue
        warning_msg = str(mock_logger.warning.call_args)
        assert 'outside' in warning_msg.lower() or 'error processing' in warning_msg.lower()

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_template_context(self, mock_get_wp, mock_render):
        """Test PDF generation passes correct context to template."""
        claim = Claim.objects.create(
            client_email='test_pdf_ctx@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        captured_context = {}
        
        def capture_render(template_name, context):
            captured_context.update(context)
            return '<html>fake</html>'
        
        mock_render.side_effect = capture_render
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-fake-pdf-content'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        
        result = generate_proof_of_work_pdf(claim)
        
        assert captured_context['claim'] == claim
        assert 'generated_at' in captured_context
        assert 'evidence_list' in captured_context
        assert 'zendesk_comments' in captured_context
        assert 'evidence_count' in captured_context
        assert 'comment_count' in captured_context

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    @patch('apps.payments.utils.logger')
    def test_pdf_generation_logs_success(self, mock_logger, mock_get_wp, mock_render):
        """Test PDF generation logs successful generation."""
        claim = Claim.objects.create(
            client_email='test_pdf_log@example.com',
            status='Received',
            flight_details='Flight AA100',
        )
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-1000-bytes'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        mock_logger.info.assert_called()
        info_msg = str(mock_logger.info.call_args)
        assert 'Generated PDF proof of work' in info_msg
        assert 'bytes' in info_msg


@pytest.mark.django_db
class TestGenerateProofOfWorkPdfIntegration:
    """Integration tests for PDF generation with real templates."""

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    @patch('apps.integrations.services.fetch_zendesk_comments')
    def test_pdf_with_real_template_rendering(self, mock_fetch, mock_get_wp, mock_render):
        """Test PDF generation renders template correctly."""
        claim = Claim.objects.create(
            client_email='integration_pdf@example.com',
            status='Searching',
            flight_details='Flight BA2490 from LHR to JFK',
            object_description='Black leather wallet',
        )
        
        # Create some evidence
        for i in range(2):
            img_buffer = create_test_image()
            evidence = ClaimEvidence.objects.create(
                claim=claim,
                description=f'Evidence image {i + 1}'
            )
            evidence.image.save(f'evidence_{i}.jpg', 
                               SimpleUploadedFile(f'evidence_{i}.jpg', 
                                                img_buffer.read(), 
                                                content_type='image/jpeg'))
        
        mock_comments = [
            {
                'author': {'name': 'Support Agent', 'email': 'support@example.com'},
                'body': 'We are looking into this matter.',
                'created_at': '2026-03-18T10:00:00Z',
                'public': True
            },
            {
                'author': {'name': 'System', 'email': ''},
                'body': 'Internal note: Customer contacted via phone.',
                'created_at': '2026-03-18T11:00:00Z',
                'public': False
            }
        ]
        mock_fetch.return_value = mock_comments
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-integration-test'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-integration-test'
        assert len(result) > 0

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_empty_claim(self, mock_get_wp, mock_render):
        """Test PDF generation with minimal claim data."""
        claim = Claim.objects.create(
            client_email='minimal_pdf@example.com',
            flight_details='Flight AA100',
        )
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-minimal'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-minimal'


@pytest.mark.django_db
class TestGenerateProofOfWorkPdfEdgeCases:
    """Edge case tests for PDF generation."""

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_special_characters_in_claim(self, mock_get_wp, mock_render):
        """Test PDF generation with special characters in claim data."""
        claim = Claim.objects.create(
            client_email='test_pdf_special@example.com',
            flight_details='Flight with "quotes" and <tags> & special chars',
            object_description='Item with emojis and unicode',
        )
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-special-chars'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-special-chars'

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_very_long_description(self, mock_get_wp, mock_render):
        """Test PDF generation with very long evidence description."""
        claim = Claim.objects.create(
            client_email='test_pdf_long@example.com',
            flight_details='Flight AA100',
        )
        
        evidence = ClaimEvidence.objects.create(
            claim=claim,
            description='A' * 10000  # Very long description
        )
        
        img_buffer = create_test_image()
        evidence.image.save('long_desc.jpg', 
                           SimpleUploadedFile('long_desc.jpg', 
                                            img_buffer.read(), 
                                            content_type='image/jpeg'))
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-long-desc'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-long-desc'

    @patch('apps.payments.utils.render_to_string')
    @patch('apps.payments.utils._get_weasyprint')
    def test_pdf_generation_multiple_evidence_files(self, mock_get_wp, mock_render):
        """Test PDF generation with multiple evidence files."""
        claim = Claim.objects.create(
            client_email='test_pdf_multi@example.com',
            flight_details='Flight AA100',
        )
        
        # Create 5 evidence files
        for i in range(5):
            img_buffer = create_test_image()
            evidence = ClaimEvidence.objects.create(
                claim=claim,
                description=f'Evidence {i + 1}'
            )
            evidence.image.save(f'evidence_{i}.jpg', 
                               SimpleUploadedFile(f'evidence_{i}.jpg', 
                                                img_buffer.read(), 
                                                content_type='image/jpeg'))
        
        mock_html_instance = Mock()
        mock_html_instance.write_pdf.return_value = b'%PDF-multiple'
        mock_get_wp.return_value = (Mock(return_value=mock_html_instance), Mock())
        mock_render.return_value = '<html>fake</html>'
        
        result = generate_proof_of_work_pdf(claim)
        
        assert result == b'%PDF-multiple'
