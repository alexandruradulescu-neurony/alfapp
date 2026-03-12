"""
Custom error handler views for LORA.
"""

from django.shortcuts import render


def custom_404(request, exception):
    """Custom 404 error handler."""
    return render(request, '404.html', status=404)


def custom_500(request):
    """Custom 500 error handler."""
    return render(request, '500.html', status=500)
