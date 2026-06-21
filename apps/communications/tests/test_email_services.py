"""
Tests for the communications email processing service.

Tests cover:
- Email header decoding and parsing
- Email body extraction
- Alias extraction from headers
- AI response parsing
- Email processing workflow
- Zendesk integration
"""

import pytest
import imaplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from unittest.mock import Mock, patch, MagicMock
from django.test import TestCase

from apps.communications.services import (
    decode_mime_header,
    extract_email_body,
    extract_email_html,
    _html_to_text,
    _sanitize_email_html,
    _build_email_note_html,
    extract_from_email,
    extract_alias_from_headers,
    extract_raw_headers,
    call_qwen_ai,
    parse_ai_response,
    mark_email_as_seen,
    post_ai_summary_to_zendesk,
    process_single_email,
    process_incoming_emails,
    AUTO_RESOLVABLE_CATEGORIES,
)
from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings


@pytest.mark.django_db
class TestDecodeMimeHeader:
    """Tests for decode_mime_header function."""

    def test_plain_text_header(self):
        """Test decoding plain text header."""
        result = decode_mime_header("Test Subject")
        assert result == "Test Subject"

    def test_empty_header(self):
        """Test decoding empty header."""
        result = decode_mime_header("")
        assert result == ""

    def test_none_header(self):
        """Test decoding None header."""
        result = decode_mime_header(None)
        assert result == ""

    def test_utf8_encoded_header(self):
        """Test decoding UTF-8 encoded header."""
        # Simulates header like: =?UTF-8?B?VGVzdCBTdWJqZWN0?=
        header = "=?UTF-8?B?VGVzdCBTdWJqZWN0?="
        result = decode_mime_header(header)
        assert "Test Subject" in result or result == "Test Subject"

    def test_header_with_special_chars(self):
        """Test decoding header with special characters."""
        result = decode_mime_header("Re: Test - 100%")
        assert "Re: Test" in result


@pytest.mark.django_db
class TestExtractEmailBody:
    """Tests for extract_email_body function."""

    def test_plain_text_email(self):
        """Test extracting body from plain text email."""
        msg = MIMEText("This is the email body", "plain")
        msg["Subject"] = "Test"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"

        result = extract_email_body(msg)
        assert result == "This is the email body"

    def test_html_email(self):
        """A single-part HTML email is converted to readable text (tags stripped)."""
        html_content = "<html><body><p>This is HTML content</p></body></html>"
        msg = MIMEText(html_content, "html")
        msg["Subject"] = "Test"

        result = extract_email_body(msg)
        assert "This is HTML content" in result
        assert "<p>" not in result and "<html>" not in result

    def test_multipart_email(self):
        """Test extracting body from multipart email."""
        msg = MIMEMultipart()
        msg["Subject"] = "Test"
        msg.attach(MIMEText("Plain text part", "plain"))
        msg.attach(MIMEText("<html>HTML part</html>", "html"))

        result = extract_email_body(msg)
        assert "Plain text part" in result

    def test_empty_email(self):
        """Test extracting body from empty email."""
        msg = MIMEText("", "plain")
        result = extract_email_body(msg)
        assert result == ""

    def test_email_with_attachments(self):
        """Test that attachments are skipped."""
        from email.mime.base import MIMEBase

        msg = MIMEMultipart()
        msg["Subject"] = "Test"
        msg.attach(MIMEText("Email body", "plain"))

        # Add fake attachment
        attachment = MIMEBase("application", "octet-stream")
        attachment.set_payload("fake data")
        attachment.add_header("Content-Disposition", "attachment", filename="test.txt")
        msg.attach(attachment)

        result = extract_email_body(msg)
        assert "Email body" in result
        assert "fake data" not in result

    def test_html_email_preserves_links_and_images(self):
        """Links keep their URL and images keep their address — nothing silently lost."""
        html = ('<p>Hello <a href="https://app.nettracer.aero/update?r=1">click here</a> '
                'to update.</p><img src="https://cdn.example.com/found.png" alt="found item">')
        result = extract_email_body(MIMEText(html, "html"))
        assert "https://app.nettracer.aero/update?r=1" in result   # link target kept
        assert "click here" in result
        assert "https://cdn.example.com/found.png" in result       # image address kept
        assert "<a" not in result and "<img" not in result

    def test_html_in_plain_text_slot_is_cleaned(self):
        """If a sender stuffs HTML into the text/plain part, it is still cleaned."""
        result = extract_email_body(MIMEText("<div>raw <b>html</b> in the plain slot</div>", "plain"))
        assert "raw html in the plain slot" in result
        assert "<div>" not in result and "<b>" not in result

    def test_multipart_prefers_real_plain_text(self):
        """A genuine text/plain part is used as-is (no stripping needed)."""
        msg = MIMEMultipart()
        msg.attach(MIMEText("Just plain words", "plain"))
        msg.attach(MIMEText("<p>HTML alt</p>", "html"))
        assert extract_email_body(msg) == "Just plain words"


