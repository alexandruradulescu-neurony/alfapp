import pytest
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.config.models import SystemSettings

SECRET = 'sidebar-secret-xyz'


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def settings_obj(db):
    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={})
    ss.sidebar_secret_token = SECRET
    ss.ai_api_key = 'test'
    ss.ai_api_base = 'https://api.example.com/v1'
    ss.ai_api_model = 'test-model'
    ss.pii_tokenization_salt = 'salt-long-enough-for-real-use'
    ss.save()
    return ss


def _briefing_body(ticket_id='70001'):
    return {
        'ticket_id': ticket_id,
        'requester_email': 'c@example.com',
        'subject': 'Lost item ALF7000001',
        'description': 'I lost my black bag on UA123',
        'comments': ['Airline says not located yet'],
    }


@pytest.mark.django_db
def test_briefing_requires_auth(api_client, settings_obj):
    resp = api_client.post(reverse('zendesk-sidebar-briefing'),
                           data=_briefing_body(), format='json')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_briefing_returns_summary_next_steps_and_facts(api_client, settings_obj):
    Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                         client_email='c@example.com', status='Searching')

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"summary": "Bag lost on UA123, searching.", '
                '"next_steps": ["Chase airport"]}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-briefing'), data=_briefing_body(), format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )

    assert resp.status_code == 200
    assert 'Bag lost' in resp.data['summary']
    assert resp.data['next_steps'] == ['Chase airport']
    assert resp.data['facts']['status'] == 'Searching'


@pytest.mark.django_db
def test_briefing_graceful_when_no_claim(api_client, settings_obj):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"summary": "No linked claim; based on ticket only.", "next_steps": []}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-briefing'),
            data=_briefing_body(ticket_id='99999'), format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert resp.data['facts'] == {}


@pytest.mark.django_db
def test_briefing_tokenizes_pii_before_ai(api_client, settings_obj):
    Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                         client_email='c@example.com', status='Searching')
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"summary":"s","next_steps":[]}'))],
        )
        body = _briefing_body()
        body['description'] = 'Reach me at alice@example.com'
        api_client.post(reverse('zendesk-sidebar-briefing'), data=body, format='json',
                        HTTP_AUTHORIZATION=f'Bearer {SECRET}')
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert 'alice@example.com' not in user_content
        assert '<EMAIL_' in user_content
