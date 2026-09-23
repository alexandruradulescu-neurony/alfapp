"""Case timeline row-generation and rendering — the team wants the "Case
timeline" in the evidence report to read like descriptive prose (bold key
facts, a real header row) instead of a flat, terse "we did X" list. Pins the
target behaviour for three real bugs found in production Zendesk data:

1. Every internal note whose first line was short and carried an image (or
   whose body mentioned "via email") was mistaken for an office-submission
   note, producing fake rows like "We reported the loss to **Call Recording
   Summary**", "… to 2 Possible Flights", "… to TSA Update", "… to Update". A
   note that lists several offices in ONE note ("IAH / TSA / UA", each with
   its own screenshot) also only ever picked up the first ("IAH"), silently
   dropping "TSA" and "UA" from the report.
2. Every call was fed to the section-sorting AI as "NOT answered … we did not
   speak with the customer" because the code checked a field name
   (`answered_by_name`) that never appears in real Zendesk call payloads —
   only `answered_by` (the handling agent) does, on every call regardless of
   whether the customer actually picked up. Nothing in the call record itself
   says whether the customer answered, so the report must stop claiming it
   does, in both the AI's input and the customer-facing report.
3. The timeline was a flat, unstyled list of terse one-liners ("Claim
   submitted on our website", "PayPal dispute received") with no header row
   and no visual emphasis on the facts that matter (IDs, amounts, offices).

Tests build a synthetic case (modelled on a real dispute) through
`build_dispute_evidence_bundle(dispute, embed_attachments=False,
use_ai=False)` and `render_to_string(report_template_for(dispute), bundle)`,
with `apps.payments.document_service._fetch_zendesk_ticket_full` patched so
nothing ever touches the network.
"""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.models import Dispute
from apps.payments import document_service as ds

UTC = dt_tz.utc


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _claim(**kw):
    base = dict(
        alf_claim_id='ALF1234567',
        client_name='Test Client',
        client_email='client@example.com',
        price_paid=Decimal('75.00'),
        zd_ticket_id='99001',
        flight_details=('Flight: 1226 | Airline: United Airlines - UA | Airport: George Bush '
                        'Intercontinental Airport / IAH | Date/Time: September 18, 2026 7:00 am'),
        flight_data={},
    )
    base.update(kw)
    return Claim.objects.create(**base)


def _dispute(claim, *, created_at=None, **kw):
    base = dict(
        paypal_dispute_id='PP-R-TST-000000001',
        buyer_email=(claim.client_email if claim else 'buyer@example.com'),
        transaction_id='TX-TST-0000001',
        transaction_date=datetime(2026, 9, 18, 15, 48, 29, tzinfo=UTC),
        dispute_reason='MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
        dispute_amount=Decimal('75.00'),
        dispute_currency='USD',
        dispute_life_cycle_stage='CHARGEBACK',
        zd_ticket_id=(claim.zd_ticket_id if claim else ''),
        claim=claim,
        raw_webhook_payload={'create_time': '2026-09-18T15:48:29.300Z',
                             'dispute_channel': 'INTERNAL',
                             'dispute_life_cycle_stage': 'CHARGEBACK'},
    )
    base.update(kw)
    d = Dispute.objects.create(**base)
    Dispute.objects.filter(pk=d.pk).update(
        created_at=created_at or datetime(2026, 9, 20, 14, 29, 16, tzinfo=UTC))
    d.refresh_from_db()
    return d


def _intake_comment(created_at='2026-09-18T15:12:54Z', reg_id='ALF1234567',
                    client_name='Test Client', client_email='client@example.com'):
    body = (f'Registration ID: {reg_id}\nName: {client_name}\n'
            f'Email: {client_email} | Phone: +15550000000\n\n'
            'Date/Time: September 18, 2026 7:00 am\n'
            'Airport: George Bush Intercontinental Airport / IAH\n'
            'Airline: United Airlines - UA Flight #: 1226 Seat #:\n\n'
            'Lost object: Carry-On')
    return {'author': {'name': client_name, 'email': client_email}, 'public': False,
            'channel': 'web', 'created_at': created_at, 'body': body, 'attachments': []}


def _agent_note(body, created_at, author_name='Agent One',
                author_email='agent1@alf.example', public=False):
    return {'author': {'name': author_name, 'email': author_email}, 'public': public,
            'channel': 'web', 'created_at': created_at, 'body': body, 'attachments': []}


def _voice_comment(duration, direction, created_at='2026-09-18T16:54:58Z',
                   answered_by='Agent One', started_at=None):
    return {'author': {'name': answered_by, 'email': 'agent1@alf.example'}, 'public': False,
            'channel': 'voice', 'created_at': created_at,
            'body': f'{direction.title()} call to +15550000000', 'attachments': [],
            'call': {'direction': direction, 'duration': duration, 'answered_by': answered_by,
                     'recorded': True, 'started_at': started_at or created_at,
                     'from_name': 'Airport Lost Found', 'from_phone': '+18310000000',
                     'to_name': 'Test Client', 'to_phone': '+15550000000'}}


