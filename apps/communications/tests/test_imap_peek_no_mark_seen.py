"""RED-phase tests: fetching mail must NOT mark it ``\\Seen``.

INVARIANT under test
--------------------
Pulling a message off the IMAP server is a *read* of the bytes, not a
human dispositioning it. Mail that needs a person must be LEFT UNREAD so
a human still sees it in the inbox. Only the explicit auto-resolve path
(``mark_email_as_seen`` -> ``store(uid, '+FLAGS', '\\Seen')``) may set the
Seen flag.

THE BUG these tests pin
-----------------------
Both fetch sites call ``conn.fetch(uid, '(RFC822)')``. In IMAP, fetching
``RFC822`` (a.k.a. ``BODY[]``) sets ``\\Seen`` as a *side effect*. The
non-marking spelling is ``BODY.PEEK[]``. So today every fetched message is
silently marked read, defeating the "leave unread for a human" design and
making the selective ``mark_email_as_seen`` pointless.

The observable contract these tests assert: the FETCH command must use the
PEEK (non-marking) form -- the spec passed to ``conn.fetch`` contains
``BODY.PEEK`` and is NOT the bare ``RFC822`` form. Covers BOTH fetch sites:

  * the per-ticket path ``check_email_for_ticket(...)`` (services.py:1114), and
  * the global sweep ``process_incoming_emails()`` (services.py:886).

These tests are EXPECTED TO FAIL until the fetch spec is changed to PEEK.
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

    def test_human_needed_email_left_unread(self, mock_ai, mock_note, mock_tags):
        # A non-auto-resolvable email (needs a human) must stay unread:
        # the fetch must not mark it seen AND no explicit \Seen store fires.
        conn = mock_conn()
        results = self.run_check(conn)

        # Sanity: it really was processed down the human-needed branch.
        self.assertEqual(len(results['processed']), 1)
        self.assertFalse(results['processed'][0].get('auto_resolved', False))

        # No explicit mark-as-seen for a human-needed message.
        conn.store.assert_not_called()
        # And the fetch itself must not have marked it read.
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
    def test_human_needed_email_left_unread(
            self, mock_settings_cls, mock_imaplib, mock_process_single):
        # The sweep itself only fetches; auto-resolve marking happens inside
        # process_single_email (mocked away here). For a human-needed message
        # the fetch must not be the one that marks it read.
        mock_settings_cls.get_instance.return_value = self._settings()

        conn = mock_conn()
        mock_imaplib.IMAP4_SSL.return_value = conn

        result = Mock()
        result.auto_resolved = False
        mock_process_single.return_value = result

        process_incoming_emails()

        # The sweep must never issue \Seen on its own (only the auto-resolve
        # branch inside process_single_email may, and that is mocked out).
        conn.store.assert_not_called()
        _assert_peek_not_rfc822(self, conn)
