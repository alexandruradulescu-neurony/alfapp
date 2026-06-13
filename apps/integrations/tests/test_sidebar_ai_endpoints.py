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
                         client_email='c@example.com', status='Claim submitted')

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
    # New contract (2026-06-13): summary mode returns the stored claim summary
    # (single source of truth); next steps are fetched on demand via
    # mode='next_steps', so they are empty here. The first open with no stored
    # summary generates and persists it.
    assert 'Bag lost' in resp.data['summary']
    assert resp.data['next_steps'] == []
    assert resp.data['facts']['status'] == 'Claim submitted'
    assert resp.data['stored'] is True
    from apps.claims.models import Claim as _C
    assert 'Bag lost' in _C.objects.get(zd_ticket_id='70001').ai_summary


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
                         client_email='c@example.com', status='Claim submitted')
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


@pytest.mark.django_db
def test_chat_requires_auth(api_client, settings_obj):
    resp = api_client.post(reverse('zendesk-sidebar-chat'),
                           data={'ticket_id': '70001', 'message': 'status?'}, format='json')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_chat_answers_scoped_to_claim(api_client, settings_obj):
    Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                         client_email='c@example.com', status='Claim submitted')

    with patch('apps.agent.services.AgentChatService.process_message') as mock_pm:
        mock_pm.return_value = MagicMock(answer='Status is Searching.', sources=['claim'])
        resp = api_client.post(
            reverse('zendesk-sidebar-chat'),
            data={'ticket_id': '70001', 'message': 'what is the status?', 'history': []},
            format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )

    assert resp.status_code == 200
    assert resp.data['answer'] == 'Status is Searching.'
    kwargs = mock_pm.call_args.kwargs
    assert kwargs.get('claim_ids') == ['ALF7000001']


@pytest.mark.django_db
def test_chat_no_claim_returns_friendly_message(api_client, settings_obj):
    resp = api_client.post(
        reverse('zendesk-sidebar-chat'),
        data={'ticket_id': '88888', 'message': 'status?'}, format='json',
        HTTP_AUTHORIZATION=f'Bearer {SECRET}',
    )
    assert resp.status_code == 200
    assert 'no lora claim' in resp.data['answer'].lower()


@pytest.mark.django_db
def test_chat_no_claim_with_ticket_content_answers_from_ticket(api_client, settings_obj):
    """An unlinked ticket should still get AI answers based on the ticket
    content the app sends (subject/description/comments)."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"answer": "The requester reports a lost black bag on UA123.", '
                '"sources": ["zendesk"]}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-chat'),
            data={
                'ticket_id': '88888', 'message': 'what was lost?', 'history': [],
                'subject': 'Lost item enquiry',
                'description': 'I lost my black bag on UA123',
                'comments': ['Airline says not located yet'],
            },
            format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert 'black bag' in resp.data['answer']
    assert resp.data['sources'] == ['zendesk']


@pytest.mark.django_db
def test_chat_no_claim_tokenizes_ticket_pii_before_ai(api_client, settings_obj):
    """Ticket content is untrusted: client PII must be tokenized before the
    AI provider sees it, exactly like the briefing endpoint."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"answer":"a","sources":[]}'))],
        )
        api_client.post(
            reverse('zendesk-sidebar-chat'),
            data={
                'ticket_id': '88888', 'message': 'who is the contact?',
                'subject': 'Lost item',
                'description': 'Reach me at alice@example.com',
                'comments': [],
            },
            format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert 'alice@example.com' not in user_content
        assert '<EMAIL_' in user_content


@pytest.mark.django_db
def test_briefing_sends_dated_comments_and_creation_date(api_client, settings_obj):
    """Dated/attributed comments and the ticket creation date must reach the AI
    so it can reason about chronology and 'when was this submitted'."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"summary":"s","next_steps":[]}'))],
        )
        body = {
            'ticket_id': '70002',
            'subject': 'Lost wallet',
            'description': 'Left at TSA',
            'ticket_created_at': '2026-05-16T20:59:00Z',
            'comments': [
                {'author': 'Gaby Smith', 'created_at': '2026-05-17T20:36:00Z',
                 'public': True, 'text': 'We located the item at the airport office'},
            ],
        }
        api_client.post(reverse('zendesk-sidebar-briefing'), data=body, format='json',
                        HTTP_AUTHORIZATION=f'Bearer {SECRET}')
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert '2026-05-16T20:59:00Z' in user_content
        assert '[2026-05-17T20:36:00Z | Gaby Smith | public]' in user_content


@pytest.mark.django_db
def test_briefing_tokenizes_client_name(api_client, settings_obj):
    """The requester's real name must never reach the AI provider."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"summary":"s","next_steps":[]}'))],
        )
        body = _briefing_body()
        body['requester_name'] = 'Alice Wonder'
        body['description'] = 'The wallet belongs to Alice Wonder.'
        api_client.post(reverse('zendesk-sidebar-briefing'), data=body, format='json',
                        HTTP_AUTHORIZATION=f'Bearer {SECRET}')
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert 'Alice Wonder' not in user_content
        assert '<NAME_' in user_content


