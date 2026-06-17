"""RED-phase fail-closed tests for IMAP credentials (strict TDD).

Invariant under test
--------------------
When an encrypted SystemSettings credential cannot be decrypted, the
encryption layer returns the DECRYPTION_FAILED sentinel (a NUL-wrapped,
opaque, NON-EMPTY string — see apps/config/encrypted_fields.py). Because the
sentinel is truthy, the existing `all([imap_host, imap_user, imap_pass])`
guard at both IMAP connect sites passes it straight through to
imaplib.IMAP4_SSL(...) / .login(...). That hands a decrypt-failure sentinel to
an external mail server as a live credential.

These tests assert the system FAILS CLOSED: a credential equal to the sentinel
must NOT result in an IMAP4_SSL connection or login. They are expected to FAIL
against current code (the bug constructs the connection anyway).

Happy-path tests (normal credentials) prove the eventual guard won't break the
working path; they must pass now and after the fix.

We never persist the sentinel (the encryption layer refuses), so we MOCK the
read: SystemSettings.get_instance() is patched to return a stub whose fields we
control. imaplib.IMAP4_SSL is patched so we can assert it was / was not called.
"""

import types

import pytest
from unittest.mock import patch, Mock

from apps.config.encrypted_fields import DECRYPTION_FAILED
from apps.communications import services
from apps.communications.services import (
    EmailNotConfigured,
    open_inbox,
    process_incoming_emails,
)


def _settings_stub(*, imap_host, imap_user, imap_pass,
                   email_analysis_prompt="prompt", email_domain="mydomain.com"):
    """A stand-in for SystemSettings.get_instance() with controllable fields."""
    return types.SimpleNamespace(
        imap_host=imap_host,
        imap_user=imap_user,
        imap_pass=imap_pass,
        email_analysis_prompt=email_analysis_prompt,
        email_domain=email_domain,
    )


# ---------------------------------------------------------------------------
# Site 1: open_inbox() — button-driven per-ticket connector (services.py:946)
# ---------------------------------------------------------------------------
class TestOpenInboxFailClosed:
    """open_inbox() must refuse to connect when any IMAP credential is the
    decrypt-failure sentinel."""

    @patch("apps.communications.services.imaplib.IMAP4_SSL")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_password_does_not_connect(self, mock_get_instance, mock_imap_ssl):
        """SENTINEL: imap_pass is the decrypt-failure sentinel -> must raise
        EmailNotConfigured and never build the IMAP connection."""
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user="user@test.com",
            imap_pass=DECRYPTION_FAILED,
        )

        with pytest.raises(EmailNotConfigured):
            open_inbox()

        mock_imap_ssl.assert_not_called()

    @patch("apps.communications.services.imaplib.IMAP4_SSL")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_host_does_not_connect(self, mock_get_instance, mock_imap_ssl):
        """SENTINEL: imap_host is the sentinel -> fail closed, no connection."""
        mock_get_instance.return_value = _settings_stub(
            imap_host=DECRYPTION_FAILED,
            imap_user="user@test.com",
            imap_pass="password",
        )

        with pytest.raises(EmailNotConfigured):
            open_inbox()

        mock_imap_ssl.assert_not_called()

    @patch("apps.communications.services.imaplib.IMAP4_SSL")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_user_does_not_connect(self, mock_get_instance, mock_imap_ssl):
        """SENTINEL: imap_user is the sentinel -> fail closed, no connection."""
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user=DECRYPTION_FAILED,
            imap_pass="password",
        )

        with pytest.raises(EmailNotConfigured):
            open_inbox()

        mock_imap_ssl.assert_not_called()

    @patch("apps.communications.services.imaplib.IMAP4_SSL")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_password_does_not_login(self, mock_get_instance, mock_imap_ssl):
        """SENTINEL: a sentinel password must never reach IMAP .login()."""
        mock_conn = Mock()
        mock_imap_ssl.return_value = mock_conn
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user="user@test.com",
            imap_pass=DECRYPTION_FAILED,
        )

        with pytest.raises(EmailNotConfigured):
            open_inbox()

        mock_conn.login.assert_not_called()

    @patch("apps.communications.services.imaplib.IMAP4_SSL")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_normal_credentials_connect(self, mock_get_instance, mock_imap_ssl):
        """HAPPY PATH: with real credentials, open_inbox() connects, logs in and
        selects INBOX. Must pass now and after the fix."""
        mock_conn = Mock()
        mock_imap_ssl.return_value = mock_conn
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user="user@test.com",
            imap_pass="real-password",
        )

        conn = open_inbox()

        assert conn is mock_conn
        mock_imap_ssl.assert_called_once()
        mock_conn.login.assert_called_once_with("user@test.com", "real-password")
        mock_conn.select.assert_called_once_with("INBOX")


# ---------------------------------------------------------------------------
# Site 2: process_incoming_emails() — sweep connector (services.py:835/851)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestProcessIncomingEmailsFailClosed:
    """The sweep path guards with the same truthy all([...]) check and then
    calls IMAP4_SSL(...) + .login(...). A sentinel credential must fail closed:
    its existing not-configured path returns stats with errors==1 and never
    constructs the connection. The load-bearing assertion is that IMAP4_SSL is
    NOT called when a credential is the sentinel."""

    @patch("apps.communications.services.imaplib")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_password_does_not_connect(self, mock_get_instance, mock_imaplib):
        """SENTINEL: imap_pass is the sentinel -> treated as not-configured,
        no IMAP4_SSL constructed."""
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user="user@test.com",
            imap_pass=DECRYPTION_FAILED,
        )

        result = process_incoming_emails()

        mock_imaplib.IMAP4_SSL.assert_not_called()
        assert result["errors"] == 1
        assert result["processed"] == 0

    @patch("apps.communications.services.imaplib")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_host_does_not_connect(self, mock_get_instance, mock_imaplib):
        """SENTINEL: imap_host is the sentinel -> no connection."""
        mock_get_instance.return_value = _settings_stub(
            imap_host=DECRYPTION_FAILED,
            imap_user="user@test.com",
            imap_pass="password",
        )

        result = process_incoming_emails()

        mock_imaplib.IMAP4_SSL.assert_not_called()
        assert result["errors"] == 1
        assert result["processed"] == 0

    @patch("apps.communications.services.imaplib")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_sentinel_user_does_not_connect(self, mock_get_instance, mock_imaplib):
        """SENTINEL: imap_user is the sentinel -> no connection."""
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user=DECRYPTION_FAILED,
            imap_pass="password",
        )

        result = process_incoming_emails()

        mock_imaplib.IMAP4_SSL.assert_not_called()
        assert result["errors"] == 1
        assert result["processed"] == 0

    @patch("apps.communications.services.imaplib")
    @patch("apps.communications.services.SystemSettings.get_instance")
    def test_normal_credentials_connect(self, mock_get_instance, mock_imaplib):
        """HAPPY PATH: with real credentials the sweep does construct the IMAP
        connection (no UNSEEN mail keeps it a short, side-effect-free run)."""
        mock_get_instance.return_value = _settings_stub(
            imap_host="imap.test.com",
            imap_user="user@test.com",
            imap_pass="real-password",
        )
        mock_conn = Mock()
        mock_imaplib.IMAP4_SSL.return_value = mock_conn
        # No UNSEEN mail -> early, clean return after a successful connect.
        mock_conn.search.return_value = ("OK", [b""])

        result = process_incoming_emails()

        mock_imaplib.IMAP4_SSL.assert_called_once()
        mock_conn.login.assert_called_once_with("user@test.com", "real-password")
        assert result["errors"] == 0
        assert result["processed"] == 0
