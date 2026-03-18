"""
URLs for the agent app.
"""

from django.urls import path

from apps.agent import views

app_name = 'agent'

urlpatterns = [
    path('chat/', views.agent_chat_view, name='agent-chat'),
]
