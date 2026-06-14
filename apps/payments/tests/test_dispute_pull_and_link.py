"""Tests for the dispute backlog tools:
  * list_paypal_disputes() — enumerate disputes from PayPal's List API (pulls the
    disputes that predate the webhook subscription).
  * dispute_pull_from_paypal view — bulk-ingest those, summarise, surface unmatched.
  * dispute_link_claim view — manually attach an unmatched dispute to a claim.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.models import Dispute, DisputeActivityLog
from apps.payments.paypal_disputes_service import list_paypal_disputes

User = get_user_model()

SVC = 'apps.payments.paypal_disputes_service'


class _FakeResp:
    """Minimal context-manager stand-in for urllib's urlopen response."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _dispute(**overrides):
    base = dict(
        paypal_dispute_id='PP-D-1', status='RECEIVED',
        dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
        dispute_amount='100.00', dispute_currency='USD',
        buyer_email='buyer@example.com', buyer_name='Buyer',
        transaction_id='TXN-1', transaction_date='2026-03-15T10:00:00Z',
    )
    base.update(overrides)
    return Dispute.objects.create(**base)


@patch(f'{SVC}.get_paypal_access_token', return_value='tok')
class TestListPaypalDisputes(TestCase):

    def test_collects_ids_from_single_page(self, _tok):
        page = {'items': [{'dispute_id': 'PP-D-1'}, {'dispute_id': 'PP-D-2'}], 'links': []}
        with patch(f'{SVC}.urllib.request.urlopen', return_value=_FakeResp(page)):
            ids = list_paypal_disputes()
        self.assertEqual(ids, ['PP-D-1', 'PP-D-2'])

    def test_follows_next_link_for_pagination(self, _tok):
        page1 = {'items': [{'dispute_id': 'PP-D-1'}],
                 'links': [{'rel': 'next', 'href': 'https://api.test/v1/customer/disputes?page=2'}]}
        page2 = {'items': [{'dispute_id': 'PP-D-2'}], 'links': []}
        with patch(f'{SVC}.urllib.request.urlopen', side_effect=[_FakeResp(page1), _FakeResp(page2)]):
            ids = list_paypal_disputes()
        self.assertEqual(ids, ['PP-D-1', 'PP-D-2'])

    def test_no_token_returns_empty(self, mock_tok):
        mock_tok.return_value = None
        self.assertEqual(list_paypal_disputes(), [])

    def test_skips_resolved_disputes_by_default(self, _tok):
        page = {'items': [
            {'dispute_id': 'OPEN-1', 'status': 'UNDER_REVIEW', 'dispute_state': 'REQUIRED_ACTION'},
            {'dispute_id': 'DONE-1', 'status': 'RESOLVED', 'dispute_state': 'RESOLVED'},
            {'dispute_id': 'DONE-2', 'dispute_state': 'RESOLVED'},
        ], 'links': []}
        with patch(f'{SVC}.urllib.request.urlopen', return_value=_FakeResp(page)):
            ids = list_paypal_disputes()
        self.assertEqual(ids, ['OPEN-1'])

    def test_include_resolved_returns_all(self, _tok):
        page = {'items': [
            {'dispute_id': 'OPEN-1', 'status': 'UNDER_REVIEW'},
            {'dispute_id': 'DONE-1', 'status': 'RESOLVED'},
        ], 'links': []}
        with patch(f'{SVC}.urllib.request.urlopen', return_value=_FakeResp(page)):
            ids = list_paypal_disputes(include_resolved=True)
        self.assertEqual(ids, ['OPEN-1', 'DONE-1'])


class TestDisputePullView(TestCase):
    URL = '/manager/disputes/pull-from-paypal/'

    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='pull_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.manager)

    def test_pull_summarises_new_existing_and_unmatched(self):
        def fake_ingest(did):
            return {
                'D1': (SimpleNamespace(claim_id=10), True),    # new, matched
                'D2': (SimpleNamespace(claim_id=None), True),  # new, UNMATCHED
                'D3': (SimpleNamespace(claim_id=20), False),   # already known
            }[did]
        with patch(f'{SVC}.list_paypal_disputes', return_value=['D1', 'D2', 'D3']), \
             patch(f'{SVC}.ingest_dispute', side_effect=fake_ingest):
            resp = self.web.post(self.URL, follow=True)
        self.assertContains(resp, 'Pulled 3')
        self.assertContains(resp, '2 new')
        self.assertContains(resp, '1 already known')
        self.assertContains(resp, '1 dispute(s) have no matching claim')

    def test_empty_list_warns(self):
        with patch(f'{SVC}.list_paypal_disputes', return_value=[]), \
             patch(f'{SVC}.ingest_dispute') as mock_ingest:
            resp = self.web.post(self.URL, follow=True)
        mock_ingest.assert_not_called()
        self.assertContains(resp, 'No disputes returned from PayPal')

    def test_non_manager_blocked(self):
        agent = User.objects.create_user(username='pull_agent', password='x', role='AGENT')
        client = Client()
        client.force_login(agent)
        with patch(f'{SVC}.list_paypal_disputes') as mock_list:
            resp = client.post(self.URL)
        mock_list.assert_not_called()
        self.assertNotEqual(resp.status_code, 200)


