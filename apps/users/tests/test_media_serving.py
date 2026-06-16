"""Tests for production media serving.

In production (DEBUG=False) user-uploaded media (claim evidence images) is
served through a login-protected route, since the files are sensitive. These
tests only apply when DEBUG=False, because the route is wired up at import time
based on DEBUG (see lora_app/urls.py). They run in CI (DEBUG=False) and skip in
a local dev shell where .env sets DEBUG=True.
"""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

User = get_user_model()

pytestmark = pytest.mark.skipif(
    settings.DEBUG,
    reason="Production media-protection route is only active when DEBUG=False",
)


@pytest.mark.django_db
def test_media_anonymous_is_redirected_to_login(client):
    """An unauthenticated request for a media file is redirected to login,
    never served."""
    # secure=True so the production SECURE_SSL_REDIRECT doesn't 301 us first.
    resp = client.get('/media/evidence/whatever.jpg', secure=True)
    assert resp.status_code == 302  # login_required redirect


@pytest.mark.django_db
def test_media_authenticated_missing_file_returns_404(client):
    """An authenticated user passes the login gate; a non-existent file then
    returns 404 (proving the request reached the serve view, not a redirect)."""
    User.objects.create_user(username='mediamgr', password='pw-test-12345')
    client.login(username='mediamgr', password='pw-test-12345')
    resp = client.get('/media/evidence/does-not-exist.jpg', secure=True)
    assert resp.status_code == 404
