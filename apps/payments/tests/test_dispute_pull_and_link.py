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
from apps.payments.paypal_disputes_service import list_paypal_disputes, ingest_dispute

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
        self.manager = User.objects.create_user(username='pull_mgr', password='x')
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

class TestDisputeLinkClaimView(TestCase):
    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='link_mgr', password='x')
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

    def test_detail_page_shows_raw_paypal_data(self):
        d = _dispute(paypal_dispute_id='PP-D-RAW',
                     raw_webhook_payload={'status': 'UNDER_REVIEW', 'dispute_id': 'PP-D-RAW'})
        resp = self.web.get(f'/manager/disputes/{d.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Raw PayPal data')
        self.assertContains(resp, 'PP-D-RAW')  # payload rendered verbatim

    def test_relink_repoints_claim_and_zendesk_ticket(self):
        # An already-linked dispute can be moved to a different claim — and the
        # Zendesk ticket must follow the new claim (the evidence builder reads
        # comments from dispute.zd_ticket_id, so a stale ticket would leak the
        # wrong customer's history into the regenerated narrative).
        other = Claim.objects.create(
            alf_claim_id='ALF5550002', zd_ticket_id='55502', client_email='o@example.com',
            status='Investigation initiated', status_category='open')
        d = _dispute(paypal_dispute_id='PP-D-C', claim=self.claim,
                     zd_ticket_id='55501', status='MATCHED')
        resp = self.web.post(self._url(d), {'claim_ref': '55502'}, follow=True)
        d.refresh_from_db()
        self.assertEqual(d.claim_id, other.id)        # moved to the new claim
        self.assertEqual(d.zd_ticket_id, '55502')     # ticket followed the claim
        self.assertContains(resp, 'Re-linked')

    def test_relink_logs_from_and_to_claim(self):
        other = Claim.objects.create(
            alf_claim_id='ALF5550002', zd_ticket_id='55502', client_email='o@example.com',
            status='Investigation initiated', status_category='open')
        d = _dispute(paypal_dispute_id='PP-D-C2', claim=self.claim,
                     zd_ticket_id='55501', status='MATCHED')
        self.web.post(self._url(d), {'claim_ref': '55502'})
        log = DisputeActivityLog.objects.filter(
            dispute=d, action='DISPUTE_MATCHED').latest('id')
        self.assertIn(str(self.claim.id), log.details)   # from
        self.assertIn(str(other.id), log.details)        # to

    def test_relink_blocked_on_txn_mismatch_without_override(self):
        # The same transaction-id guard the first link uses must also protect a
        # relink — you can't silently move a dispute onto a claim whose PayPal
        # transaction id disagrees.
        other = Claim.objects.create(
            alf_claim_id='ALF5550003', zd_ticket_id='55503', client_email='x@example.com',
            paypal_transaction_id='DIFFERENT', status='open', status_category='open')
        d = _dispute(paypal_dispute_id='PP-D-C3', claim=self.claim,
                     zd_ticket_id='55501', status='MATCHED', transaction_id='TXN-ORIG')
        resp = self.web.post(self._url(d), {'claim_ref': '55503'}, follow=True)
        d.refresh_from_db()
        self.assertEqual(d.claim_id, self.claim.id)   # unchanged — blocked
        self.assertContains(resp, 'Not linked')

class TestDisputePruneResolvedView(TestCase):
    URL = '/manager/disputes/prune-resolved/'

    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='prune_mgr', password='x')
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

def _paypal_details(dispute_id='PP-D-1', invoice_number='', custom='', buyer_email=None):
    """A realistic PayPal dispute payload (shape taken from a real response —
    note the buyer has NO email; the claim reference lives in invoice_number)."""
    buyer = {'name': 'Susan Colon', 'payer_id': 'P1'}
    if buyer_email is not None:
        buyer['email'] = buyer_email
    return {
        'dispute_id': dispute_id,
        'status': 'WAITING_FOR_SELLER_RESPONSE',
        'dispute_state': 'REQUIRED_ACTION',
        'dispute_life_cycle_stage': 'CHARGEBACK',
        'reason': 'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
        'dispute_amount': {'currency_code': 'USD', 'value': '29.00'},
        'create_time': '2026-06-09T10:20:06.168Z',
        'seller_response_due_date': '2026-06-20T06:59:59.000Z',
        'disputed_transactions': [{
            'buyer': buyer,
            'seller_transaction_id': 'TXN-9',
            'create_time': '2026-05-28T16:50:19.000Z',
            'invoice_number': invoice_number,
            'custom': custom,
            'gross_amount': {'currency_code': 'USD', 'value': '29.00'},
        }],
    }


