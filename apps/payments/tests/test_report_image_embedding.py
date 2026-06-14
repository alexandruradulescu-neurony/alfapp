"""Report image embedding (regression: inline Delta/Denver screenshots vanished,
leaving only the label text). Covers the host/auth fetch policy and the
downscale-instead-of-drop behaviour that replaced the silent 5 MB cap."""

import io
from unittest.mock import patch

from django.test import TestCase
from PIL import Image

from apps.config.models import SystemSettings
from apps.payments import document_service as ds


def _png_bytes(size=(120, 120), color='white', mode='RGB'):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format='PNG')
    return buf.getvalue()


class FetchPolicyTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'airportlf'
        ss.save()

    def test_our_host_uses_no_auth_first(self):
        # Zendesk attachment token URLs 403 under Basic auth; no-auth + UA works.
        with patch.object(ds, '_fetch_no_auth', return_value=b'IMG') as nf, \
             patch('apps.integrations.services.fetch_zendesk_attachment_bytes') as authed:
            data = ds._fetch_zendesk_image_bytes(
                'https://airportlf.zendesk.com/attachments/token/a/?name=x.png')
            nf.assert_called_once()
            authed.assert_not_called()
        self.assertEqual(data, b'IMG')

    def test_falls_back_to_authed_when_no_auth_returns_nothing(self):
        with patch.object(ds, '_fetch_no_auth', return_value=None), \
             patch('apps.integrations.services.fetch_zendesk_attachment_bytes',
                   return_value=b'AUTHED') as authed:
            data = ds._fetch_zendesk_image_bytes(
                'https://airportlf.zendesk.com/attachments/token/a/?name=x.png')
            authed.assert_called_once()
        self.assertEqual(data, b'AUTHED')

    def test_relative_url_resolved_to_our_host(self):
        with patch.object(ds, '_fetch_no_auth', return_value=b'IMG') as nf:
            ds._fetch_zendesk_image_bytes('/attachments/token/a/?name=x.png')
            called_url = nf.call_args.args[0]
        self.assertTrue(called_url.startswith('https://airportlf.zendesk.com/attachments/'))

    def test_no_auth_fetch_sends_browser_user_agent(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['ua'] = req.get_header('User-agent')

            class _R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s, n): return b'IMGBYTES'
            return _R()

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ds._fetch_no_auth('https://airportlf.zendesk.com/attachments/token/a/?name=x.png', 1000)
        self.assertTrue(captured['ua'])
        self.assertNotIn('urllib', captured['ua'].lower())   # the blocked default UA

    def test_cdn_host_fetched_without_auth(self):
        with patch.object(ds, '_fetch_no_auth', return_value=b'CDN') as nf, \
             patch('apps.integrations.services.fetch_zendesk_attachment_bytes') as authed:
            data = ds._fetch_zendesk_image_bytes('https://x123.zdusercontent.com/abc/image.png')
            nf.assert_called_once()
            authed.assert_not_called()      # our token must NOT go to the CDN
        self.assertEqual(data, b'CDN')

    def test_foreign_host_refused_without_any_fetch(self):
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes') as authed, \
             patch.object(ds, '_fetch_no_auth') as nf:
            self.assertIsNone(ds._fetch_zendesk_image_bytes('https://evil.com/x.png'))
            authed.assert_not_called()
            nf.assert_not_called()

    def test_other_zendesk_subdomain_refused(self):
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes') as authed:
            self.assertIsNone(ds._fetch_zendesk_image_bytes('https://other.zendesk.com/x.png'))
            authed.assert_not_called()


