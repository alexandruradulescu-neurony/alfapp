"""Backfill: reprocess_email_logs recovers empty bodies and re-categorizes the suspect
set (empty-body + GENERAL_CORRESPONDENCE + UNKNOWN), re-tagging Zendesk. Idempotent.

Each test scopes reprocess to its own claim (claim_id=) so it's deterministic regardless
of any rows left in the reused test DB."""
import pytest
from unittest.mock import patch, MagicMock
from email.mime.text import MIMEText

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.communications.services import reprocess_email_logs

_SHIPPING = {'summary': 'shipped', 'category': 'SHIPPING_INFORMATION',
             'action_required': True, 'auto_resolvable': False}


def _claim(n):
    return Claim.objects.create(client_email='c@e.com', alf_claim_id=f'ALF{n}', zd_ticket_id='55')


@pytest.mark.django_db
def test_recategorizes_general_correspondence_to_shipping_and_tags():
    claim = _claim(1)
    el = EmailLog.objects.create(
        claim=claim, subject='Your item shipped', body='Tracking 1Z999 via UPS',
        category=EmailLog.CATEGORY_GENERAL_CORRESPONDENCE, action_required=False,
        from_email='x@chargerback.com', zd_ticket_id='55', message_id='<a@x>')
    with patch('apps.communications.services.call_qwen_ai', return_value=_SHIPPING), \
         patch('apps.integrations.services.add_zendesk_ticket_tags') as tag:
        result = reprocess_email_logs(claim_id=claim.id)
    el.refresh_from_db()
    assert el.category == EmailLog.CATEGORY_SHIPPING_INFORMATION
    assert result['recategorized'] == 1 and result['retagged'] == 1
    assert 'ai_shipping_information' in tag.call_args[0][1]      # tag applied to the ticket


@pytest.mark.django_db
def test_recovers_empty_body_then_recategorizes():
    claim = _claim(2)
    el = EmailLog.objects.create(
        claim=claim, subject='Found', body='(No content extracted)',
        category=EmailLog.CATEGORY_GENERAL_CORRESPONDENCE, from_email='x@chargerback.com',
        zd_ticket_id='55', message_id='<b@x>')
    raw = MIMEText('<p>Tracking 1Z999, your bag is on its way</p>', 'html').as_bytes()
    with patch('apps.communications.services.open_inbox'), \
         patch('apps.communications.services.fetch_raw_by_message_id', return_value=raw), \
         patch('apps.communications.services.call_qwen_ai', return_value=_SHIPPING), \
         patch('apps.integrations.services.add_zendesk_ticket_tags'):
        result = reprocess_email_logs(claim_id=claim.id)
    el.refresh_from_db()
    assert 'Tracking 1Z999' in el.body                          # body recovered + extracted
    assert el.category == EmailLog.CATEGORY_SHIPPING_INFORMATION
    assert result['body_recovered'] == 1


@pytest.mark.django_db
def test_empty_body_unrecoverable_when_not_in_mailbox():
    claim = _claim(3)
    EmailLog.objects.create(claim=claim, subject='x', body='(No content extracted)',
                            category=EmailLog.CATEGORY_UNKNOWN, from_email='x@x.com',
                            message_id='<gone@x>')
    with patch('apps.communications.services.open_inbox'), \
         patch('apps.communications.services.fetch_raw_by_message_id', return_value=None), \
         patch('apps.communications.services.call_qwen_ai') as ai:
        result = reprocess_email_logs(claim_id=claim.id)
    assert result['body_unrecoverable'] == 1
    ai.assert_not_called()                                      # no body -> no AI call


@pytest.mark.django_db
def test_dry_run_counts_without_ai_or_imap():
    claim = _claim(4)
    EmailLog.objects.create(claim=claim, subject='a', body='real body',
                            category=EmailLog.CATEGORY_GENERAL_CORRESPONDENCE, from_email='a@x.com')
    EmailLog.objects.create(claim=claim, subject='b', body='(No content extracted)',
                            category=EmailLog.CATEGORY_OBJECT_FOUND, from_email='b@x.com')
    with patch('apps.communications.services.call_qwen_ai') as ai, \
         patch('apps.communications.services.open_inbox') as inbox:
        result = reprocess_email_logs(dry_run=True, claim_id=claim.id)
    assert result['dry_run'] is True
    assert result['examined'] == 2 and result['would_refetch'] == 1
    ai.assert_not_called()
    inbox.assert_not_called()


@pytest.mark.django_db
def test_healthy_meaningful_category_is_left_alone():
    claim = _claim(5)
    el = EmailLog.objects.create(claim=claim, subject='Found it', body='we found your bag',
                                 category=EmailLog.CATEGORY_OBJECT_FOUND, from_email='x@x.com',
                                 zd_ticket_id='55')
    with patch('apps.communications.services.call_qwen_ai') as ai, \
         patch('apps.integrations.services.add_zendesk_ticket_tags'):
        result = reprocess_email_logs(claim_id=claim.id)
    el.refresh_from_db()
    assert el.category == EmailLog.CATEGORY_OBJECT_FOUND        # untouched
    assert result['examined'] == 0                              # not in the suspect set
    ai.assert_not_called()


def test_fetch_raw_by_message_id_quotes_the_search_value():
    """The Message-ID (<...@...>) must be QUOTED in the IMAP search — unquoted, the
    server rejects every search and nothing is ever found (the body_unrecoverable bug)."""
    from apps.communications.services import fetch_raw_by_message_id
    conn = MagicMock()
    conn.search.return_value = ('OK', [b'7'])
    conn.fetch.return_value = ('OK', [(b'7 (BODY[] {5}', b'hello'), b')'])
    raw = fetch_raw_by_message_id(conn, '<abc@def.com>')
    assert raw == b'hello'                                      # happy path returns the raw bytes
    assert conn.search.call_args[0][-1] == '"<abc@def.com>"'    # value is quoted for IMAP


def test_fetch_raw_by_message_id_none_when_not_found():
    from apps.communications.services import fetch_raw_by_message_id
    conn = MagicMock()
    conn.search.return_value = ('OK', [b''])                    # no match
    assert fetch_raw_by_message_id(conn, '<gone@x>') is None
    conn.fetch.assert_not_called()
