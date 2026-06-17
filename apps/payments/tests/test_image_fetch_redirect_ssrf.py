"""RED-phase tests: SSRF via redirect in the evidence-PDF image fetch.

`_fetch_zendesk_image_bytes` (document_service.py:646) host-allowlists the
INITIAL url — only our ``<sub>.zendesk.com`` or Zendesk's signed CDN
(``*.zdusercontent.com`` / ``*.zendeskusercontent.com``); anything else returns
None. The GET is performed by ``_fetch_no_auth`` (line 622) via
``urllib.request.urlopen``, which follows HTTP redirects automatically with NO
per-hop host check. That is an SSRF hole: a url that STARTS at an allowlisted
host but 302-redirects to an internal host (``http://169.254.169.254/...``,
``http://10.0.0.1/``, ``http://127.0.0.1/``, ``http://localhost/``) or a foreign
host (``http://evil.example.com/``) is followed and fetched.

THE CONTRACT BEING SPECIFIED (test-first): the fix must PREVENT the app from
ever issuing the GET to an off-allowlist redirect target — it must REFUSE TO
FOLLOW the redirect (block before connecting), not merely discard bytes after
connecting. The intended mechanism is a custom urllib redirect handler,
``apps.payments.document_service._AllowlistRedirectHandler``, subclassing
``urllib.request.HTTPRedirectHandler`` and overriding ``redirect_request`` to
return ``None`` (refuse) when the new url's host is off-allowlist, and to return
a ``urllib.request.Request`` (delegate to super) when it IS allowlisted.

That class does not exist yet, so the CORE tests fail with AttributeError
(feature-missing RED). A DEFENSE-IN-DEPTH test additionally pins the behavioral
contract — bytes from an off-allowlist FINAL landing host must be refused — and
a HAPPY-PATH group proves the legitimate ``<sub>.zendesk.com`` ->
``*.zdusercontent.com`` redirect is NOT over-blocked. No real network.
"""

import email.message
from unittest.mock import patch

from django.test import TestCase

from apps.config.models import SystemSettings
from apps.payments import document_service as ds


_SUB = 'airportlf'


def _set_subdomain():
    ss = SystemSettings.get_instance()
    ss.zd_subdomain = _SUB
    ss.save()


class _FakeResp:
    """Mimics the http.client.HTTPResponse that a urllib opener returns AFTER
    transparently following any redirects: it carries the FINAL (post-redirect)
    url — openers set ``.url`` and expose ``.geturl()`` — and yields the body on
    ``read(n)``. Usable as a context manager (``with opener.open(...) as r``)."""

    def __init__(self, final_url, body=b'REDIRECTED-BODY'):
        self.url = final_url            # opener sets this to the final url
        self._final_url = final_url
        self._body = body

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _open_landing_at(final_url, body=b'REDIRECTED-BODY'):
    """side_effect for ``urllib.request.OpenerDirector.open`` that emulates the
    opener having transparently followed a redirect and landed at ``final_url``.
    Patching ``OpenerDirector.open`` covers BOTH ``urllib.request.urlopen`` (the
    current code) AND a custom ``build_opener(...).open`` (the likely fix)."""

    def _fake(self_or_url, *args, **kwargs):   # bound-method signature: (self, fullurl, ...)
        return _FakeResp(final_url, body)

    return _fake


# ---------------------------------------------------------------------------
# 1. CORE — redirect REFUSAL at the handler seam (the real fix surface).
# ---------------------------------------------------------------------------

