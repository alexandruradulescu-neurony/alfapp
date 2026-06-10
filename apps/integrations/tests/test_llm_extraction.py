"""
Tests for ALF claim ID parsing from Zendesk ticket subjects.

NOTE: The former TestAnalyzeZendeskTicketForClaim class was removed on
2026-06-10. It tested the pre-migration behavior where
analyze_zendesk_ticket_for_claim parsed LLM JSON directly to extract email,
phone, flight, etc. That function now reads structured Zendesk custom fields
first and only uses the LLM for the free-text description; its current
behavior is covered comprehensively in
apps/integrations/tests/test_zendesk_services.py
(TestAnalyzeZendeskTicketForClaim + TestStructuredFieldComposition).
"""

from apps.integrations.services import parse_alf_claim_id_from_subject


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
