"""Narrative evidence report — simulated Zendesk panels, flight card, and the
template render that replaces the browser-screenshot pipeline (2026-06-14)."""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase

from apps.claims.models import Claim
from apps.communications.models import EmailLog
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
            object_description='iPad Tablet\nred hard case', price_paid=Decimal('74.00'))
        d = _dispute(claim=claim, dispute_amount=Decimal('100.00'))
        nf = ds._narrative_fields(d)
        self.assertEqual(nf['client_name'], 'Lee Foley')
        self.assertEqual(nf['alf_id'], 'ALF5490789')
        self.assertEqual(nf['object'], 'iPad Tablet')      # first line only
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
        self.assertIn('Mark Johnson', html)
        self.assertIn('Internal note', html)
        self.assertIn('Public reply', html)
        # flight card rebuilt from data
        self.assertIn('American Airlines', html)
        self.assertIn('CLT', html)
        # conclusion present
        self.assertIn('Conclusion', html)


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
        self.assertEqual(bundle['screenshots'], [])
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

    def test_time_format(self):
        self.assertEqual(ds._fmt_zd_time('2026-02-03T21:14:00Z'), 'Feb 03, 2026 21:14')
        self.assertEqual(ds._fmt_zd_time(''), '')

    def test_author_fallback_when_unknown(self):
        comments = [
            {'author': {'name': 'Unknown'}, 'public': False, 'body': 'x', 'attachments': []},
            {'author': {}, 'public': True, 'body': 'y', 'attachments': []},
        ]
        panels = ds._zendesk_comment_panels(comments, embed_images=False)
        self.assertEqual(panels[0]['author'], 'Airport Lost & Found team')  # internal
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
        self.assertIn('203.0.113.7', joined)

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


class GroupedTemplateRenderTests(TestCase):
    def test_sections_and_explanations_render(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     zd_ticket_id='97001', flight_data=FLIGHT_DATA)
        d = _dispute(claim=claim, zd_ticket_id='97001')
        narrative = {
            0: {'section': 'FLIGHT_IDENTIFICATION', 'explanation': 'Confirms the route and arrival.'},
            1: {'section': 'SERVICE_INITIATION', 'explanation': 'The customer filed this claim themselves.'},
            2: {'section': 'INTERACTIONS', 'explanation': 'We kept the customer updated.'},
        }
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value=None), \
             patch.object(ds, '_narrate_evidence', return_value=narrative):
            bundle = ds.build_dispute_evidence_bundle(d)
            html = render_to_string(ds.report_template_for(d), bundle)
        self.assertIn('Service initiation', html)
        self.assertIn('Flight identification', html)
        self.assertIn('Interactions with the client', html)
        self.assertIn('Why this matters:', html)
        self.assertIn('The customer filed this claim themselves.', html)
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
        self.assertIn('203.0.113.7', html)
        self.assertIn('dedicated email address', html)   # alias paragraph
