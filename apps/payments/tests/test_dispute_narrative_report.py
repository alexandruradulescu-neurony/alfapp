"""Narrative evidence report — simulated Zendesk panels, flight card, and the
template render that replaces the browser-screenshot pipeline (2026-06-14)."""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Dispute
from apps.payments import document_service as ds
from apps.payments import frontend_views


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-D-N1', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED')
    base.update(kw)
    return Dispute.objects.create(**base)


FLIGHT_DATA = {
    'number': 'AA3196', 'airline': 'American Airlines', 'status': 'Arrived',
    'legs': [{
        'from_iata': 'CLT', 'from_city': 'Charlotte', 'to_iata': 'PBI', 'to_city': 'West Palm Beach',
        'scheduled_departure_local': '2026-02-03 18:25-05:00',
        'scheduled_arrival_local': '2026-02-03 20:05-05:00',
        'status': 'Arrived', 'from_gate': 'C10', 'to_gate': 'B12',
        'from_terminal': '', 'to_terminal': '',
    }],
}

COMMENTS = [
    {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': False,
     'created_at': '2026-02-03T21:14:00Z', 'body': 'Registration ID: ALF5490789',
     'attachments': [{'content_type': 'image/png', 'content_url': 'https://zd/att/1.png',
                      'file_name': 'flight.png'}]},
    {'author': {'name': 'Joe Snyder', 'email': 'j@alf.com'}, 'public': True,
     'created_at': '2026-02-04T10:32:00Z', 'body': 'Dear Lee, an update on your search.',
     'attachments': []},
]


class DisputeBusinessUnderstandingTests(TestCase):
    """The dispute AI must understand ALF's business to argue a case, and must
    see the buyer's own complaint so it rebuts the specific reasons given."""

    def test_both_prompts_carry_business_context(self):
        for prompt in (ds.EVIDENCE_NARRATIVE_SYSTEM_PROMPT, ds.EVIDENCE_NOTES_SYSTEM_PROMPT):
            self.assertIn('paid concierge service', prompt)
            self.assertIn('NON-REFUNDABLE', prompt)
            self.assertIn('authorise', prompt.lower())

    def test_buyer_complaint_reaches_notes_ai_as_untrusted(self):
        from apps.ai.prompt_fence import ALLOWED_TAGS
        self.assertIn('buyer_dispute_statement', ALLOWED_TAGS)
        d = _dispute(raw_webhook_payload={'evidences': [
            {'source': 'SUBMITTED_BY_BUYER',
             'notes': 'This company is a scam, I never authorised this.'}]})
        u = ds._narrative_untrusted({'dispute': d, 'panels': []})
        self.assertIn('buyer_dispute_statement', u)
        self.assertIn('never authorised', u['buyer_dispute_statement'])


class PanelBuilderTests(TestCase):
    def test_internal_vs_public_and_image_embed(self):
        with patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'):
            panels = ds._zendesk_comment_panels(COMMENTS)
        self.assertEqual(len(panels), 2)
        self.assertFalse(panels[0]['public'])
        self.assertTrue(panels[1]['public'])
        self.assertEqual(len(panels[0]['images']), 1)   # internal note had an image attachment
        self.assertEqual(panels[1]['images'], [])       # public reply had none
        self.assertIn('Registration ID', panels[0]['body'])
        self.assertEqual(panels[0]['author'], 'Mark Johnson')

    def test_non_image_attachment_never_downloads(self):
        comments = [{'author': {}, 'public': True, 'body': 'x',
                     'attachments': [{'content_type': 'application/pdf',
                                      'content_url': 'u', 'file_name': 'a.pdf'}]}]
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes') as net:
            panels = ds._zendesk_comment_panels(comments)
            net.assert_not_called()
        self.assertEqual(panels[0]['images'], [])
        # empty author on a public comment now falls back to a presentable label
        self.assertEqual(panels[0]['author'], 'Support agent')


class FlightCardTests(TestCase):
    def test_card_from_flight_data(self):
        claim = Claim.objects.create(client_email='b@example.com', flight_data=FLIGHT_DATA)
        card = ds._flight_card(claim)
        self.assertEqual(card['number'], 'AA3196')
        self.assertEqual(card['from_iata'], 'CLT')
        self.assertEqual(card['to_iata'], 'PBI')
        self.assertEqual(card['to_gate'], 'B12')

    def test_none_without_legs_or_claim(self):
        claim = Claim.objects.create(client_email='b@example.com', flight_data={})
        self.assertIsNone(ds._flight_card(claim))
        self.assertIsNone(ds._flight_card(None))


class NarrativeFieldsTests(TestCase):
    def test_fee_prefers_price_paid_and_first_object_line(self):
        claim = Claim.objects.create(
            client_email='b@example.com', client_name='Lee Foley', alf_claim_id='ALF5490789',
            object_description='Tablet\nApple iPad Pro 11-inch', price_paid=Decimal('74.00'))
        d = _dispute(claim=claim, dispute_amount=Decimal('100.00'))
        nf = ds._narrative_fields(d)
        self.assertEqual(nf['client_name'], 'Lee Foley')
        self.assertEqual(nf['alf_id'], 'ALF5490789')
        self.assertEqual(nf['object'], 'Apple iPad Pro 11-inch')  # specific item, not the generic category
        self.assertEqual(nf['fee'], Decimal('74.00'))      # price_paid wins over dispute_amount

    def test_fee_falls_back_to_dispute_amount_and_buyer_name(self):
        d = _dispute(claim=None, buyer_name='Jane Buyer', dispute_amount=Decimal('55.00'))
        nf = ds._narrative_fields(d)
        self.assertEqual(nf['client_name'], 'Jane Buyer')
        self.assertEqual(nf['fee'], Decimal('55.00'))
        self.assertEqual(nf['alf_id'], '')


