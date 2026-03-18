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
    authentication_classes = [
        'rest_framework.authentication.SessionAuthentication',
    ]
    permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
    
    def post(self, request):
        # Debug logging
        logger.info(f"Agent chat request from user: {request.user}, authenticated: {request.user.is_authenticated}, role: {getattr(request.user, 'role', 'NO ROLE')}")
        
        message = request.data.get('message')
        claim_ids = request.data.get('claimIds', [])
        
        if not message:
            return Response(
                {'error': 'Message is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = AgentChatService()
            response = service.process_message(message, claim_ids)
            
            return Response({
                'answer': response.answer,
                'sources': response.sources,
                'claims': response.claims,
                'success': True,
            })
            
        except Exception as e:
            logger.error(f"Error in agent chat: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to process message',
                    'details': str(e),
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