class TestEmailHtmlForZendeskNote:
    """The Zendesk note renders the ORIGINAL email so links and inline images show;
    we sanitize it first (Zendesk re-sanitizes too)."""

    def test_extract_html_from_single_part(self):
        msg = MIMEText("<p>hi <a href='https://x.test'>link</a></p>", "html")
        assert "href" in extract_email_html(msg)

    def test_extract_html_empty_for_plain_email(self):
        assert extract_email_html(MIMEText("just text, no markup", "plain")) == ""

    def test_extract_html_from_plain_slot_that_holds_markup(self):
        assert "<div>" in extract_email_html(MIMEText("<div>html here</div>", "plain"))

    def test_sanitize_keeps_links_and_images_drops_scripts(self):
        dirty = ('<p>ok <a href="https://x.test">go</a> '
                 '<img src="https://x.test/a.png"></p><script>alert(1)</script>')
        clean = _sanitize_email_html(dirty)
        assert "<a" in clean and "https://x.test" in clean
        assert "<img" in clean
        assert "<script" not in clean and "alert(1)" not in clean

    def test_sanitize_strips_handlers_styles_and_js_urls(self):
        clean = _sanitize_email_html(
            '<p style="x" onclick="evil()"><a href="javascript:evil()">x</a></p>')
        assert "onclick" not in clean
        assert "style=" not in clean
        assert "javascript:" not in clean

    def test_note_html_wraps_email_and_analysis(self):
        parsed = {"category": "OBJECT_NOT_FOUND", "summary": "no item yet",
                  "action_required": False, "auto_resolvable": True}
        note = _build_email_note_html(parsed, "Subj", "noreply@x.aero", "alias@d.com",
                                      '<p>Hello <a href="https://x.test/u">here</a></p>')
        assert "New Email Received" in note
        assert "https://x.test/u" in note                # link survives, rendered
        assert "OBJECT_NOT_FOUND" in note and "no item yet" in note

    @patch("apps.communications.services.post_zendesk_comment")
    def test_summary_posts_html_when_html_present(self, mock_post):
        mock_post.return_value = True
        parsed = {"category": "OBJECT_FOUND", "summary": "s", "action_required": False}
        post_ai_summary_to_zendesk(
            zd_ticket_id="9", parsed=parsed, subject="S", from_email="a@b.c",
            email_body="plain fallback", alias="al@d.com",
            email_html='<p>rich <a href="https://x.test">link</a></p>')
        _, kwargs = mock_post.call_args
        assert kwargs.get("html_body")                   # posted as rendered HTML
        assert "https://x.test" in kwargs["html_body"]
        assert "comment_body" not in kwargs              # not the plain-text path

    @patch("apps.communications.services.post_zendesk_comment")
    def test_summary_falls_back_to_plain_without_html(self, mock_post):
        mock_post.return_value = True
        parsed = {"category": "OBJECT_FOUND", "summary": "s", "action_required": False}
        post_ai_summary_to_zendesk(
            zd_ticket_id="9", parsed=parsed, subject="S", from_email="a@b.c",
            email_body="plain only", alias="al@d.com")
        _, kwargs = mock_post.call_args
        assert kwargs.get("html_body") is None or "html_body" not in kwargs
        assert "plain only" in kwargs.get("comment_body", "")