def _base_comments():
    """The full pinned fixture from the spec: c0 (dropped pre-claim noise), c1
    (intake), c2 (image-only flight lookup — NOT a filing), c3 (170s outbound
    call), c4 ("Call Recording Summary" note — NOT a filing, despite saying
    "via email"), c5 (recorded non-refundable-fee acceptance), c6 (one note
    listing three offices: IAH, TSA, UA — must yield ONE merged row naming
    all three, not just the first)."""
    return [
        {'author': {'name': 'System', 'email': ''}, 'public': True, 'channel': 'email',
         'created_at': '2026-09-18T15:09:08Z',
         'body': 'A new abandoned cart has been created for Test Client', 'attachments': []},
        _intake_comment(),
        {'author': {'name': 'Agent One', 'email': 'agent1@alf.example'}, 'public': False,
         'channel': 'web', 'created_at': '2026-09-18T16:11:09Z',
         'body': ' ![](https://example.zendesk.com/attachments/token/aaa/?name=flight.png)',
         'attachments': []},
        _voice_comment(170, 'outbound', created_at='2026-09-18T16:54:58Z',
                      started_at='2026-09-18T16:52:07Z'),
        {'author': {'name': 'Agent Two', 'email': 'agent2@alf.example'}, 'public': False,
         'channel': 'web', 'created_at': '2026-09-18T16:55:09Z', 'attachments': [],
         'body': ('**Call Recording Summary**\n\n'
                  'Caller Name: Test Client\n'
                  'Issue: Lost carry-on luggage, last seen on the shuttle bus.\n'
                  'Resolution: Information confirmed with the caller, the team will continue '
                  'to track the item, and updates will be sent via email as information '
                  'becomes available.\n'
                  'Next Steps: The team will proceed with the investigation and keep the '
                  'customer informed.')},
        _agent_note(
            ('The client was called and informed of the call being recorded for quality '
             'and training purposes to what Client agreed and approved.\n\n'
             'At minute  1:40  on our recorded line, Client approved to move forward with '
             'a non refundable fee of $ 75.00 as Client understood our service and agreed '
             'to move forward knowing no guarantees can be provided on lost items.'),
            '2026-09-18T16:56:10Z'),
        _agent_note(
            ('IAH\n\n ![](https://example.zendesk.com/attachments/token/b1/?name=a.png)\n\n'
             'TSA\n\n ![](https://example.zendesk.com/attachments/token/b2/?name=b.png)\n\n'
             'UA\n\n ![](https://example.zendesk.com/attachments/token/b3/?name=c.png)'),
            '2026-09-18T17:07:21Z'),
    ]


def _bundle(dispute, comments):
    with patch.object(ds, '_fetch_zendesk_ticket_full',
                      return_value={'ticket': {'id': dispute.zd_ticket_id}, 'comments': comments}):
        return ds.build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=False)


def _payload(channel, stage, create_time='2026-09-18T15:48:29.300Z'):
    return {'create_time': create_time, 'dispute_channel': channel,
            'dispute_life_cycle_stage': stage}


# ---------------------------------------------------------------------------
# The pinned fixture — exact rows, exact order, exact wording
# ---------------------------------------------------------------------------

class PinnedTimelineFixtureTests(TestCase):
    """The full synthetic case from the spec, exactly as it must read once the
    three bugs above are fixed."""

    def test_expected_timeline_rows_in_order(self):
        claim = _claim()
        dispute = _dispute(claim)
        bundle = _bundle(dispute, _base_comments())
        tl = bundle['timeline']

        whens = [e['when'] for e in tl]
        texts = [e['text'] for e in tl]

        self.assertEqual(whens, [
            'Sep 18, 2026 10:12', 'Sep 18, 2026 10:48', 'Sep 18, 2026 11:54',
            'Sep 18, 2026 11:56', 'Sep 18, 2026 12:07', 'Sep 20, 2026 09:29',
        ])
        self.assertEqual(texts, [
            'Lost-item service request ALF1234567 submitted on our website',
            'The buyer filed PayPal claim PP-R-TST-000000001, alleging “Item not as described”',
            'We called the customer (2m 50s)',
            ('During the recorded call, the customer agreed to proceed with the non-refundable $75 '
             'service fee, understanding that recovery of the lost item could not be guaranteed'),
            ('Lost-item information was submitted through George Bush Intercontinental Airport (IAH), '
             'TSA, and United Airlines (UA) channels'),
            'PayPal case notification received in our internal system',
        ])
        self.assertIn('<strong>ALF1234567</strong>', tl[0]['activity'])
        self.assertIn('<strong>PP-R-TST-000000001</strong>', tl[1]['activity'])
        self.assertIn('<strong>non-refundable $75 service fee</strong>', tl[3]['activity'])
        self.assertIn(
            '<strong>George Bush Intercontinental Airport (IAH), TSA, and United Airlines (UA)</strong>',
            tl[4]['activity'])
        self.assertNotIn('<strong>', tl[2]['activity'])   # the call row bolds nothing
        self.assertNotIn('<strong>', tl[5]['activity'])   # the closing row bolds nothing

        joined_text = ' '.join(texts)
        joined_activity = ' '.join(e['activity'] for e in tl)
        for forbidden in ('Call Recording Summary', '**', 'We reported the loss'):
            self.assertNotIn(forbidden, joined_text)
            self.assertNotIn(forbidden, joined_activity)

    def test_forbidden_strings_absent_from_rendered_report(self):
        claim = _claim(alf_claim_id='ALF-T02', zd_ticket_id='ZD-T02')
        dispute = _dispute(claim, zd_ticket_id='ZD-T02',
                           raw_webhook_payload=_payload('INTERNAL', 'CHARGEBACK'))
        bundle = _bundle(dispute, _base_comments())
        html = render_to_string(ds.report_template_for(dispute), bundle)
        # Scope this check to the timeline's own <table>, not the whole page — the
        # rest of the report legitimately repeats other parts of the case record.
        start = html.index('Case timeline')
        end = html.index('</table>', start) + len('</table>')
        table = html[start:end]
        for forbidden in ('Call Recording Summary', 'We reported the loss'):
            self.assertNotIn(forbidden, table)


