"""Tests for the button-driven per-ticket email check.

The check only ever touches mail addressed to ONE ticket's alias: unread,
last EMAIL_LOOKBACK_DAYS days, never processed before (Message-ID dedup).
Triggered from the LORA claim page or the Zendesk sidebar Email tab.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.communications.services import (
    AI_TAG_ATTENTION,
    EmailNotConfigured,
    InvalidAlias,
    _ai_tags_for,
    check_email_for_ticket,
    imap_since_date,
    open_inbox,
    search_alias_uids,
)
from apps.config.models import SystemSettings
from apps.integrations.services import get_ticket_email_alias

User = get_user_model()

ALIAS = 'andrei.deaconu@mailapptoday.com'
SECRET = 'email-check-test-secret'

AI_OK = {
    'summary': 'Lost & found says the bag was located.',
    'category': 'OBJECT_FOUND',
    'action_required': True,
    'auto_resolvable': False,
}


def raw_email(subject='Found your bag', from_='lostfound@airport.com',
              message_id='<m1@mail.example>', body='We found it.'):
    headers = (
        f"From: Lost and Found <{from_}>\r\n"
        f"To: {ALIAS}\r\n"
        f"Delivered-To: {ALIAS}\r\n"
        f"Subject: {subject}\r\n"
    )
    if message_id:
        headers += f"Message-ID: {message_id}\r\n"
    return (headers + "\r\n" + body).encode('utf-8')


def mock_conn(uids=b'1', fetches=None):
    """An IMAP connection whose search returns `uids` for every criteria set."""
    conn = MagicMock()
    conn.search.return_value = ('OK', [uids])
    if fetches is None:
        fetches = {b'1': raw_email()}
    def fetch(uid, spec):
        key = uid if isinstance(uid, bytes) else uid.encode()
        return ('OK', [(b'1 (RFC822)', fetches[key])])
    conn.fetch.side_effect = fetch
    return conn


# ---- date window ----

class ImapSinceDateTests(TestCase):
    def test_two_days_back_in_rfc3501_format(self):
        self.assertEqual(imap_since_date(date(2026, 6, 12)), '10-Jun-2026')

    def test_crosses_month_boundary(self):
        self.assertEqual(imap_since_date(date(2026, 7, 1)), '29-Jun-2026')


# ---- tag derivation ----

class AiTagsTests(TestCase):
    def test_object_found(self):
        self.assertEqual(_ai_tags_for('OBJECT_FOUND', False), {'ai_object_found'})

    def test_object_found_with_action(self):
        self.assertEqual(_ai_tags_for('OBJECT_FOUND', True),
                         {'ai_object_found', AI_TAG_ATTENTION})

    def test_resubmission_required(self):
        self.assertEqual(_ai_tags_for('RESUBMISSION_REQUIRED', False),
                         {'ai_resubmission_required'})

    def test_routine_mail_untagged(self):
        self.assertEqual(_ai_tags_for('GENERAL_CORRESPONDENCE', False), set())
        self.assertEqual(_ai_tags_for('SUBMISSION_CONFIRMATION', False), set())

    def test_attention_alone_for_unknown_category(self):
        self.assertEqual(_ai_tags_for('UNKNOWN', True), {AI_TAG_ATTENTION})


# ---- mailbox search ----

class SearchAliasUidsTests(TestCase):
    def test_searches_to_and_delivered_to_unread_within_window(self):
        conn = MagicMock()
        conn.search.side_effect = [('OK', [b'1 2']), ('OK', [b'2 3']), ('OK', [b'4'])]
        with patch('apps.communications.services.imap_since_date',
                   return_value='10-Jun-2026'):
            uids = search_alias_uids(conn, ALIAS)
        self.assertEqual(uids, [b'1', b'2', b'3', b'4'])  # unioned, deduped, ordered
        conn.search.assert_any_call(
            None, 'UNSEEN', 'SINCE', '10-Jun-2026', 'TO', f'"{ALIAS}"')
        conn.search.assert_any_call(
            None, 'UNSEEN', 'SINCE', '10-Jun-2026', 'HEADER', 'Delivered-To', f'"{ALIAS}"')
        conn.search.assert_any_call(
            None, 'UNSEEN', 'SINCE', '10-Jun-2026', 'HEADER', 'X-AnonAddy-Original-To', f'"{ALIAS}"')

    def test_failed_search_yields_empty(self):
        conn = MagicMock()
        conn.search.return_value = ('BAD', [None])
        self.assertEqual(search_alias_uids(conn, ALIAS), [])


# ---- connection guard ----

class OpenInboxTests(TestCase):
    def test_missing_credentials_raise_email_not_configured(self):
        ss = MagicMock(imap_host='', imap_user='', imap_pass='')
        with patch('apps.communications.services.SystemSettings') as mock_ss:
            mock_ss.get_instance.return_value = ss
            with self.assertRaises(EmailNotConfigured):
                open_inbox()


# ---- ticket alias reader ----

class GetTicketEmailAliasTests(TestCase):
    def test_reads_alias_field(self):
        ticket = {'custom_fields': [
            {'id': 11761080032028, 'value': 'TAROM'},
            {'id': 13606076120860, 'value': f'  {ALIAS.upper()} '},
        ]}
        self.assertEqual(get_ticket_email_alias(ticket), ALIAS)

    def test_missing_or_empty_field_returns_blank(self):
        self.assertEqual(get_ticket_email_alias({'custom_fields': []}), '')
        self.assertEqual(get_ticket_email_alias(
            {'custom_fields': [{'id': 13606076120860, 'value': None}]}), '')


# ---- the check itself ----

@patch('apps.communications.services.add_zendesk_ticket_tags', return_value=True)
@patch('apps.communications.services.post_ai_summary_to_zendesk', return_value=True)
@patch('apps.communications.services.call_qwen_ai', return_value=dict(AI_OK))
class CheckEmailForTicketTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='client@example.com', zd_ticket_id='80001',
            email_alias=ALIAS)

    def run_check(self, conn, claim='default'):
        claim = self.claim if claim == 'default' else claim
        with patch('apps.communications.services.open_inbox', return_value=conn):
            return check_email_for_ticket('80001', claim, ALIAS)

    def test_new_email_processed_end_to_end(self, mock_ai, mock_note, mock_tags):
        before = EmailLog.objects.count()
        results = self.run_check(mock_conn())

        self.assertEqual(results['found'], 1)
        self.assertEqual(len(results['processed']), 1)
        entry = results['processed'][0]
        self.assertEqual(entry['category'], 'OBJECT_FOUND')
        self.assertTrue(entry['note_posted'])

        self.assertEqual(EmailLog.objects.count(), before + 1)
        log = EmailLog.objects.latest('id')
        self.assertEqual(log.claim_id, self.claim.id)
        self.assertEqual(log.zd_ticket_id, '80001')
        self.assertEqual(log.message_id, '<m1@mail.example>')
        self.assertEqual(log.alias_matched, ALIAS)
        self.assertEqual(log.category, 'OBJECT_FOUND')

        mock_note.assert_called_once()
        mock_tags.assert_called_once_with(
            '80001', ['ai_attention_needed', 'ai_object_found'])
        self.assertEqual(results['tags_added'],
                         ['ai_attention_needed', 'ai_object_found'])

    def test_already_processed_email_skipped_without_ai_call(
            self, mock_ai, mock_note, mock_tags):
        EmailLog.objects.create(subject='x', body='x',
                                message_id='<m1@mail.example>')
        before = EmailLog.objects.count()
        results = self.run_check(mock_conn())

        self.assertEqual(results['already_processed'], 1)
        self.assertEqual(results['processed'], [])
        self.assertEqual(EmailLog.objects.count(), before)
        mock_ai.assert_not_called()
        mock_note.assert_not_called()
        mock_tags.assert_not_called()

    def test_unresolved_email_left_unread(self, mock_ai, mock_note, mock_tags):
        conn = mock_conn()
        self.run_check(conn)
        conn.store.assert_not_called()

    def test_auto_resolved_email_marked_read(self, mock_ai, mock_note, mock_tags):
        mock_ai.return_value = {
            'summary': 'Submission confirmed.',
            'category': 'SUBMISSION_CONFIRMATION',
            'action_required': False,
            'auto_resolvable': True,
        }
        conn = mock_conn()
        results = self.run_check(conn)
        conn.store.assert_called_once_with('1', '+FLAGS', '\\Seen')
        self.assertEqual(results['tags_added'], [])  # routine → no tags
        mock_tags.assert_not_called()

    def test_claimless_check_logs_without_claim(self, mock_ai, mock_note, mock_tags):
        results = self.run_check(mock_conn(), claim=None)
        self.assertEqual(len(results['processed']), 1)
        self.assertIsNone(EmailLog.objects.latest('id').claim_id)

    def test_per_email_failure_counted_not_raised(self, mock_ai, mock_note, mock_tags):
        mock_ai.side_effect = RuntimeError('AI down')
        results = self.run_check(mock_conn())
        self.assertEqual(results['errors'], 1)
        self.assertEqual(results['processed'], [])

    def test_missing_credentials_propagate(self, mock_ai, mock_note, mock_tags):
        with patch('apps.communications.services.open_inbox',
                   side_effect=EmailNotConfigured('no creds')):
            with self.assertRaises(EmailNotConfigured):
                check_email_for_ticket('80001', self.claim, ALIAS)

    def test_malformed_alias_never_reaches_the_mailbox(
            self, mock_ai, mock_note, mock_tags):
        # The alias is interpolated into the IMAP search command — quotes,
        # spaces or an empty value must be rejected before any connection.
        for bad in ('', 'not-an-email', 'a b@x.com', 'x"@y.com UNSEEN', None):
            with patch('apps.communications.services.open_inbox') as mock_open:
                with self.assertRaises(InvalidAlias):
                    check_email_for_ticket('80001', self.claim, bad)
                mock_open.assert_not_called()


# ---- LORA claim page endpoint ----

class ClaimCheckEmailEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='emailcheck_agent', password='x', role='AGENT')
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.claim = Claim.objects.create(
            client_email='client@example.com', zd_ticket_id='80002',
            email_alias=ALIAS)
        self.url = f'/api/claims/{self.claim.id}/check-email/'
        self.ok_results = {
            'alias': ALIAS, 'found': 1, 'processed': [{'subject': 'hi'}],
            'already_processed': 0, 'tags_added': [], 'errors': 0,
            'capped': False,
        }

    def test_requires_authentication(self):
        resp = APIClient().post(self.url)
        self.assertIn(resp.status_code, (401, 403))

    def test_cached_alias_skips_zendesk_fetch(self):
        with patch('apps.communications.services.check_email_for_ticket',
                   return_value=dict(self.ok_results)) as mock_check, \
             patch('apps.claims.views.fetch_zendesk_ticket') as mock_fetch:
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['message'], '1 new email(s) processed')
        mock_fetch.assert_not_called()
        mock_check.assert_called_once_with('80002', self.claim, ALIAS)

    def test_alias_fetched_from_ticket_and_cached(self):
        self.claim.email_alias = ''
        self.claim.save(update_fields=['email_alias'])
        ticket = {'custom_fields': [{'id': 13606076120860, 'value': ALIAS}]}
        with patch('apps.claims.views.fetch_zendesk_ticket', return_value=ticket), \
             patch('apps.communications.services.check_email_for_ticket',
                   return_value=dict(self.ok_results)):
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.email_alias, ALIAS)

    def test_ticket_without_alias_field_is_a_clear_400(self):
        self.claim.email_alias = ''
        self.claim.save(update_fields=['email_alias'])
        with patch('apps.claims.views.fetch_zendesk_ticket',
                   return_value={'custom_fields': []}):
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('alias', resp.data['error'])

    def test_imap_not_configured_is_503(self):
        with patch('apps.communications.services.check_email_for_ticket',
                   side_effect=EmailNotConfigured('no creds')):
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 503)
        self.assertIn('System settings', resp.data['error'])

    def test_malformed_alias_is_a_clear_400(self):
        with patch('apps.communications.services.check_email_for_ticket',
                   side_effect=InvalidAlias('bad')):
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('alias', resp.data['error'])

    def test_mailbox_failure_is_502(self):
        with patch('apps.communications.services.check_email_for_ticket',
                   side_effect=OSError('connection refused')):
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 502)

    def test_claim_without_ticket_is_400(self):
        claim = Claim.objects.create(client_email='c2@example.com', zd_ticket_id='')
        resp = self.api.post(f'/api/claims/{claim.id}/check-email/')
        self.assertEqual(resp.status_code, 400)


# ---- Zendesk sidebar endpoint ----

class ZendeskEmailCheckEndpointTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.save()
        # All sidebar endpoints share one failed-auth lockout counter per IP
        # (cache key sidebar_auth_fail_*). Clear it so the bad-secret test
        # below doesn't push later suites' auth tests into the 429 lockout.
        cache.clear()
        self.addCleanup(cache.clear)
        self.api = APIClient()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {SECRET}'}
        self.url = '/api/integrations/zd/email-check/'
        self.ok_results = {
            'alias': ALIAS, 'found': 0, 'processed': [],
            'already_processed': 2, 'tags_added': [], 'errors': 0,
            'capped': False,
        }

    def test_rejects_missing_secret(self):
        resp = self.api.post(self.url, {'ticket_id': '80003'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_claim_path_uses_cached_alias(self):
        claim = Claim.objects.create(
            client_email='c@example.com', zd_ticket_id='80003', email_alias=ALIAS)
        with patch('apps.communications.services.check_email_for_ticket',
                   return_value=dict(self.ok_results)) as mock_check, \
             patch('apps.integrations.views.fetch_zendesk_ticket') as mock_fetch:
            resp = self.api.post(self.url, {'ticket_id': '80003'},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['claimless'])
        self.assertEqual(resp.data['already_processed'], 2)
        mock_fetch.assert_not_called()
        mock_check.assert_called_once_with('80003', claim, ALIAS)

    def test_claimless_ticket_reads_alias_field(self):
        ticket = {'custom_fields': [{'id': 13606076120860, 'value': ALIAS}]}
        with patch('apps.integrations.views.fetch_zendesk_ticket',
                   return_value=ticket), \
             patch('apps.communications.services.check_email_for_ticket',
                   return_value=dict(self.ok_results)):
            resp = self.api.post(self.url, {'ticket_id': '99999'},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['claimless'])

    def test_ticket_without_alias_explains_itself(self):
        with patch('apps.integrations.views.fetch_zendesk_ticket',
                   return_value={'custom_fields': []}):
            resp = self.api.post(self.url, {'ticket_id': '99999'},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('no email alias', resp.data['error_message'])

    def test_missing_ticket_id_is_friendly(self):
        resp = self.api.post(self.url, {}, format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('No ticket id', resp.data['error_message'])

    def test_imap_not_configured_is_503(self):
        Claim.objects.create(
            client_email='c@example.com', zd_ticket_id='80004', email_alias=ALIAS)
        with patch('apps.communications.services.check_email_for_ticket',
                   side_effect=EmailNotConfigured('no creds')):
            resp = self.api.post(self.url, {'ticket_id': '80004'},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 503)


# ---- additive tag helper ----

class AddZendeskTicketTagsTests(TestCase):
    def test_uses_put_on_the_tags_endpoint(self):
        # PUT on /tags.json is Zendesk's ADD; POST there would REPLACE all
        # tags on the ticket — this test pins the safe verb.
        from apps.integrations import services as integration_services
        with patch.object(integration_services, '_get_zendesk_base_url',
                          return_value='https://example.zendesk.com/api/v2'), \
             patch.object(integration_services, '_get_zendesk_auth_headers',
                          return_value={'Content-Type': 'application/json'}), \
             patch('urllib.request.urlopen') as mock_open:
            ok = integration_services.add_zendesk_ticket_tags(
                '80005', ['ai_object_found'])
        self.assertTrue(ok)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.get_method(), 'PUT')
        self.assertTrue(req.full_url.endswith('/tickets/80005/tags.json'))

    def test_failure_is_reported_not_raised(self):
        from apps.integrations import services as integration_services
        with patch.object(integration_services, '_get_zendesk_base_url',
                          side_effect=ValueError('no creds')):
            self.assertFalse(integration_services.add_zendesk_ticket_tags(
                '80005', ['ai_object_found']))

    def test_empty_tag_list_is_a_noop(self):
        from apps.integrations.services import add_zendesk_ticket_tags
        with patch('urllib.request.urlopen') as mock_open:
            self.assertTrue(add_zendesk_ticket_tags('80005', []))
        mock_open.assert_not_called()