@pytest.mark.django_db
class TestExtractFromEmail:
    """Tests for extract_from_email function."""

    def test_simple_email_address(self):
        """Test extracting simple email address."""
        msg = Mock()
        msg.get.return_value = "sender@example.com"

        result = extract_from_email(msg)
        assert result == "sender@example.com"

    def test_email_with_name(self):
        """Test extracting email from 'Name <email>' format."""
        msg = Mock()
        msg.get.return_value = "John Doe <john@example.com>"

        result = extract_from_email(msg)
        assert result == "john@example.com"

    def test_empty_from_header(self):
        """Test handling empty From header."""
        msg = Mock()
        msg.get.return_value = ""

        result = extract_from_email(msg)
        assert result is None

    def test_invalid_email_format(self):
        """Test handling invalid email format."""
        msg = Mock()
        msg.get.return_value = "Invalid Email Format"

        result = extract_from_email(msg)
        assert result is None


@pytest.mark.django_db
class TestExtractAliasFromHeaders:
    """Tests for extract_alias_from_headers function."""

    @patch("apps.communications.services.SystemSettings")
    def test_alias_in_delivered_to(self, mock_settings_class):
        """Test extracting alias from Delivered-To header."""
        # Mock SystemSettings
        mock_settings = Mock()
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        msg = Mock()
        msg.get.side_effect = lambda key, default=None: {
            "Delivered-To": "client-123@mydomain.com",
            "To": "other@example.com",
            "X-Original-To": "",
            "X-RCPT-TO": "",
        }.get(key, default)

        result = extract_alias_from_headers(msg)
        assert result == "client-123@mydomain.com"

    @patch("apps.communications.services.SystemSettings")
    def test_alias_in_to_header(self, mock_settings_class):
        """Test extracting alias from To header."""
        mock_settings = Mock()
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        msg = Mock()
        msg.get.side_effect = lambda key, default=None: {
            "Delivered-To": "",
            "To": "client-456@mydomain.com",
            "X-Original-To": "",
            "X-RCPT-TO": "",
        }.get(key, default)

        result = extract_alias_from_headers(msg)
        assert result == "client-456@mydomain.com"

    @patch("apps.communications.services.SystemSettings")
    def test_no_email_domain_configured(self, mock_settings_class):
        """Test when email domain is not configured."""
        mock_settings = Mock()
        mock_settings.email_domain = ""
        mock_settings_class.get_instance.return_value = mock_settings

        msg = Mock()
        msg.get.return_value = "client-123@mydomain.com"

        result = extract_alias_from_headers(msg)
        assert result is None

    @patch("apps.communications.services.SystemSettings")
    def test_alias_not_matching_domain(self, mock_settings_class):
        """Test when alias doesn't match configured domain."""
        mock_settings = Mock()
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        msg = Mock()
        msg.get.return_value = "client-123@otherdomain.com"

        result = extract_alias_from_headers(msg)
        assert result is None

    @patch("apps.communications.services.SystemSettings")
    def test_exception_handling(self, mock_settings_class):
        """Test exception handling in alias extraction."""
        mock_settings_class.get_instance.side_effect = Exception("Test error")

        msg = Mock()
        msg.get.return_value = "client-123@mydomain.com"

        result = extract_alias_from_headers(msg)
        assert result is None


@pytest.mark.django_db
class TestExtractRawHeaders:
    """Tests for extract_raw_headers function."""

    def test_extract_headers(self):
        """Test extracting raw headers."""
        msg = Mock()
        msg.items.return_value = [
            ("From", "sender@example.com"),
            ("To", "recipient@example.com"),
            ("Subject", "Test Subject"),
        ]

        result = extract_raw_headers(msg)
        assert "From: sender@example.com" in result
        assert "To: recipient@example.com" in result
        assert "Subject: Test Subject" in result

    def test_exception_handling(self):
        """Test exception handling."""
        msg = Mock()
        msg.items.side_effect = Exception("Test error")

        result = extract_raw_headers(msg)
        assert result == ""


