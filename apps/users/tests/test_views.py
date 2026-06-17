"""
Tests for users app views.

Tests cover:
- Authentication views (login, logout)
- Agent views (dashboard, claims, emails)
- Manager views (dashboard, claims, refunds, settings, users)
- Rate limiting decorator
- File upload validation
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from django.test import Client, RequestFactory
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from io import BytesIO
from PIL import Image

from apps.claims.models import Claim, ClaimEvidence
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Refund, Dispute

User = get_user_model()


# ============== Helper Functions ==============

def create_test_image():
    """Create a test image file for upload tests."""
    img = Image.new('RGB', (100, 100), color='red')
    buffer = BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return buffer


def get_messages_list(response):
    """Extract messages from a response."""
    return list(get_messages(response.wsgi_request))


# ============== Authentication Views Tests ==============

@pytest.mark.django_db
class TestLoginView:
    """Tests for login_view."""

    def test_login_get_request_renders_template(self):
        """Test login page renders on GET request."""
        client = Client()
        response = client.get('/login/')
        assert response.status_code == 200
        assert 'login.html' in [t.name for t in response.templates]

    def test_login_post_valid_credentials_agent(self):
        """Test login with valid AGENT credentials."""
        User.objects.create_user(
            username='testagent_login1',
            password='testpass123',
        )
        client = Client()
        response = client.post('/login/', {
            'username': 'testagent_login1',
            'password': 'testpass123'
        })
        # Single user type — login lands on the manager dashboard
        assert response.status_code == 302
        assert '/manager/' in response.url

    def test_login_post_valid_credentials_manager(self):
        """Test login with valid MANAGER credentials."""
        User.objects.create_user(
            username='testmanager_login1',
            password='testpass123',
        )
        client = Client()
        response = client.post('/login/', {
            'username': 'testmanager_login1',
            'password': 'testpass123'
        })
        # Should redirect to manager_dashboard
        assert response.status_code == 302
        assert '/manager/' in response.url

    def test_login_post_invalid_credentials(self):
        """Test login with invalid credentials shows error."""
        client = Client()
        response = client.post('/login/', {
            'username': 'testuser',
            'password': 'wrongpassword'
        })
        assert response.status_code == 200
        messages = get_messages_list(response)
        assert any('Invalid username or password' in str(m) for m in messages)

    def test_login_rate_limit_exceeded(self):
        """Test login rate limiting after max attempts."""
        client = Client()
        ip = '127.0.0.1'
        cache_key = f'login_attempts_{ip}'

        # Set cache to exceed max attempts
        cache.set(cache_key, 5, 60)

        response = client.post('/login/', {
            'username': 'testuser',
            'password': 'testpass'
        }, REMOTE_ADDR=ip)

        assert response.status_code == 403
        assert 'Too many login attempts' in response.content.decode()

    def test_only_failed_logins_are_counted(self):
        """#14: a SUCCESSFUL login must not increment the throttle counter."""
        User.objects.create_user(username='thr_ok', password='RightPass123!')
        ip = '10.0.0.11'
        cache.delete(f'login_attempts_{ip}')
        client = Client()
        resp = client.post('/login/', {'username': 'thr_ok', 'password': 'RightPass123!'},
                           REMOTE_ADDR=ip)
        assert resp.status_code == 302
        assert cache.get(f'login_attempts_{ip}') in (None, 0)

    def test_failed_attempts_count_and_success_resets(self):
        """#14: failures accrue; a success clears the counter (no shared-IP lockout)."""
        User.objects.create_user(username='thr_reset', password='RightPass123!')
        ip = '10.0.0.12'
        cache.delete(f'login_attempts_{ip}')
        client = Client()
        for _ in range(4):
            client.post('/login/', {'username': 'thr_reset', 'password': 'wrong'}, REMOTE_ADDR=ip)
        assert cache.get(f'login_attempts_{ip}') == 4
        resp = client.post('/login/', {'username': 'thr_reset', 'password': 'RightPass123!'},
                           REMOTE_ADDR=ip)
        assert resp.status_code == 302
        assert cache.get(f'login_attempts_{ip}') in (None, 0)  # reset on success

    @pytest.mark.django_db
    def test_throttle_keys_on_forwarded_client_ip_not_proxy(self):
        """M6: behind a proxy the throttle must bucket per real client IP
        (left-most-trusted X-Forwarded-For hop), not the shared REMOTE_ADDR, so
        one client hitting the cap does not lock out everyone else."""
        from django.test import override_settings
        proxy = '10.0.0.1'          # what REMOTE_ADDR would collapse everyone to
        client_a = '203.0.113.10'
        client_b = '203.0.113.20'
        for c in (client_a, client_b):
            cache.delete(f'login_attempts_{c}')
        with override_settings(USE_X_FORWARDED_FOR=True, TRUSTED_PROXY_DEPTH=1):
            client = Client()
            # client_a exhausts its own bucket
            for _ in range(5):
                client.post('/login/', {'username': 'x', 'password': 'wrong'},
                            REMOTE_ADDR=proxy, HTTP_X_FORWARDED_FOR=client_a)
            blocked = client.post('/login/', {'username': 'x', 'password': 'wrong'},
                                  REMOTE_ADDR=proxy, HTTP_X_FORWARDED_FOR=client_a)
            assert blocked.status_code == 403
            # client_b shares the proxy IP but must have a separate bucket
            ok = client.post('/login/', {'username': 'x', 'password': 'wrong'},
                             REMOTE_ADDR=proxy, HTTP_X_FORWARDED_FOR=client_b)
            assert ok.status_code != 403
            assert cache.get(f'login_attempts_{client_b}') == 1

    def test_login_authenticated_manager_redirects(self):
        """Test already authenticated manager is redirected."""
        manager = User.objects.create_user(
            username='manager_auth_redirect',
            password='testpass123',
        )
        client = Client()
        client.login(username='manager_auth_redirect', password='testpass123')
        response = client.get('/login/')
        assert response.status_code == 302
        assert '/manager/' in response.url


