"""
Custom decorators for role-based access control in LORA.
"""

from functools import wraps
from django.contrib.auth.decorators import login_required as django_login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def role_required(*allowed_roles):
    """
    Decorator to restrict access to users with specific roles.
    
    Usage:
        @role_required('MANAGER')
        @role_required('AGENT', 'MANAGER')
    """
    def decorator(view_func):
        @wraps(view_func)
        @django_login_required
        def _wrapped_view(request, *args, **kwargs):
            user = request.user
            
            # Check if user has a role attribute
            if not hasattr(user, 'role'):
                raise PermissionDenied("User role not configured")
            
            # Check if user's role is in allowed roles
            if user.role not in allowed_roles:
                raise PermissionDenied(
                    f"Access denied. Required roles: {', '.join(allowed_roles)}. "
                    f"Your role: {user.role}"
                )
            
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


def agent_required(view_func):
    """Decorator to require AGENT or MANAGER role."""
    return role_required('AGENT', 'MANAGER')(view_func)


def manager_required(view_func):
    """Decorator to require MANAGER role only."""
    return role_required('MANAGER')(view_func)


def login_redirect(view_func):
    """
    Decorator for login view that redirects based on user role.
    MANAGER -> /manager/
    AGENT -> /agent/
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if request.user.is_authenticated:
            if hasattr(request.user, 'role'):
                if request.user.role == 'MANAGER':
                    return redirect('manager_dashboard')
                elif request.user.role == 'AGENT':
                    return redirect('agent_dashboard')
            return redirect('agent_dashboard')  # Default fallback
        return view_func(request, *args, **kwargs)
    return _wrapped_view