@pytest.mark.django_db
class TestParseAIResponse:
    """Tests for parse_ai_response function."""

    def test_valid_json_response(self):
        """Test parsing valid JSON response."""
        raw_response = '{"summary": "Test summary", "category": "OBJECT_FOUND", "action_required": true, "auto_resolvable": false}'

        result = parse_ai_response(raw_response)

        assert result["summary"] == "Test summary"
        assert result["category"] == "OBJECT_FOUND"
        assert result["action_required"] is True
        assert result["auto_resolvable"] is False

    def test_json_with_markdown_code_blocks(self):
        """Test parsing JSON wrapped in markdown code blocks."""
        raw_response = '```json\n{"summary": "Test", "category": "OBJECT_NOT_FOUND"}\n```'

        result = parse_ai_response(raw_response)

        assert result["summary"] == "Test"
        assert result["category"] == "OBJECT_NOT_FOUND"

    def test_invalid_json_fallback(self):
        """Test fallback for invalid JSON."""
        raw_response = "This is not JSON at all"

        result = parse_ai_response(raw_response)

        # When JSON parsing fails completely, returns default values
        assert result["summary"] == ""
        # Category defaults to UNKNOWN
        assert result["category"] == "UNKNOWN"
        # action_required and auto_resolvable should be False
        assert result["action_required"] is False
        assert result["auto_resolvable"] is False

    def test_category_normalization(self):
        """Test category normalization to valid choices."""
        raw_response = '{"summary": "Test", "category": "object_found"}'

        result = parse_ai_response(raw_response)

        assert result["category"] == "OBJECT_FOUND"

    def test_invalid_category_inference(self):
        """Test category inference from text when invalid category provided."""
        raw_response = '{"summary": "The object was not found after searching"}'

        result = parse_ai_response(raw_response)

        assert result["category"] == "OBJECT_NOT_FOUND"

    def test_auto_resolvable_detection(self):
        """Test auto_resolvable field detection."""
        raw_response = '{"summary": "Test", "category": "SUBMISSION_CONFIRMATION", "auto_resolvable": true}'

        result = parse_ai_response(raw_response)

        assert result["auto_resolvable"] is True

    def test_action_required_detection(self):
        """Test action_required field detection."""
        raw_response = '{"summary": "Test", "action_required": "yes"}'

        result = parse_ai_response(raw_response)

        assert result["action_required"] is True

    def test_empty_response(self):
        """Test handling empty response."""
        result = parse_ai_response("")

        assert result["summary"] == ""
        assert result["category"] == "UNKNOWN"
        assert result["action_required"] is False

    def test_nested_json_response(self):
        """Test parsing nested JSON response."""
        raw_response = '{"data": {"summary": "Test", "category": "OBJECT_FOUND"}}'

        result = parse_ai_response(raw_response)

        # Should extract from nested structure
        assert result is not None


@pytest.mark.django_db
class TestMarkEmailAsSeen:
    """Tests for mark_email_as_seen function."""

    def test_successful_mark_as_seen(self):
        """Test successfully marking email as seen."""
        mock_imap = Mock()
        mock_imap.store.return_value = "OK"

        result = mark_email_as_seen(mock_imap, "123")

        assert result is True
        mock_imap.store.assert_called_once_with("123", "+FLAGS", "\\Seen")

    def test_mark_as_seen_failure(self):
        """Test failure when marking email as seen."""
        mock_imap = Mock()
        mock_imap.store.side_effect = Exception("IMAP error")

        result = mark_email_as_seen(mock_imap, "123")

        assert result is False


@pytest.mark.django_db
class TestPostAISummaryToZendesk:
    """Tests for post_ai_summary_to_zendesk function."""

    @patch("apps.communications.services.post_zendesk_comment")
    def test_successful_post(self, mock_post_comment):
        """Test successfully posting to Zendesk."""
        mock_post_comment.return_value = True

        parsed = {
            "category": "OBJECT_FOUND",
            "summary": "Test summary",
            "action_required": True,
        }

        result = post_ai_summary_to_zendesk(
            zd_ticket_id="12345",
            parsed=parsed,
            subject="Test Subject",
            from_email="sender@example.com",
            email_body="Test body",
            alias="client-123@mydomain.com",
        )

        assert result is True
        mock_post_comment.assert_called_once()

    @patch("apps.communications.services.post_zendesk_comment")
    def test_post_failure(self, mock_post_comment):
        """Test failure when posting to Zendesk."""
        mock_post_comment.return_value = False

        parsed = {
            "category": "OBJECT_FOUND",
            "summary": "Test summary",
            "action_required": True,
        }

        result = post_ai_summary_to_zendesk(
            zd_ticket_id="12345",
            parsed=parsed,
            subject="Test Subject",
            from_email="sender@example.com",
            email_body="Test body",
        )

        assert result is False

    def test_empty_ticket_id(self):
        """Test handling empty ticket ID."""
        parsed = {
            "category": "OBJECT_FOUND",
            "summary": "Test summary",
        }

        result = post_ai_summary_to_zendesk(
            zd_ticket_id="",
            parsed=parsed,
            subject="Test",
            from_email="sender@example.com",
            email_body="Test",
        )

        assert result is False


