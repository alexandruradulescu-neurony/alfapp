"""Evidence-report editor: stop round-tripping megabytes of base64 through the
browser (the 2026-09-20 incident — a report with a handful of embedded photos
made the in-place WYSIWYG editor unusable, because every GET inlined every
photo into the iframe `srcdoc` as a `data:image/...;base64,...` URI, and every
Save posted the WHOLE document — photos included — straight back).

The fix under test: images stop being inlined into the editor's HTML. The
GET srcdoc (and whatever the browser posts back) reference a photo via a
tiny per-document, per-index URL (`disputes:dispute_document_image`); the
actual bytes live only in the stored `content_html` row and in the dedicated
image response. On Save, the server re-inlines each referenced photo — by
index, from the CURRENTLY STORED content_html — before handing the HTML to
the PDF renderer (which cannot fetch relative URLs) and before persisting it.

None of this exists yet, so most of these tests are RED for the same root
cause: `reverse('disputes:dispute_document_image', ...)` raises
NoReverseMatch because the URL name doesn't exist. Where a test can fail for
a more specific reason without needing that URL (the srcdoc still embedding
`data:image`, the empty-content guard, the PDF-failure messaging, the
version_increment default, and the `javascript:` stripping), it does —
see the per-class docstrings below.
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


# --- fixtures / helpers ------------------------------------------------------

# Three distinct, recognisable, REAL byte payloads — never confused with each
# other when substring-matched out of stored HTML or a captured PDF-render call.
IMG_PAYLOADS = [b'PNG-ONE', b'PNG-TWO', b'JPG-THREE']
IMG_MIMES = ['image/png', 'image/png', 'image/jpeg']


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode('ascii')


def _data_uri(index: int) -> str:
    """The data URI stored for image `index` (0-based, per IMG_PAYLOADS/IMG_MIMES)."""
    return f'data:{IMG_MIMES[index]};base64,{_b64(IMG_PAYLOADS[index])}'


def _stored_html_with_images(text='Hi &amp; bye', indices=(0, 1, 2)):
    """Build a content_html value the way document_service currently stores an
    EVIDENCE_REPORT with embedded photos: data-URI <img> tags in document
    order (the position in `indices` IS the 0-based "index" the new image
    view keys off), plus some text to prove text still round-trips."""
    imgs = ''.join(f'<img src="{_data_uri(i)}" alt="photo{i}">' for i in indices)
    return f'<html><body><p>{text}</p>{imgs}</body></html>'


def _simple_html(text='original'):
    return f'<html><body><p>{text}</p></body></html>'


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-IMG', buyer_email='b@e.com', transaction_id='TX-IMG',
                transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED', raw_webhook_payload={})
    base.update(kw)
    return Dispute.objects.create(**base)


def _report_doc(content_html, version=1):
    return DisputeDocument.objects.create(
        dispute=_dispute(), doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
        status='DRAFT', generated_by='MANUAL', content_html=content_html, version=version)


def _image_url(doc, index):
    """The URL the editor is expected to reference photo `index` of `doc`
    with. Raises NoReverseMatch until the URL name exists — that is the RED
    failure for every test that needs it."""
    return reverse('disputes:dispute_document_image', args=[doc.id, index])


def _extract_srcdoc(response):
    """Pull the raw srcdoc="..." attribute value out of the rendered editor
    page and HTML-unescape it once, the way a browser parsing the attribute
    would, so we can inspect the markup it actually contains."""
    match = re.search(r'srcdoc="([^"]*)"', response.content.decode())
    assert match, "no srcdoc attribute found in the rendered editor page"
    return html.unescape(match.group(1))


def _message_list(response):
    return list(messages.get_messages(response.wsgi_request))


class _Base(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='img_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)


# --- A. GET no longer inlines images into the srcdoc ------------------------

class EditorGetExternalizesImagesTests(_Base):
    """A — the editor's srcdoc must reference photos by URL, not embed them."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_stored_html_with_images())

    def test_srcdoc_has_no_data_image(self):
        """RED today: the current view embeds every photo as data:image."""
        resp = self.web.get(reverse('disputes:dispute_edit_document', args=[self.doc.id]))
        self.assertEqual(resp.status_code, 200)
        srcdoc = _extract_srcdoc(resp)
        self.assertNotIn('data:image', srcdoc)

    def test_srcdoc_references_each_image_url_by_index(self):
        """RED today: NoReverseMatch (the URL name doesn't exist yet)."""
        resp = self.web.get(reverse('disputes:dispute_edit_document', args=[self.doc.id]))
        srcdoc = _extract_srcdoc(resp)
        for i in range(3):
            self.assertIn(_image_url(self.doc, i), srcdoc)

    def test_srcdoc_keeps_the_document_text(self):
        """Text must still round-trip from the stored row — today nothing
        checks this, and it happens to already pass, but it must keep
        passing once images stop being embedded inline."""
        resp = self.web.get(reverse('disputes:dispute_edit_document', args=[self.doc.id]))
        srcdoc = _extract_srcdoc(resp)
        self.assertIn('Hi &amp; bye', srcdoc)


