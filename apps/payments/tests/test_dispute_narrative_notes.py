"""Phase A — the AI-written evidence narrative (the PayPal `notes` text):
build_dispute_narrative_notes + its assembly, fallback, and PII handling."""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Dispute
from apps.payments import document_service as ds


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-D-NOTES', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED')
    base.update(kw)
    return Dispute.objects.create(**base)


FLIGHT_DATA = {
    'number': 'AA3196', 'airline': 'American Airlines', 'status': 'Arrived',
    'legs': [{'from_iata': 'CLT', 'from_city': 'Charlotte', 'to_iata': 'PBI',
              'to_city': 'West Palm Beach', 'status': 'Arrived'}],
}

COMMENTS = [
    {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': False,
     'created_at': '2026-02-03T21:14:00Z', 'body': 'Reported the lost iPad to Delta lost & found.',
     'attachments': []},
    {'author': {'name': 'Joe Snyder', 'email': 'j@alf.com'}, 'public': True,
     'created_at': '2026-02-04T10:32:00Z', 'body': 'Dear customer, an update on your search.',
     'attachments': []},
]


def _full_claim():
    return Claim.objects.create(
        client_email='lee@example.com', client_name='Lee Foley', alf_claim_id='ALF5490789',
        zd_ticket_id='97001', object_description='iPad Tablet\nred hard case',
        lost_location='TSA / Security Check', flight_data=FLIGHT_DATA, price_paid=Decimal('74.00'))