@pytest.mark.django_db
class TestProcessSingleEmail:
    """Tests for process_single_email function."""

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.match_alias_to_zendesk_ticket")
    @patch("apps.communications.services.extract_from_email")
    @patch("apps.communications.services.extract_alias_from_headers")
    @patch("apps.communications.services.extract_email_body")
    @patch("apps.communications.services.decode_mime_header")
    @patch("apps.communications.services.call_qwen_ai")
    @patch("apps.communications.services.parse_ai_response")
    @patch("apps.communications.services.post_ai_summary_to_zendesk")
    def test_successful_email_processing(
        self, mock_post_zd, mock_parse_ai, mock_call_ai,
        mock_decode, mock_extract_body, mock_extract_alias,
        mock_extract_from, mock_match_alias, mock_settings
    ):
        """Test successful email processing workflow."""
        # Mock settings
        mock_settings_instance = Mock()
        mock_settings_instance.email_domain = "mydomain.com"
        mock_settings.return_value = mock_settings_instance

        # Mock email extraction
        mock_extract_from.return_value = "sender@example.com"
        mock_extract_alias.return_value = "client-123@mydomain.com"
        mock_extract_body.return_value = "Email body"
        mock_decode.return_value = "Test Subject"

        # Mock Zendesk matching
        mock_match_alias.return_value = {"id": "12345"}

        # Mock AI
        mock_call_ai.return_value = {"raw_response": '{"summary": "Test", "category": "OBJECT_FOUND"}'}
        mock_parse_ai.return_value = {
            "summary": "Test summary",
            "category": "OBJECT_FOUND",
            "action_required": False,
            "auto_resolvable": False,
        }

        # Mock Zendesk posting
        mock_post_zd.return_value = True

        # Create mock IMAP connection
        mock_imap = Mock()

        # Create test claim
        claim = Claim.objects.create(
            zd_ticket_id="12345",
            client_email="sender@example.com",
        )

        # Process email
        result = process_single_email(
            imap_conn=mock_imap,
            uid="123",
            msg_data=b"fake email data",
            ai_prompt="Test prompt",
        )

        assert result is not None
        assert isinstance(result, EmailLog)
        assert result.zd_ticket_id == "12345"
        assert result.claim == claim

    @patch("apps.communications.services.match_alias_to_zendesk_ticket")
    @patch("apps.communications.services.extract_from_email")
    @patch("apps.communications.services.extract_alias_from_headers")
    @patch("apps.communications.services.call_qwen_ai")
    @patch("apps.communications.services.parse_ai_response")
    @patch("apps.communications.services.post_ai_summary_to_zendesk")
    def test_auto_sweep_forwards_email_html_for_rendered_note(
        self, mock_post_zd, mock_parse_ai, mock_call_ai,
        mock_extract_alias, mock_extract_from, mock_match_alias,
    ):
        """The automatic sweep posts the SAME rendered note as the manual check —
        it forwards the original email HTML to post_ai_summary_to_zendesk so links
        and inline images render in the ticket (regression: it used to post text)."""
        mock_extract_from.return_value = "noreply@x.aero"
        mock_extract_alias.return_value = "client-1@mydomain.com"
        mock_match_alias.return_value = {"id": "12345"}
        mock_call_ai.return_value = {"raw_response": "{}"}
        mock_parse_ai.return_value = {
            "summary": "s", "category": "OBJECT_NOT_FOUND",
            "action_required": False, "auto_resolvable": True,
        }
        mock_post_zd.return_value = True
        # Claim already in LORA → skips the import branch (no SystemSettings needed).
        Claim.objects.create(zd_ticket_id="12345", client_email="c@e.com")

        m = MIMEText('<p>Hi <a href="https://x.test/u">here</a></p>', "html")
        m["Subject"] = "Update"
        m["Message-ID"] = "<auto-sweep-test@x.test>"

        result = process_single_email(
            imap_conn=Mock(), uid="1", msg_data=m.as_bytes(), ai_prompt="p")

        assert result is not None
        _, kwargs = mock_post_zd.call_args
        assert "https://x.test/u" in kwargs["email_html"]   # rendered note gets the HTML

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.match_alias_to_zendesk_ticket")
    @patch("apps.communications.services.extract_from_email")
    @patch("apps.communications.services.extract_alias_from_headers")
    @patch("apps.communications.services.extract_email_body")
    @patch("apps.communications.services.call_qwen_ai")
    @patch("apps.communications.services.parse_ai_response")
    def test_email_no_zendesk_match(
        self, mock_parse_ai, mock_call_ai, mock_extract_body,
        mock_extract_alias, mock_extract_from, mock_match_alias, mock_settings
    ):
        """Test email processing when no Zendesk match found."""
        mock_settings_instance = Mock()
        mock_settings_instance.email_domain = "mydomain.com"
        mock_settings.return_value = mock_settings_instance

        mock_extract_from.return_value = "sender@example.com"
        mock_extract_alias.return_value = "client-123@mydomain.com"
        mock_extract_body.return_value = "Email body"
        mock_match_alias.return_value = None  # No match

        mock_call_ai.return_value = {"raw_response": '{"summary": "Test"}'}
        mock_parse_ai.return_value = {
            "summary": "Test",
            "category": "GENERAL_CORRESPONDENCE",
            "action_required": False,
            "auto_resolvable": False,
        }

        mock_imap = Mock()

        result = process_single_email(
            imap_conn=mock_imap,
            uid="123",
            msg_data=b"fake data",
            ai_prompt="Test",
        )

        assert result is not None
        assert result.zd_ticket_id == ""
        assert result.claim is None

    @patch("apps.communications.services.extract_from_email")
    def test_email_no_sender(self, mock_extract_from):
        """Test email processing when sender cannot be extracted."""
        mock_extract_from.return_value = None

        mock_imap = Mock()

        result = process_single_email(
            imap_conn=mock_imap,
            uid="123",
            msg_data=b"fake data",
            ai_prompt="Test",
        )

        assert result is None

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.extract_from_email")
    @patch("apps.communications.services.extract_alias_from_headers")
    @patch("apps.communications.services.extract_email_body")
    @patch("apps.communications.services.call_qwen_ai")
    @patch("apps.communications.services.parse_ai_response")
    def test_auto_resolved_email(
        self, mock_parse_ai, mock_call_ai, mock_extract_body,
        mock_extract_alias, mock_extract_from, mock_settings
    ):
        """Test email that is auto-resolved."""
        mock_settings_instance = Mock()
        mock_settings_instance.email_domain = "mydomain.com"
        mock_settings.return_value = mock_settings_instance
        # Backlog-import branch is off here (production default); this test is
        # about auto-resolve, not the on-demand claim import.
        mock_settings.get_instance.return_value.import_claims_from_email = False

        mock_extract_from.return_value = "sender@example.com"
        mock_extract_alias.return_value = "client-123@mydomain.com"
        mock_extract_body.return_value = "Email body"

        # call_qwen_ai now returns structured fields directly (post-AIClient
        # migration). process_single_email reads category/auto_resolvable from
        # this dict, not from parse_ai_response.
        mock_call_ai.return_value = {
            "summary": "Test",
            "category": "SUBMISSION_CONFIRMATION",  # an AUTO_RESOLVABLE_CATEGORY
            "action_required": False,
            "auto_resolvable": True,
        }

        mock_imap = Mock()
        mock_match_alias = patch("apps.communications.services.match_alias_to_zendesk_ticket")
        mock_match_alias.start().return_value = {"id": "12345"}

        result = process_single_email(
            imap_conn=mock_imap,
            uid="123",
            msg_data=b"fake data",
            ai_prompt="Test",
        )

        mock_match_alias.stop()

        assert result is not None
        assert result.auto_resolved is True
        # Should mark email as seen
        mock_imap.store.assert_called()


