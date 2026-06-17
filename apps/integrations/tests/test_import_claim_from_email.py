"""Tests for the on-demand 'import claim from Zendesk' backlog-transition helper.

Covers:
  * import_claim_from_zendesk_ticket() — the function that mirrors an EXISTING
    Zendesk claim into LORA when an inbound email matches a ticket LORA hasn't
    imported yet (it never fabricates a claim).
  * the email-path wiring in process_single_email — the new branch only fires
    when the claim is missing AND the import_claims_from_email switch is ON.
"""

import pytest
from unittest.mock import patch, Mock

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.communications.services import process_single_email
from apps.integrations.services import import_claim_from_zendesk_ticket


def _extracted(**overrides):
    """Complete extracted-data dict (mirrors the webhook's _full_extracted_data)."""
    base = {
        'client_email': 'customer@example.com', 'client_name': '', 'flight_details': '',
        'object_description': '', 'phone': '', 'alternate_email': '', 'claim_number': '',
        'billing_address': '', 'shipping_address': '', 'incident_details': '',
        'lost_location': '', 'deadline_date': '', 'deadline_time': '', 'deadline_timezone': '',
        'price_paid': '', 'payment_method': '', 'payment_status': '', 'woocommerce_id': '',
        'tracking_info': '',
    }
    base.update(overrides)
    return base


@pytest.mark.django_db
class TestImportClaimFromZendeskTicket:
    """Unit tests for import_claim_from_zendesk_ticket()."""

    def test_imports_claim_and_mirrors_current_status(self):
        """A backlog ticket is imported at its CURRENT custom status, not reset."""
        ticket = {
            'id': '777', 'subject': 'Lost bag - ALF7777777',
            'requester_id': 555, 'custom_fields': [], 'custom_status_id': '999',
        }
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value=_extracted(client_email='c@example.com',
                                            object_description='Black bag')), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject',
                   return_value='ALF7777777'), \
             patch('apps.integrations.services.resolve_custom_status',
                   return_value={'name': 'Awaiting airport reply', 'category': 'pending'}), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):
            claim, created = import_claim_from_zendesk_ticket('777')

        assert created is True
        assert claim is not None
        assert claim.zd_ticket_id == '777'
        assert claim.alf_claim_id == 'ALF7777777'
        assert claim.client_email == 'c@example.com'
        assert claim.object_description == 'Black bag'
        # Status mirrored from the ticket's CURRENT custom status (not reset to
        # 'Investigation initiated').
        assert claim.status == 'Awaiting airport reply'
        assert claim.status_category == 'pending'
        assert Claim.objects.filter(zd_ticket_id='777').count() == 1

    def test_status_falls_back_when_custom_status_unresolved(self):
        """An unresolved status id must never be stored as the claim status."""
        ticket = {
            'id': '778', 'subject': 'Lost coat - ALF7778888',
            'requester_id': 1, 'custom_fields': [], 'custom_status_id': '12345',
        }
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=ticket), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.services.analyze_zendesk_ticket_for_claim',
                   return_value=_extracted(client_email='c@example.com')), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject',
                   return_value='ALF7778888'), \
             patch('apps.integrations.services.resolve_custom_status',
                   return_value={'name': '12345', 'category': ''}), \
             patch('apps.integrations.briefing.refresh_claim_summary', return_value=False):
            claim, created = import_claim_from_zendesk_ticket('778')

        assert created is True
        assert claim.status == 'Investigation initiated'  # safe named fallback
        assert claim.status != '12345'

    def test_skips_ticket_without_alf_claim_number(self):
        """A non-claim ticket (phone/email auto-ticket) is never imported."""
        ticket = {'id': '779', 'subject': 'Re: random', 'custom_fields': []}
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=ticket), \
             patch('apps.integrations.services.parse_alf_claim_id_from_subject', return_value=''), \
             patch('apps.integrations.services._get_custom_field_value', return_value=''):
            claim, created = import_claim_from_zendesk_ticket('779')

        assert claim is None
        assert created is False
        assert not Claim.objects.filter(zd_ticket_id='779').exists()

    def test_returns_existing_without_refetching(self):
        """If the claim already exists, return it and never touch Zendesk/AI."""
        existing = Claim.objects.create(
            alf_claim_id='ALF0000001', zd_ticket_id='780', client_email='x@example.com',
            status='Investigation initiated', status_category='open')
        with patch('apps.integrations.services.fetch_zendesk_ticket') as mock_fetch:
            claim, created = import_claim_from_zendesk_ticket('780')

        assert claim == existing
        assert created is False
        mock_fetch.assert_not_called()

    def test_returns_none_when_ticket_cannot_be_fetched(self):
        """Zendesk unreachable → (None, False), no claim, no crash."""
        with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=None):
            claim, created = import_claim_from_zendesk_ticket('781')

        assert claim is None
        assert created is False


@pytest.mark.django_db
class TestEmailPathImportWiring:
    """The new branch in process_single_email is gated by the switch."""

    def _run(self, flag_on, import_side_effect):
        """Drive process_single_email with an alias that matches ticket 888 and
        NO local claim, with import_claims_from_email = flag_on. The import
        function is mocked with the given side_effect (a callable or None)."""
        with patch('apps.communications.services.SystemSettings') as mock_ss, \
             patch('apps.communications.services.match_alias_to_zendesk_ticket',
                   return_value={'id': '888'}), \
             patch('apps.communications.services.extract_from_email',
                   return_value='sender@airport.com'), \
             patch('apps.communications.services.extract_alias_from_headers',
                   return_value='alias-888@mydomain.com'), \
             patch('apps.communications.services.extract_email_body', return_value='Body'), \
             patch('apps.communications.services.decode_mime_header', return_value='Subject'), \
             patch('apps.communications.services.call_qwen_ai',
                   return_value={'summary': 'S', 'category': 'GENERAL_CORRESPONDENCE',
                                 'action_required': False, 'auto_resolvable': False}), \
             patch('apps.communications.services.post_ai_summary_to_zendesk', return_value=True), \
             patch('apps.integrations.services.import_claim_from_zendesk_ticket',
                   side_effect=import_side_effect) as mock_import:
            mock_ss.get_instance.return_value.import_claims_from_email = flag_on
            result = process_single_email(
                imap_conn=Mock(), uid='1', msg_data=b'raw',
                ai_prompt='P')
        return result, mock_import

    def test_imports_when_flag_on(self):
        """No local claim + flag ON → import is called and its claim is used.

        The lookup must MISS first, so the import side-effect is what actually
        creates the claim (mirroring the real on-demand import)."""
        def fake_import(ticket_id):
            claim = Claim.objects.create(
                alf_claim_id='ALF0000888', zd_ticket_id=ticket_id,
                client_email='c@example.com', status='Investigation initiated',
                status_category='open')
            return claim, True

        result, mock_import = self._run(flag_on=True, import_side_effect=fake_import)

        assert isinstance(result, EmailLog)
        mock_import.assert_called_once_with('888')
        assert result.claim is not None
        assert result.claim.zd_ticket_id == '888'

    def test_does_not_import_when_flag_off(self):
        result, mock_import = self._run(flag_on=False,
                                        import_side_effect=lambda t: (None, False))

        assert isinstance(result, EmailLog)
        mock_import.assert_not_called()
        assert result.claim is None