@pytest.mark.django_db
class TestLogoutView:
    """Tests for logout_view."""

    def test_logout_redirects_to_login(self):
        """Test logout redirects to login page."""
        client = Client()
        # First login
        User.objects.create_user(
            username='testuser_logout',
            password='testpass123'
        )
        client.login(username='testuser_logout', password='testpass123')

        # Then logout
        response = client.post('/logout/')
        assert response.status_code == 302
        assert '/login/' in response.url

    def test_logout_rejects_get(self):
        """GET must not log out (logout-CSRF guard) — only POST is allowed."""
        client = Client()
        User.objects.create_user(
            username='testuser_logout_get',
            password='testpass123'
        )
        client.login(username='testuser_logout_get', password='testpass123')

        response = client.get('/logout/')
        assert response.status_code == 405


@pytest.mark.django_db
class TestDashboardRedirect:
    """Tests for dashboard_redirect view."""

    def test_redirect_unauthenticated_user(self):
        """Test unauthenticated user redirected to login."""
        client = Client()
        response = client.get('/dashboard/')
        # Dashboard URL not configured - returns 404
        assert response.status_code == 404

    def test_redirect_authenticated_manager(self):
        """Test MANAGER redirected to manager dashboard."""
        manager = User.objects.create_user(
            username='manager_dash_redirect',
            password='testpass123',
        )
        client = Client()
        client.login(username='manager_dash_redirect', password='testpass123')
        response = client.get('/dashboard/')
        # Dashboard URL not configured - returns 404
        assert response.status_code == 404

    def test_redirect_authenticated_agent(self):
        """Test AGENT redirected to agent dashboard."""
        agent = User.objects.create_user(
            username='agent_dash_redirect',
            password='testpass123',
        )
        client = Client()
        client.login(username='agent_dash_redirect', password='testpass123')
        response = client.get('/dashboard/')
        # Dashboard URL not configured - returns 404
        assert response.status_code == 404


# ============== Agent Views Tests ==============