@pytest.mark.django_db
class TestProcessIncomingEmails:
    """Tests for process_incoming_emails function."""

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.imaplib")
    def test_no_imap_credentials(self, mock_imaplib, mock_settings_class):
        """Test when IMAP credentials are not configured."""
        mock_settings = Mock()
        mock_settings.imap_host = ""
        mock_settings.imap_user = ""
        mock_settings.imap_pass = ""
        mock_settings.email_analysis_prompt = "Test prompt"
        mock_settings.email_domain = ""
        mock_settings_class.get_instance.return_value = mock_settings

        result = process_incoming_emails()

        assert result["errors"] == 1
        assert result["processed"] == 0

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.imaplib")
    def test_no_unseen_emails(self, mock_imaplib_class, mock_settings_class):
        """Test when there are no unseen emails."""
        mock_settings = Mock()
        mock_settings.imap_host = "imap.test.com"
        mock_settings.imap_user = "user@test.com"
        mock_settings.imap_pass = "password"
        mock_settings.email_analysis_prompt = "Test prompt"
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        # Mock IMAP connection
        mock_imap = Mock()
        mock_imaplib_class.IMAP4_SSL.return_value = mock_imap
        mock_imap.search.return_value = ("OK", [b""])

        result = process_incoming_emails()

        assert result["processed"] == 0
        assert result["errors"] == 0

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.imaplib")
    @patch("apps.communications.services.process_single_email")
    def test_process_multiple_emails(
        self, mock_process_single, mock_imaplib_class, mock_settings_class
    ):
        """Test processing multiple emails."""
        mock_settings = Mock()
        mock_settings.imap_host = "imap.test.com"
        mock_settings.imap_user = "user@test.com"
        mock_settings.imap_pass = "password"
        mock_settings.email_analysis_prompt = "Test prompt"
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        # Mock IMAP connection
        mock_imap = Mock()
        mock_imap.select.return_value = ("OK", [])
        mock_imap.login.return_value = ("OK", [])
        mock_imap.close.return_value = ("OK", [])
        mock_imap.logout.return_value = ("OK", [])
        mock_imaplib_class.IMAP4_SSL.return_value = mock_imap
        mock_imap.search.return_value = ("OK", [b"1 2 3"])
        
        # Mock fetch to return proper tuple structure
        mock_imap.fetch.return_value = ("OK", [(b"", b"email data")])

        # Mock process_single_email to return truthy result
        mock_result = Mock()
        mock_result.auto_resolved = False
        mock_process_single.return_value = mock_result

        result = process_incoming_emails()

        # processed is incremented for each UID
        assert result["processed"] == 3
        # matched is incremented when process_single_email returns truthy result
        assert result["matched"] == 3
        # process_single_email is called for each UID
        assert mock_process_single.call_count == 3

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.imaplib")
    def test_imap_connection_error(self, mock_imaplib_class, mock_settings_class):
        """Test IMAP connection error."""
        mock_settings = Mock()
        mock_settings.imap_host = "imap.test.com"
        mock_settings.imap_user = "user@test.com"
        mock_settings.imap_pass = "password"
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        mock_imaplib_class.IMAP4_SSL.side_effect = Exception("Connection failed")

        result = process_incoming_emails()

        # Connection error is caught and logged, error count is incremented
        assert result["errors"] == 1
        assert result["processed"] == 0

    @patch("apps.communications.services.SystemSettings")
    @patch("apps.communications.services.imaplib")
    def test_search_error(self, mock_imaplib_class, mock_settings_class):
        """Test IMAP search error."""
        mock_settings = Mock()
        mock_settings.imap_host = "imap.test.com"
        mock_settings.imap_user = "user@test.com"
        mock_settings.imap_pass = "password"
        mock_settings.email_domain = "mydomain.com"
        mock_settings_class.get_instance.return_value = mock_settings

        mock_imap = Mock()
        mock_imaplib_class.IMAP4_SSL.return_value = mock_imap
        mock_imap.search.return_value = ("BAD", None)

        result = process_incoming_emails()

        assert result["processed"] == 0


@pytest.mark.django_db
class TestAutoResolvableCategories:
    """Tests for AUTO_RESOLVABLE_CATEGORIES constant."""

    def test_contains_expected_categories(self):
        """Test that auto-resolvable categories are defined."""
        assert "SUBMISSION_CONFIRMATION" in AUTO_RESOLVABLE_CATEGORIES
        assert "OBJECT_NOT_FOUND" in AUTO_RESOLVABLE_CATEGORIES

    def test_is_list(self):
        """Test that AUTO_RESOLVABLE_CATEGORIES is a list."""
        assert isinstance(AUTO_RESOLVABLE_CATEGORIES, list)
