"""
Frontend URL patterns for Dispute Management (MANAGER role only).

These URLs are included under /manager/disputes/ in the main URL configuration.
All views are protected by @manager_required decorator.
"""

from django.urls import path

from apps.payments.frontend_views import (
    dispute_list,
    dispute_create,
    dispute_detail,
    dispute_pull_from_paypal,
    dispute_prune_resolved,
    dispute_refresh_from_paypal,
    dispute_link_claim,
    dispute_generate_documents,
    dispute_edit_document,
    dispute_delete_document,
    dispute_accept_claim,
    dispute_set_category,
    dispute_prepare_submission,
    dispute_submit_to_paypal,
    dispute_delete_submission_image,
    dispute_preview_invoice,
)

app_name = 'disputes'

urlpatterns = [
    # Dispute list, manual create, and detail
    path('', dispute_list, name='dispute_list'),
    path('create/', dispute_create, name='dispute_create'),
    path('pull-from-paypal/', dispute_pull_from_paypal, name='dispute_pull_from_paypal'),
    path('prune-resolved/', dispute_prune_resolved, name='dispute_prune_resolved'),
    path('<int:dispute_id>/', dispute_detail, name='dispute_detail'),
    path('<int:dispute_id>/refresh-from-paypal/', dispute_refresh_from_paypal, name='dispute_refresh_from_paypal'),
    path('<int:dispute_id>/link-claim/', dispute_link_claim, name='dispute_link_claim'),

    # Document management
    path('<int:dispute_id>/generate-documents/', dispute_generate_documents, name='dispute_generate_documents'),
    path('<int:dispute_id>/set-category/', dispute_set_category, name='dispute_set_category'),
    path('<int:dispute_id>/accept-claim/', dispute_accept_claim, name='dispute_accept_claim'),

    # Back-and-forth submissions (compose → save draft → send → conversation thread)
    path('<int:dispute_id>/prepare-submission/', dispute_prepare_submission, name='dispute_prepare_submission'),
    path('<int:dispute_id>/submit-to-paypal/', dispute_submit_to_paypal, name='dispute_submit_to_paypal'),
    path('<int:dispute_id>/preview-invoice/', dispute_preview_invoice, name='dispute_preview_invoice'),
    path('submission-images/<int:image_id>/delete/', dispute_delete_submission_image, name='dispute_delete_submission_image'),

    # Document actions (separate URL namespace for documents)
    path('documents/<int:document_id>/edit/', dispute_edit_document, name='dispute_edit_document'),
    path('documents/<int:document_id>/delete/', dispute_delete_document, name='dispute_delete_document'),
]
