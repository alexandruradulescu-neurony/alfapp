"""The client-reply webhook: when the client posts a public reply INSIDE Zendesk,
LORA re-runs the existing summary + risk assessment (no other trigger reacts to
this event). The reply is read live and assessed — nothing is mirrored into the DB.
"""

import pytest
from unittest.mock import patch
from django.urls import reverse
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.integrations.views.webhooks import assess_client_reply


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def system_settings():
    ss, created = SystemSettings.objects.get_or_create(
        pk=1, defaults={'sidebar_secret_token': 'test-webhook-secret'})
    if not created:
        ss.sidebar_secret_token = 'test-webhook-secret'
        ss.save()
    return ss


# --- assess_client_reply (the core) ---------------------------------------

@pytest.mark.django_db
def test_assess_runs_when_latest_public_comment_is_the_client():
    claim = Claim.objects.create(client_email='cust@x.com', client_name='C',
                                 alf_claim_id='ALFCR1', zd_ticket_id='555')
    comments = [
        {'public': True, 'author': {'email': 'agent@alf.com'}, 'body': 'searching'},
        {'public': True, 'author': {'email': 'cust@x.com'},
         'body': 'this is taking forever, I want a refund'},   # newest public = client
    ]
    with patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={'id': '555'}), \
         patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=comments), \
         patch('apps.integrations.views.webhooks.refresh_claim_summary') as mock_refresh:
        result = assess_client_reply(claim)
    mock_refresh.assert_called_once()                  # existing assessment is re-run
    assert result['outcome'] == 'assessed'


@pytest.mark.django_db
def test_assess_skips_when_latest_public_comment_is_an_agent():
    claim = Claim.objects.create(client_email='cust@x.com', alf_claim_id='ALFCR2',
                                 zd_ticket_id='556')
    comments = [
        {'public': True, 'author': {'email': 'cust@x.com'}, 'body': 'hi'},
        {'public': True, 'author': {'email': 'agent@alf.com'}, 'body': 'we are on it'},  # newest = agent
    ]
    with patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={'id': '556'}), \
         patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=comments), \
         patch('apps.integrations.views.webhooks.refresh_claim_summary') as mock_refresh:
        result = assess_client_reply(claim)
    mock_refresh.assert_not_called()                   # not a client reply -> no assessment
    assert result['outcome'] == 'not_client_reply'


# --- the webhook view -----------------------------------------------------

@pytest.mark.django_db
def test_webhook_assesses_a_known_ticket(api_client, system_settings):
    Claim.objects.create(client_email='cust@x.com', alf_claim_id='ALFCR3', zd_ticket_id='777')
    url = reverse('zendesk-client-reply-webhook')
    with patch('apps.integrations.views.webhooks.assess_client_reply',
               return_value={'outcome': 'assessed', 'claim_id': 1}) as mock_assess:
        resp = api_client.post(url, {'ticket_id': '777'}, format='json',
                               HTTP_X_WEBHOOK_SECRET='test-webhook-secret')
    assert resp.status_code == 200
    mock_assess.assert_called_once()


@pytest.mark.django_db
def test_webhook_unknown_ticket_is_noop(api_client, system_settings):
    url = reverse('zendesk-client-reply-webhook')
    with patch('apps.integrations.views.webhooks.assess_client_reply') as mock_assess:
        resp = api_client.post(url, {'ticket_id': '999999'}, format='json',
                               HTTP_X_WEBHOOK_SECRET='test-webhook-secret')
    assert resp.status_code == 200
    mock_assess.assert_not_called()
    assert 'no claim' in resp.json()['message'].lower()


@pytest.mark.django_db
def test_webhook_requires_the_shared_secret(api_client, system_settings):
    url = reverse('zendesk-client-reply-webhook')
    no_secret = api_client.post(url, {'ticket_id': '777'}, format='json')
    wrong_secret = api_client.post(url, {'ticket_id': '777'}, format='json',
                                   HTTP_X_WEBHOOK_SECRET='wrong')
    assert no_secret.status_code == 401
    assert wrong_secret.status_code == 401
