"""
API URLs for the agent app.
"""

from django.urls import path

from apps.agent.views import AgentChatAPIView

app_name = 'api-agent'

urlpatterns = [
    path('chat/', AgentChatAPIView.as_view(), name='agent-chat-api'),
]
