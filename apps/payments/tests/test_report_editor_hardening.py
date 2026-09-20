"""Hardening gaps found in code review of the evidence-report editor (see
apps/payments/report_images.py, apps/payments/frontend_views.py's
dispute_edit_document/dispute_document_image/strip_active_html, and
templates/manager/dispute_edit_document.html — all already implement the
base "externalize on GET / re-inline on POST" feature covered by
test_report_editor_images.py, which currently passes in full).

This file pins the review findings that base feature does NOT yet cover:

1. SVG (and other non-raster) data URIs are served/indexed with their
   stored mime instead of being excluded from the image index entirely.
2. `javascript:` obfuscation (control characters split across the scheme,
   HTML-entity-encoded letters) survives the save-time sanitizer.
3. Concurrent edits: no optimistic-concurrency guard exists, so two
   managers editing the same document race silently.
4. Posted image URL forms: a query string or fragment on a re-posted
   image URL isn't recognised, even though absolute host-prefixed forms
   already are.
5. Bad/whitespace-wrapped base64: malformed base64 already 404s (for an
   incidental reason), but base64 wrapped with embedded newlines is never
   recognised as an image at all.
6. Legacy RESPONSE_LETTER documents going through the now-shared POST
   path (re-inlining + concurrency check) must not regress.

Also covered as smaller, targeted pins: only GET is accepted on the
per-image endpoint; uppercase `<IMG SRC=...>` markup; and the dirty-flag
template hooks (keydown/paste) that back the unsaved-changes guard.

Where a test already passes against today's implementation (a few do, for
incidental or genuine reasons), its docstring says so explicitly instead of
claiming RED.
"""

import base64
import html
import re
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments.models import Dispute, DisputeDocument

User = get_user_model()

EVIDENCE_REPORT = DisputeDocument.DOC_TYPE_EVIDENCE_REPORT
RESPONSE_LETTER = DisputeDocument.DOC_TYPE_RESPONSE_LETTER


# --- fixtures / helpers (mirrors test_report_editor_images.py's pattern) ----

# Three distinct, recognisable, REAL byte payloads — never confused with each
# other when substring-matched out of stored HTML or a captured response.
IMG_PAYLOADS = [b'PNG-ONE', b'PNG-TWO', b'JPG-THREE']
IMG_MIMES = ['image/png', 'image/png', 'image/jpeg']


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode('ascii')


def _uri(mime: str, payload: bytes) -> str:
    return f'data:{mime};base64,{_b64(payload)}'


def _data_uri(index: int) -> str:
    """The data URI stored for image `index` (0-based, per IMG_PAYLOADS/IMG_MIMES)."""
    return _uri(IMG_MIMES[index], IMG_PAYLOADS[index])


def _wrapped_b64(payload: bytes, width: int = 76) -> str:
    """`payload`'s base64 encoding, hard-wrapped with a newline every
    `width` characters — the shape some base64 producers emit (RFC 2045
    MIME-style wrapping), as opposed to one unbroken line."""
    raw = _b64(payload)
    return '\n'.join(raw[i:i + width] for i in range(0, len(raw), width))


def _stored_html_with_images(text='Hi &amp; bye', indices=(0, 1, 2)):
    imgs = ''.join(f'<img src="{_data_uri(i)}" alt="photo{i}">' for i in indices)
    return f'<html><body><p>{text}</p>{imgs}</body></html>'


def _simple_html(text='original'):
    return f'<html><body><p>{text}</p></body></html>'


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-HARDEN', buyer_email='b@e.com', transaction_id='TX-HARDEN',
                transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED', raw_webhook_payload={})
    base.update(kw)
    return Dispute.objects.create(**base)


def _doc(content_html, doc_type=EVIDENCE_REPORT, version=1):
    return DisputeDocument.objects.create(
        dispute=_dispute(), doc_type=doc_type,
        status='DRAFT', generated_by='MANUAL', content_html=content_html, version=version)


def _image_url(doc, index):
    return reverse('disputes:dispute_document_image', args=[doc.id, index])


def _extract_srcdoc(response):
    match = re.search(r'srcdoc="([^"]*)"', response.content.decode())
    assert match, "no srcdoc attribute found in the rendered editor page"
    return html.unescape(match.group(1))


def _message_list(response):
    return list(messages.get_messages(response.wsgi_request))


# A `href="..."` or `src="..."` attribute (either quote style), used to scan
# saved HTML for a still-dangerous URL after sanitization.
_HREF_SRC_ATTR_RE = re.compile(
    r'''\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')''', re.IGNORECASE)


