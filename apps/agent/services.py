"""
Agent Chat Service for LORA.

Provides AI-powered chat interface for querying claim information.
Fetches data from LORA database and Zendesk API, generates responses via LLM.
"""

import re
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from django.db.models import QuerySet
from django.db.models import Q

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
        # Pattern to detect email addresses
        self.email_pattern = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
        # Pattern to detect potential names (2+ words, case insensitive)
        self.name_pattern = re.compile(r'\b[a-z]+\s+[a-z]+\b', re.IGNORECASE)
    
    def process_message(self, message: str, claim_ids: Optional[List[int]] = None, conversation_history: Optional[List[Dict]] = None) -> ChatResponse:
        """
        Process user message and generate AI response.
        
        Args:
            message: User's chat message
            claim_ids: Optional list of claim IDs to include in context
            conversation_history: Optional list of previous messages for context
        
        Returns:
            ChatResponse with answer, sources, and claim data
        """
        # Step 1: Detect claim IDs from message
        detected_ids = self.detect_claim_ids(message)
        
        # Step 1b: Try to detect customer name or email and find claims
        if not detected_ids and not claim_ids:
            name_or_email = self.detect_name_or_email(message)
            if name_or_email:
                claims = self.search_claims_by_name_or_email(name_or_email)
                if claims:
                    # Found claims by name/email
                    detected_ids = [c.alf_claim_id for c in claims[:1]]
        
        # Combine with provided claim IDs
        all_claim_ids = list(set(detected_ids))
        if claim_ids:
            all_claim_ids.extend([str(cid) for cid in claim_ids])
        
        # Step 2: Fetch context for detected claims
        context = self.fetch_context(all_claim_ids) if all_claim_ids else {'claims': [], 'emails': {}, 'refunds': {}, 'timeline': {}, 'zendesk': {}, 'sources': []}
        
        # Step 3: Build prompt with context + conversation history
        prompt = self.build_prompt(message, context, conversation_history)
        
        # Step 4: Call LLM
        llm_answer = self._call_llm(prompt)
        
        # Step 5: Return response
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
    
    def detect_name_or_email(self, message: str) -> Optional[str]:
        """
        Detect email address or potential customer name in message.
        
        Args:
            message: User's message
        
        Returns:
            Email address or name if detected, None otherwise
        """
        # First try to find email
        emails = self.email_pattern.findall(message)
        if emails:
            return emails[0]
        
        # Look for patterns like "for emma williamson", "ticket for john doe", etc.
        keywords = ['for', 'about', 'regarding', 'customer', 'client', 'claim']
        message_lower = message.lower()
        
        for keyword in keywords:
            if keyword in message_lower:
                # Find the part after the keyword
                idx = message_lower.find(keyword)
                after_keyword = message[idx + len(keyword):].strip()
                # Try to extract name (2+ words) from this part
                names = self.name_pattern.findall(after_keyword)
                if names:
                    return names[0]
        
        # If message looks like it's asking about a person (contains "who", "what", "find")
        question_words = ['who', 'what', 'where', 'find', 'search', 'look']
        if any(word in message_lower for word in question_words):
            names = self.name_pattern.findall(message)
            if names:
                return names[0]
        
        # Try to find any 2-word name in the message
        names = self.name_pattern.findall(message)
        if names:
            # Filter out common non-name phrases
            filtered = [n for n in names if n.lower() not in ['claim id', 'ticket for', 'show me']]
            if filtered:
                return filtered[0]
        
        return None
    
    def search_claims_by_name_or_email(self, search_term: str) -> List:
        """
        Search for claims by customer name or email.
        
        Args:
            search_term: Email address or customer name
        
        Returns:
            List of matching Claim objects
        """
        from apps.claims.models import Claim
        from django.db.models import Q
        
        # If it's an email, search by email field
        if '@' in search_term:
            return list(Claim.objects.filter(
                Q(client_email__icontains=search_term) |
                Q(alternate_email__icontains=search_term)
            ).order_by('-created_at')[:5])
        
        # If it's a name, search in email and descriptions
        # Split name into parts and search for each part
        name_parts = search_term.split()
        query = Q()
        for part in name_parts:
            query |= Q(client_email__icontains=part)
            query |= Q(alternate_email__icontains=part)
            query |= Q(object_description__icontains=part)
        
        return list(Claim.objects.filter(query).order_by('-created_at')[:5])
    
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
    
    def build_prompt(self, message: str, context: Dict[str, Any], conversation_history: Optional[List[Dict]] = None) -> str:
        """
        Build LLM prompt with context and conversation history.
        
        Args:
            message: User's question
            context: Fetched claim data
            conversation_history: Previous messages in conversation
        
        Returns:
            Formatted prompt for LLM
        """
        # Build context string
        context_parts = []
        
        # Add claim data if available
        for claim in context.get('claims', []):
            if 'error' not in claim:
                context_parts.append(f"""CLAIM: {claim['alf_claim_id']}
Email: {claim['client_email']} | Phone: {claim['phone']}
Status: {claim['fulfillment_status']} | Financial: {claim['financial_status']} | Dispute: {claim['dispute_status']}
Zendesk: {claim['zd_ticket_id'] or 'None'}
Flight: {claim['flight_details']}
Object: {claim['object_description']}
Created: {claim['created_at']}""")
        
        # Add emails
        for claim_id, emails in context.get('emails', {}).items():
            for email in emails:
                context_parts.append(f"EMAIL [{email['received_at']}]: {email['subject']} - {email['ai_summary']}")
        
        # Add refunds
        for claim_id, refunds in context.get('refunds', {}).items():
            for refund in refunds:
                context_parts.append(f"REFUND: {refund['amount']} ({refund['status']}) - {refund['reason']}")
        
        # Add Zendesk
        for claim_id, ticket in context.get('zendesk', {}).items():
            if 'error' not in ticket:
                context_parts.append(f"ZENDESK #{ticket['ticket_id']}: {ticket['status']} - {ticket['subject']}")
                for comment in ticket.get('recent_comments', []):
                    context_parts.append(f"  {comment['author']}: {comment['body']}")
        
        claim_context = "\n".join(context_parts) if context_parts else ""
        
        # Build conversation history
        history_text = ""
        if conversation_history:
            history_parts = []
            for msg in conversation_history[-10:]:  # Last 10 messages
                role = "User" if msg['role'] == 'user' else "Assistant"
                history_parts.append(f"{role}: {msg['content']}")
            history_text = "\n".join(history_parts)
        
        return f"""You are a helpful AI assistant for LORA, a lost luggage recovery service.

You help agents by answering questions about claims naturally.

CRITICAL RULES:
1. Respond ONLY in natural conversational English
2. NEVER output JSON or structured data
3. NEVER output fields like "summary", "category", "action_required"
4. Use complete sentences and paragraphs
5. Be friendly and helpful

{f"Previous conversation:\n{history_text}\n" if history_text else ""}{f"Claim data available:\n{claim_context}\n" if claim_context else ""}
User: {message}

Assistant (respond naturally in English):"""
    
    def _call_llm(self, prompt: str) -> str:
        """
        Call LLM API to generate response for chat.
        Uses a conversational system prompt, NOT the JSON email analysis prompt.
        
        Args:
            prompt: Formatted prompt with conversation context
        
        Returns:
            LLM-generated answer
        """
        from openai import OpenAI
        from apps.config.models import SystemSettings
        
        # Check if AI is configured
        try:
            settings = SystemSettings.get_instance()
            if not settings.ai_api_key:
                return """⚠️ **AI Not Configured**

The AI API key is not configured in SystemSettings.

To enable AI chat:
1. Go to **Manager → Configuration**
2. Set the **AI API Key** field
3. Save settings

Once configured, I'll be able to provide intelligent answers about your claims."""
            
            api_key = settings.ai_api_key
            api_base = settings.ai_api_base
            model = settings.ai_api_model
        except Exception as e:
            logger.error(f"Error checking AI configuration: {e}")
            return "I apologize, but I encountered an error accessing the AI service."
        
        try:
            # Initialize OpenAI client
            client = OpenAI(api_key=api_key, base_url=api_base)
            
            # Log the prompt being sent
            logger.info(f"Sending to LLM (first 500 chars): {prompt[:500]}...")
            
            # Use a conversational system prompt
            system_prompt = "You are a helpful, friendly AI assistant for LORA, a lost luggage recovery service. You help agents by answering questions about claims in natural, conversational English. NEVER output JSON or structured data. Always respond in complete sentences and paragraphs."
            
            # Call the API
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=1000,
            )
            
            raw_response = response.choices[0].message.content.strip()
            
            # Log the response
            logger.info(f"LLM response (first 500 chars): {raw_response[:500]}...")
            
            # If response looks like JSON, catch it
            if raw_response.strip().startswith('{') and 'summary' in raw_response.lower():
                logger.warning(f"LLM returned JSON instead of natural language: {raw_response[:200]}")
                return "I apologize, but I encountered an error processing that request. Could you please rephrase your question?"
            
            if not raw_response or 'error' in raw_response.lower():
                return "I apologize, but the AI service returned an error. Please try again."
            
            return raw_response
            
        except Exception as e:
            logger.error(f"LLM call failed: {e}", exc_info=True)
            return f"""⚠️ **AI Service Error**

I encountered an error while processing your request. Please try again."""
    
    def _handle_multiple_claims(self, message: str, context: Dict) -> ChatResponse:
        """
        Handle case where multiple claims found by name/email.
        
        Args:
            message: User's message
            context: Dictionary with claims list and count
        
        Returns:
            ChatResponse with list of matching claims
        """
        claims = context['claims']
        count = context['count']
        
        claim_list = "\n".join([
            f"- **{c.alf_claim_id}** - {c.client_email} (Status: {c.get_fulfillment_status_display()})"
            for c in claims
        ])
        
        return ChatResponse(
            answer=f"""I found **{count} claims** matching your search:

{claim_list}

Please specify which claim you'd like to know more about by using the ALF claim ID.

**Example:**
- "Show me emails for {claims[0].alf_claim_id}"
- "What's the status of {claims[0].alf_claim_id}?"
""",
            sources=['LORA'],
            claims=[{
                'alf_claim_id': c.alf_claim_id,
                'client_email': c.client_email,
                'status': c.get_fulfillment_status_display(),
            } for c in claims],
        )
    
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
