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
    with patch('apps.communications.management.commands.rematch_orphan_emails.find_zendesk_ticket_for_email',
               return_value=({'id': '777'}, 'case-777@alias.io')), \
         patch('apps.communications.management.commands.rematch_orphan_emails.post_ai_summary_to_zendesk') as note, \
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
    with patch('apps.communications.management.commands.rematch_orphan_emails.find_zendesk_ticket_for_email',
               return_value=({'id': '1'}, 'a@b.com')), \
         patch('apps.communications.management.commands.rematch_orphan_emails.post_ai_summary_to_zendesk') as note:
        call_command('rematch_orphan_emails', '--dry-run')
    assert note.called is False
    el.refresh_from_db()
    assert el.zd_ticket_id == ''   # untouched
