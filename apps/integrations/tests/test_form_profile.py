import pytest
from unittest.mock import patch
from apps.claims.models import Claim
from apps.integrations.form_profile import (
    FormProfile, build_form_profile, profile_to_secrets_and_facts)


@pytest.mark.django_db
def test_build_form_profile_returns_real_values_via_untokenize():
    claim = Claim.objects.create(
        client_email='real@e.com', client_name='Bronach Brother',
        email_alias='alias-9@mailapptoday.com', phone='+1 555 0100',
        object_description='green Eagle Creek suitcase', alf_claim_id='ALF9',
        zd_ticket_id='55')
    ticket_data = {'subject': 'ALF9', 'description': 'lost suitcase',
                   'comments': [{'author': 'client', 'public': True,
                                 'text': 'Baggage tag: 0081234567, confirmation 3BMD36'}]}
    # AIClient.complete is the boundary: it returns an ALREADY-untokenized FormProfile.
    fake = FormProfile(first_name='Bronach', last_name='Brother',
                       email_alias='alias-9@mailapptoday.com', item_type='Suitcase',
                       baggage_tag='0081234567', booking_confirmation='3BMD36',
                       airport='Newark Liberty / EWR')
    with patch('apps.integrations.form_profile.AIClient') as Client:
        Client.return_value.complete.return_value = fake
        profile = build_form_profile(claim, ticket_data)
    assert profile.baggage_tag == '0081234567'        # recovered ID, not a <PHONE_..> mask
    assert profile.booking_confirmation == '3BMD36'
    assert profile.item_type == 'Suitcase'
    kwargs = Client.return_value.complete.call_args.kwargs
    assert kwargs['response_schema'] is FormProfile
    assert not kwargs['call_site'].startswith('dispute')   # => DeepSeek, not Anthropic


@pytest.mark.django_db
def test_build_form_profile_returns_none_on_ai_failure():
    claim = Claim.objects.create(client_email='c@e.com', alf_claim_id='ALF1', zd_ticket_id='55')
    ticket_data = {'description': 'lost a bag', 'comments': []}   # non-empty => reaches the AI call
    with patch('apps.integrations.form_profile.AIClient') as Client:
        Client.return_value.complete.side_effect = RuntimeError('boom')
        assert build_form_profile(claim, ticket_data) is None     # caller falls back


def test_profile_split_keeps_pii_in_secrets_and_facts_clean():
    p = FormProfile(first_name='Bronach', last_name='Brother',
                    email_alias='alias-9@mailapptoday.com', phone='+15550100',
                    item_type='Suitcase', item_description='green Eagle Creek bag, tag "Bronach"',
                    airport='Newark Liberty / EWR', state='IL', baggage_tag='0081234567',
                    street='7509 N Pecatonica Rd')
    secrets, facts = profile_to_secrets_and_facts(p)
    # PII / IDs / free-text -> secrets
    assert secrets['x_client_first_name'] == 'Bronach'
    assert secrets['x_client_email'] == 'alias-9@mailapptoday.com'
    assert secrets['x_baggage_tag'] == '0081234567'
    assert secrets['x_item_description'].startswith('green Eagle Creek')
    assert secrets['x_street_address'] == '7509 N Pecatonica Rd'
    # non-PII dropdown facts -> visible
    assert facts['Item type'] == 'Suitcase'
    assert facts['Airport'] == 'Newark Liberty / EWR'
    assert facts['State'] == 'IL'
    # the personal values must NOT appear in the visible facts
    blob = ' '.join(facts.values())
    assert 'Bronach' not in blob and 'alias-9@mailapptoday.com' not in blob and '0081234567' not in blob


def test_profile_split_drops_unresolved_mask_tokens_from_secrets():
    p = FormProfile(first_name='<NAME_abc123>', phone='+15550100')   # an untokenize miss
    secrets, _ = profile_to_secrets_and_facts(p)
    assert 'x_client_first_name' not in secrets        # never send a <...> token to the form
    assert secrets['x_client_phone'] == '+15550100'
