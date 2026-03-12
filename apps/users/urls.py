"""
URLs for the users app including frontend dashboard views.
"""

from django.urls import path, include
from apps.users import views

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('', views.dashboard_redirect, name='dashboard'),

    # Agent views
    path('agent/', views.agent_dashboard, name='agent_dashboard'),
    path('agent/claims/', views.agent_claims, name='agent_claims'),
    path('agent/claims/<int:claim_id>/', views.agent_claim_detail, name='agent_claim_detail'),
    path('agent/claims/<int:claim_id>/status/', views.agent_update_status, name='agent_update_status'),
    path('agent/claims/<int:claim_id>/upload/', views.agent_upload_evidence, name='agent_upload_evidence'),
    path('agent/emails/', views.agent_emails, name='agent_emails'),
    path('agent/emails/<int:email_id>/', views.agent_email_detail, name='agent_email_detail'),

    # Manager views
    path('manager/', views.manager_dashboard, name='manager_dashboard'),
    path('manager/claims/', views.manager_claims, name='manager_claims'),
    path('manager/claims/<int:claim_id>/assign/', views.agent_assign_claim, name='agent_assign_claim'),
    path('manager/settings/', views.manager_settings, name='manager_settings'),
    path('manager/users/', views.manager_users, name='manager_users'),

    # Dispute management views (MANAGER only)
    path('manager/disputes/', include('apps.payments.frontend_urls')),
]
