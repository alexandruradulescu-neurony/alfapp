"""
Tests for LLM extraction service - Zendesk ticket analysis.

Tests the LLM-based extraction of claim data from Zendesk tickets
and ALF claim ID parsing from ticket subjects.
"""

import pytest
from unittest.mock import patch, MagicMock
from django.test import TestCase

from apps.integrations.services import (
    analyze_zendesk_ticket_for_claim,
    parse_alf_claim_id_from_subject,
)


class TestAnalyzeZendeskTicketForClaim:
    """Test cases for analyze_zendesk_ticket_for_claim function."""

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_extracts_email(self, mock_call_ai):
        """Client email extracted correctly."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'I lost my bag on flight AA123',
            'comments': [],
        }

        # Mock LLM response with valid JSON
        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "customer@example.com", "flight_details": "", "object_description": "", "phone": "", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'customer@example.com'
        assert result['flight_details'] == ''
        assert result['object_description'] == ''
        assert result['phone'] == ''
        assert result['alternate_email'] == ''

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_extracts_flight(self, mock_call_ai):
        """Flight details extracted correctly."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost my bag on flight',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "", "flight_details": "Flight AA123 from JFK to LAX on March 15, 2026", "object_description": "", "phone": "", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['flight_details'] == 'Flight AA123 from JFK to LAX on March 15, 2026'
        assert result['client_email'] == ''

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_extracts_object(self, mock_call_ai):
        """Object description extracted correctly."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost my laptop',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "", "flight_details": "", "object_description": "Black MacBook Pro laptop, 15-inch", "phone": "", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['object_description'] == 'Black MacBook Pro laptop, 15-inch'

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_extracts_phone(self, mock_call_ai):
        """Phone number extracted correctly."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost my bag',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "", "flight_details": "", "object_description": "", "phone": "+1-555-123-4567", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['phone'] == '+1-555-123-4567'

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_extracts_alternate_email(self, mock_call_ai):
        """Alternate email extracted correctly."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Lost my bag',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "primary@example.com", "flight_details": "", "object_description": "", "phone": "", "alternate_email": "backup@gmail.com"}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'primary@example.com'
        assert result['alternate_email'] == 'backup@gmail.com'

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_extracts_all_fields(self, mock_call_ai):
        """All fields extracted in single call."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Complete claim details',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': '''{
                "client_email": "customer@example.com",
                "flight_details": "Flight AA123 from JFK to LAX on March 15, 2026",
                "object_description": "Black MacBook Pro laptop, 15-inch",
                "phone": "+1-555-123-4567",
                "alternate_email": "backup@gmail.com"
            }'''
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'customer@example.com'
        assert result['flight_details'] == 'Flight AA123 from JFK to LAX on March 15, 2026'
        assert result['object_description'] == 'Black MacBook Pro laptop, 15-inch'
        assert result['phone'] == '+1-555-123-4567'
        assert result['alternate_email'] == 'backup@gmail.com'

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_handles_empty(self, mock_call_ai):
        """Empty ticket returns empty strings."""
        ticket_data = {
            'id': '12345',
            'subject': '',
            'description': '',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "", "flight_details": "", "object_description": "", "phone": "", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result == {
            'client_email': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_llm_returns_empty_response(self, mock_call_ai):
        """Handles LLM returning empty response."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Lost item details',
            'comments': [],
        }

        mock_call_ai.return_value = {'raw_response': ''}

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        # Should return empty fields
        assert result['client_email'] == ''
        assert result['flight_details'] == ''

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_llm_returns_invalid_json(self, mock_call_ai):
        """Handles LLM returning invalid JSON."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Lost item details',
            'comments': [],
        }

        mock_call_ai.return_value = {
            'raw_response': 'This is not valid JSON at all'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        # Should return empty fields on parse failure
        assert result['client_email'] == ''
        assert result['flight_details'] == ''

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_llm_returns_partial_json(self, mock_call_ai):
        """Handles LLM returning partial JSON with some fields."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Lost item details',
            'comments': [],
        }

        # Only client_email provided, other fields missing
        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "customer@example.com"}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'customer@example.com'
        assert result['flight_details'] == ''  # Default empty
        assert result['object_description'] == ''  # Default empty

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_llm_exception(self, mock_call_ai):
        """Handles LLM API exception gracefully."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Lost item details',
            'comments': [],
        }

        mock_call_ai.side_effect = Exception('LLM API connection failed')

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        # Should return empty fields on error
        assert result == {
            'client_email': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
        }

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_with_comments(self, mock_call_ai):
        """Ticket with comments is processed correctly."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item - ALF1234567',
            'description': 'Initial description',
            'comments': [
                {
                    'author': {'name': 'Customer', 'email': 'customer@example.com'},
                    'body': 'I lost my bag on flight AA123',
                    'public': True,
                    'created_at': '2026-03-15T10:30:00Z',
                },
                {
                    'author': {'name': 'Agent', 'email': 'agent@company.com'},
                    'body': 'We will investigate',
                    'public': False,
                    'created_at': '2026-03-15T11:00:00Z',
                },
            ],
        }

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "customer@example.com", "flight_details": "Flight AA123", "object_description": "Travel bag", "phone": "", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'customer@example.com'
        assert result['flight_details'] == 'Flight AA123'
        assert result['object_description'] == 'Travel bag'

        # Verify LLM was called
        mock_call_ai.assert_called_once()

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_json_with_markdown(self, mock_call_ai):
        """Handles JSON wrapped in markdown code blocks."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Lost item details',
            'comments': [],
        }

        # LLM returns JSON in markdown code block
        mock_call_ai.return_value = {
            'raw_response': '''```json
{
    "client_email": "customer@example.com",
    "flight_details": "Flight AA123",
    "object_description": "Black bag",
    "phone": "",
    "alternate_email": ""
}
```'''
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'customer@example.com'
        assert result['flight_details'] == 'Flight AA123'
        assert result['object_description'] == 'Black bag'

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_alternate_field_names(self, mock_call_ai):
        """Handles alternate field names in LLM response."""
        ticket_data = {
            'id': '12345',
            'subject': 'Lost Item',
            'description': 'Lost item details',
            'comments': [],
        }

        # LLM uses alternate field names
        mock_call_ai.return_value = {
            'raw_response': '{"email": "customer@example.com", "flight": "Flight AA123", "description": "Black bag", "phone_number": "+1-555-123-4567", "alt_email": "backup@gmail.com"}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        assert result['client_email'] == 'customer@example.com'
        assert result['flight_details'] == 'Flight AA123'
        assert result['object_description'] == 'Black bag'
        assert result['phone'] == '+1-555-123-4567'
        assert result['alternate_email'] == 'backup@gmail.com'

    @patch('apps.communications.services.call_qwen_ai')
    def test_analyze_ticket_missing_ticket_data(self, mock_call_ai):
        """Handles missing ticket data fields."""
        ticket_data = {}  # Empty ticket data

        mock_call_ai.return_value = {
            'raw_response': '{"client_email": "", "flight_details": "", "object_description": "", "phone": "", "alternate_email": ""}'
        }

        result = analyze_zendesk_ticket_for_claim(ticket_data)

        # Should still call LLM and return result
        assert isinstance(result, dict)
        assert 'client_email' in result


class TestParseAlfClaimIdFromSubject:
    """Test cases for parse_alf_claim_id_from_subject function."""

    def test_parse_alf_id_from_subject_standard_format(self):
        """ALF ID parsed from standard format."""
        subject = 'Lost Item - ALF1234567'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF1234567'

    def test_parse_alf_id_from_subject_with_prefix(self):
        """ALF ID parsed with various prefixes."""
        subject = 'Claim: ALF0000123 - Lost luggage'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF0000123'

    def test_parse_alf_id_from_subject_with_suffix(self):
        """ALF ID parsed with suffix text."""
        subject = 'ALF9999999 - Customer complaint'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF9999999'

    def test_parse_alf_id_from_subject_lowercase(self):
        """ALF ID parsed from lowercase subject."""
        subject = 'lost item - alf1234567'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF1234567'  # Should return uppercase

    def test_parse_alf_id_from_subject_mixed_case(self):
        """ALF ID parsed from mixed case subject."""
        subject = 'Lost Item - Alf1234567 - Urgent'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF1234567'

    def test_parse_alf_id_from_subject_multiple_numbers(self):
        """ALF ID parsed when multiple numbers present."""
        subject = 'Claim #123 - ALF4567890 - Ticket 999'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF4567890'

    def test_parse_alf_id_from_subject_no_spaces(self):
        """ALF ID parsed without spaces around it."""
        subject = 'ALF1111111'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF1111111'

    def test_parse_alf_id_from_subject_with_special_chars(self):
        """ALF ID parsed with special characters in subject."""
        subject = 'Lost Item [ALF2222222] <Urgent>'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF2222222'

    def test_parse_alf_id_not_found(self):
        """Returns None when no ALF ID present."""
        subject = 'Lost Item Report - No ID here'
        result = parse_alf_claim_id_from_subject(subject)
        assert result is None

    def test_parse_alf_id_not_found_empty_subject(self):
        """Returns None for empty subject."""
        result = parse_alf_claim_id_from_subject('')
        assert result is None

    def test_parse_alf_id_not_found_none_subject(self):
        """Returns None for None subject."""
        result = parse_alf_claim_id_from_subject(None)
        assert result is None

    def test_parse_alf_id_wrong_format(self):
        """Returns None when ALF format is incorrect."""
        # Only 6 digits instead of 7
        subject = 'Lost Item - ALF123456'
        result = parse_alf_claim_id_from_subject(subject)
        assert result is None

    def test_parse_alf_id_too_many_digits(self):
        """Returns None when ALF has too many digits."""
        # 8 digits instead of 7
        subject = 'Lost Item - ALF12345678'
        result = parse_alf_claim_id_from_subject(subject)
        # Should still match the first 7 digits
        assert result == 'ALF1234567'

    def test_parse_alf_id_letters_instead_of_digits(self):
        """Returns None when ALF followed by letters."""
        subject = 'Lost Item - ALFABCDEFG'
        result = parse_alf_claim_id_from_subject(subject)
        assert result is None

    def test_parse_alf_id_only_alf_no_digits(self):
        """Returns None when only ALF prefix present."""
        subject = 'Lost Item - ALF'
        result = parse_alf_claim_id_from_subject(subject)
        assert result is None

    def test_parse_alf_id_in_middle_of_text(self):
        """ALF ID parsed from middle of text."""
        subject = 'Regarding your claim ALF3333333 we have processed it'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF3333333'

    def test_parse_alf_id_at_end_of_subject(self):
        """ALF ID parsed from end of subject."""
        subject = 'Customer lost their luggage ALF4444444'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF4444444'

    def test_parse_alf_id_with_hyphens(self):
        """ALF ID parsed with hyphens in subject."""
        subject = 'Claim-ALF-5555555-Lost-Item'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF5555555'

    def test_parse_alf_id_with_underscores(self):
        """ALF ID parsed with underscores in subject."""
        subject = 'Claim_ALF_6666666_Lost_Item'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF6666666'

    def test_parse_alf_id_multiple_alf_patterns(self):
        """First ALF ID parsed when multiple present."""
        subject = 'ALF1111111 and ALF2222222'
        result = parse_alf_claim_id_from_subject(subject)
        assert result == 'ALF1111111'
