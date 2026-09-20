"""Second code-review pass on the evidence-report editor's save-time
sanitizer (apps.payments.frontend_views.strip_active_html) and per-image
serving endpoint (apps.payments.frontend_views.dispute_document_image,
apps.payments.report_images.parse_data_uri / extract_image_data_uris) --
findings BEYOND the first round already pinned in
test_report_editor_hardening.py (which passes in full today).

This file pins the second-review findings:

1. Unquoted attribute values: strip_active_html's href=/src= regex only
   matches a QUOTED value ("..." or '...'). `<a href=javascript:alert(1)>`
   (no quotes at all) never matches that pattern, so the dangerous scheme
   is never even offered to the dangerous-scheme check and survives
   verbatim in the saved document.

2. Other URL-bearing attributes: the dangerous-scheme check only inspects
   attributes literally named `href` or `src`. `xlink:href` (SVG links),
   `formaction` / `action` (form submission targets), `data` (<object>),
   and `srcset` (<img>) are never inspected at all, so a dangerous scheme
   there survives untouched -- while ordinary, non-dangerous values in
   those same attributes must keep working exactly as before.

3. Other dangerous schemes (vbscript:, data:text/html) carried through the
   SAME obfuscations round-1 covered for javascript: (a control character
   split across the scheme, an HTML-entity-encoded first letter), plus
   data:text/html reaching through <iframe src> as well as <a href>. A
   raster data:image URI must never be caught by this net.

4. dispute_document_image serves the mime type exactly as stored:
   report_images.parse_data_uri lowercases the mime only for its
   _RASTER_MIMES membership check, not for the value it returns, so a
   stored `data:IMAGE/PnG;base64,...` is served back with a mixed-case
   Content-Type instead of the canonical lowercase form.

5. report_images.extract_image_data_uris matches `_DATA_IMAGE_RE` against
   a STRIPPED copy of the src value but appends the UN-stripped value to
   its output list. A whitespace-padded data URI (spaces inside the
   attribute's quotes, outside the URI itself) is therefore still
   recognised for externalizing the srcdoc, but parse_data_uri's
   `uri.startswith('data:')` check then fails on the leading space and
   dispute_document_image 404s instead of serving the photo.

Where a test already passes against today's implementation (some do --
see individual docstrings), that is stated explicitly instead of claiming
RED, and confirmed by actually running the file, not by prediction.
"""

import base64
import html
import re
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments.models import Dispute, DisputeDocument

User = get_user_model()

EVIDENCE_REPORT = DisputeDocument.DOC_TYPE_EVIDENCE_REPORT


# --- fixtures / helpers (mirrors test_report_editor_hardening.py's pattern) -

def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode('ascii')


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-SANITIZER', buyer_email='b@e.com', transaction_id='TX-SANITIZER',
                transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED', raw_webhook_payload={})
    base.update(kw)
    return Dispute.objects.create(**base)


def _doc(content_html, doc_type=EVIDENCE_REPORT, version=1):
    return DisputeDocument.objects.create(
        dispute=_dispute(), doc_type=doc_type,
        status='DRAFT', generated_by='MANUAL', content_html=content_html, version=version)


def _simple_html(text='original'):
    return f'<html><body><p>{text}</p></body></html>'


def _image_url(doc, index):
    return reverse('disputes:dispute_document_image', args=[doc.id, index])


def _extract_srcdoc(response):
    match = re.search(r'srcdoc="([^"]*)"', response.content.decode())
    assert match, "no srcdoc attribute found in the rendered editor page"
    return html.unescape(match.group(1))


