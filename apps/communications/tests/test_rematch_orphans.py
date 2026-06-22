import pytest
from unittest.mock import patch
from django.core.management import call_command
from apps.claims.models import Claim
from apps.communications.models import EmailLog


@pytest.mark.django_db
def test_rematch_routes_orphan_to_ticket():
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='777', alf_claim_id='ALF7')
    el = EmailLog.objects.create(
        subject='Found your bag', body='we found it', ai_summary='found',
        category='OBJECT_FOUND', action_required=True, auto_resolved=False,
        from_email='lf@inst.example', to_email='case-777@alias.io', zd_ticket_id='',
        raw_headers='To: case-777@alias.io\nDelivered-To: claims@airportlostfound.com\n')
    # The command now delegates to recover_orphan_emails in services — patch there.
    with patch('apps.communications.services.find_zendesk_ticket_for_email',
               return_value=({'id': '777'}, 'case-777@alias.io')), \
         patch('apps.communications.services.post_ai_summary_to_zendesk') as note, \
         patch('apps.integrations.services.add_zendesk_ticket_tags', return_value=True):
        call_command('rematch_orphan_emails')
    el.refresh_from_db()
    assert el.zd_ticket_id == '777'
    assert el.claim_id == claim.id
    note.assert_called_once()


@pytest.mark.django_db
def test_rematch_dry_run_changes_nothing():
    el = EmailLog.objects.create(subject='s', body='b', from_email='x@y.com', zd_ticket_id='',
                                 raw_headers='To: a@b.com\n', category='OBJECT_NOT_FOUND')
    # The command now delegates to recover_orphan_emails in services — patch there.
    with patch('apps.communications.services.find_zendesk_ticket_for_email',
               return_value=({'id': '1'}, 'a@b.com')), \
         patch('apps.communications.services.post_ai_summary_to_zendesk') as note:
        call_command('rematch_orphan_emails', '--dry-run')
    assert note.called is False
    el.refresh_from_db()
    assert el.zd_ticket_id == ''   # untouched


# ---------------------------------------------------------------------------
# recover_orphan_emails() service function tests
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_recover_orphan_emails_routes_orphan():
    """recover_orphan_emails routes an orphan EmailLog to its ticket."""
    from apps.communications.services import recover_orphan_emails
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='888', alf_claim_id='ALF8')
    el = EmailLog.objects.create(
        subject='Bag found', body='we have it', ai_summary='found',
        category='OBJECT_FOUND', action_required=True, auto_resolved=False,
        from_email='lf@inst.example', to_email='case-888@alias.io', zd_ticket_id='',
        raw_headers='To: case-888@alias.io\n')
    with patch('apps.communications.services.find_zendesk_ticket_for_email',
               return_value=({'id': '888'}, 'case-888@alias.io')), \
         patch('apps.communications.services.post_ai_summary_to_zendesk') as note, \
         patch('apps.integrations.services.add_zendesk_ticket_tags', return_value=True):
        result = recover_orphan_emails(dry_run=False)
    assert result['matched'] == 1
    assert result['dry_run'] is False
    el.refresh_from_db()
    assert el.zd_ticket_id == '888'
    assert el.claim_id == claim.id
    note.assert_called_once()


@pytest.mark.django_db
def test_recover_orphan_emails_dry_run_returns_matched_but_changes_nothing():
    """dry_run=True counts matches but writes nothing."""
    from apps.communications.services import recover_orphan_emails
    el = EmailLog.objects.create(
        subject='s', body='b', from_email='x@y.com', zd_ticket_id='',
        raw_headers='To: a@b.com\n', category='OBJECT_NOT_FOUND')
    with patch('apps.communications.services.find_zendesk_ticket_for_email',
               return_value=({'id': '1'}, 'a@b.com')), \
         patch('apps.communications.services.post_ai_summary_to_zendesk') as note:
        result = recover_orphan_emails(dry_run=True)
    assert result['matched'] >= 1
    assert result['dry_run'] is True
    note.assert_not_called()
    el.refresh_from_db()
    assert el.zd_ticket_id == ''   # untouched