class FramingTests(TestCase):
    def test_known_category_has_framing(self):
        self.assertIn('authorised', ds.CATEGORY_FRAMING['UNAUTHORISED']['lead'].lower())

    def test_unknown_category_uses_default(self):
        d = _dispute(dispute_reason='')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': []}):
            bundle = ds.build_dispute_evidence_bundle(d, embed_attachments=False)
        self.assertEqual(bundle['framing'], ds.DEFAULT_FRAMING)


class TemplateRenderTests(TestCase):
    def test_renders_narrative_html_end_to_end(self):
        claim = Claim.objects.create(
            client_email='b@example.com', client_name='Lee Foley', alf_claim_id='ALF5490789',
            zd_ticket_id='97001', object_description='iPad Tablet', flight_data=FLIGHT_DATA,
            price_paid=Decimal('74.00'), lost_location='TSA / Security Check')
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_reason='UNAUTHORISED',
                     dispute_amount=Decimal('74.00'), dispute_currency='USD')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': '97001'}, 'comments': COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        # narrative header + identity
        self.assertIn('Dispute Settlement Support Information', html)
        self.assertIn('Lee Foley', html)
        self.assertIn('ALF5490789', html)
        # category framing
        self.assertIn(ds.CATEGORY_FRAMING['UNAUTHORISED']['headline'], html)
        # simulated panels + badges
        # the "Registration ID…" note is pinned as the lead of the case record
        self.assertIn('Claim submitted by the customer', html)
        # public reply from a non-client author → an outbound-to-customer panel
        self.assertIn('Email to the customer', html)
        # flight card rebuilt from data
        self.assertIn('American Airlines', html)
        self.assertIn('CLT', html)
        # conclusion present
        self.assertIn('Conclusion', html)

    def test_intake_panel_rendered_exactly_once(self):
        # Regression: the intake panel ("Claim submitted by the customer") used to
        # render twice — once as the case-record lead and again inside the
        # SERVICE_INITIATION section — printing two identical panels back to back
        # (worst on "not as described", where that section leads).
        claim = Claim.objects.create(
            client_email='b@example.com', client_name='Lee Foley', alf_claim_id='ALF5490789',
            zd_ticket_id='97001', object_description='iPad', price_paid=Decimal('74.00'))
        d = _dispute(claim=claim, zd_ticket_id='97001',
                     dispute_reason='MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
                     dispute_amount=Decimal('74.00'), dispute_currency='USD')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': '97001'}, 'comments': COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertEqual(html.count('Claim submitted by the customer'), 1)

    def test_abandoned_cart_notice_dropped_from_evidence(self):
        # Regression: the WooCommerce "abandoned cart" notice predates payment and
        # is pre-claim noise — it must never appear as case evidence, regardless of
        # whether/how the AI classifies records.
        claim = Claim.objects.create(client_email='cust@x.com', client_name='Cust',
                                     alf_claim_id='ALFAC', zd_ticket_id='97001',
                                     price_paid=Decimal('74.00'))
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_reason='UNAUTHORISED')
        comments = [
            {'author': {'name': 'Cust', 'email': 'cust@x.com'}, 'public': True,
             'body': 'A new abandoned cart has been created for Cust', 'attachments': [],
             'channel': 'email', 'created_at': '2026-06-13T17:41:00Z'},
            {'author': {'name': 'Agent', 'email': 'a@alf.com'}, 'public': True,
             'body': 'searching now', 'attachments': [],
             'channel': 'email', 'created_at': '2026-06-14T13:00:00Z'},
        ]
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': '97001'}, 'comments': comments}):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertNotIn('abandoned cart', html.lower())
        self.assertTrue(all('abandoned cart' not in (p.get('body') or '').lower()
                            for p in bundle['panels']))


class PanelFidelityTests(TestCase):
    """Zendesk-faithful panels: call cards, and inbound/outbound/internal direction."""

    def _voice(self, duration=30, direction='outbound'):
        return {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': False,
                'body': 'Outbound call to +1 (425) 652-8782 ...', 'attachments': [],
                'channel': 'voice', 'created_at': '2026-06-14T17:00:32Z',
                'call': {'direction': direction, 'from_name': 'Airport Lost Found',
                         'from_phone': '+1 (831) 273-4817', 'to_name': 'Elizabeth',
                         'to_phone': '+1 (425) 652-8782', 'started_at': '2026-06-14T17:00:32Z',
                         'duration': duration, 'answered_by': 'Mark Johnson', 'recorded': True}}

    def test_voice_comment_becomes_a_call_card(self):
        p = ds._zendesk_comment_panels([self._voice()], embed_images=False, client_email='e@x.com')[0]
        self.assertEqual(p['kind'], 'call')
        self.assertEqual(p['call']['label'], 'Outbound call')
        self.assertEqual(p['call']['length'], '30 seconds')
        self.assertEqual(p['call']['answered_by'], 'Mark Johnson')
        self.assertIn('+1 (425) 652-8782', p['call']['to'])
        self.assertTrue(p['call']['recorded'])

    def test_inbound_outbound_internal_classification(self):
        comments = [
            {'author': {'name': 'Cust', 'email': 'cust@x.com'}, 'public': True, 'body': 'lost it',
             'attachments': [], 'channel': 'email', 'created_at': '2026-06-14T12:00:00Z'},
            {'author': {'name': 'Agent', 'email': 'a@alf.com'}, 'public': True, 'body': 'searching',
             'attachments': [], 'channel': 'email', 'created_at': '2026-06-14T13:00:00Z'},
            {'author': {'name': 'Agent', 'email': 'a@alf.com'}, 'public': False, 'body': 'note',
             'attachments': [], 'channel': 'web', 'created_at': '2026-06-14T14:00:00Z'},
        ]
        dirs = [p['direction'] for p in ds._zendesk_comment_panels(
            comments, embed_images=False, client_email='cust@x.com')]
        self.assertEqual(dirs, ['inbound', 'outbound', 'internal'])

    def test_duration_formatting(self):
        self.assertEqual(ds._fmt_call_duration(30), '30 seconds')
        self.assertEqual(ds._fmt_call_duration(1), '1 second')
        self.assertEqual(ds._fmt_call_duration(149), '2m 29s')
        self.assertEqual(ds._fmt_call_duration(None), '')

    def test_call_card_renders_with_all_fields(self):
        claim = Claim.objects.create(client_email='cust@x.com', client_name='Cust',
                                     alf_claim_id='ALFCALL', zd_ticket_id='97001',
                                     price_paid=Decimal('74.00'))
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_reason='UNAUTHORISED')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': '97001'}, 'comments': [self._voice(duration=149)]}):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertIn('Outbound call', html)
        self.assertIn('2m 29s', html)              # length
        self.assertIn('+1 (425) 652-8782', html)   # to number shown on the card
        self.assertIn('Answered by', html)