class AssemblyTests(TestCase):
    def test_unauthorised_leads_with_authorisation(self):
        notes = ds._assemble_narrative_notes(
            {'opening': 'O', 'authorization': 'AUTH', 'service_delivery': 'SVC', 'closing': 'C'},
            reason='UNAUTHORISED')
        # opening first, then numbered authorisation before service, closing last
        self.assertTrue(notes.startswith('O'))
        self.assertIn('1. Proof the customer authorised this purchase\nAUTH', notes)
        self.assertIn('2. Proof we delivered the paid service\nSVC', notes)
        self.assertTrue(notes.rstrip().endswith('C'))
        self.assertLess(notes.index('AUTH'), notes.index('SVC'))

    def test_not_received_leads_with_service(self):
        notes = ds._assemble_narrative_notes(
            {'opening': 'O', 'authorization': 'AUTH', 'service_delivery': 'SVC', 'closing': 'C'},
            reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        self.assertIn('1. Proof we delivered the paid service\nSVC', notes)
        self.assertIn('2. Proof the customer authorised this purchase\nAUTH', notes)
        self.assertLess(notes.index('SVC'), notes.index('AUTH'))

    def test_empty_sections_are_skipped(self):
        notes = ds._assemble_narrative_notes(
            {'opening': '', 'authorization': 'AUTH', 'service_delivery': '', 'closing': ''},
            reason='UNAUTHORISED')
        # only the one non-empty proof, renumbered to 1, nothing else
        self.assertEqual(notes, '1. Proof the customer authorised this purchase\nAUTH')


class FallbackTests(TestCase):
    """No AI key configured → deterministic template narrative, facts-only."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = ''
        ss.save()

    def test_fallback_uses_real_facts_and_structure(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_amount=Decimal('74.00'),
                     dispute_currency='USD')
        # Ticket created at 21:10 by the abandoned-cart notice; the customer paid
        # and the intake note posted at 21:14 — the submission date must follow
        # the intake note, not the earlier ticket-creation time.
        intake = {'author': {'name': 'System', 'email': 'sys@alf.com'}, 'public': False,
                  'created_at': '2026-02-03T21:14:00Z', 'attachments': [],
                  'body': 'Registration ID: ALF5490789\nName: Lee Foley\nDate/Time: February 3, 2026'}
        ticket = {'created_at': '2026-02-03T21:10:00Z',
                  'custom_fields': [{'id': ds.SUBMISSION_IP_FIELD_ID, 'value': '203.0.113.7'}]}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': ticket, 'comments': [intake] + COMMENTS}):
            out = ds.build_dispute_narrative_notes(d)
        self.assertEqual(out['source'], 'FALLBACK')
        notes = out['notes']
        self.assertIn('Lee Foley', notes)              # real name present
        self.assertIn('ALF5490789', notes)             # our reference
        self.assertIn('American Airlines', notes)      # flight verified
        self.assertIn('Terms and Conditions', notes)
        self.assertIn('resolve this dispute in our favour', notes)
        self.assertIn('Feb 03, 2026 15:14', notes)     # consent date from the intake note (21:14 UTC)
        self.assertIn('203.0.113.7', notes.replace('​', ''))  # IP zero-width-spaced for display

    def test_fallback_counts_our_public_updates(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}):
            out = ds.build_dispute_narrative_notes(d)
        # one public reply in COMMENTS
        self.assertIn('We sent the customer 1 update', out['notes'])

    def test_fallback_states_service_not_product_and_fits_limit(self):
        # The service-not-product framing is folded INTO the narrative now (no
        # separate transport prefix), so even the deterministic fallback must assert
        # it — and the whole note must fit PayPal's 2000-char hard limit.
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}):
            out = ds.build_dispute_narrative_notes(d)
        self.assertIn('not a physical product', out['notes'].lower())
        self.assertLessEqual(len(out['notes']), 2000)

    def test_use_ai_false_forces_fallback_even_with_key(self):
        SystemSettings.get_instance()  # key may be set elsewhere; use_ai=False must still skip AI
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': []}), \
             patch('apps.ai.client.AIClient.complete') as ai:
            out = ds.build_dispute_narrative_notes(d, use_ai=False)
            ai.assert_not_called()
        self.assertEqual(out['source'], 'FALLBACK')


class AIPathTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.pii_tokenization_salt = 'unit-test-salt'
        ss.save()

    def test_ai_sections_are_assembled_and_marked(self):
        from apps.ai.schemas import DisputeNarrative
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        fake = DisputeNarrative(opening='We contest this dispute.',
                                authorization='The customer filed it themselves.',
                                service_delivery='We reported the item and updated them.',
                                closing='Please resolve in our favour.')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch('apps.ai.client.AIClient.complete', return_value=fake) as ai:
            out = ds.build_dispute_narrative_notes(d, manager_note='Stress the IP match.')
            ai.assert_called_once()
            # manager note rides in as a trusted fact, not fenced as untrusted
            kwargs = ai.call_args.kwargs
            self.assertEqual(kwargs['call_site'], 'dispute_narrative_notes')
            self.assertEqual(kwargs['trusted']['manager_emphasis'], 'Stress the IP match.')
            # PII is force-masked: the real client name is offered as known_pii
            self.assertIn('Lee Foley', kwargs['known_pii']['names'])
        self.assertEqual(out['source'], 'AI')
        self.assertIn('We contest this dispute.', out['notes'])
        self.assertIn('Please resolve in our favour.', out['notes'])

    def test_ai_failure_falls_back(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch('apps.ai.client.AIClient.complete', side_effect=RuntimeError('boom')):
            out = ds.build_dispute_narrative_notes(d)
        self.assertEqual(out['source'], 'FALLBACK')
        self.assertIn('Lee Foley', out['notes'])

    def test_full_path_through_real_tokenizer_restores_pii(self):
        """Exercise AIClient end-to-end (only the network faked): the LLM sees a
        masked name, the assembled notes must contain the REAL name (PayPal is
        inside the trust zone)."""
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')

        seen = {}

        class _Msg:
            content = ('{"opening":"We contest this.","authorization":"Filed by '
                       'the customer.","service_delivery":"We did the work.",'
                       '"closing":"Resolve for us."}')

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _FakeClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        seen['messages'] = kwargs.get('messages')
                        return _Completion()

        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch('apps.ai.client._build_openai_client', return_value=_FakeClient()):
            out = ds.build_dispute_narrative_notes(d)

        self.assertEqual(out['source'], 'AI')
        # The provider never saw the raw client name...
        sent = str(seen['messages'])
        self.assertNotIn('Lee Foley', sent)
        # ...the response had no name token, so notes carry only the AI text.
        self.assertIn('We contest this.', out['notes'])


class NotesLengthWarningTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = ''
        ss.save()

    def test_warns_when_over_paypal_cap(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        long_sections = {k: 'x' * 600 for k in
                         ('opening', 'authorization', 'service_delivery', 'closing')}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': []}), \
             patch.object(ds, '_fallback_narrative_sections', return_value=long_sections), \
             patch.object(ds.logger, 'warning') as warn:
            out = ds.build_dispute_narrative_notes(d)
        self.assertGreater(len(out['notes']), ds.PAYPAL_NOTES_MAX_CHARS)
        self.assertTrue(any('caps dispute notes' in str(c) for c in warn.call_args_list))


class NarrativeLengthEnforcementTests(TestCase):
    """The AI cannot count its own characters, so build_dispute_narrative_notes
    MEASURES the assembled note and, if it overshoots, hands the exact overage
    back to the model and asks for a shorter rewrite — capped, never looping
    forever, and always keeping the shortest draft it produced."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.pii_tokenization_salt = 'unit-test-salt'
        ss.save()

    def _narr(self, n):
        from apps.ai.schemas import DisputeNarrative
        block = 'x' * n
        return DisputeNarrative(opening=block, authorization=block,
                                service_delivery=block, closing=block)

    def test_overlong_draft_is_condensed_via_retry(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        # First reply blows past PayPal's cap; the retry comes back short.
        replies = [self._narr(700), self._narr(80)]
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch('apps.ai.client.AIClient.complete', side_effect=replies) as ai:
            out = ds.build_dispute_narrative_notes(d)
        self.assertEqual(ai.call_count, 2)                       # it retried once
        self.assertEqual(out['source'], 'AI')
        self.assertLessEqual(len(out['notes']), ds.PAYPAL_NOTES_MAX_CHARS)
        # the retry's instructions changed and named the target ceiling
        first_prompt = ai.call_args_list[0].kwargs['system_prompt']
        retry_prompt = ai.call_args_list[1].kwargs['system_prompt']
        self.assertNotEqual(retry_prompt, first_prompt)
        self.assertIn(str(ds.NOTES_TARGET_CHARS), retry_prompt)

    def test_first_draft_within_target_does_not_retry(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch('apps.ai.client.AIClient.complete',
                   side_effect=[self._narr(80)]) as ai:
            out = ds.build_dispute_narrative_notes(d)
        ai.assert_called_once()                                  # no wasted retry
        self.assertEqual(out['source'], 'AI')

    def test_gives_up_after_max_attempts_and_keeps_shortest(self):
        claim = _full_claim()
        d = _dispute(claim=claim, zd_ticket_id='97001')
        # Every attempt overshoots, but each is shorter than the one before.
        sizes = [900 - i * 100 for i in range(ds.NARRATIVE_MAX_ATTEMPTS)]
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch('apps.ai.client.AIClient.complete',
                   side_effect=[self._narr(n) for n in sizes]) as ai:
            out = ds.build_dispute_narrative_notes(d)
        self.assertEqual(ai.call_count, ds.NARRATIVE_MAX_ATTEMPTS)  # capped, no infinite loop
        self.assertEqual(out['source'], 'AI')
        # It returned the SHORTEST attempt, not the first.
        shortest = min(sizes)
        self.assertIn('x' * shortest, out['notes'])