@pytest.mark.django_db
def test_briefing_next_steps_mode(api_client, settings_obj):
    """mode='next_steps' generates only next steps, on demand."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"next_steps": ["Call MCO lost and found about item 526-3047"]}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-briefing'),
            data={**_briefing_body(), 'mode': 'next_steps'}, format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert resp.data['next_steps'] == ['Call MCO lost and found about item 526-3047']


@pytest.mark.django_db
def test_chat_no_claim_tokenizes_client_name(api_client, settings_obj):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"answer":"a","sources":[]}'))],
        )
        api_client.post(
            reverse('zendesk-sidebar-chat'),
            data={'ticket_id': '88888', 'message': 'who owns it?',
                  'requester_name': 'Alice Wonder',
                  'subject': 'Lost wallet',
                  'description': 'Wallet of Alice Wonder found at gate.',
                  'comments': []},
            format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert 'Alice Wonder' not in user_content
        assert '<NAME_' in user_content


# --- draft endpoint ---

@pytest.mark.django_db
def test_draft_requires_auth(api_client, settings_obj):
    resp = api_client.post(reverse('zendesk-sidebar-draft'),
                           data={'ticket_id': '70001', 'draft_type': 'client_update'},
                           format='json')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_draft_rejects_unknown_type(api_client, settings_obj):
    resp = api_client.post(
        reverse('zendesk-sidebar-draft'),
        data={**_briefing_body(), 'draft_type': 'love_letter'}, format='json',
        HTTP_AUTHORIZATION=f'Bearer {SECRET}',
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_draft_client_update_returns_body(api_client, settings_obj):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"body": "Dear client, good news: your wallet was located and is being shipped."}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-draft'),
            data={**_briefing_body(), 'draft_type': 'client_update'}, format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert 'Dear client' in resp.data['body']


@pytest.mark.django_db
def test_draft_institution_reply_returns_body(api_client, settings_obj):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"body": "Hello, following up on item 526-3047 — any update on the shipment?"}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-draft'),
            data={**_briefing_body(), 'draft_type': 'institution_reply'}, format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert '526-3047' in resp.data['body']


@pytest.mark.django_db
def test_draft_tokenizes_pii_before_ai(api_client, settings_obj):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"body":"b"}'))],
        )
        body = _briefing_body()
        body['draft_type'] = 'client_update'
        body['requester_name'] = 'Alice Wonder'
        body['description'] = 'Alice Wonder can be reached at alice@example.com'
        api_client.post(reverse('zendesk-sidebar-draft'), data=body, format='json',
                        HTTP_AUTHORIZATION=f'Bearer {SECRET}')
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert 'alice@example.com' not in user_content
        assert 'Alice Wonder' not in user_content


# --- needs-attention block ---

@pytest.mark.django_db
def test_briefing_returns_attention_list(api_client, settings_obj):
    from apps.communications.models import EmailLog
    claim = Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                                 client_email='c@example.com', status='Claim submitted')
    EmailLog.objects.create(claim=claim, subject='Airport needs more details',
                            body='', category='UNKNOWN',
                            action_required=True, auto_resolved=False)
    EmailLog.objects.create(claim=claim, subject='Auto ack', body='',
                            category='SUBMISSION_CONFIRMATION',
                            action_required=False, auto_resolved=True)

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"summary":"s","next_steps":[]}'))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-briefing'), data=_briefing_body(), format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert len(resp.data['attention']) == 1
    assert resp.data['attention'][0]['subject'] == 'Airport needs more details'
    assert 'date' in resp.data['attention'][0]
