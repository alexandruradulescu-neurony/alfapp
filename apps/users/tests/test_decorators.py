"""
Tests for users app decorators.

Tests cover:
- role_required decorator
- agent_required decorator
- manager_required decorator
- login_redirect decorator
"""

import pytest
from unittest.mock import Mock, patch
from django.test import RequestFactory
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser

from apps.users.decorators import (
    role_required,
    agent_required,
    manager_required,
    login_redirect,
)

# Use RequestFactory for proper request objects
rf = RequestFactory()


@pytest.mark.django_db
class TestRoleRequired:
    """Tests for role_required decorator."""

    def test_role_required_unauthenticated_user(self):
        """Test unauthenticated user is redirected to login."""
        @role_required('AGENT')
        def test_view(request):
            return 'success'

        request = rf.get('/test/')
        request.user = AnonymousUser()

        # login_required redirects to login page for unauthenticated users
        response = test_view(request)
        assert response.status_code == 302
        assert '/login/' in response.url

    def test_role_required_user_without_role_attribute(self):
        """Test user without role attribute raises PermissionDenied."""
        @role_required('AGENT')
        def test_view(request):
            return 'success'

        request = Mock()
        user = Mock()
        user.is_authenticated = True
        # Remove role attribute
        del user.role
        request.user = user

        with pytest.raises(PermissionDenied, match="User role not configured"):
            test_view(request)

    def test_role_required_wrong_role(self):
        """Test user with wrong role raises PermissionDenied."""
        @role_required('MANAGER')
        def test_view(request):
            return 'success'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        with pytest.raises(PermissionDenied, match="Access denied"):
            test_view(request)

    def test_role_required_correct_role(self):
        """Test user with correct role is allowed."""
        @role_required('AGENT')
        def test_view(request):
            return 'success'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        result = test_view(request)
        assert result == 'success'

    def test_role_required_multiple_roles_allowed(self):
        """Test decorator with multiple allowed roles."""
        @role_required('AGENT', 'MANAGER')
        def test_view(request):
            return 'success'

        # Test with AGENT
        request_agent = Mock()
        request_agent.user = Mock()
        request_agent.user.is_authenticated = True
        request_agent.user.role = 'AGENT'

        assert test_view(request_agent) == 'success'

        # Test with MANAGER
        request_manager = Mock()
        request_manager.user = Mock()
        request_manager.user.is_authenticated = True
        request_manager.user.role = 'MANAGER'

        assert test_view(request_manager) == 'success'

    def test_role_required_multiple_roles_denied(self):
        """Test decorator with multiple roles denies wrong role."""
        @role_required('AGENT', 'MANAGER')
        def test_view(request):
            return 'success'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'OTHER_ROLE'

        with pytest.raises(PermissionDenied, match="Access denied"):
            test_view(request)


@pytest.mark.django_db
class TestAgentRequired:
    """Tests for agent_required decorator."""

    def test_agent_required_unauthenticated_user(self):
        """Test unauthenticated user is redirected to login."""
        request = rf.get('/test/')
        request.user = AnonymousUser()

        @agent_required
        def test_view(request):
            return 'success'

        # login_required redirects to login page for unauthenticated users
        response = test_view(request)
        assert response.status_code == 302
        assert '/login/' in response.url

    def test_agent_required_agent_user(self):
        """Test AGENT user is allowed."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        @agent_required
        def test_view(request):
            return 'success'

        result = test_view(request)
        assert result == 'success'

    def test_agent_required_manager_user(self):
        """Test MANAGER user is also allowed (agents + managers)."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        @agent_required
        def test_view(request):
            return 'success'

        result = test_view(request)
        assert result == 'success'

    def test_agent_required_wrong_role(self):
        """Test user with wrong role is denied."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'OTHER'

        @agent_required
        def test_view(request):
            return 'success'

        with pytest.raises(PermissionDenied):
            test_view(request)


@pytest.mark.django_db
class TestManagerRequired:
    """Tests for manager_required decorator."""

    def test_manager_required_unauthenticated_user(self):
        """Test unauthenticated user is redirected to login."""
        request = rf.get('/test/')
        request.user = AnonymousUser()

        @manager_required
        def test_view(request):
            return 'success'

        # login_required redirects to login page for unauthenticated users
        response = test_view(request)
        assert response.status_code == 302
        assert '/login/' in response.url

    def test_manager_required_manager_user(self):
        """Test MANAGER user is allowed."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        @manager_required
        def test_view(request):
            return 'success'

        result = test_view(request)
        assert result == 'success'

    def test_manager_required_agent_user(self):
        """Test AGENT user is denied (managers only)."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        @manager_required
        def test_view(request):
            return 'success'

        with pytest.raises(PermissionDenied):
            test_view(request)

    def test_manager_required_wrong_role(self):
        """Test user with wrong role is denied."""
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'OTHER'

        @manager_required
        def test_view(request):
            return 'success'

        with pytest.raises(PermissionDenied):
            test_view(request)


@pytest.mark.django_db
class TestLoginRedirect:
    """Tests for login_redirect decorator."""

    def test_login_redirect_unauthenticated_user_calls_view(self):
        """Test unauthenticated user proceeds to view (login page)."""
        @login_redirect
        def test_view(request):
            return 'login_page'

        request = Mock()
        request.user = AnonymousUser()

        result = test_view(request)
        assert result == 'login_page'

    @patch('apps.users.decorators.redirect')
    def test_login_redirect_authenticated_manager(self, mock_redirect):
        """Test authenticated MANAGER is redirected to manager_dashboard."""
        from django.http import HttpResponse
        mock_redirect.return_value = HttpResponse('redirect')

        @login_redirect
        def test_view(request):
            return 'login_page'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        test_view(request)
        mock_redirect.assert_called_with('manager_dashboard')

    @patch('apps.users.decorators.redirect')
    def test_login_redirect_authenticated_agent(self, mock_redirect):
        """Test authenticated AGENT is redirected to agent_dashboard."""
        from django.http import HttpResponse
        mock_redirect.return_value = HttpResponse('redirect')

        @login_redirect
        def test_view(request):
            return 'login_page'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        test_view(request)
        mock_redirect.assert_called_with('agent_dashboard')

    @patch('apps.users.decorators.redirect')
    def test_login_redirect_authenticated_user_no_role(self, mock_redirect):
        """Test authenticated user without role redirects to default."""
        from django.http import HttpResponse
        mock_redirect.return_value = HttpResponse('redirect')

        @login_redirect
        def test_view(request):
            return 'login_page'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        # User has no role attribute
        del request.user.role

        test_view(request)
        mock_redirect.assert_called_with('agent_dashboard')

    @patch('apps.users.decorators.redirect')
    def test_login_redirect_authenticated_user_unknown_role(self, mock_redirect):
        """Test authenticated user with unknown role redirects to default."""
        from django.http import HttpResponse
        mock_redirect.return_value = HttpResponse('redirect')

        @login_redirect
        def test_view(request):
            return 'login_page'

        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'UNKNOWN_ROLE'

        test_view(request)
        # For unknown role, it falls through to default redirect
        mock_redirect.assert_called_with('agent_dashboard')