class AllowlistRedirectHandlerTests(TestCase):
    """The fix adds ``_AllowlistRedirectHandler.redirect_request`` which returns
    None to REFUSE following an off-allowlist redirect (blocking before any
    connection to the redirect target), and returns a Request to ALLOW an
    allowlisted one. This is the primary, connection-level contract."""

    def setUp(self):
        _set_subdomain()

    def _handler(self):
        return ds._AllowlistRedirectHandler()

    def _call(self, handler, newurl):
        import urllib.request
        req = urllib.request.Request(f'https://{_SUB}.zendesk.com/start')
        headers = email.message.Message()
        return handler.redirect_request(req, None, 302, 'Found', headers, newurl)

    # ---- off-allowlist redirect targets must be REFUSED (return None) ----

    def test_refuses_redirect_to_cloud_metadata(self):
        self.assertIsNone(
            self._call(self._handler(), 'http://169.254.169.254/latest/meta-data/'))

    def test_refuses_redirect_to_private_10_net(self):
        self.assertIsNone(self._call(self._handler(), 'http://10.0.0.1/x'))

    def test_refuses_redirect_to_loopback_ip(self):
        self.assertIsNone(self._call(self._handler(), 'http://127.0.0.1/x'))

    def test_refuses_redirect_to_localhost(self):
        self.assertIsNone(self._call(self._handler(), 'http://localhost/x'))

    def test_refuses_redirect_to_foreign_host(self):
        self.assertIsNone(self._call(self._handler(), 'http://evil.example.com/x'))

    # ---- allowlisted redirect targets must be ALLOWED (return a Request) ----

    def test_allows_redirect_to_signed_cdn(self):
        import urllib.request
        out = self._call(self._handler(), 'https://abc.zdusercontent.com/signed')
        self.assertIsInstance(out, urllib.request.Request)

    def test_allows_redirect_to_our_zendesk_host(self):
        import urllib.request
        out = self._call(self._handler(), f'https://{_SUB}.zendesk.com/other')
        self.assertIsInstance(out, urllib.request.Request)


# ---------------------------------------------------------------------------
# 2. DEFENSE-IN-DEPTH — the post-hoc behavioral contract.
# ---------------------------------------------------------------------------

class FetchNoAuthFinalHostTests(TestCase):
    """Even if a redirect somehow slipped through, ``_fetch_no_auth`` must not
    RETURN bytes whose FINAL landing host is off-allowlist. Network is mocked at
    ``OpenerDirector.open`` so this holds whether the code uses ``urlopen`` or a
    custom opener."""

    def setUp(self):
        _set_subdomain()

    def test_returns_none_when_final_host_off_allowlist(self):
        import urllib.request
        with patch.object(urllib.request.OpenerDirector, 'open',
                          new=_open_landing_at('http://169.254.169.254/latest/meta-data/')):
            data = ds._fetch_no_auth(
                f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png', 1000)
        self.assertIsNone(data)

    def test_returns_none_when_final_host_is_foreign(self):
        import urllib.request
        with patch.object(urllib.request.OpenerDirector, 'open',
                          new=_open_landing_at('http://evil.example.com/x.png')):
            data = ds._fetch_no_auth(
                f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png', 1000)
        self.assertIsNone(data)

    def test_returns_bytes_when_final_host_allowlisted(self):
        import urllib.request
        with patch.object(urllib.request.OpenerDirector, 'open',
                          new=_open_landing_at(
                              f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png',
                              body=b'IMGBYTES')):
            data = ds._fetch_no_auth(
                f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png', 1000)
        self.assertEqual(data, b'IMGBYTES')


# ---------------------------------------------------------------------------
# 3. HAPPY PATH — the legitimate Zendesk -> signed-CDN redirect must survive.
# ---------------------------------------------------------------------------

class LegitimateRedirectHappyPathTests(TestCase):
    """The fix must NOT over-block the real Zendesk attachment flow: a token url
    on ``<sub>.zendesk.com`` 302-redirects to the signed content CDN
    (``*.zdusercontent.com``). The handler must ALLOW that redirect, and a
    normal no-redirect allowlisted fetch must still return its bytes."""

    def setUp(self):
        _set_subdomain()

    def test_handler_allows_zendesk_to_cdn_redirect(self):
        import urllib.request
        req = urllib.request.Request(
            f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png')
        headers = email.message.Message()
        out = ds._AllowlistRedirectHandler().redirect_request(
            req, None, 302, 'Found', headers,
            'https://x123.zdusercontent.com/abc/signed/image.png')
        self.assertIsInstance(out, urllib.request.Request)

    def test_no_redirect_allowlisted_fetch_returns_bytes(self):
        import urllib.request
        with patch.object(urllib.request.OpenerDirector, 'open',
                          new=_open_landing_at(
                              f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png',
                              body=b'IMGBYTES')):
            data = ds._fetch_no_auth(
                f'https://{_SUB}.zendesk.com/attachments/token/a/?name=x.png', 1000)
        self.assertEqual(data, b'IMGBYTES')
