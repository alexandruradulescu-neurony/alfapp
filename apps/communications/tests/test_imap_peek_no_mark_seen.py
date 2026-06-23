"""Fetching mail must NOT mark it ``\\Seen`` as a side effect — the read flag is
set EXPLICITLY after an email is filed, never by the act of fetching.

INVARIANT under test
--------------------
Pulling a message off the IMAP server is a *read of the bytes*, not a disposition.
So both fetch sites must use ``BODY.PEEK[]`` (non-marking), never bare ``RFC822`` /
``BODY[]`` (which sets ``\\Seen`` as a side effect). The Seen flag is set by an
explicit ``mark_email_as_seen`` -> ``store(uid, '+FLAGS', '\\Seen')`` once filed.

Disposition contract (``read = LORA handled it``)
-------------------------------------------------
Every email LORA files is marked read via that explicit store — even one needing a
human, because what needs attention is tracked in the app, not the inbox read-state.
Only genuinely UNMATCHED mail (no ticket) is left unread. Covers BOTH fetch sites:

  * the per-ticket path ``check_email_for_ticket(...)``, and
  * the global sweep ``process_incoming_emails()``.
"""

from unittest.mock import MagicMock, Mock, patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.communications.services import (
    check_email_for_ticket,
    process_incoming_emails,
)

ALIAS = 'andrei.deaconu@mailapptoday.com'

# A categorization that NEEDS A HUMAN -> must be left unread (not auto-resolved).
AI_HUMAN_NEEDED = {
    'summary': 'Lost & found says the bag was located -- needs agent action.',
    'category': 'OBJECT_FOUND',
    'action_required': True,
    'auto_resolvable': False,
}


def raw_email(subject='Found your bag', from_='lostfound@airport.com',
              message_id='<peek-m1@mail.example>', body='We found it.'):
    """A small, real RFC822 message as raw bytes (built with stdlib email)."""
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
    """An IMAP connection whose search returns ``uids`` for every criteria set.

    ``fetch`` returns a realistic ``('OK', [(b'1 (... {N}', <raw bytes>)])``
    tuple regardless of the spec it is handed, so the code under test runs to
    completion -- we then inspect WHICH spec it asked for.
    """
    conn = MagicMock()
    conn.search.return_value = ('OK', [uids])
    if fetches is None:
        fetches = {b'1': raw_email()}

    def fetch(uid, spec):
        key = uid if isinstance(uid, bytes) else uid.encode()
        return ('OK', [(b'1 (RFC822 {%d}' % len(fetches[key]), fetches[key])])

    conn.fetch.side_effect = fetch
    return conn


def _fetch_specs(conn):
    """Every spec string the code passed as the 2nd arg to ``conn.fetch``."""
    return [call.args[1] for call in conn.fetch.call_args_list]


def _assert_peek_not_rfc822(testcase, conn):
    """The fetch must use the non-marking PEEK form, never bare RFC822."""
    specs = _fetch_specs(conn)
    testcase.assertTrue(specs, 'expected conn.fetch to be called at least once')
    for spec in specs:
        testcase.assertIn(
            'BODY.PEEK', spec,
            f"fetch spec {spec!r} must use the non-marking BODY.PEEK form; "
            f"'(RFC822)'/'BODY[]' sets \\Seen as a side effect and marks the "
            f"message read")
        testcase.assertNotEqual(
            spec.replace(' ', ''), '(RFC822)',
            "fetch spec is the bare '(RFC822)' form, which marks the message "
            "\\Seen -- this is the bug")


# ---------------------------------------------------------------------------
# Per-ticket path: check_email_for_ticket(...)
# ---------------------------------------------------------------------------

@patch('apps.communications.services.add_zendesk_ticket_tags', return_value=True)
@patch('apps.communications.services.post_ai_summary_to_zendesk', return_value=True)
@patch('apps.communications.services.call_qwen_ai', return_value=dict(AI_HUMAN_NEEDED))
class CheckEmailForTicketPeekTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='client@example.com', zd_ticket_id='80001',
            email_alias=ALIAS)

    def run_check(self, conn):
        with patch('apps.communications.services.open_inbox', return_value=conn):
            return check_email_for_ticket('80001', self.claim, ALIAS)

    def test_fetch_uses_peek_not_rfc822(self, mock_ai, mock_note, mock_tags):
        conn = mock_conn()
        self.run_check(conn)
        _assert_peek_not_rfc822(self, conn)

    def test_filed_email_marked_read_via_explicit_store(self, mock_ai, mock_note, mock_tags):
        # New contract: a filed email is marked read ("read = handled"), even a
        # human-needed one. The fetch still uses PEEK (fetching != dispositioning);
        # the \Seen comes from an EXPLICIT store after filing, not from the fetch.
        conn = mock_conn()
        results = self.run_check(conn)

        # Sanity: it really was processed down the human-needed branch.
        self.assertEqual(len(results['processed']), 1)
        self.assertFalse(results['processed'][0].get('auto_resolved', False))

        # Filed → an explicit \Seen store fires; the fetch itself stays PEEK.
        conn.store.assert_called_once_with('1', '+FLAGS', '\\Seen')
        _assert_peek_not_rfc822(self, conn)


# ---------------------------------------------------------------------------
# Global sweep path: process_incoming_emails()
# ---------------------------------------------------------------------------

class ProcessIncomingEmailsPeekTests(TestCase):
    def _settings(self):
        s = Mock()
        s.imap_host = 'imap.test.com'
        s.imap_user = 'user@test.com'
        s.imap_pass = 'password'
        s.email_analysis_prompt = 'Test prompt'
        s.email_domain = 'mydomain.com'
        return s

    @patch('apps.communications.services.process_single_email')
    @patch('apps.communications.services.imaplib')
    @patch('apps.communications.services.SystemSettings')
    def test_fetch_uses_peek_not_rfc822(
            self, mock_settings_cls, mock_imaplib, mock_process_single):
        mock_settings_cls.get_instance.return_value = self._settings()

        conn = mock_conn()
        mock_imaplib.IMAP4_SSL.return_value = conn

        # Keep the LLM/Zendesk pipeline out of it -- a human-needed result.
        result = Mock()
        result.auto_resolved = False
        mock_process_single.return_value = result

        process_incoming_emails()

        _assert_peek_not_rfc822(self, conn)

    @patch('apps.communications.services.process_single_email')
    @patch('apps.communications.services.imaplib')
    @patch('apps.communications.services.SystemSettings')
    def test_sweep_delegates_marking_to_process_single_email(
            self, mock_settings_cls, mock_imaplib, mock_process_single):
        # The sweep loop only FETCHES (with PEEK); deciding/setting the read flag
        # happens inside process_single_email (mocked out here). So the sweep loop
        # itself must never issue a \Seen store.
        mock_settings_cls.get_instance.return_value = self._settings()

        conn = mock_conn()
        mock_imaplib.IMAP4_SSL.return_value = conn

        result = Mock()
        result.auto_resolved = False
        mock_process_single.return_value = result

        process_incoming_emails()

        conn.store.assert_not_called()
        _assert_peek_not_rfc822(self, conn)
