"""Unit tests for mirror_status_change — the status-mirror logic extracted out of
ZendeskClaimWebhookView. These exercise the logic WITHOUT the HTTP layer, which is
the point of the extraction. (The webhook's HTTP mapping stays covered by
test_zendesk_claim_webhook.py.)"""

import pytest
from unittest.mock import patch

from apps.claims.models import Claim
from apps.integrations.views import webhooks


@pytest.mark.django_db
def test_no_status_id_is_a_noop_outcome():
    claim = Claim.objects.create(alf_claim_id='ALFM000001', client_email='a@b.com', status='Open')
    assert webhooks.mirror_status_change(claim, '') == {'outcome': 'no_status'}


@pytest.mark.django_db
@patch('apps.integrations.views.webhooks.resolve_custom_status')
def test_unresolved_status_is_dropped(mock_resolve):
    claim = Claim.objects.create(alf_claim_id='ALFM000002', client_email='a@b.com', status='Investigating')
    mock_resolve.return_value = {'name': '99999', 'category': 'open'}  # resolver echoes the raw id
    res = webhooks.mirror_status_change(claim, '99999')
    assert res == {'outcome': 'unresolved', 'claim_id': claim.id}
    claim.refresh_from_db()
    assert claim.status == 'Investigating'  # left untouched


@pytest.mark.django_db
@patch('apps.integrations.views.webhooks.resolve_custom_status')
def test_same_status_is_no_change(mock_resolve):
    claim = Claim.objects.create(alf_claim_id='ALFM000003', client_email='a@b.com', status='Item found')
    mock_resolve.return_value = {'name': 'Item found', 'category': 'open'}
    res = webhooks.mirror_status_change(claim, '123')
    assert res == {'outcome': 'no_change', 'claim_id': claim.id, 'status': 'Item found'}


@pytest.mark.django_db
@patch('apps.communications.client_updates.sync_cadence_for_status')
@patch('apps.integrations.views.webhooks.refresh_claim_summary', return_value=False)
@patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value=None)
@patch('apps.integrations.views.webhooks.resolve_custom_status')
def test_status_change_mirrors_and_writes_timeline(mock_resolve, mock_fetch, mock_refresh, mock_cadence):
    claim = Claim.objects.create(alf_claim_id='ALFM000004', client_email='a@b.com',
                                 zd_ticket_id='555', status='Investigating')
    mock_resolve.return_value = {'name': 'Item found', 'category': 'open'}

    res = webhooks.mirror_status_change(claim, '123')

    assert res == {'outcome': 'updated', 'claim_id': claim.id, 'status': 'Item found'}
    claim.refresh_from_db()
    assert claim.status == 'Item found'
    assert claim.status_category == 'open'
    # The history entry is written in the same atomic block; when the AI call
    # does not succeed the fallback is the deterministic transition headline.
    entry = claim.updates.first()
    assert entry is not None
    assert entry.update_type == 'STATUS_CHANGE'
    assert 'Item found' in entry.llm_summary
    assert entry.llm_summary != ''
    # The cadence side-effect was invoked (its own behaviour is unit-tested separately).
    mock_cadence.assert_called_once_with(claim, '123')