class TimelineTests(TestCase):
    """The case timeline starts with the claim submission (with date+time), lists
    our actions + customer replies chronologically, and drops pre-claim noise."""

    def test_ordered_timestamped_and_drops_pre_claim_noise(self):
        claim = Claim.objects.create(client_email='cust@x.com', alf_claim_id='ALFTL')
        Claim.objects.filter(pk=claim.pk).update(
            created_at=datetime(2026, 6, 13, 17, 43, tzinfo=dt_tz.utc))
        claim.refresh_from_db()
        d = _dispute(claim=claim)
        Dispute.objects.filter(pk=d.pk).update(
            created_at=datetime(2026, 6, 20, 12, 0, tzinfo=dt_tz.utc))
        d.refresh_from_db()
        comments = [
            {'author': {'email': 'cust@x.com'}, 'public': True, 'channel': 'email',
             'created_at': '2026-06-13T17:41:00Z', 'body': 'A new abandoned cart has been created'},
            {'author': {'email': 'a@alf.com'}, 'public': False, 'channel': 'voice',
             'created_at': '2026-06-13T17:57:00Z', 'body': '',
             'call': {'direction': 'outbound', 'duration': 401, 'started_at': '2026-06-13T17:57:00Z'}},
            {'author': {'email': 'a@alf.com'}, 'public': True, 'channel': 'email',
             'created_at': '2026-06-16T17:02:00Z', 'body': 'update'},
            {'author': {'email': 'cust@x.com'}, 'public': True, 'channel': 'email',
             'created_at': '2026-06-17T09:00:00Z', 'body': 'thanks'},
        ]
        tl = ds._build_timeline(d, comments)
        labels = [e['label'] for e in tl]
        self.assertEqual(labels[0], 'Claim submitted on our website')   # genuine first step
        self.assertEqual(labels[-1], 'PayPal dispute received')
        self.assertIn('We called the customer (6m 41s)', labels)
        self.assertIn('We emailed the customer an update', labels)
        self.assertIn('The customer replied to us', labels)
        self.assertNotIn('abandoned cart', ' '.join(labels).lower())     # pre-claim noise dropped
        self.assertNotIn('First contacted the customer', labels)         # we never initiate
        self.assertRegex(tl[0]['when'], r'\d{1,2}:\d{2}')                 # has a timestamp


class CommentHtmlCleanupTests(TestCase):
    """Merged-ticket / MMS notes carry HTML in the plain body; render it as clean
    text and embed the linked image (don't print raw <br>/<a href> markup)."""

    MMS_BODY = ('Request #54240 "Message from: Dionna Bassham" was closed and merged.\n'
                'Dionna Bassham has sent the following MMS:<br/><br/>Attachments:<br/>'
                '<a href="https://airportlf.zendesk.com/attachments/token/abc/?name=mms_x.jpeg" '
                'target="_blank">mms_x.jpeg</a><br/>')

    def test_html_body_rendered_as_text(self):
        out = ds._clean_comment_body(self.MMS_BODY)
        self.assertNotIn('<br', out)
        self.assertNotIn('<a ', out)
        self.assertNotIn('href=', out)
        self.assertNotIn('mms_x.jpeg', out)        # image link dropped (embedded separately)
        self.assertIn('Attachments:', out)         # surrounding text kept

    def test_anchor_image_url_is_extracted_for_embedding(self):
        urls = ds._comment_inline_image_urls({'body': self.MMS_BODY, 'html_body': ''})
        self.assertTrue(any('/attachments/token/' in u for u in urls))

    def test_non_image_anchor_keeps_its_text(self):
        out = ds._clean_comment_body('Read <a href="https://x.com/terms">our terms</a> here.')
        self.assertIn('our terms', out)
        self.assertIn('here.', out)
        self.assertNotIn('<a', out)


class TransientDisputePreviewTests(TestCase):
    """--zd-ticket preview builds an UNSAVED dispute; the bundle must not try to
    query related screenshots by it (Django rejects unsaved related filters)."""

    def test_bundle_works_for_unsaved_dispute(self):
        claim = Claim.objects.create(client_email='b@example.com', zd_ticket_id='97001',
                                     flight_data=FLIGHT_DATA)
        dispute = Dispute(claim=claim, zd_ticket_id='97001', paypal_dispute_id='PREVIEW-97001',
                          dispute_reason='UNAUTHORISED', buyer_email='b@example.com',
                          transaction_id='PREVIEW',
                          transaction_date=datetime(2026, 2, 3, tzinfo=dt_tz.utc), status='RECEIVED')
        self.assertIsNone(dispute.pk)  # transient
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}):
            bundle = ds.build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=False)
        # The screenshot service was removed; the bundle no longer carries it.
        self.assertNotIn('screenshots', bundle)
        # Current bundle shape — and it builds for a transient (unsaved) dispute.
        for key in ('panels', 'sections', 'flight_card', 'narrative'):
            self.assertIn(key, bundle)
        self.assertTrue(bundle['flight_card'])
        self.assertEqual(len(bundle['panels']), 2)


