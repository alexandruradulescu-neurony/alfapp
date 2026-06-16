"""Access control for LORA.

The manager/agent role split was removed — there is ONE trusted user type, so
access is gated purely by authentication. `manager_required` / `agent_required`
are kept as thin login gates (both simply require an authenticated user) so the
~40 existing call sites don't need editing; the role checks and field are gone.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required as django_login_required
from django.shortcuts import redirect


def manager_required(view_func):
    """Require an authenticated user (the only user type)."""
    return django_login_required(view_func)


# Back-compat alias — identical gate (kept so `@agent_required` call sites work).
agent_required = manager_required


def login_redirect(view_func):
    """On the login page, send already-authenticated users to the dashboard."""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect('manager_dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped_view
