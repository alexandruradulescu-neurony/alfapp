"""
Agent Chat Service for LORA.

Provides AI-powered chat interface for querying claim information.
Fetches data from LORA database and Zendesk API, generates responses via LLM.
"""

import re
import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from django.db.models import QuerySet

logger = logging.getLogger(__name__)


@dataclass
class ChatResponse:
    """Response from agent chat service."""
    answer: str
    sources: List[str]
    claims: List[Dict[str, Any]]


class AgentChatService:
    """
    Service for AI-powered claim chat interface.
    Fetches data from LORA and Zendesk, generates responses via LLM.
    """
    
    def __init__(self):
        # Pattern to match ALF claim IDs (e.g., ALF1234567, ALF-1234567, ALF_1234567)
        self.claim_id_pattern = re.compile(r'ALF[-_]?\d{7}', re.IGNORECASE)
    
    def process_message(self, message: str, claim_ids: Optional[List[int]] = None) -> ChatResponse:
        """
        Process user message and generate AI response.
        
        Args:
            message: User's chat message
            claim_ids: Optional list of claim IDs to include in context
        
        Returns:
            ChatResponse with answer, sources, and claim data
        """
        # Step 1: Detect claim IDs from message
        detected_ids = self.detect_claim_ids(message)
        
        # Combine with provided claim IDs
        all_claim_ids = list(set(detected_ids))
        if claim_ids:
            # Convert numeric IDs to ALF format if needed
            all_claim_ids.extend([str(cid) for cid in claim_ids])
        
        if not all_claim_ids:
            # No claim IDs detected, provide general help
            return self._handle_no_claim_detected(message)
        
        # Step 2-4: Fetch context
        context = self.fetch_context(all_claim_ids)
        
        # Step 5: Build prompt
        prompt = self.build_prompt(message, context)
        
        # Step 6: Call LLM
        llm_answer = self._call_llm(prompt)
        
        # Step 7: Return response
        return ChatResponse(
            answer=llm_answer,
            sources=context['sources'],
            claims=context['claims'],
        )
    
    def detect_claim_ids(self, message: str) -> List[str]:
        """
        Extract ALF claim IDs from message.
        
        Args:
            message: User's message
        
        Returns:
            List of detected ALF claim IDs
        """
        matches = self.claim_id_pattern.findall(message)
        # Normalize format (remove hyphens/underscores)
        normalized = []
        for match in matches:
            # Extract just the digits
            digits = re.search(r'\d{7}', match)
            if digits:
                normalized.append(f"ALF{digits.group()}")
        return list(set(normalized))
    
    def fetch_context(self, claim_ids: List[str]) -> Dict[str, Any]:
        """
        Fetch all relevant data for detected claims.
        
        Args:
            claim_ids: List of ALF claim IDs
        
        Returns:
            Dictionary with claims, emails, refunds, timeline, zendesk data
        """
        from apps.claims.models import Claim
        from apps.payments.models import Refund
        from apps.communications.models import EmailLog
        from apps.integrations.services import fetch_zendesk_ticket, fetch_zendesk_comments
        
        context = {
            'claims': [],
            'emails': {},
            'refunds': {},
            'timeline': {},
            'zendesk': {},
            'sources': [],
        }
        
        for alf_id in claim_ids:
            try:
                claim = Claim.objects.select_related('assigned_to').get(alf_claim_id=alf_id)
                
                claim_data = {
                    'id': claim.id,
                    'alf_claim_id': claim.alf_claim_id,
                    'client_email': claim.client_email,
                    'fulfillment_status': claim.get_fulfillment_status_display(),
                    'financial_status': claim.get_financial_status_display(),
                    'dispute_status': claim.get_dispute_status_display(),
                    'zd_ticket_id': claim.zd_ticket_id,
                    'flight_details': claim.flight_details or 'Not provided',
                    'object_description': claim.object_description or 'Not provided',
                    'phone': claim.phone or 'Not provided',
                    'alternate_email': claim.alternate_email or 'Not provided',
                    'created_at': claim.created_at.strftime('%B %d, %Y'),
                    'ai_summary': claim.ai_summary or 'No AI summary available',
                }
                context['claims'].append(claim_data)
                context['sources'].append('LORA')
                
                # Fetch emails (last 10)
                emails = EmailLog.objects.filter(claim=claim).order_by('-received_at')[:10]
                context['emails'][alf_id] = [
                    {
                        'subject': e.subject,
                        'received_at': e.received_at.strftime('%B %d, %Y at %I:%M %p'),
                        'ai_summary': e.ai_summary or 'No summary',
                        'category': e.get_category_display(),
                        'action_required': 'Yes' if e.action_required else 'No',
                    }
                    for e in emails
                ]
                if emails:
                    context['sources'].append('EmailLog')
                
                # Fetch refunds
                refunds = Refund.objects.filter(claim=claim).order_by('-created_at')
                context['refunds'][alf_id] = [
                    {
                        'amount': f"{r.currency} {r.amount}",
                        'status': r.get_status_display(),
                        'refund_type': r.get_refund_type_display(),
                        'external_source': r.get_external_source_display(),
                        'created_at': r.created_at.strftime('%B %d, %Y'),
                        'reason': r.reason or 'No reason provided',
                    }
                    for r in refunds
                ]
                if refunds:
                    context['sources'].append('Refund')
                
                # Fetch timeline updates (last 10)
                timeline = claim.updates.all().order_by('-created_at')[:10]
                context['timeline'][alf_id] = [
                    {
                        'update_type': t.get_update_type_display(),
                        'llm_summary': t.llm_summary or 'No summary',
                        'created_at': t.created_at.strftime('%B %d, %Y at %I:%M %p'),
                    }
                    for t in timeline
                ]
                if timeline:
                    context['sources'].append('Timeline')
                
                # Fetch Zendesk ticket (if linked)
                if claim.zd_ticket_id:
                    ticket = fetch_zendesk_ticket(claim.zd_ticket_id)
                    if ticket:
                        comments = fetch_zendesk_comments(claim.zd_ticket_id)[:5]
                        context['zendesk'][alf_id] = {
                            'ticket_id': claim.zd_ticket_id,
                            'status': ticket.get('status', 'unknown'),
                            'subject': ticket.get('subject', 'No subject'),
                            'requester_id': ticket.get('requester_id', 'unknown'),
                            'recent_comments': [
                                {
                                    'author': c.get('author', {}).get('name', 'Unknown'),
                                    'body': c.get('body', '')[:200] + '...' if len(c.get('body', '')) > 200 else c.get('body', ''),
                                    'created_at': c.get('created_at', 'unknown'),
                                }
                                for c in comments
                            ]
                        }
                        context['sources'].append('Zendesk')
                    else:
                        context['zendesk'][alf_id] = {
                            'ticket_id': claim.zd_ticket_id,
                            'error': 'Failed to fetch from Zendesk',
                        }
                
            except Claim.DoesNotExist:
                context['claims'].append({
                    'alf_claim_id': alf_id,
                    'error': f'Claim {alf_id} not found in LORA',
                })
            except Exception as e:
                logger.error(f"Error fetching context for claim {alf_id}: {e}")
                context['claims'].append({
                    'alf_claim_id': alf_id,
                    'error': f'Error fetching claim data: {str(e)}',
                })
        
        return context
    
    def build_prompt(self, message: str, context: Dict[str, Any]) -> str:
        """
        Build LLM prompt with context.
        
        Args:
            message: User's question
            context: Fetched claim data
        
        Returns:
            Formatted prompt for LLM
        """
        return f"""You are a helpful AI assistant for LORA (Lost Object Recovery Automation).
You help agents find information about claims quickly and accurately.

## Available Data

Claims:
{json.dumps(context['claims'], indent=2)}

Emails (last 10 per claim):
{json.dumps(context['emails'], indent=2)}

Refunds:
{json.dumps(context['refunds'], indent=2)}

Timeline Updates:
{json.dumps(context['timeline'], indent=2)}

Zendesk Tickets:
{json.dumps(context['zendesk'], indent=2)}

## Instructions

1. Answer the user's question based ONLY on the available data above.
2. If information is missing, clearly state what you couldn't find.
3. Use markdown formatting for clarity:
   - Use **bold** for claim IDs, statuses, and important information
   - Use bullet points for lists
   - Use numbered lists for sequences
4. Keep responses concise but informative (2-5 paragraphs max).
5. If multiple claims are mentioned, organize answer by claim.
6. Suggest 2-3 related follow-up questions at the end.
7. Be professional and helpful in tone.

## User Question

{message}

## Your Response
"""
    
    def _call_llm(self, prompt: str) -> str:
        """
        Call LLM API to generate response.
        
        Args:
            prompt: Formatted prompt
        
        Returns:
            LLM-generated answer
        """
        from apps.communications.services import call_qwen_ai
        
        try:
            result = call_qwen_ai(prompt, '', 'Agent Chat Query')
            return result.get('raw_response', 'I apologize, but I encountered an error generating a response.')
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return "I apologize, but I encountered an error processing your request. Please try again."
    
    def _handle_no_claim_detected(self, message: str) -> ChatResponse:
        """
        Handle messages where no claim ID was detected.
        
        Args:
            message: User's message
        
        Returns:
            ChatResponse with helpful guidance
        """
        return ChatResponse(
            answer="""I couldn't detect a claim ID in your message. 

To help you better, please include a claim ID in your question. Claim IDs follow the format:
- **ALF1234567**
- **ALF-1234567**
- **ALF_1234567**

**Example questions:**
- "What's the status of ALF1234567?"
- "Show me emails for ALF-7654321"
- "Has ALF1234567 been refunded?"
- "What's in the Zendesk ticket for ALF1234567?"

You can also ask general questions like:
- "How do I process a refund?"
- "What are the claim statuses?"
""",
            sources=[],
            claims=[],
        )
