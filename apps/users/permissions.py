"""
Reusable permission classes for LORA API.

These permissions are used across multiple apps (claims, communications, etc.)
to enforce role-based access control.
"""

from rest_framework import permissions


class IsAgentOrManager(permissions.BasePermission):
    """
    Custom permission to allow only AGENT or MANAGER users.
    Explicitly validates the role value.
    """

    def has_permission(self, request, view):
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"IsAgentOrManager check - User: {request.user}, Auth: {request.user.is_authenticated}, Role: {getattr(request.user, 'role', 'NO ROLE')}")

        if not request.user.is_authenticated:
            logger.warning(f"Permission denied: User not authenticated")
            return False
        if not hasattr(request.user, 'role'):
            logger.warning(f"Permission denied: User has no role attribute")
            return False
        has_perm = request.user.role in ['AGENT', 'MANAGER']
        if not has_perm:
            logger.warning(f"Permission denied: Role '{request.user.role}' not in ['AGENT', 'MANAGER']")
        else:
            logger.info(f"Permission granted for role: {request.user.role}")
        return has_perm

    def has_object_permission(self, request, view, obj):
        if not request.user.is_authenticated:
            return False
        if not hasattr(request.user, 'role'):
            return False
        return request.user.role in ['AGENT', 'MANAGER']


class IsManager(permissions.BasePermission):
    """
    Custom permission to allow only MANAGER users.
    """

    def has_permission(self, request, view):
        return request.user.is_authenticated and getattr(request.user, 'role', None) == 'MANAGER'

    def has_object_permission(self, request, view, obj):
        return request.user.is_authenticated and getattr(request.user, 'role', None) == 'MANAGER'
