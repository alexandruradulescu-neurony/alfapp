"""Tests for apps.core.utils.get_client_ip."""
from django.test import RequestFactory, TestCase, override_settings

from apps.core.utils import get_client_ip


class GetClientIpTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def _req(self, *, remote="10.0.0.1", xff=None):
        extra = {"REMOTE_ADDR": remote}
        if xff is not None:
            extra["HTTP_X_FORWARDED_FOR"] = xff
        return self.rf.post("/login/", **extra)

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=1)
    def test_uses_remote_addr_when_no_forwarded_header(self):
        self.assertEqual(get_client_ip(self._req(remote="203.0.113.7")), "203.0.113.7")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=1)
    def test_single_proxy_returns_real_client_not_spoofed_left_entry(self):
        # Client spoofs a left-most value; the trusted proxy appends the real IP.
        req = self._req(remote="10.0.0.1", xff="1.1.1.1, 203.0.113.7")
        self.assertEqual(get_client_ip(req), "203.0.113.7")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=1)
    def test_single_hop_forwarded(self):
        req = self._req(remote="10.0.0.1", xff="203.0.113.7")
        self.assertEqual(get_client_ip(req), "203.0.113.7")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=2)
    def test_two_trusted_proxies(self):
        # client, proxyA, proxyB  -> with 2 trusted proxies the client is [-2]
        req = self._req(xff="203.0.113.7, 70.0.0.1, 70.0.0.2")
        self.assertEqual(get_client_ip(req), "70.0.0.1")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=3)
    def test_depth_longer_than_chain_falls_back_to_leftmost(self):
        req = self._req(xff="203.0.113.7, 70.0.0.1")
        self.assertEqual(get_client_ip(req), "203.0.113.7")

    @override_settings(USE_X_FORWARDED_FOR=False)
    def test_header_ignored_when_disabled(self):
        req = self._req(remote="10.0.0.1", xff="203.0.113.7")
        self.assertEqual(get_client_ip(req), "10.0.0.1")

    @override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=1)
    def test_empty_forwarded_header(self):
        req = self._req(remote="10.0.0.1", xff="")
        self.assertEqual(get_client_ip(req), "10.0.0.1")