class CommentCleanupTests(TestCase):
    def test_strips_internal_ai_trailer_and_markdown(self):
        body = ("**From:** someone@x.com\n**Subject:** Object enquiry\n"
                "We are sorry to inform you, the item has not been found.\n"
                "---\n"
                "**AI Analysis**\n**Category:** OBJECT_NOT_FOUND\n**Auto-Resolved:** Yes")
        clean = ds._clean_comment_body(body)
        self.assertIn('the item has not been found', clean)
        self.assertNotIn('AI Analysis', clean)
        self.assertNotIn('OBJECT_NOT_FOUND', clean)
        self.assertNotIn('**', clean)
        self.assertNotIn('---', clean)

    def test_strips_inline_image_markdown(self):
        body = 'DELTA\n![](https://airportlf.zendesk.com/attachments/token/abc/?name=image.png)'
        clean = ds._clean_comment_body(body)
        self.assertEqual(clean, 'DELTA')
        self.assertNotIn('![](', clean)
        self.assertNotIn('attachments/token', clean)

    def test_embeds_inline_body_image_and_strips_text(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'airportlf'
        ss.save()
        comments = [{
            'author': {'name': 'Mark'}, 'public': False, 'attachments': [],
            'body': 'DELTA\n![](https://airportlf.zendesk.com/attachments/token/abc/?name=image.png)',
        }]
        import io
        from PIL import Image
        buf = io.BytesIO(); Image.new('RGB', (200, 200), 'white').save(buf, format='PNG')
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes',
                   return_value=buf.getvalue()):
            panels = ds._zendesk_comment_panels(comments)
        self.assertEqual(len(panels[0]['images']), 1)
        self.assertTrue(panels[0]['images'][0]['data_uri'].startswith('data:image/'))
        self.assertEqual(panels[0]['body'], 'DELTA')  # raw markdown stripped from text

    def test_inline_image_only_fetches_our_zendesk_host(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'airportlf'
        ss.save()
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes') as fetch:
            # Foreign host and a different Zendesk subdomain must never be fetched
            # (fetch_zendesk_attachment_bytes attaches our auth headers).
            self.assertIsNone(ds._inline_image_data_uri(
                'https://evil.com/attachments/token/x/?name=image.png'))
            self.assertIsNone(ds._inline_image_data_uri(
                'https://other.zendesk.com/attachments/token/x/?name=image.png'))
        fetch.assert_not_called()

    def test_time_format(self):
        # Rendered in the app's display zone (America/Chicago) like Zendesk:
        # 21:14 UTC on Feb 3 = 15:14 CST (UTC-6).
        self.assertEqual(ds._fmt_zd_time('2026-02-03T21:14:00Z'), 'Feb 03, 2026 15:14')
        self.assertEqual(ds._fmt_zd_time(''), '')

    def test_author_fallback_when_unknown(self):
        comments = [
            {'author': {'name': 'Unknown'}, 'public': False, 'body': 'x', 'attachments': []},
            {'author': {}, 'public': True, 'body': 'y', 'attachments': []},
        ]
        panels = ds._zendesk_comment_panels(comments, embed_images=False)
        self.assertEqual(panels[0]['author'], 'Airport Lost Found team')  # internal
        self.assertEqual(panels[1]['author'], 'Support agent')              # public


class GroupingTests(TestCase):
    def _items(self):
        return [
            {'index': 0, 'kind': 'flight_card', 'flight_card': {'number': 'AA1'}},
            {'index': 1, 'kind': 'comment', 'panel': {'author': 'A'}},
            {'index': 2, 'kind': 'comment', 'panel': {'author': 'B'}},
        ]

    def test_no_narrative_single_section(self):
        sections = ds._group_into_sections(self._items(), None)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]['title'], 'Case record')
        self.assertEqual(len(sections[0]['items']), 3)

    def test_narrative_groups_orders_and_excludes(self):
        narrative = {
            0: {'section': 'FLIGHT_IDENTIFICATION', 'explanation': 'Verified flight.'},
            1: {'section': 'SERVICE_INITIATION', 'explanation': 'Their own claim.'},
            2: {'section': 'EXCLUDE', 'explanation': 'internal noise'},
        }
        sections = ds._group_into_sections(self._items(), narrative)
        titles = [s['title'] for s in sections]
        # ordered per SECTION_ORDER (service initiation before flight identification)
        self.assertEqual(titles, ['Service initiation', 'Flight identification'])
        # excluded item dropped entirely
        self.assertEqual(sum(len(s['items']) for s in sections), 2)