# --- B. The new per-image endpoint ------------------------------------------

class DocumentImageViewTests(_Base):
    """B — GET disputes:dispute_document_image streams one decoded image back
    out of the document's stored content_html. Every test here needs the new
    URL, so all fail on NoReverseMatch today (the endpoint doesn't exist)."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_stored_html_with_images())

    def test_returns_decoded_bytes_and_mime_per_index(self):
        for i in range(3):
            resp = self.web.get(_image_url(self.doc, i))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.content, IMG_PAYLOADS[i])
            self.assertEqual(resp['Content-Type'], IMG_MIMES[i])

    def test_index_out_of_range_is_404(self):
        resp = self.web.get(_image_url(self.doc, 3))  # only 0,1,2 exist
        self.assertEqual(resp.status_code, 404)

    def test_response_is_not_cached(self):
        resp = self.web.get(_image_url(self.doc, 0))
        self.assertIn('no-store', resp.get('Cache-Control', ''))

    def test_unauthenticated_is_redirected_like_other_manager_views(self):
        # Mirrors apps/users/tests/test_media_serving.py's
        # test_media_anonymous_is_redirected_to_login: manager views are
        # gated purely by login_required, which redirects (302), never 200s.
        anon = Client()
        resp = anon.get(_image_url(self.doc, 0))
        self.assertEqual(resp.status_code, 302)


# --- C. POST re-inlines referenced images by index --------------------------

class PostReinlinesImagesTests(_Base):
    """C — the browser posts back <img src="..."> URLs (photos it kept) plus
    edited text; the server must re-inline each one from the CURRENTLY
    STORED content_html before saving and before handing HTML to the PDF
    renderer (which can't fetch a relative URL)."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_stored_html_with_images(), version=1)

    def _posted_html_dropping_image_1(self):
        # The manager deleted photo 1 in the editor, so the browser only
        # posts back the URLs for photos 0 and 2 — plus a THIRD image that
        # was already a raw data URI in the posted markup (e.g. one the
        # editor never got a chance to externalise), which must survive
        # untouched rather than being treated as a URL to look up.
        already_inline = 'data:image/png;base64,' + _b64(b'PASTED-INLINE')
        return (
            '<html><body><p>Edited text here</p>'
            f'<img src="{_image_url(self.doc, 0)}" alt="photo0">'
            f'<img src="{_image_url(self.doc, 2)}" alt="photo2">'
            f'<img src="{already_inline}" alt="pasted">'
            '</body></html>'
        )

    def test_saves_and_rerenders_with_reinlined_data_uris(self):
        """RED today: NoReverseMatch building the posted HTML (the URLs the
        browser would post back don't exist yet)."""
        posted = self._posted_html_dropping_image_1()
        with patch('apps.payments.document_service._render_to_pdf',
                   return_value=b'%PDF-1.4 fake') as render:
            resp = self.web.post(
                reverse('disputes:dispute_edit_document', args=[self.doc.id]),
                {'content_html': posted, 'version_increment': 'on'})
            render.assert_called_once()
            rendered_html = render.call_args.args[0]

        self.assertEqual(resp.status_code, 302)

        self.doc.refresh_from_db()
        self.assertIn('Edited text here', self.doc.content_html)
        self.assertIn(_b64(IMG_PAYLOADS[0]), self.doc.content_html)
        self.assertIn(_b64(IMG_PAYLOADS[2]), self.doc.content_html)
        self.assertNotIn(_b64(IMG_PAYLOADS[1]), self.doc.content_html)  # deleted photo, gone
        # No leftover placeholder URL of any kind survives the re-inlining.
        self.assertNotIn(_image_url(self.doc, 0), self.doc.content_html)
        self.assertNotIn(_image_url(self.doc, 2), self.doc.content_html)
        self.assertNotIn('/images/', self.doc.content_html)

        # The PDF renderer needs actual bytes, not a URL it can't fetch.
        self.assertIn(_data_uri(0), rendered_html)
        self.assertIn(_data_uri(2), rendered_html)
        self.assertNotIn(_image_url(self.doc, 0), rendered_html)

        self.assertEqual(self.doc.version, 2)

    def test_already_inline_data_uri_in_posted_html_is_kept_unchanged(self):
        """Passes today too (there's no re-inlining logic to corrupt it yet)
        — pins that the NEW re-inlining code must leave an untouched data URI
        alone rather than trying to treat it as a lookup key."""
        already_inline = 'data:image/png;base64,' + _b64(b'PASTED-INLINE')
        posted = f'<html><body><p>x</p><img src="{already_inline}"></body></html>'
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(reverse('disputes:dispute_edit_document', args=[self.doc.id]),
                          {'content_html': posted, 'version_increment': 'on'})
        self.doc.refresh_from_db()
        self.assertIn(already_inline, self.doc.content_html)


# --- D. Empty / whitespace-only POST body -----------------------------------

class EmptyContentGuardTests(_Base):
    """D — an empty (or whitespace-only) posted body must change NOTHING
    (today it still bumps the version) and must send the manager back to the
    editor with an error, not silently redirect to the dispute as a "success".
    """

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_simple_html(), version=1)
        self.original_html = self.doc.content_html
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def _assert_nothing_changed_and_flashes_error(self, posted_content_html):
        with patch('apps.payments.document_service._render_to_pdf') as render:
            resp = self.web.post(
                self.edit_url,
                {'content_html': posted_content_html, 'version_increment': 'on'})
            render.assert_not_called()

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.content_html, self.original_html)
        self.assertEqual(self.doc.version, 1)  # RED today: current code still bumps this

        # RED today: currently redirects to dispute_detail, not back to the editor.
        self.assertRedirects(resp, self.edit_url, fetch_redirect_response=False)

        msgs = _message_list(resp)
        self.assertTrue(any(m.level == messages.ERROR for m in msgs))

    def test_empty_content_html(self):
        self._assert_nothing_changed_and_flashes_error('')

    def test_whitespace_only_content_html(self):
        self._assert_nothing_changed_and_flashes_error('   \n\t  ')