class _Base(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='sanitizer_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)


def _post_and_reload(client, doc, edit_url, posted_html):
    """POST `posted_html` as the WHOLE new content_html for an
    EVIDENCE_REPORT document (mocking the PDF renderer -- irrelevant to the
    sanitizer/image-index behaviour under test here, and not guaranteed to
    be installed in the test environment) and return the freshly reloaded,
    saved content_html."""
    with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
        resp = client.post(edit_url, {'content_html': posted_html, 'version_increment': 'on'})
    assert resp.status_code == 302, (
        f"expected a redirect after saving, got {resp.status_code}: {resp.content[:500]!r}")
    doc.refresh_from_db()
    return doc.content_html


# --- 1. Unquoted attribute values --------------------------------------------

class UnquotedAttributeValueTests(_Base):
    """1 -- strip_active_html's href=/src= regex requires a QUOTED value, so
    an unquoted `href=javascript:alert(1)` (no quotes at all -- valid HTML)
    never matches the pattern and is never even considered by the
    dangerous-scheme check."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=EVIDENCE_REPORT, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_unquoted_href_javascript_is_neutralised(self):
        """RED today: the whole `href=javascript:alert(1)` attribute is
        never matched by strip_active_html's quoted-only regex, so it
        survives verbatim, "javascript:" and "alert(1)" included."""
        saved = _post_and_reload(self.web, self.doc, self.edit_url,
                                  '<a href=javascript:alert(1)>x</a>')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(1)', saved)

    def test_unquoted_src_javascript_is_neutralised(self):
        """RED today, same root cause as the href case above."""
        saved = _post_and_reload(self.web, self.doc, self.edit_url,
                                  '<img src=javascript:alert(2)>')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(2)', saved)


# --- 2. Other URL-bearing attributes ------------------------------------------

class OtherUrlBearingAttributeTests(_Base):
    """2 -- strip_active_html's dangerous-scheme check only inspects
    attributes literally named `href` or `src`. xlink:href, formaction,
    action, object's data, and img's srcset carry URLs too but are never
    inspected at all, so a dangerous scheme there is never dropped. The
    same (legitimate) attributes must otherwise keep working unchanged,
    including an href whose value merely CONTAINS "javascript:" without
    starting with it."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=EVIDENCE_REPORT, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def _saved(self, posted_html):
        return _post_and_reload(self.web, self.doc, self.edit_url, posted_html)

    def test_xlink_href_javascript_is_neutralised(self):
        """RED today: strip_active_html's regex requires a whitespace
        character immediately before "href"; in `xlink:href` that spot is
        held by ":", so the attribute is never matched."""
        saved = self._saved('<a xlink:href="javascript:alert(1)">x</a>')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(1)', saved)

    def test_formaction_javascript_is_neutralised(self):
        """RED today: "formaction" is not "href" or "src", so this
        attribute is never inspected at all."""
        saved = self._saved('<button formaction="javascript:alert(1)">x</button>')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(1)', saved)

    def test_form_action_javascript_is_neutralised(self):
        """RED today, same root cause: a <form>'s "action" attribute is
        never inspected."""
        saved = self._saved('<form action="javascript:alert(1)"></form>')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(1)', saved)

    def test_object_data_javascript_is_neutralised(self):
        """RED today, same root cause: an <object>'s "data" attribute is
        never inspected."""
        saved = self._saved('<object data="javascript:alert(1)"></object>')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(1)', saved)

    def test_img_srcset_javascript_is_neutralised(self):
        """RED today: "srcset" is not "src" (the plain-`src`-only regex
        does not match "srcset=" -- after "src" comes "set=", not "="), so
        this attribute is never inspected."""
        saved = self._saved('<img srcset="javascript:alert(1) 1x">')
        self.assertNotIn('javascript:', saved.lower())
        self.assertNotIn('alert(1)', saved)

    def test_legit_form_action_survives(self):
        """Pinned so hardening formaction/action can't collaterally break
        an ordinary form target."""
        saved = self._saved('<form action="/manager/disputes/1/"></form>')
        self.assertIn('/manager/disputes/1/', saved)

    def test_legit_object_data_survives(self):
        """Pinned so hardening <object data> can't collaterally break an
        ordinary embed target."""
        saved = self._saved('<object data="https://example.com/x.pdf"></object>')
        self.assertIn('https://example.com/x.pdf', saved)

    def test_legit_img_srcset_survives(self):
        """Pinned so hardening srcset can't collaterally break an ordinary
        responsive-image candidate."""
        saved = self._saved('<img srcset="https://example.com/a.png 1x">')
        self.assertIn('https://example.com/a.png 1x', saved)

    def test_href_with_javascript_substring_not_at_start_survives(self):
        """Already passes today: the normalised value starts with
        "https://", not "javascript:", so strip_active_html's existing
        href check already leaves it alone -- pinned so the fix for #1/#2
        can't turn this into an over-eager match on the substring."""
        saved = self._saved('<a href="https://example.com/?q=javascript:tips">x</a>')
        self.assertIn('https://example.com/?q=javascript:tips', saved)


# --- 3. Other dangerous schemes, same obfuscations ----------------------------

class OtherDangerousSchemeTests(_Base):
    """3 -- vbscript: and data:text/html are already in strip_active_html's
    dangerous-scheme tuple, and the normalisation added for javascript:
    (HTML-unescape, strip ASCII control chars, lowercase, then compare) is
    scheme-agnostic -- so the SAME obfuscations round-1 covered only for
    javascript: are pinned here for the other two schemes, plus
    data:text/html through <iframe src> as well as <a href>. A raster
    data:image URI must never be treated as dangerous."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=EVIDENCE_REPORT, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def _saved(self, posted_html):
        return _post_and_reload(self.web, self.doc, self.edit_url, posted_html)

    def test_plain_vbscript_is_neutralised(self):
        """Already passes today: 'vbscript:' is already in
        strip_active_html's dangerous-scheme tuple, matched via the
        existing quoted href regex with no obfuscation involved."""
        saved = self._saved('<a href="vbscript:msgbox(1)">x</a>')
        self.assertNotIn('vbscript:', saved.lower())
        self.assertNotIn('msgbox(1)', saved)

    def test_tab_inside_vbscript_scheme_is_neutralised(self):
        """Already passes today: the control-character-stripping
        normalisation round-1 added for javascript: applies to every
        dangerous scheme in the tuple, vbscript: included."""
        saved = self._saved('<a href="vb\tscript:msgbox(1)">x</a>')
        self.assertNotIn('vbscript:', saved.lower())
        self.assertNotIn('msgbox(1)', saved)

    def test_entity_encoded_first_letter_of_vbscript_is_neutralised(self):
        """Already passes today, same reason: the HTML-unescape
        normalisation applies before the scheme comparison regardless of
        which dangerous scheme it decodes to."""
        saved = self._saved('<a href="&#118;bscript:msgbox(1)">x</a>')
        self.assertNotIn('vbscript:', saved.lower())
        self.assertNotIn('msgbox(1)', saved)

    def test_data_text_html_base64_in_anchor_href_is_neutralised(self):
        """Already passes today: 'data:text/html' is already in the
        dangerous-scheme tuple, and `.startswith(...)` on the normalised
        value matches regardless of what base64 payload follows it."""
        saved = self._saved(
            '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>')
        self.assertNotIn('data:text/html', saved.lower())
        self.assertNotIn('PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==', saved)

    def test_data_text_html_base64_in_iframe_src_is_neutralised(self):
        """Already passes today, same reason -- the check is not scoped to
        any particular tag, it fires on any quoted src= attribute."""
        saved = self._saved(
            '<iframe src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="></iframe>')
        self.assertNotIn('data:text/html', saved.lower())
        self.assertNotIn('PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==', saved)

    def test_data_image_in_img_src_survives(self):
        """Already passes today: data:image/... never starts with any
        dangerous scheme, so it is never a candidate for dropping --
        pinned so a broadened dangerous-scheme match can't start
        collaterally eating embedded photos."""
        payload = _b64(b'LEGIT-IMG-3')
        saved = self._saved(f'<img src="data:image/png;base64,{payload}">')
        self.assertIn(payload, saved)


# --- 4. Served mime is normalised ---------------------------------------------

class ServedMimeNormalisationTests(_Base):
    """4 -- report_images.parse_data_uri lowercases the mime ONLY for its
    _RASTER_MIMES membership check; the mime it RETURNS is the original,
    un-normalised substring from the stored data URI. dispute_document_image
    passes that straight through as the response's Content-Type, so a
    stored `data:IMAGE/PnG;base64,...` is served back as "IMAGE/PnG"
    instead of the canonical lowercase "image/png"."""

    def test_mixed_case_mime_is_served_lowercase(self):
        """RED today: parse_data_uri returns the mime exactly as stored
        ("IMAGE/PnG"), so the response's Content-Type is not the
        canonical lowercase form."""
        payload = b'MIXED-CASE-MIME-PNG'
        uri = f'data:IMAGE/PnG;base64,{_b64(payload)}'
        doc = _doc(f'<img src="{uri}">', doc_type=EVIDENCE_REPORT, version=1)
        resp = self.web.get(_image_url(doc, 0))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, payload)
        self.assertEqual(resp['Content-Type'], 'image/png')


# --- 5. Whitespace-padded data URI (padding OUTSIDE the URI itself) ----------

class WhitespacePaddedDataUriTests(_Base):
    """5 -- extract_image_data_uris checks `_DATA_IMAGE_RE` against a
    STRIPPED copy of the src value (`src.strip()`) but appends the
    UN-stripped `src` to its output list. externalize_images has the same
    "match stripped, but act on the whole attribute" shape, so a
    whitespace-padded data URI (spaces INSIDE the attribute's quotes,
    surrounding the URI) is still recognised and externalised correctly on
    GET. But dispute_document_image then calls parse_data_uri on that same
    un-stripped, padded value, and `uri.startswith('data:')` is False for a
    value with a leading space -- so it 404s instead of serving the photo."""

    def setUp(self):
        super().setUp()
        self.payload = b'WHITESPACE-PADDED-PNG'
        self.uri = f' data:image/png;base64,{_b64(self.payload)} '
        self.doc = _doc(f'<html><body><img src="{self.uri}"></body></html>',
                         doc_type=EVIDENCE_REPORT, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_srcdoc_externalizes_the_padded_data_uri(self):
        """Already passes today: externalize_images' match check is done
        against `src.strip()`, so the padded value is still recognised as
        photo index 0 and its WHOLE attribute value (padding included) is
        replaced by the index-0 URL."""
        resp = self.web.get(self.edit_url)
        self.assertEqual(resp.status_code, 200)
        srcdoc = _extract_srcdoc(resp)
        self.assertIn(_image_url(self.doc, 0), srcdoc)
        self.assertNotIn('data:image', srcdoc)

    def test_image_endpoint_serves_the_padded_data_uri(self):
        """RED today: extract_image_data_uris stores the padded (leading
        space included) src verbatim, so parse_data_uri's
        `uri.startswith('data:')` check fails and dispute_document_image
        404s instead of streaming the decoded photo."""
        resp = self.web.get(_image_url(self.doc, 0))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, self.payload)
