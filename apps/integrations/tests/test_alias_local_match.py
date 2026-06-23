"""Inbound emails match aliases against LORA's own index (Claim.email_alias), because
Zendesk's search doesn't reliably match email-valued custom fields. sync_claim_aliases
populates that index from each ticket."""
import pytest
from unittest.mock import patch
from apps.claims.models import Claim
from apps.integrations.services import match_alias_to_zendesk_ticket, sync_claim_aliases

ALIAS_FIELD = 13606076120860


@pytest.mark.django_db
def test_match_uses_local_index_and_skips_zendesk_search():
    Claim.objects.create(client_email='c@e.com', alf_claim_id='ALFL1', zd_ticket_id='53859',
                         email_alias='locmatch1@superwebmail.com')
    with patch('apps.integrations.services.fetch_zendesk_ticket',
               return_value={'id': '53859', 'subject': 'x'}) as fetch, \
         patch('apps.integrations.services.search_zendesk_tickets') as search:
        ticket = match_alias_to_zendesk_ticket('locmatch1@superwebmail.com')
    assert ticket['id'] == '53859'
    fetch.assert_called_once_with('53859')      # fetched by id — reliable
    search.assert_not_called()                  # the flaky search is never used


@pytest.mark.django_db
def test_match_local_is_case_insensitive():
    Claim.objects.create(client_email='c@e.com', alf_claim_id='ALFL2', zd_ticket_id='999',
                         email_alias='locmatch2@superwebmail.com')
    with patch('apps.integrations.services.fetch_zendesk_ticket', return_value={'id': '999'}), \
         patch('apps.integrations.services.search_zendesk_tickets') as search:
        ticket = match_alias_to_zendesk_ticket('LocMatch2@SuperWebMail.com')
    assert ticket['id'] == '999'
    search.assert_not_called()


@pytest.mark.django_db
def test_match_falls_back_to_search_when_not_in_local_index():
    with patch('apps.integrations.services.search_zendesk_tickets',
               return_value=[{'id': '777'}]) as search:
        ticket = match_alias_to_zendesk_ticket('nobody-locmatch3@alias.com')
    assert ticket['id'] == '777'
    search.assert_called_once()


@pytest.mark.django_db
def test_sync_populates_email_alias_from_ticket_field():
    claim = Claim.objects.create(client_email='c@e.com', alf_claim_id='ALFL4',
                                 zd_ticket_id='53859', email_alias='')
    fake = {'id': '53859',
            'custom_fields': [{'id': ALIAS_FIELD, 'value': 'SyncMe@superwebmail.com'}]}
    with patch('apps.integrations.services.fetch_zendesk_ticket', return_value=fake):
        result = sync_claim_aliases(only_missing=True, limit=1)
    claim.refresh_from_db()
    assert claim.email_alias == 'syncme@superwebmail.com'   # stored normalized (lowercased)
    assert result['updated'] == 1