class NarrateEvidenceTests(TestCase):
    def test_returns_none_when_ai_not_configured(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.ai_api_key = ''
        ss.save()
        d = _dispute()
        items = [{'index': 1, 'kind': 'comment', 'channel': 'internal', 'text': 'hi'}]
        self.assertIsNone(ds._narrate_evidence(d, items, None))

    def test_maps_ai_placements(self):
        from apps.ai.schemas import EvidenceNarrative, EvidencePlacement
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()
        d = _dispute()
        items = [{'index': 1, 'kind': 'comment', 'channel': 'internal', 'text': 'intake'}]
        fake = EvidenceNarrative(items=[EvidencePlacement(
            index=1, section='SERVICE_INITIATION', explanation='The intake note.')])
        with patch('apps.ai.client.AIClient.complete', return_value=fake):
            mapping = ds._narrate_evidence(d, items, None)
        self.assertEqual(mapping[1]['section'], 'SERVICE_INITIATION')
        self.assertEqual(mapping[1]['explanation'], 'The intake note.')

    def test_full_path_through_real_prompt_fence(self):
        """Exercise tokenizer + prompt fence for real (only the network call is
        faked) so an invalid fence tag fails here, not silently in prod."""
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.pii_tokenization_salt = 'unit-test-salt'
        ss.save()

        class _Msg:
            # Model wraps JSON in a markdown fence — AIClient must strip it.
            content = ('```json\n{"items":[{"index":0,"section":"FLIGHT_IDENTIFICATION",'
                       '"explanation":"Confirms the flight."}]}\n```')

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _FakeClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        return _Completion()

        d = _dispute()
        items = [{'index': 0, 'kind': 'comment', 'channel': 'internal',
                  'has_image': False, 'text': 'Intake for John Smith at 17706 130th Ave.'}]
        with patch('apps.ai.client._build_openai_client', return_value=_FakeClient()):
            mapping = ds._narrate_evidence(d, items, None)
        self.assertIsNotNone(mapping)  # no fence error → real path succeeded
        self.assertEqual(mapping[0]['section'], 'FLIGHT_IDENTIFICATION')


SUBMISSION_TICKET = {'custom_fields': [{'id': ds.SUBMISSION_IP_FIELD_ID, 'value': '203.0.113.7'}]}


class IdentityCrossCheckTests(TestCase):
    def _claim(self):
        return Claim.objects.create(client_email='lee@example.com', zd_ticket_id='97001')

    def test_match_when_submission_ip_in_client_email_headers(self):
        claim = self._claim()
        EmailLog.objects.create(claim=claim, from_email='lee@example.com', subject='re',
                                body='hi', raw_headers='Received: from x (1.2.3.4)\nX-Originating-IP: [203.0.113.7]')
        d = _dispute(claim=claim)
        ident = ds._identity_context(d, SUBMISSION_TICKET)
        self.assertTrue(ident['matched'])
        self.assertEqual(ident['submission_ip'], '203.0.113.7')
        self.assertEqual(ident['client_msg_count'], 1)

    def test_no_match_when_ip_differs(self):
        claim = self._claim()
        EmailLog.objects.create(claim=claim, from_email='lee@example.com', subject='re',
                                body='hi', raw_headers='X-Originating-IP: [198.51.100.9]')
        d = _dispute(claim=claim)
        ident = ds._identity_context(d, SUBMISSION_TICKET)
        self.assertFalse(ident['matched'])
        self.assertEqual(ident['client_msg_count'], 1)  # still counts their message

    def test_ignores_emails_not_from_client(self):
        claim = self._claim()
        EmailLog.objects.create(claim=claim, from_email='airport@den.gov', subject='re',
                                body='hi', raw_headers='X-Originating-IP: [203.0.113.7]')
        d = _dispute(claim=claim)
        ident = ds._identity_context(d, SUBMISSION_TICKET)
        self.assertFalse(ident['matched'])
        self.assertEqual(ident['client_msg_count'], 0)

    def test_private_ips_never_match(self):
        claim = self._claim()
        EmailLog.objects.create(claim=claim, from_email='lee@example.com', subject='re',
                                body='hi', raw_headers='Received: from internal (10.0.0.1)')
        d = _dispute(claim=claim)
        ticket = {'custom_fields': [{'id': ds.SUBMISSION_IP_FIELD_ID, 'value': '10.0.0.1'}]}
        self.assertFalse(ds._identity_context(d, ticket)['matched'])


class BottomLineAndTimelineTests(TestCase):
    def test_bottom_line_unauthorised_includes_identity_when_matched(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley')
        d = _dispute(claim=claim, dispute_reason='UNAUTHORISED')
        bl = ds._bottom_line(d, {'matched': True, 'submission_ip': '203.0.113.7', 'client_msg_count': 2})
        joined = ' '.join(bl)
        self.assertIn('Lee Foley', joined)
        # IP is zero-width-spaced for display (anti phone-autodetect) — strip to compare
        self.assertIn('203.0.113.7', joined.replace('​', ''))

    def test_bottom_line_not_received_is_about_service(self):
        d = _dispute(claim=None, buyer_name='Jane', dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        bl = ds._bottom_line(d, {'matched': False, 'client_msg_count': 0})
        self.assertIn('service was performed', ' '.join(bl).lower())

    def test_timeline_is_chronological(self):
        claim = Claim.objects.create(client_email='b@example.com')
        d = _dispute(claim=claim)
        tl = ds._build_timeline(d, COMMENTS)
        self.assertTrue(tl)
        self.assertTrue(all('when' in e and 'label' in e for e in tl))

    def test_bottom_line_includes_consent_timestamp_and_ip(self):
        d = _dispute(claim=None, buyer_name='Lee', dispute_reason='UNAUTHORISED')
        bl = ds._bottom_line(d, {'matched': False, 'client_msg_count': 0},
                             {'when': 'Jun 11, 2026 07:30', 'ip': '203.0.113.7'})
        joined = ' '.join(bl)
        self.assertIn('Jun 11, 2026 07:30', joined)
        self.assertIn('203.0.113.7', joined)

    def test_bundle_consent_from_intake_note_not_ticket_creation(self):
        # The ticket is created at 21:10 by the abandoned-cart notice; the
        # customer pays and the intake "Registration ID" note posts at 21:14.
        # Consent (when the claim was submitted & paid) must follow the intake
        # note, NOT the earlier ticket-creation/abandoned-cart time.
        claim = Claim.objects.create(client_email='b@example.com', zd_ticket_id='97001')
        d = _dispute(claim=claim, zd_ticket_id='97001')
        ticket = {'created_at': '2026-02-03T21:10:00Z',
                  'custom_fields': [{'id': ds.SUBMISSION_IP_FIELD_ID, 'value': '203.0.113.7'}]}
        comments = [{'author': {}, 'public': False, 'attachments': [],
                     'created_at': '2026-02-03T21:14:00Z',
                     'body': 'Registration ID: ALF000\nName: B'}]
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': ticket, 'comments': comments}):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
        # 21:14 UTC = 15:14 CST (America/Chicago) — the intake-note wall-clock,
        # not 15:10 (the abandoned-cart ticket creation).
        self.assertEqual(bundle['consent']['when'], 'Feb 03, 2026 15:14')
        self.assertEqual(bundle['consent']['ip'].replace('​', ''), '203.0.113.7')  # zero-width-spaced display


# A real recorded-acceptance internal note (ticket 54239), with the incident
# detail lines that precede the acceptance text on the same note.
_ACCEPT_NOTE = (
    'on a chair.\nSwitch 2.\nblack case - red tag\n50-60 games\n\n'
    'The client was called and informed of the call being recorded for quality '
    'and training purposes to what Client agreed and approved.\n\n'
    'At minute  6:00   on our recorded line, Client approved to move forward with '
    'a non refundable fee of $65.00 as Client understood our service and agreed to '
    'move forward knowing no guarantees can be provided on lost items.')


class RecordedAcceptanceTests(TestCase):
    """The customer's verbal acceptance of the non-refundable fee on a recorded
    call is decisive dispute evidence — detected deterministically, never left
    to the AI, and surfaced prominently."""

    def _note(self, body, public=False):
        return {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': public,
                'created_at': '2026-02-03T22:05:00Z', 'body': body, 'attachments': []}

    def test_detects_minute_fee_and_strips_incident_lines(self):
        ra = ds._recorded_acceptance([self._note(_ACCEPT_NOTE)])
        self.assertIsNotNone(ra)
        self.assertEqual(ra['minute'], '6:00')
        self.assertEqual(ra['fee'], '$65.00')
        self.assertIn('non refundable fee', ra['statement'])
        self.assertIn('no guarantees can be provided', ra['statement'])
        self.assertNotIn('on a chair', ra['statement'].lower())  # incident junk dropped

    def test_ignores_public_notes_and_passing_refund_mentions(self):
        # A public update that merely mentions "refund" must NOT trigger it.
        self.assertIsNone(ds._recorded_acceptance(
            [self._note('Dear customer, our refund policy is on our website.', public=True)]))
        # An internal note about refunds but with no recorded acceptance phrase.
        self.assertIsNone(ds._recorded_acceptance(
            [self._note('Reviewed the refund request; no recording mentioned.')]))

    def test_surfaces_in_bundle_bottomline_and_report_html(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     zd_ticket_id='97001', price_paid=Decimal('65.00'))
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        ticket = {'created_at': '2026-02-03T21:10:00Z', 'custom_fields': []}
        comments = [self._note('Registration ID: ALF1\nName: Lee'), self._note(_ACCEPT_NOTE)]
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': ticket, 'comments': comments}):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertIsNotNone(bundle['recorded_acceptance'])
        self.assertTrue(bundle['bottom_line'][0].startswith('On a recorded call'))  # leads the summary
        self.assertIn('Recorded verbal acceptance of our non-refundable terms', html)  # the callout
        self.assertIn('no guarantees can be provided on lost items', html)            # verbatim quote

    def test_absent_when_no_such_note(self):
        claim = Claim.objects.create(client_email='b@example.com', zd_ticket_id='97001')
        d = _dispute(claim=claim, zd_ticket_id='97001')
        ticket = {'created_at': '2026-02-03T21:10:00Z', 'custom_fields': []}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': ticket, 'comments': [self._note('Registration ID: ALF1')]}):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertIsNone(bundle['recorded_acceptance'])
        self.assertNotIn('Recorded verbal acceptance', html)


class CheckoutEvidenceTests(TestCase):
    """The checkout page is generated from the order record (real customer + fee)
    and embedded in the report, replacing the old fixed $55/Australian screenshot.
    The five highlights stay on the page; the explanatory text is in the report."""

    def test_split_address_common_us_format(self):
        out = ds._split_address('County Road 1140, Cooper, TX 75432, United States')
        self.assertEqual(out['street'], 'County Road 1140')
        self.assertEqual(out['suburb'], 'Cooper')
        self.assertEqual(out['state'], 'TX')
        self.assertEqual(out['postcode'], '75432')
        self.assertEqual(out['country'], 'United States')

    def test_split_address_labeled_format(self):
        # The form we actually store: labels, no commas.
        out = ds._split_address('Street Address: 110 Horseshoe Drive City: Cooper State: TX Zip: 75432 Country: US')
        self.assertEqual(out['street'], '110 Horseshoe Drive')
        self.assertEqual(out['suburb'], 'Cooper')
        self.assertEqual(out['state'], 'TX')
        self.assertEqual(out['postcode'], '75432')
        self.assertEqual(out['country'], 'US')

    def test_checkout_full_address_is_clean_not_labeled(self):
        claim = Claim.objects.create(
            client_email='b@e.com', price_paid=Decimal('65.00'),
            billing_address='Street Address: 110 Horseshoe Drive City: Cooper State: TX Zip: 75432 Country: US')
        d = _dispute(claim=claim, dispute_currency='USD')
        ctx = ds._checkout_context(d)
        self.assertEqual(ctx['full_address'], '110 Horseshoe Drive, Cooper, TX 75432, US')
        self.assertNotIn('Street Address:', ctx['full_address'])   # no labeled blob leaks
        self.assertEqual(ctx['street'], '110 Horseshoe Drive')

    def test_split_address_degrades_safely(self):
        self.assertEqual(ds._split_address('')['street'], '')          # empty is safe
        two = ds._split_address('Main St, Springfield')
        self.assertEqual(two['street'], 'Main St')

    def test_dephone_ips_spaces_each_dot_and_is_idempotent(self):
        # An IP must never reach the PDF/PayPal as 10 plain digits (phone-detect).
        self.assertEqual(ds._dephone_ips('from IP 9.8.7.6 ok'), 'from IP 9.​8.​7.​6 ok')
        once = ds._dephone_ips('IP 73.14.22.190')
        self.assertEqual(ds._dephone_ips(once), once)              # idempotent
        self.assertNotIn('9.8.7.6', ds._dephone_ips('9.8.7.6'))    # raw run is broken up

    def test_checkout_context_price_and_address(self):
        claim = Claim.objects.create(client_email='b@e.com', price_paid=Decimal('65.00'),
                                     billing_address='County Road 1140, Cooper, TX 75432, United States')
        d = _dispute(claim=claim, dispute_currency='USD', dispute_amount=Decimal('65.00'))
        ctx = ds._checkout_context(d)
        self.assertEqual(ctx['price'], '65')        # 65.00 -> 65
        self.assertEqual(ctx['currency'], 'USD')
        self.assertEqual(ctx['suburb'], 'Cooper')

    def test_report_embeds_generated_checkout_not_static(self):
        claim = Claim.objects.create(client_email='b@e.com', zd_ticket_id='97001',
                                     price_paid=Decimal('65.00'),
                                     billing_address='County Road 1140, Cooper, TX 75432, United States')
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_currency='USD',
                     dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        ticket = {'created_at': '2026-02-03T21:14:00Z', 'custom_fields': []}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': ticket, 'comments': []}):
            bundle = ds.build_dispute_evidence_bundle(d, use_ai=False)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertIn('class="cko"', html)                        # generated checkout present
        self.assertIn('$65', html)                                 # the case fee
        self.assertIn('Cooper', html)                              # the customer's address
        self.assertEqual(html.count('class="pin"'), 5)             # the five highlights
        self.assertNotIn('Annotated checkout showing fee', html)   # old static image gone
        self.assertIn('What the highlights show', html)            # legend lives in the report


class SectionOrderingTests(TestCase):
    def test_unauthorised_leads_with_service_initiation(self):
        order = ds._section_priority_for('UNAUTHORISED')
        self.assertEqual(order[0], 'SERVICE_INITIATION')

    def test_not_received_leads_with_submissions(self):
        order = ds._section_priority_for('MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        self.assertEqual(order[0], 'SUBMISSIONS')

    def test_grouping_uses_reason_order(self):
        items = [{'index': 0, 'kind': 'comment', 'panel': {'author': 'A'}},
                 {'index': 1, 'kind': 'comment', 'panel': {'author': 'B'}}]
        narrative = {0: {'section': 'SUBMISSIONS', 'explanation': ''},
                     1: {'section': 'SERVICE_INITIATION', 'explanation': ''}}
        sections = ds._group_into_sections(items, narrative, reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        self.assertEqual(sections[0]['key'], 'SUBMISSIONS')  # not-received leads with submissions


class StripActiveHtmlTests(TestCase):
    def test_removes_scripts_and_handlers_keeps_layout(self):
        dirty = ('<table><tr><td style="color:red">x</td></tr></table>'
                 '<img src="data:image/png;base64,AAA" onerror="hack()">'
                 '<script>steal()</script>')
        clean = frontend_views.strip_active_html(dirty)
        self.assertIn('<table>', clean)
        self.assertIn('style="color:red"', clean)
        self.assertIn('<img', clean)
        self.assertNotIn('<script>', clean)
        self.assertNotIn('onerror', clean)


class ManualDisputeCreateTests(TestCase):
    """The fallback path: manually create a dispute from a claim when PayPal's
    webhook never delivered it."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        User = get_user_model()
        self.mgr = User.objects.create_user(username='mc_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.claim = Claim.objects.create(
            client_email='lee@example.com', client_name='Lee Foley', alf_claim_id='ALF1',
            zd_ticket_id='97001', price_paid=Decimal('74.00'))

    def test_get_form_renders_prefilled(self):
        resp = self.web.get(f'/manager/disputes/create/?claim={self.claim.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Mark claim as disputed')
        self.assertContains(resp, 'lee@example.com')

    def test_post_creates_dispute_linked_to_claim(self):
        resp = self.web.post('/manager/disputes/create/', {
            'claim_id': self.claim.id, 'dispute_reason': 'UNAUTHORISED',
            'buyer_email': 'lee@example.com', 'dispute_amount': '74.00', 'dispute_currency': 'usd',
            'seller_response_due': '2026-07-01',
        })
        d = Dispute.objects.get(claim=self.claim)
        self.assertRedirects(resp, f'/manager/disputes/{d.id}/', fetch_redirect_response=False)
        self.assertEqual(d.dispute_reason, 'UNAUTHORISED')
        self.assertEqual(d.status, 'MATCHED')
        self.assertEqual(d.dispute_currency, 'USD')           # normalised
        self.assertEqual(d.zd_ticket_id, '97001')             # carried from claim
        self.assertTrue(d.paypal_dispute_id.startswith('MANUAL-'))  # auto-generated
        self.assertIsNotNone(d.seller_response_due)

    def test_post_uses_provided_paypal_id(self):
        self.web.post('/manager/disputes/create/', {
            'claim_id': self.claim.id, 'dispute_reason': '', 'buyer_email': 'lee@example.com',
            'paypal_dispute_id': 'PP-D-REAL-1'})
        self.assertTrue(
            Dispute.objects.filter(paypal_dispute_id='PP-D-REAL-1', claim=self.claim).exists())

    def test_missing_claim_redirects_to_list(self):
        resp = self.web.get('/manager/disputes/create/')
        self.assertRedirects(resp, '/manager/disputes/', fetch_redirect_response=False)


class ReportEditorRenderTests(TestCase):
    """Evidence reports use the in-place WYSIWYG (iframe) editor; other docs
    keep the plain textarea editor."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.urls import reverse
        from apps.payments.models import DisputeDocument
        self.reverse = reverse
        User = get_user_model()
        self.mgr = User.objects.create_user(username='ed_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)
        d = _dispute()
        self.report = DisputeDocument.objects.create(
            dispute=d, doc_type='EVIDENCE_REPORT', status='DRAFT', generated_by='MANUAL',
            content_html='<html><body><p>Hi &amp; "bye"</p></body></html>', version=1)
        self.letter = DisputeDocument.objects.create(
            dispute=d, doc_type='RESPONSE_LETTER', status='DRAFT', generated_by='AI',
            content_html='Subject\n\nBody', version=1)

    def test_report_uses_inplace_iframe_editor(self):
        resp = self.web.get(self.reverse('disputes:dispute_edit_document', args=[self.report.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'reportFrame')
        self.assertContains(resp, 'srcdoc')

    def test_letter_uses_textarea_editor(self):
        resp = self.web.get(self.reverse('disputes:dispute_edit_document', args=[self.letter.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="content_html"')
        self.assertNotContains(resp, 'reportFrame')


class GroupedTemplateRenderTests(TestCase):
    def test_sections_and_explanations_render(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     zd_ticket_id='97001', flight_data=FLIGHT_DATA)
        d = _dispute(claim=claim, zd_ticket_id='97001')
        # COMMENTS[0] ("Registration ID…") is pinned as the intake lead, so the
        # AI-grouped items are the flight card (0) and Joe's public reply (1).
        narrative = {
            0: {'section': 'FLIGHT_IDENTIFICATION', 'explanation': 'Confirms the route and arrival.'},
            1: {'section': 'INTERACTIONS', 'explanation': 'We kept the customer updated.'},
        }
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value=None), \
             patch.object(ds, '_narrate_evidence', return_value=narrative):
            bundle = ds.build_dispute_evidence_bundle(d)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertIn('Claim submitted by the customer', html)   # intake pinned as lead
        self.assertIn('Flight identification', html)
        self.assertIn('Interactions with the client', html)
        self.assertIn('Why this matters:', html)
        self.assertIn('We kept the customer updated.', html)
        # new blocks
        self.assertIn('In summary', html)        # bottom-line box
        self.assertIn('Case timeline', html)     # timeline

    def test_identity_callout_and_alias_paragraph_render(self):
        claim = Claim.objects.create(client_email='lee@example.com', client_name='Lee Foley',
                                     zd_ticket_id='97001', email_alias='case-1@alias.example')
        EmailLog.objects.create(claim=claim, from_email='lee@example.com', subject='re',
                                body='hi', raw_headers='X-Originating-IP: [203.0.113.7]')
        d = _dispute(claim=claim, zd_ticket_id='97001', dispute_reason='UNAUTHORISED')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': SUBMISSION_TICKET, 'comments': []}), \
             patch.object(ds, '_narrate_evidence', return_value={}):
            bundle = ds.build_dispute_evidence_bundle(d)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertTrue(bundle['identity']['matched'])
        self.assertIn('Identity confirmed', html)        # identity callout
        self.assertIn('203.0.113.7', html.replace('​', ''))  # IP zero-width-spaced for display
        self.assertIn('dedicated email address', html)   # alias paragraph


class ClaimsResponsePhoneTests(TestCase):
    """We only assert a phone-reachability contradiction where one is DUE.
    Our outbound calls don't prove the customer could reach US, so unanswered
    calls must concede the point and pivot — never claim 'records show the
    opposite' (which both reads as a non-sequitur and is false on voicemail)."""

    def _dispute(self):
        claim = Claim.objects.create(client_email='c@e.com', client_name='T', alf_claim_id='ALFX')
        # Blank buyer statement -> every theme fires (incl. the phone point).
        return _dispute(claim=claim, dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
                        raw_webhook_payload={}), claim

    def test_unanswered_calls_concede_and_pivot_no_false_contradiction(self):
        d, claim = self._dispute()
        comments = [{'channel': 'voice', 'call': {'duration': 33}},      # voicemail (no answered_by_name)
                    {'channel': 'voice', 'call': {'duration': 7}},
                    {'public': True, 'author': {'email': 'a@alf.com'}, 'body': 'update'}]
        point = ds._claims_response(d, comments, claim, {})['points'][0]
        self.assertNotIn('show the opposite', point)     # no fake contradiction
        self.assertNotIn('telephoned', point)
        self.assertIn('do not dispute', point)           # honest concession
        self.assertIn('does not depend on telephone', point)   # pivot to service

    def test_answered_call_asserts_the_contradiction(self):
        d, claim = self._dispute()
        comments = [{'channel': 'voice', 'call': {'duration': 120, 'answered_by_name': 'Mark'}}]
        point = ds._claims_response(d, comments, claim, {})['points'][0]
        self.assertIn('connected with the customer by phone', point)