# ---------------------------------------------------------------------------
# A. Rendering: header row, bold dates, bold facts, hostile-value escaping
# ---------------------------------------------------------------------------

class TimelineRenderingTests(TestCase):
    def test_header_row_has_date_and_activity_columns(self):
        claim = _claim(alf_claim_id='ALF-A01', zd_ticket_id='ZD-A01')
        dispute = _dispute(claim, zd_ticket_id='ZD-A01', raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        html = render_to_string(ds.report_template_for(dispute), bundle)
        self.assertIn('<th', html)
        self.assertIn('Date &amp; Time', html)
        self.assertIn('>Activity<', html)

    def test_row_date_is_bold(self):
        claim = _claim(alf_claim_id='ALF-A02', zd_ticket_id='ZD-A02')
        dispute = _dispute(claim, zd_ticket_id='ZD-A02', raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        html = render_to_string(ds.report_template_for(dispute), bundle)
        when = bundle['timeline'][0]['when']
        self.assertIn(f'<strong>{when}</strong>', html)

    def test_bold_facts_render_as_strong_tags_on_the_page(self):
        claim = _claim(alf_claim_id='ALF-A03', zd_ticket_id='ZD-A03')
        dispute = _dispute(claim, zd_ticket_id='ZD-A03', raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        html = render_to_string(ds.report_template_for(dispute), bundle)
        self.assertIn('<strong>ALF-A03</strong>', html)

    def test_hostile_office_label_is_escaped_not_live_markup(self):
        claim = _claim(alf_claim_id='ALF-A04', zd_ticket_id='ZD-A04')
        dispute = _dispute(claim, zd_ticket_id='ZD-A04', raw_webhook_payload={})
        note = _agent_note('<script>x</script>', '2026-09-18T17:07:21Z')
        note['attachments'] = [{'content_type': 'image/png',
                                'content_url': 'https://example.zendesk.com/a.png',
                                'file_name': 'a.png'}]
        bundle = _bundle(dispute, [_intake_comment(), note])
        html = render_to_string(ds.report_template_for(dispute), bundle)
        self.assertNotIn('<script>x</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_hostile_claim_id_is_escaped_not_live_markup(self):
        claim = _claim(alf_claim_id='<b>ALFA05</b>', zd_ticket_id='ZD-A05')
        dispute = _dispute(claim, zd_ticket_id='ZD-A05', raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        activity = bundle['timeline'][0]['activity']
        self.assertNotIn('<b>ALFA05</b>', activity)
        self.assertIn('&lt;b&gt;ALFA05&lt;/b&gt;', activity)
        html = render_to_string(ds.report_template_for(dispute), bundle)
        self.assertNotIn('<b>ALFA05</b>', html)


# ---------------------------------------------------------------------------
# B. PayPal filing row — wording depends on dispute_channel + lifecycle stage
# ---------------------------------------------------------------------------

class PayPalFilingRowTests(TestCase):
    def _row_for(self, dispute):
        bundle = _bundle(dispute, [_intake_comment()])
        rows = [e for e in bundle['timeline'] if dispute.paypal_dispute_id in e['text']]
        self.assertEqual(len(rows), 1,
                         f"expected exactly one filing row, timeline={bundle['timeline']}")
        return rows[0]

    def test_external_channel_uses_card_issuer_wording(self):
        claim = _claim(alf_claim_id='ALF-B01', zd_ticket_id='ZD-B01')
        dispute = _dispute(claim, paypal_dispute_id='PP-B01', zd_ticket_id='ZD-B01',
                           dispute_reason='UNAUTHORISED',
                           raw_webhook_payload=_payload('EXTERNAL', 'CHARGEBACK'))
        row = self._row_for(dispute)
        self.assertEqual(
            row['text'],
            'The buyer disputed the payment with their card issuer (PayPal case PP-B01), '
            'alleging “Unauthorized transaction”')
        self.assertIn('<strong>PP-B01</strong>', row['activity'])

    def test_inquiry_stage_internal_uses_opened_wording(self):
        claim = _claim(alf_claim_id='ALF-B02', zd_ticket_id='ZD-B02')
        dispute = _dispute(claim, paypal_dispute_id='PP-B02', zd_ticket_id='ZD-B02',
                           dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
                           raw_webhook_payload=_payload('INTERNAL', 'INQUIRY'))
        row = self._row_for(dispute)
        self.assertEqual(
            row['text'],
            'The buyer opened PayPal dispute PP-B02, alleging “Item not received”')

    def test_other_reason_drops_alleging_clause(self):
        claim = _claim(alf_claim_id='ALF-B03', zd_ticket_id='ZD-B03')
        dispute = _dispute(claim, paypal_dispute_id='PP-B03', zd_ticket_id='ZD-B03',
                           dispute_reason='OTHER',
                           raw_webhook_payload=_payload('INTERNAL', 'CHARGEBACK'))
        row = self._row_for(dispute)
        self.assertNotIn('alleging', row['text'])
        self.assertIn('PP-B03', row['text'])

    def test_blank_reason_drops_alleging_clause(self):
        claim = _claim(alf_claim_id='ALF-B04', zd_ticket_id='ZD-B04')
        dispute = _dispute(claim, paypal_dispute_id='PP-B04', zd_ticket_id='ZD-B04',
                           dispute_reason='', raw_webhook_payload=_payload('INTERNAL', 'CHARGEBACK'))
        row = self._row_for(dispute)
        self.assertNotIn('alleging', row['text'])

    def test_missing_create_time_omits_filing_row(self):
        claim = _claim(alf_claim_id='ALF-B05', zd_ticket_id='ZD-B05')
        dispute = _dispute(claim, paypal_dispute_id='PP-B05', zd_ticket_id='ZD-B05',
                           raw_webhook_payload={'dispute_channel': 'INTERNAL'})
        bundle = _bundle(dispute, [_intake_comment()])
        rows = [e for e in bundle['timeline'] if 'PP-B05' in e['text']]
        self.assertEqual(rows, [])

    def test_reason_wording_map(self):
        wording = {
            'MERCHANDISE_OR_SERVICE_NOT_RECEIVED': 'Item not received',
            'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED': 'Item not as described',
            'UNAUTHORISED': 'Unauthorized transaction',
            'CREDIT_NOT_PROCESSED': 'Credit not processed',
            'DUPLICATE_TRANSACTION': 'Duplicate transaction',
            'INCORRECT_AMOUNT': 'Incorrect amount',
            'PAYMENT_BY_OTHER_MEANS': 'Paid by other means',
            'CANCELED_RECURRING_BILLING': 'Canceled recurring billing',
            'PROBLEM_WITH_REMITTANCE': 'Problem with remittance',
        }
        for i, (reason, phrase) in enumerate(wording.items()):
            with self.subTest(reason=reason):
                claim = _claim(alf_claim_id=f'ALF-B1{i}', zd_ticket_id=f'ZD-B1{i}')
                dispute = _dispute(claim, paypal_dispute_id=f'PP-B1{i}', zd_ticket_id=f'ZD-B1{i}',
                                   dispute_reason=reason,
                                   raw_webhook_payload=_payload('INTERNAL', 'CHARGEBACK'))
                row = self._row_for(dispute)
                self.assertIn(f'alleging “{phrase}”', row['text'])


class PayPalLoggedRowManualDisputeTests(TestCase):
    """C. A manually-created dispute (no real PayPal webhook) must not claim a
    notification was 'received' — we logged it ourselves."""

    def test_manual_dispute_id_uses_logged_wording(self):
        claim = _claim(alf_claim_id='ALF-C01', zd_ticket_id='ZD-C01')
        dispute = _dispute(claim, paypal_dispute_id='MANUAL-ZD-C01', zd_ticket_id='ZD-C01',
                           raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        self.assertEqual(bundle['timeline'][-1]['text'], 'PayPal case logged in our internal system')
        self.assertNotIn('notification received', bundle['timeline'][-1]['text'])


class ClaimSubmittedWordingTests(TestCase):
    """D. Without an alf_claim_id there is no ID to bold or to mention."""

    def test_no_alf_claim_id_drops_bold_and_id(self):
        claim = _claim(alf_claim_id=None, zd_ticket_id='ZD-D01')
        dispute = _dispute(claim, paypal_dispute_id='PP-D01', zd_ticket_id='ZD-D01',
                           raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        first = bundle['timeline'][0]
        self.assertEqual(first['text'], 'Lost-item service request submitted on our website')
        self.assertNotIn('<strong>', first['activity'])


# ---------------------------------------------------------------------------
# E. Recorded acceptance row
# ---------------------------------------------------------------------------

_ACCEPT_75 = (
    'The client was called and informed of the call being recorded for quality and training '
    'purposes to what Client agreed and approved.\n\n'
    'At minute  1:40  on our recorded line, Client approved to move forward with a non '
    'refundable fee of $ 75.00 as Client understood our service and agreed to move forward '
    'knowing no guarantees can be provided on lost items.')

_ACCEPT_65_50 = (
    'The client was called and informed of the call being recorded for quality and training '
    'purposes to what Client agreed and approved.\n\n'
    'At minute  2:15  on our recorded line, Client approved to move forward with a non '
    'refundable fee of $65.50 as Client understood our service and agreed to move forward '
    'knowing no guarantees can be provided on lost items.')

_ACCEPT_NO_AMOUNT = (
    'The client was called and informed of the call being recorded for quality and training '
    'purposes to what Client agreed and approved.\n\n'
    'At minute  3:30  on our recorded line, Client approved to move forward with a non '
    'refundable fee as Client understood our service and agreed to move forward knowing no '
    'guarantees can be provided on lost items.')

_ACCEPT_NO_GUARANTEE_WORD = (
    'The client was called and informed of the call being recorded for quality and training '
    'purposes to what Client agreed and approved.\n\n'
    'At minute  4:45  on our recorded line, Client approved to move forward with a non '
    'refundable fee of $75.00 as Client understood our service and agreed to move forward.')


class RecordedAcceptanceTimelineRowTests(TestCase):
    def _acceptance_row(self, dispute, comments):
        bundle = _bundle(dispute, comments)
        matches = [e for e in bundle['timeline'] if 'non-refundable' in e['text']]
        self.assertEqual(len(matches), 1, bundle['timeline'])
        return matches[0]

    def test_fee_75_formats_without_cents_at_the_notes_own_time(self):
        claim = _claim(alf_claim_id='ALF-E01', zd_ticket_id='ZD-E01')
        dispute = _dispute(claim, zd_ticket_id='ZD-E01', raw_webhook_payload={})
        row = self._acceptance_row(dispute, [_intake_comment(),
                                             _agent_note(_ACCEPT_75, '2026-09-18T16:56:10Z')])
        self.assertEqual(row['when'], 'Sep 18, 2026 11:56')
        self.assertIn('non-refundable $75 service fee', row['text'])
        self.assertIn('understanding that recovery of the lost item could not be guaranteed',
                      row['text'])
        self.assertIn('<strong>non-refundable $75 service fee</strong>', row['activity'])

    def test_fee_65_50_keeps_cents(self):
        claim = _claim(alf_claim_id='ALF-E02', zd_ticket_id='ZD-E02')
        dispute = _dispute(claim, zd_ticket_id='ZD-E02', raw_webhook_payload={})
        row = self._acceptance_row(dispute, [_intake_comment(),
                                             _agent_note(_ACCEPT_65_50, '2026-09-18T16:56:10Z')])
        self.assertIn('non-refundable $65.50 service fee', row['text'])

    def test_no_amount_in_note_falls_back_to_claim_price_paid(self):
        claim = _claim(alf_claim_id='ALF-E03', zd_ticket_id='ZD-E03', price_paid=Decimal('75.00'))
        dispute = _dispute(claim, zd_ticket_id='ZD-E03', raw_webhook_payload={})
        row = self._acceptance_row(dispute, [_intake_comment(),
                                             _agent_note(_ACCEPT_NO_AMOUNT, '2026-09-18T16:56:10Z')])
        self.assertIn('non-refundable $75 service fee', row['text'])

    def test_no_guarantee_word_drops_the_clause(self):
        claim = _claim(alf_claim_id='ALF-E04', zd_ticket_id='ZD-E04')
        dispute = _dispute(claim, zd_ticket_id='ZD-E04', raw_webhook_payload={})
        row = self._acceptance_row(
            dispute, [_intake_comment(), _agent_note(_ACCEPT_NO_GUARANTEE_WORD, '2026-09-18T16:56:10Z')])
        self.assertIn('non-refundable $75 service fee', row['text'])
        self.assertNotIn('understanding that recovery', row['text'])

    def test_no_acceptance_note_no_row(self):
        claim = _claim(alf_claim_id='ALF-E05', zd_ticket_id='ZD-E05')
        dispute = _dispute(claim, zd_ticket_id='ZD-E05', raw_webhook_payload={})
        bundle = _bundle(dispute, [_intake_comment()])
        joined = ' '.join(e['text'] for e in bundle['timeline'])
        self.assertNotIn('non-refundable', joined)
        self.assertNotIn('agreed to proceed', joined)


# ---------------------------------------------------------------------------
# F. Office-name expansion — codes resolved ONLY from the claim's own data
# ---------------------------------------------------------------------------

class OfficeNameExpansionTests(TestCase):
    def _filing_row(self, claim_kw, first_line):
        claim = _claim(**claim_kw)
        dispute = _dispute(claim, zd_ticket_id=claim_kw['zd_ticket_id'], raw_webhook_payload={})
        note = _agent_note(first_line, '2026-09-18T17:07:21Z')
        note['attachments'] = [{'content_type': 'image/png',
                                'content_url': 'https://example.zendesk.com/a.png',
                                'file_name': 'a.png'}]
        bundle = _bundle(dispute, [_intake_comment(), note])
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        return rows[0]

    def test_airport_code_expands_from_flight_details(self):
        row = self._filing_row(
            dict(alf_claim_id='ALF-F01', zd_ticket_id='ZD-F01',
                 flight_details='Airport: George Bush Intercontinental Airport / IAH'),
            'IAH')
        self.assertIn('George Bush Intercontinental Airport (IAH)', row['text'])

    def test_airline_code_expands_from_flight_details(self):
        row = self._filing_row(
            dict(alf_claim_id='ALF-F02', zd_ticket_id='ZD-F02',
                 flight_details='Airline: United Airlines - UA'),
            'UA')
        self.assertIn('United Airlines (UA)', row['text'])

    def test_leg_airport_expands_from_flight_data(self):
        row = self._filing_row(
            dict(alf_claim_id='ALF-F03', zd_ticket_id='ZD-F03',
                 flight_data={'legs': [{'from_iata': 'ORD', 'from_name': "O'Hare International Airport",
                                        'to_iata': 'LAX', 'to_name': 'Los Angeles International Airport'}]}),
            'ORD')
        self.assertIn("O'Hare International Airport (ORD)", row['text'])

    def test_airline_expands_from_flight_data_number_prefix(self):
        row = self._filing_row(
            dict(alf_claim_id='ALF-F04', zd_ticket_id='ZD-F04',
                 flight_data={'number': 'AA3196', 'airline': 'American Airlines'}),
            'AA')
        self.assertIn('American Airlines (AA)', row['text'])

    def test_unknown_code_stays_as_written(self):
        row = self._filing_row(dict(alf_claim_id='ALF-F05', zd_ticket_id='ZD-F05'), 'LAX')
        self.assertIn('LAX', row['text'])
        self.assertNotIn('(LAX)', row['text'])   # no invented expansion for an unrecognised code

    def test_tsa_alone_stays_tsa(self):
        row = self._filing_row(dict(alf_claim_id='ALF-F06', zd_ticket_id='ZD-F06'), 'TSA')
        self.assertIn('TSA', row['text'])

    def test_tsa_then_airport_code_expands_the_airport(self):
        row = self._filing_row(
            dict(alf_claim_id='ALF-F07', zd_ticket_id='ZD-F07',
                 flight_details="Airport: O'Hare International Airport / ORD"),
            'TSA ORD')
        self.assertIn("TSA at O'Hare International Airport (ORD)", row['text'])

    def test_airport_code_then_tsa_also_expands(self):
        row = self._filing_row(
            dict(alf_claim_id='ALF-F08', zd_ticket_id='ZD-F08',
                 flight_details="Airport: O'Hare International Airport / ORD"),
            'ORD TSA')
        self.assertIn("TSA at O'Hare International Airport (ORD)", row['text'])

    def test_via_email_suffix_stripped_unknown_code_kept(self):
        row = self._filing_row(dict(alf_claim_id='ALF-F09', zd_ticket_id='ZD-F09'), 'HNL VIA E-MAIL')
        self.assertIn('HNL', row['text'])
        self.assertNotIn('via', row['text'].lower())

    def test_multiword_allcaps_label_is_title_cased(self):
        row = self._filing_row(dict(alf_claim_id='ALF-F10', zd_ticket_id='ZD-F10'), 'SOUTHWEST')
        self.assertIn('Southwest', row['text'])
        self.assertNotIn('SOUTHWEST', row['text'])


# ---------------------------------------------------------------------------
# G. Notes that must NEVER become a filing row, despite looking like one
# ---------------------------------------------------------------------------

class NonFilingNoiseTests(TestCase):
    NOT_FILINGS = [
        '**Call Recording Summary**', '2 Possible Flights', 'Correct Flight',
        'Flight IS A NO Match', 'Update', 'Updates', 'TSA Update',
        'No Match In LHR', '1St And 2 Call Attempt',
    ]

    def _timeline_with_image_note(self, tag, body):
        claim = _claim(alf_claim_id=f'ALF-{tag}', zd_ticket_id=f'ZD-{tag}')
        dispute = _dispute(claim, paypal_dispute_id=f'PP-{tag}', zd_ticket_id=f'ZD-{tag}',
                           raw_webhook_payload={})
        note = _agent_note(body, '2026-09-18T17:07:21Z')
        note['attachments'] = [{'content_type': 'image/png',
                                'content_url': 'https://example.zendesk.com/a.png',
                                'file_name': 'a.png'}]
        bundle = _bundle(dispute, [_intake_comment(), note])
        return bundle['timeline']

    def test_lookalike_labels_never_become_filings(self):
        for i, label in enumerate(self.NOT_FILINGS):
            with self.subTest(label=label):
                tl = self._timeline_with_image_note(f'G{i:02d}', label)
                joined = ' '.join(e['text'] for e in tl)
                self.assertNotIn('We reported the loss', joined)
                self.assertNotIn('submitted through', joined)
                self.assertNotIn(label.strip('*'), joined)

    def test_via_email_phrase_with_attachment_is_not_a_filing(self):
        tl = self._timeline_with_image_note('GVEM', 'Sent the update via email')
        joined = ' '.join(e['text'] for e in tl)
        self.assertNotIn('submitted through', joined)
        self.assertNotIn('We reported the loss', joined)

    def test_southwest_with_image_is_still_a_filing(self):
        tl = self._timeline_with_image_note('GSW1', 'SOUTHWEST')
        texts = [e['text'] for e in tl]
        self.assertIn('Lost-item information was submitted through Southwest channels', texts)

    def test_report_submitted_text_without_image_is_a_filing(self):
        claim = _claim(alf_claim_id='ALF-GRS1', zd_ticket_id='ZD-GRS1')
        dispute = _dispute(claim, zd_ticket_id='ZD-GRS1', raw_webhook_payload={})
        note = _agent_note('Report submitted to the airline lost and found desk, reference #4471.',
                           '2026-09-18T17:07:21Z')
        bundle = _bundle(dispute, [_intake_comment(), note])
        joined = ' '.join(e['text'] for e in bundle['timeline'])
        self.assertIn('submitted through', joined)


# ---------------------------------------------------------------------------
# H. Merging consecutive filing notes into one row
# ---------------------------------------------------------------------------

class FilingMergeTests(TestCase):
    def _filing_note(self, label, when):
        note = _agent_note(label, when)
        note['attachments'] = [{'content_type': 'image/png',
                                'content_url': 'https://example.zendesk.com/x.png', 'file_name': 'x.png'}]
        return note

    def test_three_consecutive_filings_merge_into_one_row(self):
        claim = _claim(alf_claim_id='ALF-H01', zd_ticket_id='ZD-H01')
        dispute = _dispute(claim, zd_ticket_id='ZD-H01', raw_webhook_payload={})
        comments = [_intake_comment(),
                    self._filing_note('AAA', '2026-09-18T17:30:00Z'),
                    self._filing_note('BBB', '2026-09-18T17:31:00Z'),
                    self._filing_note('CCC', '2026-09-18T17:33:00Z')]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertIn('AAA, BBB, and CCC', rows[0]['text'])
        self.assertEqual(rows[0]['when'], 'Sep 18, 2026 12:30')   # the first note's own time

    def test_two_offices_joined_without_oxford_comma(self):
        claim = _claim(alf_claim_id='ALF-H02', zd_ticket_id='ZD-H02')
        dispute = _dispute(claim, zd_ticket_id='ZD-H02', raw_webhook_payload={})
        comments = [_intake_comment(),
                    self._filing_note('AAA', '2026-09-18T17:30:00Z'),
                    self._filing_note('BBB', '2026-09-18T17:31:00Z')]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 1)
        self.assertIn('AAA and BBB', rows[0]['text'])

    def test_filings_two_hours_apart_stay_separate_rows(self):
        claim = _claim(alf_claim_id='ALF-H03', zd_ticket_id='ZD-H03')
        dispute = _dispute(claim, zd_ticket_id='ZD-H03', raw_webhook_payload={})
        comments = [_intake_comment(),
                    self._filing_note('AAA', '2026-09-18T17:30:00Z'),
                    self._filing_note('BBB', '2026-09-18T19:30:00Z')]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 2)

    def test_public_email_between_filings_breaks_the_merge(self):
        claim = _claim(alf_claim_id='ALF-H04', zd_ticket_id='ZD-H04', client_email='h04@example.com')
        dispute = _dispute(claim, zd_ticket_id='ZD-H04', raw_webhook_payload={})
        comments = [
            _intake_comment(client_email='h04@example.com'),
            self._filing_note('AAA', '2026-09-18T17:30:00Z'),
            {'author': {'name': 'Agent', 'email': 'a@alf.example'}, 'public': True, 'channel': 'email',
             'created_at': '2026-09-18T17:31:00Z', 'body': 'We are still searching.', 'attachments': []},
            self._filing_note('BBB', '2026-09-18T17:33:00Z'),
        ]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 2)


# ---------------------------------------------------------------------------
# I. Call rows never assert whether the call was answered
# ---------------------------------------------------------------------------

class CallRowWordingTests(TestCase):
    def test_outbound_short_call_wording(self):
        claim = _claim(alf_claim_id='ALF-I01', zd_ticket_id='ZD-I01')
        dispute = _dispute(claim, zd_ticket_id='ZD-I01', raw_webhook_payload={})
        comments = [_intake_comment(), _voice_comment(13, 'outbound')]
        bundle = _bundle(dispute, comments)
        texts = [e['text'] for e in bundle['timeline']]
        self.assertIn('We called the customer (13 seconds)', texts)

    def test_inbound_long_call_wording(self):
        claim = _claim(alf_claim_id='ALF-I02', zd_ticket_id='ZD-I02')
        dispute = _dispute(claim, zd_ticket_id='ZD-I02', raw_webhook_payload={})
        comments = [_intake_comment(), _voice_comment(331, 'inbound')]
        bundle = _bundle(dispute, comments)
        texts = [e['text'] for e in bundle['timeline']]
        self.assertIn('The customer called us (5m 31s)', texts)

    def test_no_call_row_implies_answer_status(self):
        claim = _claim(alf_claim_id='ALF-I03', zd_ticket_id='ZD-I03')
        dispute = _dispute(claim, zd_ticket_id='ZD-I03', raw_webhook_payload={})
        comments = [_intake_comment(),
                    _voice_comment(13, 'outbound', created_at='2026-09-18T16:54:58Z'),
                    _voice_comment(331, 'inbound', created_at='2026-09-18T18:00:00Z')]
        bundle = _bundle(dispute, comments)
        call_texts = [e['text'] for e in bundle['timeline'] if 'called' in e['text'].lower()]
        self.assertEqual(len(call_texts), 2)
        for forbidden in ('answered', 'unanswered', 'voicemail', 'spoke', 'reached'):
            for t in call_texts:
                self.assertNotIn(forbidden, t.lower())


# ---------------------------------------------------------------------------
# J. Public comments: our updates vs. the customer's replies
# ---------------------------------------------------------------------------

class PublicCommentRowTests(TestCase):
    def test_our_reply_wording(self):
        claim = _claim(alf_claim_id='ALF-J01', zd_ticket_id='ZD-J01')
        dispute = _dispute(claim, zd_ticket_id='ZD-J01', raw_webhook_payload={})
        comments = [_intake_comment(),
                    {'author': {'name': 'Agent', 'email': 'a@alf.example'}, 'public': True,
                     'channel': 'email', 'created_at': '2026-09-19T12:00:00Z',
                     'body': 'Checking in with an update.', 'attachments': []}]
        bundle = _bundle(dispute, comments)
        texts = [e['text'] for e in bundle['timeline']]
        self.assertIn('We emailed the customer an update on their case', texts)

    def test_customer_reply_wording(self):
        claim = _claim(alf_claim_id='ALF-J02', zd_ticket_id='ZD-J02', client_email='j02@example.com')
        dispute = _dispute(claim, zd_ticket_id='ZD-J02', raw_webhook_payload={})
        comments = [_intake_comment(client_email='j02@example.com'),
                    {'author': {'name': 'Test Client', 'email': 'j02@example.com'}, 'public': True,
                     'channel': 'email', 'created_at': '2026-09-19T12:00:00Z',
                     'body': 'Any news?', 'attachments': []}]
        bundle = _bundle(dispute, comments)
        texts = [e['text'] for e in bundle['timeline']]
        self.assertIn('The customer replied to us', texts)

    def test_rows_are_chronological_in_central_time(self):
        claim = _claim(alf_claim_id='ALF-J03', zd_ticket_id='ZD-J03')
        dispute = _dispute(claim, zd_ticket_id='ZD-J03',
                           raw_webhook_payload=_payload('INTERNAL', 'CHARGEBACK'))
        bundle = _bundle(dispute, _base_comments())
        parsed = [datetime.strptime(e['when'], '%b %d, %Y %H:%M') for e in bundle['timeline']]
        self.assertEqual(parsed, sorted(parsed))
        self.assertTrue(all('when' in e and 'activity' in e and 'text' in e
                            for e in bundle['timeline']))


# ---------------------------------------------------------------------------
# K. The call-context bug: what the AI actually sees
# ---------------------------------------------------------------------------

class CallContextAIInputTests(TestCase):
    """Nothing in a Zendesk call record says whether the customer answered —
    only who handled it. The text handed to any AI, and the untrusted notes
    input built for PayPal, must never claim otherwise."""

    def test_evidence_narrative_ai_input_has_no_answered_claim(self):
        claim = _claim(alf_claim_id='ALF-K01', zd_ticket_id='ZD-K01')
        dispute = _dispute(claim, zd_ticket_id='ZD-K01', raw_webhook_payload={})
        comments = [_intake_comment(),
                    _voice_comment(170, 'outbound', created_at='2026-09-18T16:54:58Z',
                                  started_at='2026-09-18T16:52:07Z')]
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()
        captured = {}

        def fake_complete(*, call_site=None, untrusted=None, **kw):
            from apps.ai.schemas import EvidenceNarrative
            if call_site == 'dispute_evidence_narrative':
                captured['untrusted'] = untrusted
            return EvidenceNarrative(items=[])

        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': comments}), \
             patch('apps.ai.client.AIClient.complete', side_effect=fake_complete):
            ds.build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=True)

        self.assertIn('untrusted', captured, 'dispute_evidence_narrative was never called')
        blob = str(captured['untrusted'])
        self.assertIn('2m 50s', blob)   # the call length IS present
        for forbidden in ('NOT answered', 'did not speak', '(answered)'):
            self.assertNotIn(forbidden, blob)

    def test_narrative_untrusted_never_claims_answered_status(self):
        claim = _claim(alf_claim_id='ALF-K02', zd_ticket_id='ZD-K02')
        dispute = _dispute(claim, zd_ticket_id='ZD-K02',
                           raw_webhook_payload=_payload('INTERNAL', 'CHARGEBACK'))
        bundle = _bundle(dispute, _base_comments())
        untrusted = ds._narrative_untrusted(bundle)
        blob = str(untrusted)
        for forbidden in ('NOT answered', 'did not speak', '(answered)'):
            self.assertNotIn(forbidden, blob)


# ---------------------------------------------------------------------------
# L. Call card in the rendered report
# ---------------------------------------------------------------------------

class CallCardRenderingTests(TestCase):
    def _render_call(self, direction, tag):
        claim = _claim(alf_claim_id=f'ALF-{tag}', zd_ticket_id=f'ZD-{tag}')
        dispute = _dispute(claim, zd_ticket_id=f'ZD-{tag}', raw_webhook_payload={})
        comments = [_intake_comment(), _voice_comment(90, direction)]
        bundle = _bundle(dispute, comments)
        return render_to_string(ds.report_template_for(dispute), bundle)

    def test_outbound_call_shows_called_by_not_answered_by(self):
        html = self._render_call('outbound', 'LOUT')
        self.assertIn('Called by Agent One', html)
        self.assertNotIn('Answered by', html)

    def test_inbound_call_still_shows_answered_by(self):
        html = self._render_call('inbound', 'LIN1')
        self.assertIn('Answered by Agent One', html)
