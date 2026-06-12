"""AnonAddy unified-inbox sender decoding (2026-06-12).

Forwarded mail rewrites From into an encoded alias
(alias+real.local=real.domain@aliasdomain) and preserves the true sender in
X-AnonAddy-Original-Sender. LORA must record the real sender, not a mangled
slice of the encoded From (the bug: neurony.ro@mailapptoday.com).
"""

import email

from django.test import TestCase

from apps.communications.services import (
    decode_alias_encoded_address,
    extract_from_email,
)

# The real headers captured from production (trimmed to what matters).
REAL_FORWARDED = (
    b'From: "Alexandru Radulescu \'alexandru.radulescu at neurony.ro\'"\r\n'
    b' <andrei.deaconu+alexandru.radulescu=neurony.ro@mailapptoday.com>\r\n'
    b'To: andrei.deaconu@mailapptoday.com\r\n'
    b'X-AnonAddy-Original-Sender: alexandru.radulescu@neurony.ro\r\n'
    b'X-AnonAddy-Original-To: andrei.deaconu@mailapptoday.com\r\n'
    b'Subject: Test\r\n\r\nbody\r\n'
)


class DecodeAliasEncodedAddressTests(TestCase):
    def test_alias_plus_contact_form(self):
        self.assertEqual(
            decode_alias_encoded_address('andrei.deaconu+alexandru.radulescu=neurony.ro@mailapptoday.com'),
            'alexandru.radulescu@neurony.ro')

    def test_contact_only_form(self):
        self.assertEqual(
            decode_alias_encoded_address('alexandru.radulescu=neurony.ro@mailapptoday.com'),
            'alexandru.radulescu@neurony.ro')

    def test_plain_address_not_decoded(self):
        self.assertEqual(decode_alias_encoded_address('john@example.com'), '')

    def test_plus_addressing_not_decoded(self):
        # normal plus-addressing has no '=' → not an AnonAddy contact encoding
        self.assertEqual(decode_alias_encoded_address('john+newsletter@example.com'), '')


class ExtractFromEmailTests(TestCase):
    def test_prefers_anonaddy_original_sender_header(self):
        msg = email.message_from_bytes(REAL_FORWARDED)
        self.assertEqual(extract_from_email(msg), 'alexandru.radulescu@neurony.ro')

    def test_regression_does_not_record_mangled_slice(self):
        msg = email.message_from_bytes(REAL_FORWARDED)
        self.assertNotEqual(extract_from_email(msg), 'neurony.ro@mailapptoday.com')

    def test_decodes_from_when_header_absent(self):
        raw = (
            b'From: <andrei.deaconu+alexandru.radulescu=neurony.ro@mailapptoday.com>\r\n'
            b'To: andrei.deaconu@mailapptoday.com\r\nSubject: x\r\n\r\nbody\r\n'
        )
        msg = email.message_from_bytes(raw)
        self.assertEqual(extract_from_email(msg), 'alexandru.radulescu@neurony.ro')

    def test_plain_sender_unchanged(self):
        raw = b'From: Jane Doe <jane@example.com>\r\nSubject: x\r\n\r\nbody\r\n'
        msg = email.message_from_bytes(raw)
        self.assertEqual(extract_from_email(msg), 'jane@example.com')