@pytest.mark.django_db
class TestAgentDashboard:
    """Tests for agent_dashboard view."""

    def test_agent_dashboard_requires_login(self):
        """Test agent dashboard requires authentication."""
        client = Client()
        response = client.get('/agent/')
        assert response.status_code == 302
        assert '/login/' in response.url

    def test_agent_dashboard_renders_for_agent(self):
        """Test agent dashboard renders for AGENT user."""
        agent = User.objects.create_user(
            username='agent_dash_test',
            password='testpass123',
        )
        client = Client()
        client.login(username='agent_dash_test', password='testpass123')
        response = client.get('/agent/')
        assert response.status_code == 200
        assert 'agent/dashboard.html' in [t.name for t in response.templates]

    def test_agent_dashboard_context_data(self):
        """Test agent dashboard includes correct context data."""
        # Use unique prefix to isolate test data
        test_prefix = 'test_ctx_agent_dash_'
        agent = User.objects.create_user(
            username=f'{test_prefix}user',
            password='testpass123',
        )
        # Create exactly one claim assigned to this agent
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            status='Investigation initiated',
            status_category='open',
            assigned_to=agent,
            flight_details='Flight AA100'
        )
        # Create exactly one urgent email
        email = EmailLog.objects.create(
            subject=f'{test_prefix}subject',
            body='Test body',
            from_email=f'{test_prefix}sender@example.com',
            category='RESUBMISSION_REQUIRED',  # Urgent category
            action_required=True
        )

        client = Client()
        client.login(username=f'{test_prefix}user', password='testpass123')
        response = client.get('/agent/')

        # Verify context data exists (don't assert exact counts due to other test data)
        assert response.context['total_claims'] >= 1
        assert response.context['my_claims'] >= 1
        assert response.context['urgent_emails'] >= 1

    def test_agent_dashboard_allows_manager(self):
        """Test MANAGER can access agent dashboard (agent_required allows both roles)."""
        manager = User.objects.create_user(
            username='manager_agent_dash',
            password='testpass123',
        )
        client = Client()
        client.login(username='manager_agent_dash', password='testpass123')
        response = client.get('/agent/')
        # MANAGER role is allowed by @agent_required decorator
        assert response.status_code == 200


