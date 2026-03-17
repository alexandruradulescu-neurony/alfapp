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
        if not request.user.is_authenticated:
            return False
        if not hasattr(request.user, 'role'):
            return False
        return request.user.role in ['AGENT', 'MANAGER']

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