def _normalised_attr_value(raw_value: str) -> str:
    """The robust-comparison form the review called for: HTML-entity-decode,
    drop every ASCII control character (obfuscations hide inside the scheme
    using literal tabs/newlines), then lowercase."""
    value = html.unescape(raw_value)
    value = re.sub(r'[\x00-\x1f]', '', value)
    return value.strip().lower()


def _assert_no_javascript_url_attrs(html_text: str):
    """Fail if any href=/src= attribute in `html_text`, once normalised,
    resolves to a `javascript:` URL."""
    for match in _HREF_SRC_ATTR_RE.finditer(html_text or ''):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        normalised = _normalised_attr_value(raw)
        assert not normalised.startswith('javascript:'), (
            f"a javascript: URL survived sanitization in an href/src attribute: {raw!r} "
            f"(normalised: {normalised!r})")


class _Base(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='harden_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)


# --- 1. Only raster photos are images ---------------------------------------

class RasterOnlyImageIndexTests(_Base):
    """1 — a data URI whose mime isn't image/{png,jpeg,jpg,gif,webp} (e.g. an
    SVG) must never be counted in the photo index: it must stay inline in
    the srcdoc untouched, and the per-index image endpoint must never serve
    it under someone else's slot."""

    def setUp(self):
        super().setUp()
        self.png_uri = _uri('image/png', b'RASTER-PNG')
        self.svg_uri = _uri('image/svg+xml', b'<svg>not-a-photo</svg>')
        html_ = f'<html><body><p>x</p><img src="{self.png_uri}"><img src="{self.svg_uri}"></body></html>'
        self.doc = _doc(html_)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_srcdoc_externalizes_the_png_but_leaves_the_svg_inline(self):
        """RED today: report_images._DATA_IMAGE_RE accepts any `image/*`
        mime, so the SVG is counted as photo index 1 and externalized to a
        URL exactly like the PNG — it does NOT stay inline."""
        resp = self.web.get(self.edit_url)
        self.assertEqual(resp.status_code, 200)
        srcdoc = _extract_srcdoc(resp)
        self.assertIn(_image_url(self.doc, 0), srcdoc)
        self.assertIn('data:image/svg+xml', srcdoc)

    def test_svg_is_not_assigned_an_image_index(self):
        """RED today: since the SVG IS counted (as index 1), fetching index
        1 currently 200s with the SVG bytes instead of 404ing."""
        resp = self.web.get(_image_url(self.doc, 1))
        self.assertEqual(resp.status_code, 404)

    def test_png_at_index_0_is_served_with_its_content_type(self):
        """Already passes today: the PNG is (and must remain) index 0
        regardless of how the SVG ends up handled."""
        resp = self.web.get(_image_url(self.doc, 0))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'RASTER-PNG')
        self.assertEqual(resp['Content-Type'], 'image/png')


# --- 2. javascript: obfuscations ---------------------------------------------

class JavascriptUrlObfuscationTests(_Base):
    """2 — strip_active_html's javascript: filter only matches the literal,
    contiguous, case-folded text "javascript:" immediately after the opening
    quote. Several real obfuscations defeat that and must be caught instead
    by normalising (HTML-unescape + strip ASCII control chars + lowercase)
    before comparing."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=RESPONSE_LETTER, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def _post_and_reload(self, posted_html):
        resp = self.web.post(self.edit_url,
                              {'content_html': posted_html, 'version_increment': 'on'})
        self.assertEqual(resp.status_code, 302)
        self.doc.refresh_from_db()
        return self.doc.content_html

    def test_tab_inside_scheme_is_neutralised(self):
        """RED today: a literal TAB between "java" and "script:" breaks the
        contiguous-text match, so the handler survives verbatim."""
        saved = self._post_and_reload('<a href="java\tscript:alert(1)">x</a>')
        _assert_no_javascript_url_attrs(saved)

    def test_newline_inside_scheme_is_neutralised(self):
        """RED today, same root cause as the tab case above."""
        saved = self._post_and_reload('<a href="java\nscript:alert(1)">x</a>')
        _assert_no_javascript_url_attrs(saved)

    def test_decimal_entity_encoded_first_letter_is_neutralised(self):
        """RED today: strip_active_html matches raw source text, never
        HTML-entity-decoding first, so "&#106;avascript:" (decodes to
        "javascript:") isn't recognised as the dangerous scheme at all."""
        saved = self._post_and_reload('<a href="&#106;avascript:alert(1)">x</a>')
        _assert_no_javascript_url_attrs(saved)

    def test_hex_entity_encoded_first_letter_is_neutralised(self):
        """RED today, same root cause as the decimal-entity case above."""
        saved = self._post_and_reload('<a href="&#x6A;avascript:alert(1)">x</a>')
        _assert_no_javascript_url_attrs(saved)

    def test_uppercase_scheme_is_already_neutralised(self):
        """Already passes today: strip_active_html's regex uses
        re.IGNORECASE, and "JAVASCRIPT:" is still one contiguous literal
        run, so it's stripped exactly like the lowercase form."""
        saved = self._post_and_reload('<img src="JAVASCRIPT:alert(1)">')
        _assert_no_javascript_url_attrs(saved)

    def test_legit_links_and_images_survive(self):
        """Already passes today — pinned so hardening the javascript:
        filter can't collaterally damage ordinary links or already-inline
        data-URI images."""
        payload = _b64(b'LEGIT-IMG')
        posted = ('<a href="https://example.com/x?a=1">link</a>'
                  f'<img src="data:image/png;base64,{payload}">')
        saved = self._post_and_reload(posted)
        _assert_no_javascript_url_attrs(saved)
        self.assertIn('https://example.com/x?a=1', saved)
        self.assertIn(payload, saved)