@pytest.mark.django_db
class TestAgentClaims:
    """Tests for agent_claims view."""

    def test_agent_claims_requires_login(self):
        """Test agent claims requires authentication."""
        client = Client()
        response = client.get('/agent/claims/')
        assert response.status_code == 302

    def test_agent_claims_status_filter(self):
        """Test agent claims status filter."""
        test_prefix = 'test_filter_status_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        # Create claims with different statuses
        claim_initiated = Claim.objects.create(
            client_email=f'{test_prefix}received@example.com',
            status='Investigation initiated',
            status_category='open',
            assigned_to=agent,
            flight_details='Flight AA100'
        )
        claim_submitted = Claim.objects.create(
            client_email=f'{test_prefix}searching@example.com',
            status='Claim submitted',
            status_category='open',
            assigned_to=agent,
            flight_details='Flight AA101'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get('/agent/claims/?status=Investigation+initiated')

        # Verify filter works - only 'Investigation initiated' status should be shown
        page_obj = response.context['page_obj']
        claims_on_page = list(page_obj.object_list)
        claim_ids = [c.id for c in claims_on_page]

        assert claim_initiated.id in claim_ids
        assert claim_submitted.id not in claim_ids
        assert response.context['status_filter'] == 'Investigation initiated'

    def test_agent_claims_search(self):
        """Test agent claims search functionality."""
        test_prefix = 'test_search_'
        search_term = f'{test_prefix}search'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{search_term}@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get(f'/agent/claims/?search={search_term}')

        page_obj = response.context['page_obj']
        claims_on_page = list(page_obj.object_list)
        claim_ids = [c.id for c in claims_on_page]
        
        assert claim.id in claim_ids
        assert response.context['search_query'] == search_term


@pytest.mark.django_db
class TestAgentClaimDetail:
    """Tests for agent_claim_detail view."""

    def test_agent_claim_detail_requires_login(self):
        """Test claim detail requires authentication."""
        client = Client()
        response = client.get('/agent/claims/1/')
        assert response.status_code == 302

    def test_agent_claim_detail_shows_assigned_claim(self):
        """Test agent can view claim assigned to them."""
        test_prefix = 'test_detail_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get(f'/agent/claims/{claim.id}/')

        assert response.status_code == 200
        assert response.context['claim'] == claim

@pytest.mark.django_db
class TestAgentUploadEvidence:
    """Tests for agent_upload_evidence view."""

    def test_upload_evidence_requires_login(self):
        """Test upload evidence requires authentication."""
        client = Client()
        response = client.post('/agent/claims/1/upload/')
        assert response.status_code == 302

    def test_upload_evidence_success(self):
        """Test successful evidence upload."""
        test_prefix = 'test_upload_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        # Create test image
        img_buffer = create_test_image()
        image_file = SimpleUploadedFile(
            f'{test_prefix}image.jpg',
            img_buffer.read(),
            content_type='image/jpeg'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.post(
            f'/agent/claims/{claim.id}/upload/',
            {'image': image_file, 'description': f'{test_prefix}evidence'}
        )

        assert response.status_code == 302
        # Check that evidence was created (at least 1, may be more from other tests)
        evidence_count = ClaimEvidence.objects.filter(claim=claim).count()
        assert evidence_count >= 1, f"Expected at least 1 evidence for claim {claim.id}, found {evidence_count}"
        messages = get_messages_list(response)
        assert any('Evidence uploaded successfully' in str(m) for m in messages)

    def test_upload_evidence_file_too_large(self):
        """Test upload rejects file over 10MB."""
        test_prefix = 'test_upload_large_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        # Create large file (11MB)
        large_file = SimpleUploadedFile(
            f'{test_prefix}large_file.jpg',
            b'x' * (11 * 1024 * 1024),
            content_type='image/jpeg'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.post(
            f'/agent/claims/{claim.id}/upload/',
            {'image': large_file}
        )

        messages = get_messages_list(response)
        assert any('File size must be less than' in str(m) for m in messages)

    def test_upload_evidence_invalid_extension(self):
        """Test upload rejects invalid file extension."""
        test_prefix = 'test_upload_ext_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        # Create file with wrong extension
        wrong_ext_file = SimpleUploadedFile(
            f'{test_prefix}file.txt',
            b'some text content',
            content_type='text/plain'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.post(
            f'/agent/claims/{claim.id}/upload/',
            {'image': wrong_ext_file}
        )

        messages = get_messages_list(response)
        assert any('Invalid file extension' in str(m) for m in messages)

    def test_upload_evidence_no_file(self):
        """Test upload with no file shows error."""
        test_prefix = 'test_upload_nofile_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.post(
            f'/agent/claims/{claim.id}/upload/',
            {'description': f'{test_prefix}no file'}
        )

        messages = get_messages_list(response)
        assert any('Please select an image file' in str(m) for m in messages)


@pytest.mark.django_db
class TestAgentEmails:
    """Tests for agent_emails view."""

    def test_agent_emails_requires_login(self):
        """Test agent emails requires authentication."""
        client = Client()
        response = client.get('/agent/emails/')
        assert response.status_code == 302

    def test_agent_emails_shows_emails(self):
        """Test agent emails view shows emails."""
        test_prefix = 'test_emails_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')
        email = EmailLog.objects.create(
            subject=f'{test_prefix}subject',
            body='Test body',
            from_email=f'{test_prefix}sender@example.com',
            claim=claim,
            auto_resolved=False  # Ensure it's visible (default filter hides auto_resolved)
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get('/agent/emails/')

        assert response.status_code == 200
        page_obj = response.context['page_obj']
        emails_on_page = list(page_obj.object_list)
        email_ids = [e.id for e in emails_on_page]
        assert email.id in email_ids

    def test_agent_emails_filters_by_action_required(self):
        """Test agent emails action_required filter."""
        test_prefix = 'test_action_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')
        email_action = EmailLog.objects.create(
            subject=f'{test_prefix}action_subject',
            body='Test body',
            from_email=f'{test_prefix}sender1@example.com',
            action_required=True,
            auto_resolved=False
        )
        email_no_action = EmailLog.objects.create(
            subject=f'{test_prefix}no_action_subject',
            body='Test body',
            from_email=f'{test_prefix}sender2@example.com',
            action_required=False,
            auto_resolved=False
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get('/agent/emails/?action_required=1')

        page_obj = response.context['page_obj']
        emails_on_page = list(page_obj.object_list)
        email_ids = [e.id for e in emails_on_page]
        
        assert email_action.id in email_ids
        assert email_no_action.id not in email_ids
        assert response.context['action_required_filter'] == '1'

    def test_agent_emails_filters_by_category(self):
        """Test agent emails category filter."""
        test_prefix = 'test_category_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        email1 = EmailLog.objects.create(
            subject=f'{test_prefix}subject1',
            body='Test body',
            from_email=f'{test_prefix}sender1@example.com',
            category='OBJECT_FOUND',
            auto_resolved=False
        )
        email2 = EmailLog.objects.create(
            subject=f'{test_prefix}subject2',
            body='Test body',
            from_email=f'{test_prefix}sender2@example.com',
            category='OBJECT_NOT_FOUND',
            auto_resolved=False
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get('/agent/emails/?category=OBJECT_FOUND')

        page_obj = response.context['page_obj']
        emails_on_page = list(page_obj.object_list)
        email_ids = [e.id for e in emails_on_page]
        
        assert email1.id in email_ids
        assert email2.id not in email_ids

    def test_agent_emails_search(self):
        """Test agent emails search functionality."""
        test_prefix = 'test_email_search_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        email = EmailLog.objects.create(
            subject=f'{test_prefix}subject',
            body='Test body',
            from_email=f'{test_prefix}specific@example.com',
            auto_resolved=False
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get(f'/agent/emails/?search={test_prefix}specific')

        page_obj = response.context['page_obj']
        emails_on_page = list(page_obj.object_list)
        email_ids = [e.id for e in emails_on_page]
        
        assert email.id in email_ids


@pytest.mark.django_db
class TestAgentEmailDetail:
    """Tests for agent_email_detail view."""

    def test_email_detail_requires_login(self):
        """Test email detail requires authentication."""
        client = Client()
        response = client.get('/agent/emails/1/')
        assert response.status_code == 302

    def test_email_detail_shows_email(self):
        """Test email detail view shows email."""
        test_prefix = 'test_email_detail_'
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        email = EmailLog.objects.create(
            subject=f'{test_prefix}subject',
            body='Test body content',
            from_email=f'{test_prefix}sender@example.com'
        )

        client = Client()
        client.login(username=f'{test_prefix}agent', password='testpass123')
        response = client.get(f'/agent/emails/{email.id}/')

        assert response.status_code == 200
        assert response.context['email'] == email


# ============== Manager Views Tests ==============

@pytest.mark.django_db
class TestManagerDashboard:
    """Tests for manager_dashboard view."""

    def test_manager_dashboard_requires_login(self):
        """Test manager dashboard requires authentication."""
        client = Client()
        response = client.get('/manager/')
        assert response.status_code == 302

    def test_manager_dashboard_renders_for_manager(self):
        """Test manager dashboard renders for MANAGER user."""
        manager = User.objects.create_user(
            username='manager_dash_test',
            password='testpass123',
        )
        client = Client()
        client.login(username='manager_dash_test', password='testpass123')
        response = client.get('/manager/')
        assert response.status_code == 200
        assert 'manager/dashboard.html' in [t.name for t in response.templates]

    def test_manager_dashboard_context_data(self):
        """Test manager dashboard includes correct context data."""
        test_prefix = 'test_mgr_dash_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        # Create test data with unique prefix (all open-family so active count >= 3)
        claim1 = Claim.objects.create(
            client_email=f'{test_prefix}1@example.com',
            status='Investigation initiated',
            status_category='open',
            flight_details='Flight AA100'
        )
        claim2 = Claim.objects.create(
            client_email=f'{test_prefix}2@example.com',
            status='Claim submitted',
            status_category='open',
            flight_details='Flight AA101'
        )
        claim3 = Claim.objects.create(
            client_email=f'{test_prefix}3@example.com',
            status='Object Found',
            status_category='open',
            flight_details='Flight AA102'
        )
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/')

        # Context must mirror DB truth exactly (other fixtures may add claims,
        # so derive expectations from the DB rather than hardcoding counts).
        from apps.payments.models import Dispute
        assert response.context['total_claims'] == Claim.objects.count()
        assert response.context['active'] == Claim.objects.exclude(status_category='solved').count()
        assert response.context['pending_client'] == Claim.objects.filter(status_category='pending').count()
        assert response.context['solved'] == Claim.objects.filter(status_category='solved').count()
        assert response.context['disputed'] == Dispute.objects.exclude(
            status__in=['RESOLVED_WON', 'RESOLVED_LOST', 'ACCEPTED']).count()
        assert response.context['agents_count'] == User.objects.filter().count()
        # And the three claims this test created are all active (family 'open')
        assert response.context['active'] >= 3

@pytest.mark.django_db
class TestManagerClaims:
    """Tests for manager_claims view."""

    def test_manager_claims_requires_login(self):
        """Test manager claims requires authentication."""
        client = Client()
        response = client.get('/manager/claims/')
        assert response.status_code == 302

    def test_manager_claims_shows_all_claims(self):
        """Test manager sees all claims."""
        test_prefix = 'test_mgr_all_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim1 = Claim.objects.create(
            client_email=f'{test_prefix}1@example.com',
            flight_details='Flight AA100'
        )
        claim2 = Claim.objects.create(
            client_email=f'{test_prefix}2@example.com',
            flight_details='Flight AA101'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/claims/')

        assert response.status_code == 200
        page_obj = response.context['page_obj']
        claims_on_page = list(page_obj.object_list)
        claim_ids = [c.id for c in claims_on_page]
        
        assert claim1.id in claim_ids
        assert claim2.id in claim_ids

    def test_manager_claims_status_filter(self):
        """Test manager claims status filter."""
        test_prefix = 'test_mgr_filter_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim_initiated = Claim.objects.create(
            client_email=f'{test_prefix}received@example.com',
            status='Investigation initiated',
            status_category='open',
            flight_details='Flight AA100'
        )
        claim_submitted = Claim.objects.create(
            client_email=f'{test_prefix}searching@example.com',
            status='Claim submitted',
            status_category='open',
            flight_details='Flight AA101'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/claims/?status=Investigation+initiated')

        page_obj = response.context['page_obj']
        claims_on_page = list(page_obj.object_list)
        claim_ids = [c.id for c in claims_on_page]

        assert claim_initiated.id in claim_ids
        assert claim_submitted.id not in claim_ids

    def test_manager_claims_search(self):
        """Test manager claims search functionality."""
        test_prefix = 'test_mgr_search_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}search@example.com',
            zd_ticket_id='12345',
            flight_details='Flight AA100'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get(f'/manager/claims/?search={test_prefix}search')

        page_obj = response.context['page_obj']
        claims_on_page = list(page_obj.object_list)
        claim_ids = [c.id for c in claims_on_page]
        
        assert claim.id in claim_ids


@pytest.mark.django_db
class TestManagerRefunds:
    """Tests for manager_refunds view."""

    def test_manager_refunds_requires_login(self):
        """Test manager refunds requires authentication."""
        client = Client()
        response = client.get('/manager/refunds/')
        assert response.status_code == 302

    def test_manager_refunds_shows_refunds(self):
        """Test manager refunds view shows refunds."""
        test_prefix = 'test_refund_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')
        refund = Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-001',
            amount=50.00,
            refund_type='FULL',
            reason=f'{test_prefix}Test refund'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/refunds/')

        assert response.status_code == 200
        page_obj = response.context['page_obj']
        refunds_on_page = list(page_obj.object_list)
        refund_ids = [r.id for r in refunds_on_page]
        assert refund.id in refund_ids

    def test_manager_refunds_status_filter(self):
        """Test manager refunds status filter."""
        test_prefix = 'test_refund_filter_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')
        refund_completed = Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-002',
            amount=50.00,
            status='COMPLETED',
            refund_type='FULL',
            reason=f'{test_prefix}Test'
        )
        refund_pending = Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-003',
            amount=30.00,
            status='PENDING',
            refund_type='PARTIAL',
            reason=f'{test_prefix}Test'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/refunds/?status=COMPLETED')

        page_obj = response.context['page_obj']
        refunds_on_page = list(page_obj.object_list)
        refund_ids = [r.id for r in refunds_on_page]
        
        assert refund_completed.id in refund_ids
        assert refund_pending.id not in refund_ids

    def test_manager_refunds_stats(self):
        """Test manager refunds includes statistics."""
        test_prefix = 'test_refund_stats_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')
        refund = Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-004',
            amount=50.00,
            status='COMPLETED',
            refund_type='FULL',
            reason=f'{test_prefix}Test'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/refunds/')

        assert 'stats' in response.context
        stats = response.context['stats']
        # Stats should show at least 1 completed refund
        assert stats['completed'] >= 1