class TestIngestDisputeMatching(TestCase):
    """ingest_dispute matches by invoice ALF id / WooCommerce order — NOT email
    (PayPal never sends the buyer email)."""

    def setUp(self):
        SystemSettings.get_instance()
        self.claim = Claim.objects.create(
            alf_claim_id='ALF7410846', zd_ticket_id='74108', client_email='c@example.com',
            woocommerce_id='36298', status='Investigation initiated', status_category='open')

    def test_matches_by_invoice_alf_id(self):
        details = _paypal_details(dispute_id='PP-D-INV', invoice_number='ccbfae-ALF7410846')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            dispute, created = ingest_dispute('PP-D-INV')
        self.assertTrue(created)
        self.assertEqual(dispute.claim_id, self.claim.id)
        self.assertEqual(dispute.status, 'MATCHED')

    def test_matches_by_custom_woocommerce_id(self):
        details = _paypal_details(dispute_id='PP-D-CUST', invoice_number='', custom='36298')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            dispute, _created = ingest_dispute('PP-D-CUST')
        self.assertEqual(dispute.claim_id, self.claim.id)

    def test_no_reference_lands_unmatched(self):
        details = _paypal_details(dispute_id='PP-D-NONE', invoice_number='no-ref', custom='')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            dispute, _created = ingest_dispute('PP-D-NONE')
        self.assertIsNone(dispute.claim_id)
        self.assertEqual(dispute.status, 'RECEIVED')

    def test_repull_self_heals_existing_unmatched(self):
        d = _dispute(paypal_dispute_id='PP-D-HEAL', raw_webhook_payload={})  # claim=None
        details = _paypal_details(dispute_id='PP-D-HEAL', invoice_number='x-ALF7410846')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            returned, created = ingest_dispute('PP-D-HEAL')
        self.assertFalse(created)
        d.refresh_from_db()
        self.assertEqual(d.claim_id, self.claim.id)
        self.assertEqual(d.status, 'MATCHED')

    def test_double_verify_links_when_txn_agrees(self):
        self.claim.paypal_transaction_id = 'TXN-9'  # matches _paypal_details default
        self.claim.save()
        details = _paypal_details(dispute_id='PP-D-OK', invoice_number='x-ALF7410846')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            dispute, _created = ingest_dispute('PP-D-OK')
        self.assertEqual(dispute.claim_id, self.claim.id)
        self.assertEqual(dispute.status, 'MATCHED')

    def test_double_verify_blocks_alf_link_on_txn_mismatch(self):
        self.claim.paypal_transaction_id = 'TXN-OTHER'  # disagrees with dispute's TXN-9
        self.claim.save()
        details = _paypal_details(dispute_id='PP-D-BAD', invoice_number='x-ALF7410846')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            dispute, _created = ingest_dispute('PP-D-BAD')
        # ALF link refused (txn disagrees) and no other claim carries TXN-9.
        self.assertIsNone(dispute.claim_id)
        self.assertEqual(dispute.status, 'RECEIVED')

    def test_matches_by_transaction_id_when_no_alf(self):
        self.claim.paypal_transaction_id = 'TXN-9'
        self.claim.save()
        details = _paypal_details(dispute_id='PP-D-TXN', invoice_number='no-alf-here')
        with patch(f'{SVC}.fetch_dispute_details', return_value=details):
            dispute, _created = ingest_dispute('PP-D-TXN')
        self.assertEqual(dispute.claim_id, self.claim.id)

    def test_concurrent_ingest_is_idempotent(self):
        """G2: a manual pull racing a webhook (or two pulls) can both pass the
        existence check; the second create() hits the unique paypal_dispute_id
        constraint and must adopt the winning row, not raise a 500."""
        from unittest.mock import MagicMock
        from django.db import IntegrityError
        from apps.payments import paypal_disputes_service as svc
        winner = MagicMock(id=777)
        details = {
            'case_id': 'C1', 'dispute_amount': {'value': '10.00', 'currency_code': 'USD'},
            'reason': '', 'dispute_life_cycle_stage': 'INQUIRY',
            'disputed_transactions': [{}], 'seller_response_due_date': None,
            'create_time': None,
        }
        with patch.object(svc, 'fetch_dispute_details', return_value=details), \
             patch.object(svc, '_match_claim_for_dispute', return_value=None), \
             patch.object(svc, 'Dispute') as MockDispute:
            MockDispute.VALID_REASONS = []
            # existence check -> miss, then re-fetch after the lost race -> winner
            MockDispute.objects.filter.return_value.first.side_effect = [None, winner]
            MockDispute.objects.create.side_effect = IntegrityError('dup')
            returned, created = svc.ingest_dispute('PP-D-RACE')
        self.assertFalse(created)
        self.assertIs(returned, winner)