class DownscaleTests(TestCase):
    def test_small_png_passes_through_lossless(self):
        raw = _png_bytes((200, 200))
        out, mime = ds._downscale_for_embed(raw)
        self.assertEqual(out, raw)            # untouched — crisp screenshot text
        self.assertEqual(mime, 'image/png')

    def test_tiny_image_is_skipped(self):
        # tracking pixel / icon → dropped, not embedded
        out, mime = ds._downscale_for_embed(_png_bytes((12, 12)))
        self.assertEqual(out, b'')
        self.assertIsNone(ds._embed_image_data_uri(_png_bytes((12, 12))))

    def test_oversized_image_is_resized_and_embedded(self):
        raw = _png_bytes((4000, 3000), color='blue')
        out, mime = ds._downscale_for_embed(raw)
        res = Image.open(io.BytesIO(out))
        self.assertLessEqual(max(res.size), ds._IMG_EMBED_MAX_DIM)  # fits the bound
        self.assertIn(mime, ('image/jpeg', 'image/png'))

    def test_transparency_preserved_as_png(self):
        # large enough to force re-encode, with alpha
        raw = _png_bytes((2000, 2000), color=(0, 0, 0, 0), mode='RGBA')
        out, mime = ds._downscale_for_embed(raw)
        self.assertEqual(mime, 'image/png')

    def test_non_image_bytes_fall_back(self):
        out, mime = ds._downscale_for_embed(b'not an image at all')
        self.assertEqual(out, b'not an image at all')
        self.assertEqual(mime, 'image/jpeg')

    def test_embed_data_uri_roundtrip_and_none(self):
        uri = ds._embed_image_data_uri(_png_bytes())
        self.assertTrue(uri.startswith('data:image/'))
        self.assertIsNone(ds._embed_image_data_uri(None))
        self.assertIsNone(ds._embed_image_data_uri(b''))


class InlineUrlExtractionTests(TestCase):
    def test_extracts_from_html_body_and_markdown_deduped(self):
        c = {
            'body': 'DELTA\n![](https://airportlf.zendesk.com/attachments/token/a/?name=x.png)',
            'html_body': ('<p>DELTA</p>'
                          '<img src="https://airportlf.zendesk.com/attachments/token/b/?name=y.png">'
                          '<img src="https://airportlf.zendesk.com/attachments/token/a/?name=x.png">'),
        }
        urls = ds._comment_inline_image_urls(c)
        self.assertEqual(len(urls), 2)  # markdown 'a' + html 'b'; html 'a' is a dupe
        self.assertTrue(any('token/a' in u for u in urls))
        self.assertTrue(any('token/b' in u for u in urls))

    def test_empty_when_no_images(self):
        self.assertEqual(ds._comment_inline_image_urls({'body': 'hi', 'html_body': '<p>hi</p>'}), [])


class PanelHtmlImageEmbedTests(TestCase):
    """The exact regression: a screenshot pasted in the agent editor lives only
    in html_body as <img>; the plain body keeps just the label ("DELTA"). The
    image must still be embedded, the label preserved."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'airportlf'
        ss.save()

    def test_html_body_pasted_image_is_embedded(self):
        comments = [{
            'author': {'name': 'Agent'}, 'public': False, 'attachments': [],
            'body': 'DELTA',  # plain body has ONLY the label, no markdown image
            'html_body': ('<p>DELTA</p><img src="https://airportlf.zendesk.com/'
                          'attachments/token/x/?name=delta.png">'),
        }]
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes',
                   return_value=_png_bytes((400, 300))):
            panels = ds._zendesk_comment_panels(comments)
        self.assertEqual(len(panels[0]['images']), 1)
        self.assertTrue(panels[0]['images'][0]['data_uri'].startswith('data:image/'))
        self.assertEqual(panels[0]['body'], 'DELTA')


class AttachmentEmbedTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'airportlf'
        ss.save()

    def test_non_image_attachment_never_fetches(self):
        with patch.object(ds, '_fetch_zendesk_image_bytes') as fetch:
            self.assertIsNone(ds._attachment_data_uri('application/pdf', 'https://airportlf.zendesk.com/x'))
            fetch.assert_not_called()

    def test_image_attachment_is_embedded(self):
        with patch.object(ds, '_fetch_zendesk_image_bytes', return_value=_png_bytes()):
            uri = ds._attachment_data_uri('image/png', 'https://airportlf.zendesk.com/attachments/x')
        self.assertTrue(uri.startswith('data:image/'))
