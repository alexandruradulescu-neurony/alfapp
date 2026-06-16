"""
Custom error handler views for LORA.
"""

from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string


def custom_404(request, exception):
    """Custom 404 error handler."""
    return render(request, '404.html', status=404)


def custom_500(request):
    """Custom 500 error handler.

    Rendered WITHOUT request-bound context processors: the original 500 may have
    been caused by app state (DB down, a failing context processor), so rendering
    through the full request template path could raise again and mask the real
    error. Fall back to a hardcoded response if even the static template fails.
    """
    try:
        html = render_to_string('500.html')  # no request -> no context processors
    except Exception:
        html = ('<h1>Server Error (500)</h1>'
                '<p>Something went wrong on our end. Please try again later.</p>')
    return HttpResponse(html, status=500)
