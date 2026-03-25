"""
Tests for users app permissions.

Tests cover:
- IsAgentOrManager permission class
- IsManager permission class
"""

import pytest
from unittest.mock import Mock

from apps.users.permissions import IsAgentOrManager, IsManager


@pytest.mark.django_db
class TestIsAgentOrManager:
    """Tests for IsAgentOrManager permission class."""

    def test_has_permission_unauthenticated_user(self):
        """Test unauthenticated user is denied."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = False

        assert permission.has_permission(request, Mock()) is False

    def test_has_permission_user_without_role_attribute(self):
        """Test user without role attribute is denied."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = None  # No role attribute

        # hasattr will return True but role is None
        assert permission.has_permission(request, Mock()) is False

    def test_has_permission_agent_user(self):
        """Test AGENT user is allowed."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        assert permission.has_permission(request, Mock()) is True

    def test_has_permission_manager_user(self):
        """Test MANAGER user is allowed."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        assert permission.has_permission(request, Mock()) is True

    def test_has_object_permission_unauthenticated_user(self):
        """Test unauthenticated user is denied object permission."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = False

        assert permission.has_object_permission(request, Mock(), Mock()) is False

    def test_has_object_permission_user_without_role_attribute(self):
        """Test user without role attribute is denied object permission."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = None

        assert permission.has_object_permission(request, Mock(), Mock()) is False

    def test_has_object_permission_agent_user(self):
        """Test AGENT user is allowed object permission."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        assert permission.has_object_permission(request, Mock(), Mock()) is True

    def test_has_object_permission_manager_user(self):
        """Test MANAGER user is allowed object permission."""
        permission = IsAgentOrManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        assert permission.has_object_permission(request, Mock(), Mock()) is True


@pytest.mark.django_db
class TestIsManager:
    """Tests for IsManager permission class."""

    def test_has_permission_unauthenticated_user(self):
        """Test unauthenticated user is denied."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = False

        assert permission.has_permission(request, Mock()) is False

    def test_has_permission_user_without_role_attribute(self):
        """Test user without role attribute is denied."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        # getattr with default None when role doesn't exist
        del request.user.role

        assert permission.has_permission(request, Mock()) is False

    def test_has_permission_agent_user(self):
        """Test AGENT user is denied."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        assert permission.has_permission(request, Mock()) is False

    def test_has_permission_manager_user(self):
        """Test MANAGER user is allowed."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        assert permission.has_permission(request, Mock()) is True

    def test_has_object_permission_unauthenticated_user(self):
        """Test unauthenticated user is denied object permission."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = False

        assert permission.has_object_permission(request, Mock(), Mock()) is False

    def test_has_object_permission_user_without_role_attribute(self):
        """Test user without role attribute is denied object permission."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        del request.user.role

        assert permission.has_object_permission(request, Mock(), Mock()) is False

    def test_has_object_permission_agent_user(self):
        """Test AGENT user is denied object permission."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'AGENT'

        assert permission.has_object_permission(request, Mock(), Mock()) is False

    def test_has_object_permission_manager_user(self):
        """Test MANAGER user is allowed object permission."""
        permission = IsManager()
        request = Mock()
        request.user = Mock()
        request.user.is_authenticated = True
        request.user.role = 'MANAGER'

        assert permission.has_object_permission(request, Mock(), Mock()) is True
