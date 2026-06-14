"""
Agent Chat views for LORA.

Provides API endpoint and frontend page for AI-powered claim chat.
"""

import logging

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.authentication import SessionAuthentication

from apps.users.permissions import IsAgentOrManager
from apps.agent.services import AgentChatService

logger = logging.getLogger(__name__)


class AgentChatAPIView(APIView):
    """
    AI-powered chat interface for querying claim information.
    
    POST /api/agent/chat/
    Body: {
        "message": "What's the status of ALF1234567?",
        "claimIds": [123]  // optional
    }
    """
    authentication_classes = [SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
    
    def post(self, request):
        # NB: never log request.headers/session here — they carry the session
        # cookie and CSRF token (credential leakage into logs).
        message = request.data.get('message')
        conversation_history = request.data.get('conversationHistory', [])
        
        if not message:
            return Response(
                {'error': 'Message is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = AgentChatService()
            response = service.process_message(message, conversation_history=conversation_history)
            
            return Response({
                'answer': response.answer,
                'sources': response.sources,
                'claims': response.claims,
                'success': True,
            })
            
        except Exception:
            # Log the full traceback server-side; don't leak internals to the client.
            logger.exception("Error in agent chat")
            return Response(
                {
                    'error': 'Failed to process message',
                    'message': 'An unexpected error occurred. Please check the server logs and try again.',
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@login_required
def agent_chat_view(request):
    """
    Render the AI agent chat page.
    
    GET /agent/chat/
    """
    return render(request, 'agent/chat.html')