# --- 3. Optimistic concurrency ------------------------------------------------

class OptimisticConcurrencyTests(_Base):
    """3 — two managers opening the same evidence report must not silently
    clobber each other. The editor page must round-trip the version it was
    loaded from (`base_version`), and a POST whose base_version disagrees
    with the CURRENT stored version must be rejected, not saved."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=EVIDENCE_REPORT, version=3)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_get_page_has_base_version_hidden_input(self):
        """RED today: no such field is rendered anywhere in the template."""
        resp = self.web.get(self.edit_url)
        self.assertContains(resp, 'name="base_version" value="3"')

    def test_stale_base_version_blocks_the_save(self):
        """RED today: there is no concurrency check at all, so a stale
        base_version currently still overwrites the document."""
        posted = _simple_html('Attempted stale overwrite')
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF') as render:
            resp = self.web.post(self.edit_url, {
                'content_html': posted, 'version_increment': 'on', 'base_version': '2',
            })
            render.assert_not_called()

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.version, 3)
        self.assertNotIn('Attempted stale overwrite', self.doc.content_html)
        self.assertRedirects(resp, self.edit_url, fetch_redirect_response=False)
        msgs = _message_list(resp)
        self.assertTrue(any(
            m.level == messages.ERROR and 'reload' in str(m).lower() for m in msgs))

    def test_matching_base_version_saves_normally(self):
        """Already passes today (there's no check to block it), and must
        keep passing once the stale-version guard above is implemented."""
        posted = _simple_html('Fresh edit')
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(self.edit_url, {
                'content_html': posted, 'version_increment': 'on', 'base_version': '3',
            })
        self.doc.refresh_from_db()
        self.assertIn('Fresh edit', self.doc.content_html)
        self.assertEqual(self.doc.version, 4)

    def test_missing_base_version_saves_normally(self):
        """Already passes today, same reason — pinned as the lenient path
        for older clients whose page doesn't post a base_version at all."""
        posted = _simple_html('Lenient old client edit')
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(self.edit_url, {
                'content_html': posted, 'version_increment': 'on',
            })
        self.doc.refresh_from_db()
        self.assertIn('Lenient old client edit', self.doc.content_html)
        self.assertEqual(self.doc.version, 4)


# --- 4. Posted image URL forms ------------------------------------------------

class PostedImageUrlFormsTests(_Base):
    """4 — a browser may post back a photo's URL in several equivalent
    shapes: relative, absolute (scheme+host prefixed), with a cache-busting
    query string, or with a fragment. All four must still resolve to the
    same stored photo and get re-inlined."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_stored_html_with_images(), doc_type=RESPONSE_LETTER, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])
        self.relative_url = _image_url(self.doc, 0)

    def _post_with_src_and_reload(self, src):
        posted = f'<html><body><p>edited</p><img src="{src}" alt="photo0"></body></html>'
        self.web.post(self.edit_url, {'content_html': posted, 'version_increment': 'on'})
        self.doc.refresh_from_db()
        return self.doc.content_html

    def test_https_testserver_absolute_form(self):
        """Already passes today: _index_from_image_url's matcher already
        strips a recognised http(s)://host prefix before comparing paths."""
        saved = self._post_with_src_and_reload('https://testserver' + self.relative_url)
        self.assertIn(_b64(IMG_PAYLOADS[0]), saved)
        self.assertNotIn('/images/', saved)

    def test_http_testserver_absolute_form(self):
        """Already passes today, same reason as the https form above."""
        saved = self._post_with_src_and_reload('http://testserver' + self.relative_url)
        self.assertIn(_b64(IMG_PAYLOADS[0]), saved)
        self.assertNotIn('/images/', saved)

    def test_url_with_query_string(self):
        """RED today: the matcher requires the path to END with '/' before
        it even looks at the prefix, so a trailing '?v=3' makes it bail out
        with None — the posted src is left as an unresolved URL instead of
        being swapped back for the real photo."""
        saved = self._post_with_src_and_reload(self.relative_url + '?v=3')
        self.assertIn(_b64(IMG_PAYLOADS[0]), saved)
        self.assertNotIn('/images/', saved)

    def test_url_with_fragment(self):
        """RED today, same root cause as the query-string case: a trailing
        '#x' also breaks the trailing-'/' check."""
        saved = self._post_with_src_and_reload(self.relative_url + '#x')
        self.assertIn(_b64(IMG_PAYLOADS[0]), saved)
        self.assertNotIn('/images/', saved)


# --- 5. Uppercase tags/attributes ---------------------------------------------

class UppercaseMarkupTests(_Base):
    """5 — some serialisations of editable HTML emit uppercase tag/attribute
    names (<IMG SRC=...>). Both the GET externalisation and the POST
    re-inlining must handle that exactly like the lowercase form."""

    def setUp(self):
        super().setUp()
        html_ = f'<html><body><p>x</p><IMG SRC="{_data_uri(0)}"></body></html>'
        self.doc = _doc(html_, doc_type=EVIDENCE_REPORT, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_get_externalizes_uppercase_img_tag(self):
        """Already passes today: report_images.py's tag/attribute regexes
        are already compiled with re.IGNORECASE."""
        resp = self.web.get(self.edit_url)
        srcdoc = _extract_srcdoc(resp)
        self.assertIn(_image_url(self.doc, 0), srcdoc)
        self.assertNotIn('data:image', srcdoc)

    def test_post_reinlines_uppercase_img_tag(self):
        """Already passes today, same reason — the src VALUE (a normal-case
        URL) is what's matched against; tag/attribute case is irrelevant."""
        posted = f'<html><body><p>edited</p><IMG SRC="{_image_url(self.doc, 0)}"></body></html>'
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(self.edit_url, {'content_html': posted, 'version_increment': 'on'})
        self.doc.refresh_from_db()
        self.assertIn(_b64(IMG_PAYLOADS[0]), self.doc.content_html)
        self.assertNotIn('/images/', self.doc.content_html)


# --- 6. Bad / whitespace-wrapped base64 ---------------------------------------

class BadOrWhitespaceBase64Tests(_Base):
    """6 — stored base64 that can't be decoded must 404, never 500; and
    base64 that's been hard-wrapped with embedded newlines (some producers
    emit 76-char lines, RFC 2045 style) must still be recognised as a photo
    and decoded correctly, not silently dropped."""

    def test_garbage_base64_404s_not_500s(self):
        """Already passes today, for an incidental reason: the '!' and '-'
        characters fall outside report_images._DATA_IMAGE_RE's base64
        character class, so this <img> is never recognised as a data-image
        at all — it's excluded from the index entirely, and index 0 is
        immediately out of range. Kept as a black-box pin against a 500
        regression regardless of which code path ends up producing the
        404 (recognised-but-undecodable vs. never-recognised)."""
        html_ = '<img src="data:image/png;base64,!!!not-base64!!!">'
        doc = _doc(html_, doc_type=EVIDENCE_REPORT, version=1)
        resp = self.web.get(_image_url(doc, 0))
        self.assertEqual(resp.status_code, 404)

    def test_whitespace_wrapped_base64_is_externalised_in_srcdoc(self):
        """RED today: _DATA_IMAGE_RE's payload class doesn't include '\\n',
        so a wrapped payload is never recognised as a data-image — it stays
        inline verbatim (raw data: URI, newlines and all) instead of
        becoming an index-0 URL."""
        wrapped = _wrapped_b64(b'LONG-WRAPPED-PAYLOAD-BYTES-REPEATED-' * 3)
        html_ = f'<html><body><img src="data:image/png;base64,{wrapped}"></body></html>'
        doc = _doc(html_, doc_type=EVIDENCE_REPORT, version=1)
        resp = self.web.get(reverse('disputes:dispute_edit_document', args=[doc.id]))
        srcdoc = _extract_srcdoc(resp)
        self.assertIn(_image_url(doc, 0), srcdoc)
        self.assertNotIn('data:image', srcdoc)

    def test_whitespace_wrapped_base64_decodes_correctly(self):
        """RED today, same root cause: never recognised as index 0, so
        fetching it 404s instead of streaming the correctly decoded bytes."""
        payload = b'LONG-WRAPPED-PAYLOAD-BYTES-REPEATED-' * 3
        wrapped = _wrapped_b64(payload)
        html_ = f'<html><body><img src="data:image/png;base64,{wrapped}"></body></html>'
        doc = _doc(html_, doc_type=EVIDENCE_REPORT, version=1)
        resp = self.web.get(_image_url(doc, 0))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, payload)
        self.assertEqual(resp['Content-Type'], 'image/png')


# --- 7. The image endpoint only answers GET -----------------------------------

class ImageEndpointMethodTests(_Base):
    """7 — dispute_document_image must only ever serve a GET; any other
    verb must 405 rather than silently executing the same code."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_stored_html_with_images(), doc_type=EVIDENCE_REPORT, version=1)

    def test_post_is_405(self):
        """RED today: the view has no method restriction at all, so a POST
        runs the exact same code as a GET and returns 200."""
        resp = self.web.post(_image_url(self.doc, 0))
        self.assertEqual(resp.status_code, 405)


# --- 8. Legacy RESPONSE_LETTER documents through the shared POST path --------

class LegacyResponseLetterPostPathTests(_Base):
    """8 — the POST path is now shared with image re-inlining and (once
    fixed) the concurrency check. A legacy RESPONSE_LETTER, which has no
    images, no base_version field, and no PDF, must not regress."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=RESPONSE_LETTER, version=1)
        self.original_html = self.doc.content_html
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_empty_content_html_changes_nothing_and_flashes_error(self):
        """Already passes today: the empty-body guard runs unconditionally,
        before any doc-type-specific branching."""
        resp = self.web.post(self.edit_url, {'content_html': '', 'version_increment': 'on'})
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.content_html, self.original_html)
        self.assertEqual(self.doc.version, 1)
        self.assertRedirects(resp, self.edit_url, fetch_redirect_response=False)
        msgs = _message_list(resp)
        self.assertTrue(any(m.level == messages.ERROR for m in msgs))

    def test_missing_version_increment_saves_without_bumping(self):
        """Already passes today: the version_increment semantics are
        doc-type-agnostic."""
        posted = _simple_html('Letter text, no bump')
        resp = self.web.post(self.edit_url, {'content_html': posted})
        self.assertEqual(resp.status_code, 302)
        self.doc.refresh_from_db()
        self.assertIn('Letter text, no bump', self.doc.content_html)
        self.assertEqual(self.doc.version, 1)

    def test_explicit_version_increment_saves_and_bumps_with_success_message(self):
        """Already passes today: a RESPONSE_LETTER never enters the
        PDF-regeneration branch, so the plain success message always fires
        (there's no PDF to fail regenerating)."""
        posted = _simple_html('Letter text, bumped')
        resp = self.web.post(self.edit_url, {'content_html': posted, 'version_increment': 'on'})
        self.assertEqual(resp.status_code, 302)
        self.doc.refresh_from_db()
        self.assertIn('Letter text, bumped', self.doc.content_html)
        self.assertEqual(self.doc.version, 2)
        msgs = _message_list(resp)
        self.assertTrue(any(m.level == messages.SUCCESS for m in msgs))


# --- 9. Template hooks for dirty tracking -------------------------------------

class DirtyTrackingTemplateHooksTests(_Base):
    """9 — the unsaved-changes guard relies on a `dirty` flag armed by the
    iframe's 'input' event. Keydown/paste don't always surface as an
    'input' event inside a designMode document across browsers, so the
    template needs dedicated listeners for them too, alongside the existing
    beforeunload guard."""

    def setUp(self):
        super().setUp()
        self.doc = _doc(_simple_html(), doc_type=EVIDENCE_REPORT, version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_has_keydown_listener(self):
        """RED today: no keydown listener exists in the template."""
        resp = self.web.get(self.edit_url)
        self.assertContains(resp, 'keydown')

    def test_has_paste_listener(self):
        """RED today: no paste listener exists in the template."""
        resp = self.web.get(self.edit_url)
        self.assertContains(resp, 'paste')

    def test_still_has_beforeunload(self):
        """Already passes today — pinned so adding keydown/paste can't
        accidentally drop the existing beforeunload guard."""
        resp = self.web.get(self.edit_url)
        self.assertContains(resp, 'beforeunload')