# --- E. PDF re-render failure must not claim success ------------------------

class PdfRerenderFailureTests(_Base):
    """E — if _render_to_pdf fails (returns None, or raises), the text edit
    must still be SAVED (don't lose the manager's work), but today's view
    always flashes success regardless — that must stop being true when the
    PDF didn't actually regenerate."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_simple_html(), version=3)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def _assert_saved_with_no_success_message(self, resp):
        self.doc.refresh_from_db()
        self.assertIn('Edited despite PDF trouble', self.doc.content_html)
        self.assertEqual(self.doc.version, 4)

        msgs = _message_list(resp)
        # RED today: the view's `else` branch always calls messages.success(...).
        self.assertFalse(any(m.level == messages.SUCCESS for m in msgs))
        self.assertTrue(any(
            m.level in (messages.WARNING, messages.ERROR) and 'PDF' in str(m)
            for m in msgs))

    def test_render_returns_none(self):
        posted = _simple_html('Edited despite PDF trouble')
        with patch('apps.payments.document_service._render_to_pdf', return_value=None):
            resp = self.web.post(self.edit_url,
                                 {'content_html': posted, 'version_increment': 'on'})
        self._assert_saved_with_no_success_message(resp)

    def test_render_raises(self):
        posted = _simple_html('Edited despite PDF trouble')
        with patch('apps.payments.document_service._render_to_pdf',
                   side_effect=Exception('boom')):
            resp = self.web.post(self.edit_url,
                                 {'content_html': posted, 'version_increment': 'on'})
        self._assert_saved_with_no_success_message(resp)


# --- F. version_increment semantics -----------------------------------------

class VersionIncrementTests(_Base):
    """F — a MISSING version_increment field must mean "don't bump". Today
    `request.POST.get('version_increment', 'on')` treats a missing field
    exactly like 'on', so this is a real behaviour change."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_simple_html(), version=5)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_missing_field_does_not_bump_version(self):
        """RED today: a missing field currently still increments the version."""
        posted = _simple_html('No bump please')
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(self.edit_url, {'content_html': posted})  # no version_increment key
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.version, 5)
        self.assertIn('No bump please', self.doc.content_html)

    def test_explicit_on_bumps_version(self):
        """Already passes today; pinned so the fix for the missing-field case
        above can't accidentally break the explicit-'on' case."""
        posted = _simple_html('Bump please')
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(self.edit_url,
                          {'content_html': posted, 'version_increment': 'on'})
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.version, 6)