class TestDisputeLinkClaimView(TestCase):
    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='link_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.manager)
        self.claim = Claim.objects.create(
            alf_claim_id='ALF5550001', zd_ticket_id='55501', client_email='c@example.com',
            status='Investigation initiated', status_category='open')

    def _url(self, dispute):
        return f'/manager/disputes/{dispute.id}/link-claim/'

    def test_links_by_ticket_id_and_flips_to_matched(self):
        d = _dispute(paypal_dispute_id='PP-D-A')
        resp = self.web.post(self._url(d), {'claim_ref': '55501'}, follow=True)
        d.refresh_from_db()
        self.assertEqual(d.claim_id, self.claim.id)
        self.assertEqual(d.status, 'MATCHED')
        self.assertEqual(d.zd_ticket_id, '55501')
        self.assertTrue(DisputeActivityLog.objects.filter(
            dispute=d, action='DISPUTE_MATCHED').exists())
        self.assertContains(resp, 'Linked dispute to claim')

    def test_links_by_alf_id_and_email(self):
        for ref in ('ALF5550001', 'c@example.com'):
            d = _dispute(paypal_dispute_id=f'PP-D-{ref}')
            self.web.post(self._url(d), {'claim_ref': ref})
            d.refresh_from_db()
            self.assertEqual(d.claim_id, self.claim.id)

    def test_no_match_warns_and_leaves_unlinked(self):
        d = _dispute(paypal_dispute_id='PP-D-B')
        resp = self.web.post(self._url(d), {'claim_ref': 'nope-99999'}, follow=True)
        d.refresh_from_db()
        self.assertIsNone(d.claim_id)
        self.assertContains(resp, 'No claim found')

    def test_already_linked_is_a_noop(self):
        other = Claim.objects.create(
            alf_claim_id='ALF5550002', zd_ticket_id='55502', client_email='o@example.com',
            status='Investigation initiated', status_category='open')
        d = _dispute(paypal_dispute_id='PP-D-C', claim=self.claim, status='MATCHED')
        resp = self.web.post(self._url(d), {'claim_ref': '55502'}, follow=True)
        d.refresh_from_db()
        self.assertEqual(d.claim_id, self.claim.id)  # unchanged
        self.assertContains(resp, 'already linked')

    def test_non_manager_blocked(self):
        d = _dispute(paypal_dispute_id='PP-D-D')
        agent = User.objects.create_user(username='link_agent', password='x', role='AGENT')
        client = Client()
        client.force_login(agent)
        resp = client.post(self._url(d), {'claim_ref': '55501'})
        d.refresh_from_db()
        self.assertIsNone(d.claim_id)
        self.assertNotEqual(resp.status_code, 200)


class TestDisputePruneResolvedView(TestCase):
    URL = '/manager/disputes/prune-resolved/'

    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='prune_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.manager)

    def test_deletes_only_paypal_resolved(self):
        open_d = _dispute(paypal_dispute_id='OPEN-1', raw_webhook_payload={'status': 'UNDER_REVIEW'})
        resolved_d = _dispute(paypal_dispute_id='DONE-1', raw_webhook_payload={'status': 'RESOLVED'})
        manual_d = _dispute(paypal_dispute_id='MANUAL-1', raw_webhook_payload={})  # no payload
        resp = self.web.post(self.URL, follow=True)
        self.assertTrue(Dispute.objects.filter(pk=open_d.pk).exists())
        self.assertFalse(Dispute.objects.filter(pk=resolved_d.pk).exists())
        self.assertTrue(Dispute.objects.filter(pk=manual_d.pk).exists())
        self.assertContains(resp, 'Removed 1')

    def test_nothing_to_remove(self):
        _dispute(paypal_dispute_id='OPEN-1', raw_webhook_payload={'status': 'UNDER_REVIEW'})
        resp = self.web.post(self.URL, follow=True)
        self.assertContains(resp, 'No resolved disputes')

    def test_non_manager_blocked(self):
        resolved_d = _dispute(paypal_dispute_id='DONE-1', raw_webhook_payload={'status': 'RESOLVED'})
        agent = User.objects.create_user(username='prune_agent', password='x', role='AGENT')
        client = Client()
        client.force_login(agent)
        resp = client.post(self.URL)
        self.assertTrue(Dispute.objects.filter(pk=resolved_d.pk).exists())
        self.assertNotEqual(resp.status_code, 200)
