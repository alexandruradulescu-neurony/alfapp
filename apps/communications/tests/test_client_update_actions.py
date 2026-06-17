"""Unit tests for the client-update action helpers extracted out of
ZendeskClientUpdatesView: sync_cadence_for_status, regenerate_initial_update,
send_initial_update. HTTP-free — the view's message strings/guards stay covered
by test_zendesk_updates_endpoint.py."""

import pytest
from unittest.mock import patch

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.communications import client_updates as cu


def _claim(**kw):
    base = dict(alf_claim_id='ALFC000001', client_email='a@b.com',
                zd_ticket_id='900', status='Submitted')
    base.update(kw)
    return Claim.objects.create(**base)


@pytest.mark.django_db
def test_sync_cadence_drafts_and_schedules_on_trigger_id():
    ss = SystemSettings.get_instance()
    ss.client_report_trigger_status_id = '777'
    ss.client_report_trigger_status = ''
    ss.save()
    claim = _claim()
    with patch('apps.communications.client_report.build_client_update_message', return_value='DRAFT') as build, \
         patch('apps.communications.client_updates.schedule_next') as sched, \
         patch('apps.communications.client_updates.claim_is_closed', return_value=False):
        cu.sync_cadence_for_status(claim, '777')
    claim.refresh_from_db()
    assert claim.client_report_draft == 'DRAFT'
    assert build.call_args.kwargs.get('polish') is False  # template-only, NOT polished
    sched.assert_called_once()


@pytest.mark.django_db
def test_sync_cadence_name_fallback_when_no_trigger_id():
    ss = SystemSettings.get_instance()
    ss.client_report_trigger_status_id = ''
    ss.client_report_trigger_status = 'Submitted'
    ss.save()
    claim = _claim(status='Submitted')
    with patch('apps.communications.client_report.build_client_update_message', return_value='DRAFT'), \
         patch('apps.communications.client_updates.schedule_next'), \
         patch('apps.communications.client_updates.claim_is_closed', return_value=False):
        cu.sync_cadence_for_status(claim, 'unrelated-id')  # id branch can't match (no trigger_id)
    claim.refresh_from_db()
    assert claim.client_report_draft == 'DRAFT'  # matched by name fallback


@pytest.mark.django_db
def test_sync_cadence_skips_when_already_drafted():
    ss = SystemSettings.get_instance()
    ss.client_report_trigger_status_id = '777'
    ss.save()
    claim = _claim(client_report_draft='already here')
    with patch('apps.communications.client_report.build_client_update_message') as build, \
         patch('apps.communications.client_updates.schedule_next') as sched, \
         patch('apps.communications.client_updates.claim_is_closed', return_value=False):
        cu.sync_cadence_for_status(claim, '777')
    build.assert_not_called()
    sched.assert_not_called()


@pytest.mark.django_db
def test_sync_cadence_cancels_when_closed():
    ss = SystemSettings.get_instance()
    ss.client_report_trigger_status_id = '777'
    ss.save()
    claim = _claim()
    with patch('apps.communications.client_updates.claim_is_closed', return_value=True), \
         patch('apps.communications.client_updates.cancel_open_follow_ups') as cancel, \
         patch('apps.communications.client_report.build_client_update_message', return_value='DRAFT'), \
         patch('apps.communications.client_updates.schedule_next'):
        cu.sync_cadence_for_status(claim, 'nomatch')  # not entered_trigger
    cancel.assert_called_once_with(claim)


@pytest.mark.django_db
def test_regenerate_initial_update_uses_polish_true():
    claim = _claim()
    with patch('apps.communications.client_report.build_client_update_message', return_value='POLISHED') as build:
        assert cu.regenerate_initial_update(claim) is True
    claim.refresh_from_db()
    assert claim.client_report_draft == 'POLISHED'
    assert build.call_args.kwargs.get('polish') is True


@pytest.mark.django_db
def test_send_initial_update_post_fails_writes_no_state():
    claim = _claim(client_report_draft='draft-before')
    with patch('apps.integrations.services.post_zendesk_comment', return_value=None):
        assert cu.send_initial_update(claim, 'hello client') is False
    claim.refresh_from_db()
    assert claim.client_report_sent_at is None
    assert claim.client_report_draft == 'draft-before'  # unchanged on failure


@pytest.mark.django_db
def test_send_initial_update_success_records_sent():
    claim = _claim()
    with patch('apps.integrations.services.post_zendesk_comment', return_value={'ok': 1}) as post:
        assert cu.send_initial_update(claim, 'hello client') is True
    claim.refresh_from_db()
    assert claim.client_report_draft == 'hello client'  # edited body saved verbatim
    assert claim.client_report_sent_at is not None
    assert post.call_args.kwargs.get('is_internal') is False  # PUBLIC reply