# --- G. strip_active_html also neutralises javascript: URLs -----------------

class JavascriptUrlStrippedTests(_Base):
    """G — script tags and on*= handlers are already covered by
    EditDocSanitizeTests.test_script_stripped_on_save (not duplicated here).
    This pins the NEW requirement: a `javascript:` URL in href/src must not
    survive the save, either."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_simple_html(), version=1)
        self.edit_url = reverse('disputes:dispute_edit_document', args=[self.doc.id])

    def test_javascript_href_and_src_are_neutralised(self):
        """RED today: strip_active_html only strips <script> and on*=
        handlers, so both javascript: URLs survive verbatim."""
        posted = ('<a href="javascript:alert(1)">x</a>'
                  '<img src="javascript:alert(2)">')
        with patch('apps.payments.document_service._render_to_pdf', return_value=b'%PDF'):
            self.web.post(self.edit_url,
                          {'content_html': posted, 'version_increment': 'on'})
        self.doc.refresh_from_db()
        self.assertNotIn('javascript:', self.doc.content_html.lower())


# --- H. Template hooks on GET ------------------------------------------------

class EditorTemplateHooksTests(_Base):
    """H — the GET page needs an unsaved-changes guard, and must keep posting
    version_increment='on' for the in-place WYSIWYG form (the missing-field
    semantics from F are about the plain-textarea editor's checkbox, not
    this hidden field)."""

    def setUp(self):
        super().setUp()
        self.doc = _report_doc(_simple_html(), version=1)

    def test_has_beforeunload_guard(self):
        """RED today: no unsaved-changes guard exists in the template yet."""
        resp = self.web.get(reverse('disputes:dispute_edit_document', args=[self.doc.id]))
        self.assertContains(resp, 'beforeunload')

    def test_has_version_increment_hidden_field(self):
        """Already passes today; guards against the WYSIWYG form's hidden
        field being dropped or defaulted differently while wiring up F."""
        resp = self.web.get(reverse('disputes:dispute_edit_document', args=[self.doc.id]))
        self.assertContains(resp, 'name="version_increment" value="on"')