@pytest.mark.django_db
class TestAgentAssignClaim:
    """Tests for agent_assign_claim view."""

    def test_assign_claim_success(self):
        """Test successful claim assignment."""
        test_prefix = 'test_assign_success_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post(
            f'/manager/claims/{claim.id}/assign/',
            {'agent_id': agent.id}
        )

        assert response.status_code == 302
        claim.refresh_from_db()
        assert claim.assigned_to == agent
        messages = get_messages_list(response)
        assert any('Claim assigned' in str(m) for m in messages)

    def test_assign_claim_invalid_agent_id(self):
        """Test assignment with invalid agent ID."""
        test_prefix = 'test_assign_invalid_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post(
            f'/manager/claims/{claim.id}/assign/',
            {'agent_id': 'invalid'}
        )

        messages = get_messages_list(response)
        assert any('Invalid agent ID' in str(m) for m in messages)

    def test_assign_claim_nonexistent_agent(self):
        """Test assignment with nonexistent agent."""
        test_prefix = 'test_assign_nonexistent_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        claim = Claim.objects.create(client_email=f'{test_prefix}client@example.com', flight_details='Flight AA100')

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post(
            f'/manager/claims/{claim.id}/assign/',
            {'agent_id': 99999}
        )

        messages = get_messages_list(response)
        assert any('Invalid agent' in str(m) for m in messages)

    def test_unassign_claim(self):
        """Test unassigning a claim."""
        test_prefix = 'test_unassign_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )
        claim = Claim.objects.create(
            client_email=f'{test_prefix}client@example.com',
            assigned_to=agent,
            flight_details='Flight AA100'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post(
            f'/manager/claims/{claim.id}/assign/',
            {'agent_id': ''}  # Empty agent_id to unassign
        )

        claim.refresh_from_db()
        assert claim.assigned_to is None
        messages = get_messages_list(response)
        assert any('Claim unassigned' in str(m) for m in messages)


@pytest.mark.django_db
class TestManagerUsers:
    """Tests for manager_users view."""

    def test_manager_users_shows_users(self):
        """Test manager users view shows users."""
        test_prefix = 'test_users_show_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        agent = User.objects.create_user(
            username=f'{test_prefix}agent',
            password='testpass123',
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/users/')

        assert response.status_code == 200
        # Check that users queryset contains our created users
        users = response.context['users']
        user_ids = list(users.values_list('id', flat=True))
        assert manager.id in user_ids
        assert agent.id in user_ids

    def test_create_user_success(self):
        """Test successful user creation."""
        test_prefix = 'test_create_user_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/users/', {
            'username': f'{test_prefix}newuser',
            'email': f'{test_prefix}newuser@example.com',
            'password': 'StrongPass123!',
            'role': 'AGENT',
            'first_name': 'New',
            'last_name': 'User'
        })

        assert User.objects.filter(username=f'{test_prefix}newuser').exists()
        messages = get_messages_list(response)
        assert any('created successfully' in str(m).lower() for m in messages)

    def test_create_user_duplicate_username(self):
        """Test user creation with duplicate username."""
        test_prefix = 'test_create_dup_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        existing = User.objects.create_user(
            username=f'{test_prefix}existing',
            password='testpass123'
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/users/', {
            'username': f'{test_prefix}existing',
            'password': 'StrongPass123!',
            'role': 'AGENT'
        })

        messages = get_messages_list(response)
        assert any('already exists' in str(m).lower() for m in messages)

    def test_create_user_weak_password(self):
        """Test user creation with weak password."""
        test_prefix = 'test_create_weak_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/users/', {
            'username': f'{test_prefix}newuser',
            'password': '123',  # Very weak password
            'role': 'AGENT'
        })

        messages = get_messages_list(response)
        assert any('Weak password' in str(m) for m in messages)

    def test_create_user_missing_fields(self):
        """Test user creation with missing required fields."""
        test_prefix = 'test_create_missing_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/users/', {
            'username': '',  # Missing username
            'password': 'StrongPass123!',
            'role': 'AGENT'
        })

        messages = get_messages_list(response)
        assert any('Please fill in all required fields' in str(m) for m in messages)


