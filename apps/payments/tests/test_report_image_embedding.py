"""Report image embedding (regression: inline Delta/Denver screenshots vanished,
leaving only the label text). Covers the host/auth fetch policy and the
downscale-instead-of-drop behaviour that replaced the silent 5 MB cap."""

import io
from unittest.mock import patch

from django.test import TestCase
from PIL import Image

from apps.config.models import SystemSettings
from apps.payments import document_service as ds


def _png_bytes(size=(8, 8), color='white', mode='RGB'):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format='PNG')
    return buf.getvalue()


class FetchPolicyTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'airportlf'
        ss.save()

    def test_our_host_uses_authed_fetch(self):
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes',
                   return_value=b'IMG') as f:
            data = ds._fetch_zendesk_image_bytes(
                'https://airportlf.zendesk.com/attachments/token/a/?name=x.png')
            f.assert_called_once()
        self.assertEqual(data, b'IMG')

    def test_relative_url_resolved_to_our_host(self):
        with patch('apps.integrations.services.fetch_zendesk_attachment_bytes',
                   return_value=b'IMG') as f:
            ds._fetch_zendesk_image_bytes('/attachments/token/a/?name=x.png')
            called_url = f.call_args.args[0]
        self.assertTrue(called_url.startswith('https://airportlf.zendesk.com/attachments/'))

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
        raw = _png_bytes((20, 20))
        out, mime = ds._downscale_for_embed(raw)
        self.assertEqual(out, raw)            # untouched — crisp screenshot text
        self.assertEqual(mime, 'image/png')

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