class TestDisputeListViewFilter(TestCase):
    """Default list view shows only disputes that still need a reply; under-review
    and resolved are hidden (reachable via the view tabs)."""
    URL = '/manager/disputes/'

    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='view_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.manager)
        self.action = _dispute(paypal_dispute_id='A1', raw_webhook_payload={
            'status': 'WAITING_FOR_SELLER_RESPONSE', 'dispute_state': 'REQUIRED_ACTION'})
        self.review = _dispute(paypal_dispute_id='R1', raw_webhook_payload={
            'status': 'UNDER_REVIEW', 'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        self.resolved = _dispute(paypal_dispute_id='X1', raw_webhook_payload={'status': 'RESOLVED'})
        self.manual = _dispute(paypal_dispute_id='M1', raw_webhook_payload={})  # manual, no payload

    def _ids(self, resp):
        return {d.paypal_dispute_id for d in resp.context['page_obj']}

    def test_default_hides_review_and_resolved(self):
        ids = self._ids(self.web.get(self.URL))
        self.assertIn('A1', ids)        # awaiting reply
        self.assertIn('M1', ids)        # manual (no payload) stays visible
        self.assertNotIn('R1', ids)     # under PayPal review → hidden
        self.assertNotIn('X1', ids)     # resolved → hidden

    def test_review_view_shows_only_under_review(self):
        self.assertEqual(self._ids(self.web.get(self.URL, {'view': 'review'})), {'R1'})

    def test_all_view_shows_everything(self):
        self.assertEqual(self._ids(self.web.get(self.URL, {'view': 'all'})),
                         {'A1', 'R1', 'X1', 'M1'})

    def test_view_counts(self):
        vc = self.web.get(self.URL).context['view_counts']
        self.assertEqual(vc['action'], 2)    # A1 + M1
        self.assertEqual(vc['review'], 1)
        self.assertEqual(vc['resolved'], 1)
        self.assertEqual(vc['all'], 4)


class TestDisputeListRedesign(TestCase):
    """The redesigned list is a manager snapshot: action-first tabs, the
    response deadline surfaced, clickable rows → detail, and the old noise
    (7-card stat strip, status dropdown, internal ID/PayPal-ID columns) gone."""
    URL = '/manager/disputes/'

    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(username='dl_redesign', password='x')
        self.web = Client()
        self.web.force_login(self.manager)
        from django.utils import timezone
        from datetime import timedelta
        self.overdue = _dispute(
            paypal_dispute_id='OD1', status='GATHERING_DATA',
            seller_response_due=timezone.now() - timedelta(days=1),
            raw_webhook_payload={'status': 'WAITING_FOR_SELLER_RESPONSE'})

    def test_redesign_markers_present_and_old_removed(self):
        html = self.web.get(self.URL + '?view=all').content.decode()
        # action-first tabs + search
        self.assertIn('?view=action', html)
        self.assertIn('?view=resolved', html)
        # clickable row → detail
        self.assertIn(f"/manager/disputes/{self.overdue.id}/", html)
        self.assertIn('window.location=', html)
        # deadline is the key signal — overdue surfaced
        self.assertIn('Overdue', html)
        # old noise gone
        self.assertNotIn('Status Summary', html)
        self.assertNotIn('<th>ID</th>', html)
        self.assertNotIn('<th>PayPal ID</th>', html)
        self.assertNotIn('All Statuses', html)   # status dropdown removed


class TestNeedsReplyExcludesUnactionable(TestCase):
    """Needs reply = only disputes we can still reply to. Overdue (deadline passed)
    and waiting-on-buyer disputes move out of it into their own lenses."""
    URL = '/manager/disputes/'

    def setUp(self):
        from django.utils import timezone
        from datetime import timedelta
        SystemSettings.get_instance()
        self.mgr = User.objects.create_user(username='buckets_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.actionable = _dispute(
            paypal_dispute_id='ACT', seller_response_due=timezone.now() + timedelta(days=3),
            raw_webhook_payload={'status': 'WAITING_FOR_SELLER_RESPONSE', 'dispute_state': 'REQUIRED_ACTION'})
        self.overdue = _dispute(
            paypal_dispute_id='OD', seller_response_due=timezone.now() - timedelta(days=1),
            raw_webhook_payload={'status': 'WAITING_FOR_SELLER_RESPONSE', 'dispute_state': 'REQUIRED_ACTION'})
        self.buyer = _dispute(
            paypal_dispute_id='BUY',
            raw_webhook_payload={'status': 'WAITING_FOR_BUYER_RESPONSE',
                                 'dispute_state': 'REQUIRED_OTHER_PARTY_ACTION'})

    def _ids(self, resp):
        return {d.paypal_dispute_id for d in resp.context['page_obj']}

    def test_needs_reply_excludes_overdue_and_buyer(self):
        # default view is 'action' (Needs reply) — only the still-repliable one
        self.assertEqual(self._ids(self.web.get(self.URL)), {'ACT'})

    def test_overdue_lens_shows_overdue_only(self):
        self.assertEqual(self._ids(self.web.get(self.URL, {'view': 'overdue'})), {'OD'})

    def test_buyer_wait_moves_to_review_lens(self):
        self.assertIn('BUY', self._ids(self.web.get(self.URL, {'view': 'review'})))

    def test_view_counts_split_correctly(self):
        vc = self.web.get(self.URL).context['view_counts']
        self.assertEqual(vc['action'], 1)    # ACT (still repliable)
        self.assertEqual(vc['overdue'], 1)   # OD (deadline passed)
        self.assertEqual(vc['review'], 1)    # BUY (awaiting buyer folds into review)