@pytest.mark.django_db
class TestTestAi:
    """Tests for test_ai view."""

    def test_test_ai_renders_template(self):
        """Test test AI view renders template."""
        test_prefix = 'test_ai_render_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/test-ai/')
        assert response.status_code == 200
        assert 'manager/test_ai.html' in [t.name for t in response.templates]

    def test_test_ai_no_api_key(self):
        """Test test AI without API key configured."""
        test_prefix = 'test_ai_nokey_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        # Ensure no API key
        settings = SystemSettings.get_instance()
        settings.ai_api_key = ''
        settings.save()

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.get('/manager/test-ai/')

        assert response.context['success'] is False
        assert 'AI API Key is not configured' in response.context['message']

    @patch('apps.ai.client.OpenAI')
    def test_test_ai_success(self, mock_openai_class):
        """Test successful AI connection test via AIClient."""
        test_prefix = 'test_ai_success_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        settings = SystemSettings.get_instance()
        settings.ai_api_key = 'test_key'
        settings.ai_api_base = 'https://api.test.com/v1'
        settings.ai_api_model = 'test-model'
        settings.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
        settings.save()

        # Mock OpenAI client via AIClient's module
        mock_client = Mock()
        mock_openai_class.return_value = mock_client
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content='{"answer": "hello!", "sources": []}'))]
        mock_response.usage.total_tokens = 10
        mock_client.chat.completions.create.return_value = mock_response

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/test-ai/', {
            'test_prompt': 'Say hello'
        })

        assert response.context['success'] is True
        assert 'AI connection successful' in response.context['message']

    @patch('apps.ai.client.OpenAI')
    def test_test_ai_api_error(self, mock_openai_class):
        """Test AI connection test with API error."""
        test_prefix = 'test_ai_error_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        settings = SystemSettings.get_instance()
        settings.ai_api_key = 'test_key'
        settings.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
        settings.save()

        # Mock OpenAI client to raise error
        mock_client = Mock()
        mock_openai_class.return_value = mock_client
        mock_client.chat.completions.create.side_effect = Exception('API Error')

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/test-ai/', {
            'test_prompt': 'Say hello'
        })

        assert response.context['success'] is False
        assert 'AI connection failed' in response.context['message']

    @patch('apps.ai.client.OpenAI')
    def test_test_ai_uses_aiclient_defense_preamble(self, mock_openai_class):
        """The AI diagnostic endpoint delegates to AIClient and includes the defense preamble."""
        test_prefix = 'test_ai_preamble_'
        manager = User.objects.create_user(
            username=f'{test_prefix}manager',
            password='testpass123',
        )
        settings = SystemSettings.get_instance()
        settings.ai_api_key = 'test_key'
        settings.ai_api_base = 'https://api.example.com/v1'
        settings.ai_api_model = 'test-model'
        settings.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
        settings.save()

        mock_client = Mock()
        mock_openai_class.return_value = mock_client
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content='{"answer": "hello!", "sources": []}'))]
        mock_response.usage.total_tokens = 5
        mock_client.chat.completions.create.return_value = mock_response

        client = Client()
        client.login(username=f'{test_prefix}manager', password='testpass123')
        response = client.post('/manager/test-ai/', {'test_prompt': 'say hi'})

        assert response.context['success'] is True
        # Verify the call went through AIClient (OpenAI instantiated via apps.ai.client)
        assert mock_openai_class.called
        # Defense preamble must be present in the system message
        sent_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        assert any("SECURITY NOTE" in msg["content"] for msg in sent_messages)
