"""
Zendesk integration views for LORA.
Provides API endpoints for Zendesk sidebar widget.
"""

import hmac
import json
import logging

from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from django.db import IntegrityError, transaction
from django.db.models import Count

from apps.claims.models import Claim, ClaimUpdateTimeline
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Dispute, Refund
from apps.payments.refund_service import RefundService
from django.utils import timezone

from apps.integrations.services import (
    tag_zendesk_ticket_as_refunded,
    add_refund_comment_to_zendesk,
    fetch_zendesk_ticket,
    fetch_zendesk_comments,
    get_ticket_email_alias,
    post_zendesk_comment,
    resolve_custom_status,
    safe_date,
    safe_decimal,
    _compose_flight_details as compose_flight_details,
)
from apps.claims.services import compute_deadline_at
from apps.integrations.briefing import ALF_BUSINESS_CONTEXT, refresh_claim_summary
from apps.integrations.flight_lookup import (
    FlightProviderNotConfigured,
    analyze_flight_match,
    derive_flight_verdict,
    find_candidate_flights,
    format_candidates_note,
    format_flight_note,
    format_no_number_note,
    format_not_found_note,
    lookup_flight,
    normalize_flight,
    parse_airline_hint,
    parse_airport_hint,
    parse_date_hint,
    parse_flight_query,
    parse_time_hint,
)

logger = logging.getLogger(__name__)


class ZendeskSidebarAuth:
    """
    Custom authentication for Zendesk sidebar widget.
    Validates the Authorization header against the sidebar_secret_token.
    Uses constant-time comparison to prevent timing attacks.
    """

    @staticmethod
    def authenticate(request) -> bool:
        """
        Check if the Authorization header matches the sidebar secret token.
        Returns True if authenticated, False otherwise.
        Uses hmac.compare_digest for constant-time comparison.
        """
        auth_header = request.headers.get('Authorization', '')
        
        # Get the expected token from SystemSettings
        try:
            system_settings = SystemSettings.get_instance()
            expected_token = system_settings.sidebar_secret_token
        except Exception as e:
            logger.error(f"Error loading SystemSettings for sidebar auth: {e}")
            return False
        
        if not expected_token:
            logger.warning("Sidebar secret token not configured in SystemSettings")
            return False
        
        # Support both "Bearer <token>" and raw token formats
        if auth_header.startswith('Bearer '):
            provided_token = auth_header[7:]  # Remove "Bearer " prefix
        else:
            provided_token = auth_header
        
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(
            provided_token.encode('utf-8'),
            expected_token.encode('utf-8')
        )


class ZendeskSidebarView(APIView):
    """
    Zendesk sidebar widget endpoint.

    GET /api/zd/info/?email=<customer_email>[&ticket_id=<zd_ticket_id>]

    Authentication: Authorization header must match sidebar_secret_token from SystemSettings.

    Parameters:
    - email: Customer email address (optional if ticket_id provided)
    - ticket_id: Zendesk ticket ID (optional if email provided)

    If ticket_id provided: fetches claim by zd_ticket_id, extracts requester email
    If email provided: uses email directly to lookup claim
    Both can be provided for flexibility

    Returns enriched payload with:
    - claim: status and details if linked (existing functionality)
    - emails: {
        total: count of emails,
        unresolved: count (action_required=True AND NOT auto_resolved),
        latest_category: most recent email's category,
        category_breakdown: {OBJECT_FOUND: N, OBJECT_NOT_FOUND: N, ...}
      }
    - disputes: {
        total: count of disputes,
        active: [{id, status, amount, currency, seller_response_due}] (up to 5 most recent)
      }
    - submissions_tracking: {
        total: count of SUBMISSION_CONFIRMATION emails,
        responses_received: count of GENERAL_CORRESPONDENCE emails
      }

    Returns 404 if no claim found for the email/ticket_id.
    Returns 403 if authentication fails.
    """

    permission_classes = [AllowAny]  # Custom auth via sidebar secret token

    def get(self, request, *args, **kwargs):
        # Get parameters from query params
        customer_email = request.query_params.get('email', '').strip().lower()
        ticket_id = request.query_params.get('ticket_id', '').strip()

        logger.info(f"Sidebar data request - email: {customer_email or 'N/A'}, ticket_id: {ticket_id or 'N/A'}")

        # Validate that at least one parameter is provided
        if not customer_email and not ticket_id:
            return Response(
                {'error': 'Either email or ticket_id parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Authenticate using sidebar secret token
        if not ZendeskSidebarAuth.authenticate(request):
            # Rate limit failed auth attempts by IP
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)  # 5 min window
            
            logger.warning(f"Failed sidebar auth attempt for email: {customer_email or 'N/A'}, ticket_id: {ticket_id or 'N/A'}, IP: {ip}, attempt: {failed_attempts + 1}")
            
            if failed_attempts >= 5:
                return Response(
                    {'error': 'Too many failed attempts. Please try again later.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )
            
            return Response(
                {'error': 'Unauthorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Lookup claim
        claim = None
        try:
            if ticket_id:
                # First try to lookup by ticket_id
                claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
                if claim:
                    # Extract requester email from the claim
                    customer_email = claim.client_email
                    logger.info(f"Found claim #{claim.id} via ticket_id: {ticket_id}")
            
            # If no claim found yet, try lookup by email
            if not claim and customer_email:
                claim = Claim.objects.filter(client_email=customer_email).first()
                if claim:
                    logger.info(f"Found claim #{claim.id} via email: {customer_email}")

            if not claim:
                logger.info(f"No claim found for email: {customer_email}, ticket_id: {ticket_id}")
                return Response(
                    {'error': 'No claim found for this email or ticket_id'},
                    status=status.HTTP_404_NOT_FOUND
                )

        except Exception as e:
            logger.error(f"Error fetching claim for email: {customer_email}, ticket_id: {ticket_id}: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            # Build enriched response data
            response_data = self._build_enriched_sidebar_data(claim, customer_email)
            
            logger.info(f"Sidebar data returned for claim #{claim.id}, email: {customer_email}")
            return Response(response_data)

        except Exception as e:
            logger.error(f"Error building sidebar data for claim #{claim.id}, email: {customer_email}: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _build_enriched_sidebar_data(self, claim, customer_email):
        """
        Build enriched sidebar data with claim, emails, disputes, and submissions tracking.
        Uses optimized queries with .aggregate() for counts and .values() for breakdowns.
        """
        # Base claim data (existing functionality)
        base_data = {
            'found': True,
            'claim_id': claim.id,
            'claim_status': claim.status,
            'zd_ticket_id': claim.zd_ticket_id or '',
            'created_at': claim.created_at.isoformat() if claim.created_at else None,
            'flight_details': claim.flight_details or '',
        }

        # Enrich with emails data
        emails_data = self._get_emails_data(claim)
        
        # Enrich with disputes data
        disputes_data = self._get_disputes_data(claim)
        
        # Enrich with submissions tracking
        submissions_data = self._get_submissions_tracking(claim)

        # Combine all data
        response_data = {
            **base_data,
            'emails_processed': emails_data['total'],  # Backward compatibility
            'emails': emails_data,
            'disputes': disputes_data,
            'submissions_tracking': submissions_data,
        }

        return response_data

    def _get_emails_data(self, claim):
        """
        Get enriched email statistics for a claim.
        Uses optimized queries with .aggregate() for counts.
        """
        # Total count
        total_count = EmailLog.objects.filter(claim=claim).count()
        
        # Unresolved count: action_required=True AND auto_resolved=False
        unresolved_count = EmailLog.objects.filter(
            claim=claim,
            action_required=True,
            auto_resolved=False
        ).count()
        
        # Latest email category (most recent by received_at)
        latest_email = EmailLog.objects.filter(claim=claim).order_by('-received_at').first()
        latest_category = latest_email.category if latest_email else None
        
        # Category breakdown using .values() and .annotate()
        category_breakdown = {}
        category_counts = EmailLog.objects.filter(claim=claim).values('category').annotate(
            count=Count('id')
        ).order_by('-count')
        
        for item in category_counts:
            category_breakdown[item['category']] = item['count']
        
        return {
            'total': total_count,
            'unresolved': unresolved_count,
            'latest_category': latest_category,
            'category_breakdown': category_breakdown,
        }

    def _get_disputes_data(self, claim):
        """
        Get dispute statistics for a claim.
        Returns total count and up to 5 most recent active disputes.
        """
        # Total count
        total_count = Dispute.objects.filter(claim=claim).count()
        
        # Active disputes: any dispute that is not in a resolved state
        # Active statuses: RECEIVED, MATCHED, GATHERING_DATA, DOCUMENTS_READY, UNDER_REVIEW, EVIDENCE_SENT
        active_statuses = ['RECEIVED', 'MATCHED', 'GATHERING_DATA', 'DOCUMENTS_READY', 'UNDER_REVIEW', 'EVIDENCE_SENT']
        
        active_disputes_qs = Dispute.objects.filter(
            claim=claim,
            status__in=active_statuses
        ).order_by('-created_at')[:5]  # Limit to 5 most recent
        
        active_disputes = [
            {
                'id': dispute.id,
                'status': dispute.status,
                'amount': str(dispute.dispute_amount) if dispute.dispute_amount else None,
                'currency': dispute.dispute_currency,
                'seller_response_due': dispute.seller_response_due.isoformat() if dispute.seller_response_due else None,
            }
            for dispute in active_disputes_qs
        ]
        
        return {
            'total': total_count,
            'active': active_disputes,
        }

    def _get_submissions_tracking(self, claim):
        """
        Track submission-related emails for a claim.
        - total: count of SUBMISSION_CONFIRMATION emails
        - responses_received: count of GENERAL_CORRESPONDENCE emails
        """
        # Count SUBMISSION_CONFIRMATION emails
        submission_confirmations = EmailLog.objects.filter(
            claim=claim,
            category='SUBMISSION_CONFIRMATION'
        ).count()
        
        # Count GENERAL_CORRESPONDENCE emails (responses)
        general_correspondence = EmailLog.objects.filter(
            claim=claim,
            category='GENERAL_CORRESPONDENCE'
        ).count()
        
        return {
            'total': submission_confirmations,
            'responses_received': general_correspondence,
        }


class ZendeskBriefingView(APIView):
    """POST /api/integrations/zd/briefing/
    Body: {ticket_id, requester_email, requester_name, subject, description,
           ticket_created_at, comments[], mode?}
    mode='summary' (default) → {summary, next_steps[], facts{}}
    mode='next_steps'        → {next_steps[]} (generated on demand)
    Auth: ZendeskSidebarAuth (sidebar_secret_token)."""

    permission_classes = [AllowAny]

    BRIEFING_PROMPT = ALF_BUSINESS_CONTEXT + (
        "Write a briefing of at most 3 sentences for the ALF agent opening this "
        "ticket. Lead with the current lifecycle stage and its key identifiers "
        "(e.g. item found — item number, where it is, retrieval method; or still "
        "searching and since when), then say what is currently awaited and from "
        "whom. Use ONLY facts present in the provided content; never invent "
        "dates, people or procedures. "
        'Respond as JSON: {"summary": "..."}.'
    )

    NEXT_STEPS_PROMPT = ALF_BUSINESS_CONTEXT + (
        "List up to 4 concrete next actions the ALF agent should take NOW on "
        "this ticket, consistent with how ALF actually works: chase institutions "
        "by email or phone, answer/update the client, arrange retrieval or "
        "courier, confirm delivery, close the case. Base every action ONLY on "
        "the provided content; if nothing is pending, say so in a single step. "
        'Respond as JSON: {"next_steps": ["..."]}.'
    )

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed briefing auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        from apps.ai.client import AIClient
        from apps.ai.schemas import BriefingSummary, NextSteps
        from apps.ai.exceptions import AIResponseValidationError
        from apps.claims.models import Claim
        from apps.integrations.services import build_claim_facts, build_ticket_thread

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        mode = str(data.get('mode', 'summary')).strip() or 'summary'
        logger.info(f"Briefing request for ticket_id: {ticket_id or 'N/A'} (mode={mode})")

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        facts = build_claim_facts(claim) if claim else {}

        # "Needs attention": unresolved institution emails, for panel display
        # only — external subjects stay OUT of the trusted AI channel.
        attention = []
        if claim:
            from django.utils import timezone
            unresolved = (claim.emails.filter(action_required=True, auto_resolved=False)
                          .order_by('-received_at')[:5])
            attention = [{'date': timezone.localtime(e.received_at).date().isoformat(),
                          'subject': e.subject[:90], 'category': e.category}
                         for e in unresolved]

        trusted = {'claim_facts': str(facts)} if facts else None
        untrusted = build_ticket_thread(data)

        known_names = [str(data.get('requester_name', '')).strip()]
        if claim and getattr(claim, 'client_name', ''):
            known_names.append(claim.client_name)
        known_pii = {'aliases': [], 'names': [n for n in known_names if n]}

        # SUMMARY (default mode), claim linked → SINGLE SOURCE OF TRUTH.
        # The sidebar shows the SAME stored summary the LORA app shows
        # (claim.ai_summary), written by the one shared engine. It is NOT
        # regenerated on every open. We regenerate + persist only when the
        # agent explicitly asks (refresh=true, the Regenerate button) or when
        # there is no stored summary yet — so a refresh in the sidebar and a
        # refresh in the app update the one copy everyone sees.
        if mode != 'next_steps' and claim:
            from apps.integrations.briefing import refresh_claim_summary
            refresh = bool(data.get('refresh'))
            if refresh or not (claim.ai_summary or '').strip():
                ticket_data = {
                    'subject': data.get('subject', ''),
                    'description': data.get('description', ''),
                    'created_at': data.get('ticket_created_at', ''),
                    'comments': data.get('comments') or [],
                }
                refresh_claim_summary(claim, ticket_data)  # best-effort; persists
                claim.refresh_from_db(fields=['ai_summary', 'ai_summary_updated_at'])
            summary = (claim.ai_summary or '').strip() or \
                'No summary yet — click Regenerate to create one.'
            updated = (claim.ai_summary_updated_at.isoformat()
                       if claim.ai_summary_updated_at else None)
            return Response(
                {'summary': summary, 'next_steps': [], 'facts': facts,
                 'attention': attention, 'summary_updated_at': updated, 'stored': True},
                status=status.HTTP_200_OK,
            )

        # next_steps (on-demand, derived — not stored), OR a claimless ticket
        # summary (no claim to store against → transient briefing).
        if mode == 'next_steps':
            prompt, schema = self.NEXT_STEPS_PROMPT, NextSteps
        else:
            prompt, schema = self.BRIEFING_PROMPT, BriefingSummary

        try:
            result = AIClient.complete(
                system_prompt=prompt,
                trusted=trusted,
                untrusted=untrusted,
                known_pii=known_pii,
                response_schema=schema,
                call_site='zendesk_briefing',
                temperature=0.4,
                max_tokens=500,
            )
        except AIResponseValidationError as e:
            logger.warning(f"Briefing AI validation failed for ticket {ticket_id} (mode={mode}): {e}")
            if mode == 'next_steps':
                return Response({'next_steps': []}, status=status.HTTP_200_OK)
            return Response(
                {'summary': 'Briefing unavailable right now. Please use the Chat tab or retry.',
                 'next_steps': [], 'facts': facts, 'attention': attention,
                 'summary_updated_at': None, 'stored': False},
                status=status.HTTP_200_OK,
            )

        if mode == 'next_steps':
            return Response({'next_steps': result.next_steps}, status=status.HTTP_200_OK)
        return Response(
            {'summary': result.summary, 'next_steps': result.next_steps,
             'facts': facts, 'attention': attention,
             'summary_updated_at': None, 'stored': False},
            status=status.HTTP_200_OK,
        )


class ZendeskDraftView(APIView):
    """POST /api/integrations/zd/draft/
    Body: same ticket context as the briefing + draft_type
          ('client_update' | 'institution_reply')
    Returns: {body} — an email draft the agent reviews in the composer.
    Auth: ZendeskSidebarAuth (sidebar_secret_token)."""

    permission_classes = [AllowAny]

    PROMPTS = {
        'client_update': ALF_BUSINESS_CONTEXT + (
            "Draft the next email FROM ALF TO THE CLIENT for this case. Mirror "
            "the greeting, tone and sign-off style of previous ALF emails in the "
            "thread. Lead with the current status in plain words; if the item is "
            "found, cover the retrieval logistics and what the client must do "
            "next; if still searching, say what ALF has done since the last "
            "update and what happens next. Warm and concise (under 180 words). "
            "Use ONLY facts from the provided content; keep placeholders "
            "verbatim. No subject line. "
            'Respond as JSON: {"body": "..."}.'
        ),
        'institution_reply': ALF_BUSINESS_CONTEXT + (
            "Draft a reply FROM ALF TO THE INSTITUTION (airport / airline / "
            "lost-and-found office) whose email appears most recently in the "
            "thread. Reference the case identifiers present in the content "
            "(item number, flight, dates, item description) and ask precisely "
            "for what is needed to move the case forward (search status, "
            "retrieval arrangement, shipping). Professional and brief. Use ONLY "
            "facts from the provided content; keep placeholders verbatim. No "
            "subject line. "
            'Respond as JSON: {"body": "..."}.'
        ),
    }

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed draft auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        from apps.ai.client import AIClient
        from apps.ai.schemas import EmailDraft
        from apps.ai.exceptions import AIResponseValidationError
        from apps.claims.models import Claim
        from apps.integrations.services import build_claim_facts, build_ticket_thread

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        draft_type = str(data.get('draft_type', '')).strip()
        if draft_type not in self.PROMPTS:
            return Response(
                {'error': "draft_type must be 'client_update' or 'institution_reply'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        logger.info(f"Draft request for ticket_id: {ticket_id or 'N/A'} (type={draft_type})")

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        facts = build_claim_facts(claim) if claim else {}

        trusted = {'claim_facts': str(facts)} if facts else None
        untrusted = build_ticket_thread(data)

        known_names = [str(data.get('requester_name', '')).strip()]
        if claim and getattr(claim, 'client_name', ''):
            known_names.append(claim.client_name)
        known_pii = {'aliases': [], 'names': [n for n in known_names if n]}

        try:
            result = AIClient.complete(
                system_prompt=self.PROMPTS[draft_type],
                trusted=trusted,
                untrusted=untrusted,
                known_pii=known_pii,
                response_schema=EmailDraft,
                call_site='zendesk_draft',
                temperature=0.5,
                max_tokens=1200,
            )
        except AIResponseValidationError as e:
            logger.warning(f"Draft AI validation failed for ticket {ticket_id} ({draft_type}): {e}")
            return Response({'body': ''}, status=status.HTTP_200_OK)

        return Response({'body': result.body}, status=status.HTTP_200_OK)


class ZendeskChatView(APIView):
    """POST /api/integrations/zd/chat/
    Body: {ticket_id, message, history[], subject?, description?, comments[]?}
    Returns: {answer, sources[]} — AI chat scoped to the ticket's claim.
    If no claim is linked but the app sent ticket content, answers from the
    ticket content alone (untrusted channel, PII-tokenized).
    Auth: ZendeskSidebarAuth (sidebar_secret_token)."""

    permission_classes = [AllowAny]

    TICKET_ONLY_PROMPT = ALF_BUSINESS_CONTEXT + (
        "No LORA claim is linked to this ticket. Answer the ALF agent's question "
        "using ONLY the ticket content provided. Be specific — quote dates, item "
        "numbers and locations when present. If the agent asks for a translation "
        "of any email or comment in the content, translate it faithfully and "
        "completely into the requested language (English unless stated "
        "otherwise). If the answer is not in the ticket content, say you don't "
        "see it in the ticket. "
        'Respond as JSON: {"answer": "...", "sources": ["zendesk"]}.'
    )

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed chat auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        from apps.claims.models import Claim
        from apps.agent.services import AgentChatService

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        message = str(data.get('message', '')).strip()
        history = data.get('history') or []

        if not message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        if not claim:
            return self._ticket_only_chat(data, ticket_id, message, history)

        logger.info(f"Sidebar chat for ticket_id: {ticket_id}, claim: {claim.alf_claim_id}")
        result = AgentChatService().process_message(
            message=message,
            claim_ids=[claim.alf_claim_id],
            conversation_history=history,
        )
        return Response({'answer': result.answer, 'sources': getattr(result, 'sources', [])},
                        status=status.HTTP_200_OK)

    def _ticket_only_chat(self, data, ticket_id, message, history):
        """Chat for tickets with no linked claim: answer from the ticket content
        the app sent. Ticket content is untrusted (external senders) and goes
        through the same tokenize-and-fence path as everything else; the agent's
        question is trusted, mirroring AgentChatService."""
        from apps.integrations.services import build_ticket_thread

        untrusted = build_ticket_thread(data)
        has_content = bool(untrusted['ticket_subject'].strip()
                           or untrusted['ticket_description'].strip()
                           or untrusted.get('zendesk_comment'))
        if not has_content:
            return Response(
                {'answer': 'No LORA claim is linked to this ticket yet, so I cannot answer '
                           'claim-specific questions here.', 'sources': []},
                status=status.HTTP_200_OK,
            )

        from apps.ai.client import AIClient
        from apps.ai.schemas import ChatAnswer
        from apps.ai.exceptions import AIResponseValidationError

        trusted = {'agent_question': message}
        if history:
            history_parts = []
            for msg in history[-10:]:
                if not isinstance(msg, dict):
                    continue
                role = 'User' if msg.get('role') == 'user' else 'Assistant'
                history_parts.append(f"{role}: {str(msg.get('content', ''))[:1000]}")
            if history_parts:
                trusted['conversation_history'] = "\n".join(history_parts)

        requester_name = str(data.get('requester_name', '')).strip()
        known_pii = {'aliases': [], 'names': [requester_name] if requester_name else []}

        try:
            result = AIClient.complete(
                system_prompt=self.TICKET_ONLY_PROMPT,
                trusted=trusted,
                untrusted=untrusted,
                known_pii=known_pii,
                response_schema=ChatAnswer,
                call_site='zendesk_ticket_chat',
                temperature=0.7,
                max_tokens=1000,
            )
        except AIResponseValidationError as e:
            logger.warning(f"Ticket-only chat AI validation failed for ticket {ticket_id}: {e}")
            return Response(
                {'answer': "I couldn't process that just now — please try again.",
                 'sources': []},
                status=status.HTTP_200_OK,
            )

        logger.info(f"Ticket-only sidebar chat for ticket_id: {ticket_id} (no linked claim)")
        return Response({'answer': result.answer, 'sources': result.sources},
                        status=status.HTTP_200_OK)


class ZendeskTicketSyncView(APIView):
    """
    Endpoint to sync a claim with Zendesk.
    Creates a Zendesk ticket if the claim doesn't have one.
    
    POST /api/zd/sync/
    Body: {"claim_id": <id>}
    
    Authentication: Sidebar secret token
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # Authenticate
        if not ZendeskSidebarAuth.authenticate(request):
            return Response(
                {'error': 'Unauthorized'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        claim_id = request.data.get('claim_id')
        
        if not claim_id:
            return Response(
                {'error': 'claim_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from apps.integrations.services import create_zendesk_ticket
            
            claim = Claim.objects.filter(id=claim_id).first()
            if not claim:
                return Response(
                    {'error': 'Claim not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check if ticket already exists
            if claim.zd_ticket_id:
                return Response({
                    'message': 'Ticket already exists',
                    'zd_ticket_id': claim.zd_ticket_id,
                })
            
            # Create Zendesk ticket
            ticket_data = create_zendesk_ticket(
                subject=f"Lost Object Claim #{claim.id} - {claim.client_email}",
                comment_body=f"Claim details:\n\nFlight: {claim.flight_details or 'Not provided'}\nStatus: {claim.status}",
                requester_email=claim.client_email,
                tags=['lora', 'lost-object', f'claim-{claim.id}'],
            )
            
            if ticket_data:
                # Update claim with ticket ID
                claim.zd_ticket_id = str(ticket_data['id'])
                claim.save(update_fields=['zd_ticket_id', 'updated_at'])
                
                return Response({
                    'message': 'Ticket created successfully',
                    'zd_ticket_id': ticket_data['id'],
                    'ticket_url': ticket_data.get('url', ''),
                })
            else:
                return Response(
                    {'error': 'Failed to create Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"Error syncing claim {claim_id} to Zendesk: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RefundWebhookView(APIView):
    """
    Webhook endpoint for receiving refund notifications from Zendesk/WordPress.

    Authentication: every request must carry a matching X-Webhook-Secret header
    (compared against SystemSettings.sidebar_secret_token).  The secret is checked
    before the request body is parsed.

    Expects POST request with JSON payload:
    {
        "event": "refund_processed",
        "claim_number": "123",
        "refund_id": "WC-456",
        "refund_amount": "50.00",
        "currency": "USD",
        "reason": "Customer request",
        "order_id": "789",
        "zd_ticket_id": "12345"
    }

    Implements idempotency via Refund.paypal_refund_id unique constraint.
    """
    permission_classes = [AllowAny]  # Webhook secret verification

    def post(self, request):
        """
        Process refund webhook from WordPress/Zendesk.
        """
        try:
            # Auth is mandatory: a webhook without the correct shared secret
            # is rejected before the body is parsed.
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            expected_secret = SystemSettings.get_instance().sidebar_secret_token or ''
            if not (webhook_secret and expected_secret
                    and hmac.compare_digest(webhook_secret.encode('utf-8'),
                                            expected_secret.encode('utf-8'))):
                logger.warning("Rejected refund webhook: missing or invalid X-Webhook-Secret")
                return Response({'error': 'Invalid webhook secret'},
                                status=status.HTTP_401_UNAUTHORIZED)

            data = request.data

            # Validate required fields
            required_fields = ['claim_number', 'refund_id', 'refund_amount']
            for field in required_fields:
                if field not in data:
                    logger.warning(f"Missing required field: {field}")
                    return Response(
                        {'error': f'Missing required field: {field}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Process refund
            currency = str(data.get('currency', 'USD'))
            service = RefundService()
            result = service.process_woocommerce_refund(
                claim_number=str(data['claim_number']),
                refund_amount=data['refund_amount'],
                refund_id=str(data['refund_id']),
                order_id=str(data.get('order_id', '')),
                reason=data.get('reason', ''),
                currency=currency,
                refund_type=data.get('refund_type'),
            )

            if result['success']:
                # Tag Zendesk ticket only on a FRESH refund — a retry of an
                # already-processed refund must not post duplicate notes.
                # Prefer the claim's own ticket id over the payload's.
                zd_ticket_id = (result['refund'].claim.zd_ticket_id
                                if result['refund'].claim else '') or data.get('zd_ticket_id')
                if zd_ticket_id and not result.get('already_processed'):
                    tag_zendesk_ticket_as_refunded(zd_ticket_id)
                    add_refund_comment_to_zendesk(
                        zd_ticket_id=zd_ticket_id,
                        refund_amount=f"{currency} {data['refund_amount']}",
                        refund_id=str(data['refund_id']),
                        reason=data.get('reason', ''),
                    )

                return Response({
                    'message': 'Refund processed successfully',
                    'refund_id': result['refund'].paypal_refund_id,
                })
            else:
                logger.error(f"Refund processing failed: {result.get('error')}")
                return Response(
                    {'error': result.get('error', 'Processing failed')},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        except Exception as e:
            logger.error(f"Error processing refund webhook: {e}", exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ZendeskClaimWebhookView(APIView):
    """
    Webhook endpoint for Zendesk custom-status changes (zen:event-type:ticket.custom_status_changed).

    Authentication: every request must carry a matching X-Webhook-Secret header
    (compared against SystemSettings.sidebar_secret_token).  The secret is checked
    before the request body is parsed or anything is logged.

    Behaviour:
    (a) Existing claim → mirrors every custom-status change onto the claim:
        - same-status payload → no-op (idempotent under Zendesk retries)
        - status change → atomic: update claim fields + write ClaimUpdateTimeline
          entry (llm_summary=''); then best-effort AI summary back-fill
        - unresolved status id (resolver returns the raw id) → dropped to prevent
          overwriting a real named status with a number
    (b) Unknown ticket at 'investigation initiated' status (INVESTIGATION_STATUS_ID)
        → full claim creation: fetch ticket, LLM extraction, Claim.objects.create,
          best-effort AI summary.  The creation status name is resolved live from
          Zendesk so that a creation retry is treated as a same-status no-op.
    (c) Unknown ticket at any other status → 200 ignored.

    Idempotency: the DB unique constraint on zd_ticket_id is the authoritative
    guard; the view-level existence check is an optimisation only.
    """

    # Zendesk custom status ID for "Investigation Initiated"
    INVESTIGATION_STATUS_ID = '11688538967068'
    permission_classes = [AllowAny]  # Webhook secret verification

    def post(self, request):
        """
        Process Zendesk claim creation or status-change webhook.
        """
        try:
            # Auth is mandatory: a webhook without the correct shared secret
            # is rejected before the body is parsed or anything is logged.
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            expected_secret = SystemSettings.get_instance().sidebar_secret_token or ''
            if not (webhook_secret and expected_secret
                    and hmac.compare_digest(webhook_secret.encode('utf-8'),
                                            expected_secret.encode('utf-8'))):
                logger.warning("Rejected Zendesk webhook: missing or invalid X-Webhook-Secret")
                return Response({'error': 'Invalid webhook secret'},
                                status=status.HTTP_401_UNAUTHORIZED)

            data = request.data
            event_data = data.get('event', {})
            detail_data = data.get('detail', {})
            custom_status = str(event_data.get('current')
                                or detail_data.get('custom_status', '') or '')
            ticket_id = detail_data.get('id') or data.get('ticket_id')
            subject = detail_data.get('subject', '')

            if not ticket_id:
                logger.warning("Zendesk webhook missing ticket id")
                return Response(
                    {'error': 'Missing required field: ticket_id'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            ticket_id = str(ticket_id)

            claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()

            if claim:
                return self._handle_status_change(claim, custom_status)

            if custom_status != self.INVESTIGATION_STATUS_ID:
                logger.info(
                    f"Ignoring webhook for ticket {ticket_id}: no claim and "
                    f"custom_status '{custom_status}' is not investigation initiated")
                return Response({
                    'message': 'Ignored: no claim and status is not investigation initiated',
                    'custom_status': custom_status,
                }, status=status.HTTP_200_OK)

            # New ticket at investigation-initiated status — create the claim.
            from apps.integrations.services import (
                ZENDESK_FIELD_CLAIM_NUMBER,
                _get_custom_field_value,
                analyze_zendesk_ticket_for_claim,
                parse_alf_claim_id_from_subject,
            )

            ticket_data = fetch_zendesk_ticket(ticket_id)
            if not ticket_data:
                logger.error(f"Failed to fetch Zendesk ticket {ticket_id}")
                return Response(
                    {'error': 'Failed to fetch Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Form-ticket gate: only the WordPress claim form produces tickets
            # carrying an ALF claim number (subject or the Claim # field).
            # Phone calls and client emails auto-created as tickets never have
            # one — they must not become claims or burn an AI extraction, even
            # when Zendesk flips them to Investigation initiated (e.g. via the
            # Open category's default status).
            alf_claim_id = parse_alf_claim_id_from_subject(subject)
            if not alf_claim_id:
                claim_number_value = _get_custom_field_value(
                    ticket_data.get('custom_fields') or [], ZENDESK_FIELD_CLAIM_NUMBER)
                alf_claim_id = parse_alf_claim_id_from_subject(claim_number_value or '')
            if not alf_claim_id:
                logger.info(
                    f"Ignoring webhook for ticket {ticket_id}: no ALF claim number "
                    f"in subject or Claim # field — not a claim-form ticket")
                return Response({
                    'message': 'Ignored: no ALF claim number — not a claim form ticket',
                }, status=status.HTTP_200_OK)

            # Fetch comments for LLM analysis
            comments = fetch_zendesk_comments(ticket_id)
            ticket_data['comments'] = comments

            # Call LLM to extract claim data
            try:
                extracted_data = analyze_zendesk_ticket_for_claim(ticket_data)
            except Exception as e:
                logger.error(f"LLM extraction failed: {e}", exc_info=True)
                # Use empty data - will trigger fallback to requester email
                extracted_data = {
                    'client_email': '',
                    'flight_details': '',
                    'object_description': '',
                    'phone': '',
                    'alternate_email': '',
                }

            # Prefer the structured "Claim #" Zendesk field over the subject-parsed
            # ID. The field is authoritative; the subject line is the fallback
            # (already resolved into alf_claim_id above). Only override when the
            # field holds a parseable ALF id, so a blank or malformed field value
            # falls back to the subject-derived id rather than corrupting it.
            claim_number_field = extracted_data.get('claim_number', '')
            if claim_number_field:
                parsed_from_field = parse_alf_claim_id_from_subject(claim_number_field)
                if parsed_from_field:
                    alf_claim_id = parsed_from_field
                    logger.info(f"Using ALF claim ID from Zendesk 'Claim #' field: {alf_claim_id}")

            # Determine if LLM extraction failed
            llm_failed = not extracted_data.get('client_email') and not extracted_data.get('flight_details')

            # Use requester email as fallback if LLM didn't extract email
            client_email = extracted_data.get('client_email', '')
            if not client_email:
                # Try to get email from webhook requester object (if present)
                requester_email = data.get('requester', {}).get('email', '')
                if requester_email:
                    client_email = requester_email
                    logger.info(f"Using requester email from webhook as fallback: {client_email}")
                else:
                    # Fetch user email from Zendesk API using requester_id
                    requester_id = detail_data.get('requester_id') or ticket_data.get('requester_id')
                    if requester_id:
                        from apps.integrations.services import fetch_zendesk_user
                        user_data = fetch_zendesk_user(requester_id)
                        if user_data:
                            client_email = user_data.get('email', '')
                            logger.info(f"Using requester email from Zendesk API: {client_email}")

            # If every email-resolution path failed, the claim cannot be routed by
            # downstream automation. Force the manual-review flag and warn loudly
            # so operators see it in the queue rather than letting it sit silently.
            if not client_email:
                llm_failed = True
                logger.warning(
                    f"Could not resolve client_email for Zendesk ticket {ticket_id} "
                    f"via any path (LLM extraction, webhook requester, Zendesk user API). "
                    f"Claim will be flagged for manual review."
                )

            # Resolve the live Zendesk label for the creation status so that a
            # creation retry (common: the inline AI work can outlive Zendesk's
            # webhook timeout) lands in _handle_status_change as a same-status
            # no-op regardless of the label's live casing.
            creation_status = resolve_custom_status(self.INVESTIGATION_STATUS_ID)
            creation_status_name = creation_status['name']
            if creation_status_name == self.INVESTIGATION_STATUS_ID:
                creation_status_name = 'Investigation initiated'  # resolver unavailable
            creation_status_category = creation_status['category'] or 'open'

            # Hoist the safe_date call so we compute it once and reuse for both
            # deadline_date= and deadline_at=.
            deadline_date_val = safe_date(extracted_data.get('deadline_date', ''))

            # Create Claim. The Claim.objects.filter check above is a cheap
            # optimization for the common case; concurrent webhooks can still
            # race past it. The DB-level unique constraint on zd_ticket_id
            # catches that race here. The atomic() savepoint isolates the
            # create so that an IntegrityError only rolls back the failed
            # insert — not any surrounding transaction — leaving us free to
            # query for the existing Claim afterward.
            try:
                with transaction.atomic():
                    claim = Claim.objects.create(
                        alf_claim_id=alf_claim_id,
                        zd_ticket_id=ticket_id,
                        client_email=client_email,
                        client_name=extracted_data.get('client_name', ''),
                        flight_details=extracted_data.get('flight_details', ''),
                        object_description=extracted_data.get('object_description', ''),
                        phone=extracted_data.get('phone', ''),
                        alternate_email=extracted_data.get('alternate_email', ''),
                        # Extended structured fields (2026-06-10). deadline_date and
                        # price_paid are coerced to their DB types; bad/empty values
                        # become None rather than raising.
                        billing_address=extracted_data.get('billing_address', ''),
                        shipping_address=extracted_data.get('shipping_address', ''),
                        incident_details=extracted_data.get('incident_details', ''),
                        lost_location=extracted_data.get('lost_location', ''),
                        deadline_date=deadline_date_val,
                        deadline_time=extracted_data.get('deadline_time', ''),
                        deadline_timezone=extracted_data.get('deadline_timezone', ''),
                        price_paid=safe_decimal(extracted_data.get('price_paid', '')),
                        payment_method=extracted_data.get('payment_method', ''),
                        payment_status=extracted_data.get('payment_status', ''),
                        woocommerce_id=extracted_data.get('woocommerce_id', ''),
                        tracking_info=extracted_data.get('tracking_info', ''),
                        status=creation_status_name,
                        status_category=creation_status_category,
                        status_changed_at=timezone.now(),
                        deadline_at=compute_deadline_at(
                            deadline_date_val,
                            extracted_data.get('deadline_time', ''),
                            extracted_data.get('deadline_timezone', ''),
                        ),
                        llm_extraction_failed=llm_failed,
                        ai_summary='',
                    )
            except IntegrityError:
                # Another concurrent webhook created the Claim between our early
                # check and our create. Look up the winner and return its info.
                existing = Claim.objects.filter(zd_ticket_id=ticket_id).first()
                if not existing:
                    # IntegrityError for some other reason (e.g., alf_claim_id collision
                    # with an unrelated claim). Let the outer handler return 500.
                    raise
                logger.info(
                    f"Race with concurrent webhook for ticket {ticket_id}; "
                    f"existing Claim #{existing.id} ({existing.alf_claim_id}) wins."
                )
                return Response({
                    'message': 'Claim already exists',
                    'claim_id': existing.id,
                    'alf_claim_id': existing.alf_claim_id,
                }, status=status.HTTP_200_OK)

            logger.info(
                f"Created Claim #{claim.id} ({alf_claim_id}) from Zendesk ticket {ticket_id}. "
                f"LLM failed: {llm_failed}"
            )

            # Real AI summary (best-effort — creation never fails on AI)
            refresh_claim_summary(claim, ticket_data)

            return Response({
                'message': 'Claim created successfully',
                'claim_id': claim.id,
                'alf_claim_id': claim.alf_claim_id,
                'zd_ticket_id': claim.zd_ticket_id,
                'llm_extraction_failed': claim.llm_extraction_failed,
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error processing Zendesk claim webhook: {e}", exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _handle_status_change(self, claim, custom_status_id):
        """Mirror a Zendesk custom-status change onto an existing claim and
        refresh the stored AI summary.

        - same-status → no-op (idempotent under Zendesk retries)
        - timeline entry (llm_summary='') is written in the same atomic block as
          the status save so a crash during the AI call never leaves the claim
          updated without a history entry
        - AI summary is best-effort: attempted after the transaction commits;
          on success the entry is back-filled
        - unresolved custom-status id (resolver returns the raw id as name) is
          silently dropped to prevent overwriting a real status name with a number
        """
        if not custom_status_id:
            return Response({'message': 'Ignored: no custom status in payload'},
                            status=status.HTTP_200_OK)

        resolved = resolve_custom_status(custom_status_id)
        new_status = resolved['name']

        # Fix 4: never overwrite a real named status with a raw numeric id.
        if new_status == str(custom_status_id) and not (claim.status or '').isdigit():
            logger.warning(
                f"Claim #{claim.id}: custom status {custom_status_id} could not be resolved; "
                f"keeping '{claim.status}'"
            )
            return Response({'error': 'Custom status could not be resolved',
                             'claim_id': claim.id}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if new_status == claim.status:
            return Response({'message': 'No change', 'claim_id': claim.id,
                             'status': claim.status}, status=status.HTTP_200_OK)

        old_status = claim.status

        # Fix 3: write the timeline entry in the same atomic block as the claim
        # save so a crash during the subsequent AI call never leaves the status
        # updated without a history entry.
        with transaction.atomic():
            claim.status = new_status
            claim.status_category = resolved['category']
            claim.status_changed_at = timezone.now()
            claim.save(update_fields=['status', 'status_category', 'status_changed_at', 'updated_at'])
            entry = ClaimUpdateTimeline.objects.create(
                claim=claim,
                zendesk_ticket_id=claim.zd_ticket_id or '',
                update_type='STATUS_CHANGE',
                changes_summary=json.dumps({'old_status': old_status, 'new_status': new_status}),
                llm_summary='',
            )
        logger.info(f"Claim #{claim.id} status mirrored: '{old_status}' -> '{new_status}'")

        # Client updates: when the claim first enters the configured submitted-
        # status, draft the initial "what we did" message (template-only here to
        # keep the webhook fast) AND schedule the first follow-up (cascade — the
        # rest are created as each one is sent). The trigger is matched by the
        # Zendesk custom-status ID (names can be renamed/duplicated); the legacy
        # name field is a fallback. When the claim closes/solves, stop the cadence.
        try:
            ss = SystemSettings.get_instance()
            trigger_id = (ss.client_report_trigger_status_id or '').strip()
            trigger_name = (ss.client_report_trigger_status or '').strip()
            entered_trigger = (
                (trigger_id and str(custom_status_id) == trigger_id)
                or (not trigger_id and trigger_name and new_status == trigger_name)
            )
            if (entered_trigger
                    and claim.client_report_sent_at is None and not claim.client_report_draft):
                from apps.communications.client_report import build_client_update_message
                from apps.communications.client_updates import schedule_next
                claim.client_report_draft = build_client_update_message(claim, polish=False)
                claim.save(update_fields=['client_report_draft', 'updated_at'])
                schedule_next(claim, timezone.now())
                logger.info(f"Client update drafted + first follow-up scheduled for claim #{claim.id}")
            # Stop the cadence when the claim is voided — solved, an open
            # dispute, or an actual refund (claim_is_closed covers all three).
            from apps.communications.client_updates import claim_is_closed, cancel_open_follow_ups
            if claim_is_closed(claim):
                cancel_open_follow_ups(claim)
        except Exception as e:
            logger.error(f"Client-update handling failed for claim #{claim.id}: {e}")

        ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        if ticket_data:
            ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)
            if refresh_claim_summary(claim, ticket_data):
                entry.llm_summary = claim.ai_summary
                entry.save(update_fields=['llm_summary'])

        return Response({'message': 'Status updated', 'claim_id': claim.id,
                         'status': new_status}, status=status.HTTP_200_OK)


class ZendeskFlightLookupView(APIView):
    """POST /api/integrations/zd/flight-lookup/
    Body: {ticket_id, refresh?: bool}

    LORA's first action button: looks up the flight on AeroDataBox,
    AI-cross-checks it against the client's report (selected airport, loss
    time/circumstances) and posts an internal note on the ticket. On
    not-found, the candidate rescue lists likely departures from the stated
    airport.

    Claim-first, fields-fallback: a linked claim supplies the flight details
    (and caches the result — the money saver). Without a claim, LORA reads
    the same structured Zendesk ticket fields the claim would have been built
    from (no ticket-text scraping) and runs a fresh, uncached lookup.
    Never touches claim.status. Auth: ZendeskSidebarAuth."""

    permission_classes = [AllowAny]

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed flight-lookup auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        if not ticket_id:
            return Response({'error_message': 'No ticket id received.'},
                            status=status.HTTP_200_OK)
        refresh = bool(request.data.get('refresh'))
        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()

        if claim:
            flight_details = claim.flight_details
        else:
            # Claimless ticket: read the same structured Zendesk fields the
            # claim would have been built from (never the ticket text).
            ticket_data = fetch_zendesk_ticket(ticket_id)
            if ticket_data is None:
                return Response(
                    {'error_message': "Couldn't read this ticket's fields from Zendesk."},
                    status=status.HTTP_200_OK)
            flight_details = compose_flight_details(ticket_data.get('custom_fields') or [])
            if not flight_details:
                return Response(
                    {'error_message': "This ticket's flight fields (Flight #, "
                                      "Date & Time, Airport) are empty."},
                    status=status.HTTP_200_OK)

        query = parse_flight_query(flight_details)
        if not query:
            return self._handle_no_number(claim, ticket_id, flight_details)

        if claim and claim.flight_data and not refresh:
            return Response({'flight': claim.flight_data, 'analysis': None,
                             'cached': True, 'note_posted': False},
                            status=status.HTTP_200_OK)

        try:
            raw_legs = lookup_flight(query['number'], query['date'])
        except FlightProviderNotConfigured:
            return Response(
                {'error': 'AeroDataBox API key is not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if raw_legs is None:
            return Response({'error': 'Flight data provider unavailable. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)

        if not raw_legs:
            return self._handle_not_found(claim, ticket_id, query, flight_details)

        flight = normalize_flight(raw_legs)
        analysis = analyze_flight_match(
            claim, flight,
            flight_details_text='' if claim else flight_details)
        verdict = derive_flight_verdict(True, analysis)
        flight['verdict'] = verdict

        if claim:
            claim.flight_data = flight
            claim.flight_data_updated_at = timezone.now()
            claim.save(update_fields=['flight_data', 'flight_data_updated_at', 'updated_at'])

        note_posted = self._post_note(ticket_id, format_flight_note(flight, analysis, verdict))

        if claim:
            ClaimUpdateTimeline.objects.create(
                claim=claim,
                zendesk_ticket_id=claim.zd_ticket_id,
                update_type='INFO_UPDATED',
                changes_summary=json.dumps({'flight_lookup': {**query, 'found': True,
                                                              'verdict': verdict['level']}}),
                llm_summary=analysis.summary if analysis else '',
            )
        subject = f"claim #{claim.id}" if claim else f"claimless ticket {ticket_id}"
        logger.info(f"Flight lookup for {subject}: {query['number']} {query['date']} "
                    f"found, verdict={verdict['level']}")
        return Response({'flight': flight, 'analysis': self._analysis_dict(analysis),
                         'verdict': verdict, 'cached': False, 'note_posted': note_posted,
                         'claimless': claim is None},
                        status=status.HTTP_200_OK)

    def _handle_no_number(self, claim, ticket_id, flight_details):
        """No flight number on the ticket: search departures by airport +
        date (narrowed to the form's airline when present) and let the AI
        rank the candidates against the client's report."""
        airport = parse_airport_hint(flight_details)
        date = parse_date_hint(flight_details)
        if not airport or not date:
            source = 'claim' if claim else "ticket's flight fields"
            return Response(
                {'error_message': f"Couldn't read a flight number and date from this {source}. "
                                  "Searching without a number needs at least the Airport "
                                  "and Date fields."},
                status=status.HTTP_200_OK)

        airline_code = parse_airline_hint(flight_details) or ''
        try:
            candidates = find_candidate_flights(
                airport, date, parse_time_hint(flight_details),
                airline_code=airline_code)
        except FlightProviderNotConfigured:
            return Response(
                {'error': 'AeroDataBox API key is not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if candidates is None:
            return Response({'error': 'Flight data provider unavailable. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)
        if not candidates:
            carrier = f'{airline_code} ' if airline_code else ''
            return Response(
                {'error_message': f"No flight number on this ticket, and no {carrier}"
                                  f"departures found from {airport} on {date}.",
                 'claimless': claim is None},
                status=status.HTTP_200_OK)

        analysis = analyze_flight_match(
            claim, None, candidates,
            flight_details_text='' if claim else flight_details)
        verdict = derive_flight_verdict(False, analysis, has_candidates=True)
        note = format_no_number_note(date, airport, candidates, analysis,
                                     verdict, airline_code)
        note_posted = self._post_note(ticket_id, note)
        if claim:
            ClaimUpdateTimeline.objects.create(
                claim=claim,
                zendesk_ticket_id=claim.zd_ticket_id,
                update_type='INFO_UPDATED',
                changes_summary=json.dumps({'flight_lookup': {
                    'number': None, 'date': date, 'airport': airport,
                    'found': False, 'candidates': len(candidates)}}),
                llm_summary=analysis.summary if analysis else '',
            )
        return Response({'error_message': 'No flight number on this ticket.',
                         'candidates': candidates,
                         'analysis': self._analysis_dict(analysis),
                         'verdict': verdict, 'claimless': claim is None,
                         'note_posted': note_posted}, status=status.HTTP_200_OK)

    def _handle_not_found(self, claim, ticket_id, query, flight_details):
        """Candidate rescue: when the flight number is not found, list likely
        departures from the client's stated airport so agents get leads
        instead of a dead end. Works with or without a claim — the hints come
        from the flight details either way."""
        from datetime import date as date_cls, timedelta

        error_message = f"No flight found for {query['number']} on {query['date']}."
        try:
            # Empty answers for old dates are usually the data plan's history
            # window, not proof the flight never existed (verified live:
            # Basic serves ~3 weeks back; beyond that comes back empty).
            if date_cls.fromisoformat(query['date']) < timezone.localdate() - timedelta(days=14):
                error_message += (" Note: this date may be beyond the AeroDataBox plan's "
                                  "history window — older flights need a higher plan.")
        except ValueError:
            pass
        airport = parse_airport_hint(flight_details)
        candidates = None
        if airport:
            try:
                candidates = find_candidate_flights(
                    airport, query['date'], parse_time_hint(flight_details))
            except FlightProviderNotConfigured:
                candidates = None

        if candidates:
            analysis = analyze_flight_match(
                claim, None, candidates,
                flight_details_text='' if claim else flight_details)
            verdict = derive_flight_verdict(False, analysis, has_candidates=True)
            note = format_candidates_note(
                query['number'], query['date'], airport, candidates, analysis, verdict)
            note_posted = self._post_note(ticket_id, note)
            if claim:
                ClaimUpdateTimeline.objects.create(
                    claim=claim,
                    zendesk_ticket_id=claim.zd_ticket_id,
                    update_type='INFO_UPDATED',
                    changes_summary=json.dumps({'flight_lookup': {
                        **query, 'found': False, 'candidates': len(candidates)}}),
                    llm_summary=analysis.summary if analysis else '',
                )
            return Response({'error_message': error_message, 'candidates': candidates,
                             'analysis': self._analysis_dict(analysis),
                             'verdict': verdict, 'claimless': claim is None,
                             'note_posted': note_posted}, status=status.HTTP_200_OK)

        verdict = derive_flight_verdict(False, None)
        note_posted = self._post_note(
            ticket_id, format_not_found_note(query['number'], query['date'], verdict))
        if claim:
            ClaimUpdateTimeline.objects.create(
                claim=claim,
                zendesk_ticket_id=claim.zd_ticket_id,
                update_type='INFO_UPDATED',
                changes_summary=json.dumps({'flight_lookup': {
                    **query, 'found': False, 'candidates': 0}}),
                llm_summary='',
            )
        return Response({'error_message': error_message, 'verdict': verdict,
                         'claimless': claim is None, 'note_posted': note_posted},
                        status=status.HTTP_200_OK)

    @staticmethod
    def _post_note(ticket_id, body):
        """Post an internal note; never let a Zendesk hiccup fail the lookup."""
        try:
            return bool(post_zendesk_comment(ticket_id, body, is_internal=True))
        except Exception as e:
            logger.warning(f"Flight note post failed for ticket {ticket_id}: {e}")
            return False

    @staticmethod
    def _analysis_dict(analysis):
        if not analysis:
            return None
        return {'summary': analysis.summary, 'mismatches': analysis.mismatches}


class ZendeskEmailCheckView(APIView):
    """POST /api/integrations/zd/email-check/
    Body: {ticket_id}

    The sidebar Email tab's button: checks the shared mailbox for new mail
    addressed to THIS ticket's email alias only (unread, last 2 days, never
    processed before). New mail gets AI categorization, an EmailLog row, an
    internal note on the ticket and additive ai_* tags. The rest of the
    inbox is untouched.

    Claim-first, fields-fallback: a linked claim caches the alias; without
    one the alias is read from the ticket's Email Alias custom field.
    Auth: ZendeskSidebarAuth."""

    permission_classes = [AllowAny]

    def post(self, request):
        from apps.communications.services import (
            EmailNotConfigured, InvalidAlias, check_email_for_ticket)

        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed email-check auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        if not ticket_id:
            return Response({'error_message': 'No ticket id received.'},
                            status=status.HTTP_200_OK)

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
        alias = claim.email_alias if claim else ''
        if not alias:
            ticket_data = fetch_zendesk_ticket(ticket_id)
            if ticket_data is None:
                return Response(
                    {'error_message': "Couldn't read this ticket's fields from Zendesk."},
                    status=status.HTTP_200_OK)
            alias = get_ticket_email_alias(ticket_data)
            if not alias:
                return Response(
                    {'error_message': 'This ticket has no email alias field — '
                                      'there is no address to check mail for.'},
                    status=status.HTTP_200_OK)
            if claim:
                claim.email_alias = alias
                claim.save(update_fields=['email_alias', 'updated_at'])

        try:
            results = check_email_for_ticket(ticket_id, claim, alias)
        except EmailNotConfigured:
            return Response(
                {'error': 'Mailbox (IMAP) credentials are not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except InvalidAlias:
            return Response(
                {'error_message': "The ticket's email alias doesn't look like an "
                                  "email address — fix the Email Alias field."},
                status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Email check failed for ticket {ticket_id}: {e}", exc_info=True)
            return Response({'error': 'Could not reach the mailbox. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)

        new_count = len(results['processed'])
        subject = f"claim #{claim.id}" if claim else f"claimless ticket {ticket_id}"
        logger.info(f"Email check for {subject} ({alias}): {new_count} new, "
                    f"{results['already_processed']} already processed, "
                    f"tags={results['tags_added']}")
        return Response({'message': f"{new_count} new email(s) processed",
                         'claimless': claim is None, **results},
                        status=status.HTTP_200_OK)


class ZendeskTicketEmailsView(APIView):
    """POST /api/integrations/zd/emails/  Body: {ticket_id}

    Read-only window onto the SAME stored EmailLog rows the LORA app shows —
    so the sidebar's Email tab lists the ticket's real email history, not just
    the last check's results. No doubling: one store, two views.
    Auth: ZendeskSidebarAuth."""

    permission_classes = [AllowAny]

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed emails-list auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        if not ticket_id:
            return Response({'emails': []}, status=status.HTTP_200_OK)

        # Match the claim's emails when linked, else by ticket id (claimless).
        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
        qs = (claim.emails.all() if claim
              else EmailLog.objects.filter(zd_ticket_id=ticket_id))
        emails = [{
            'id': e.id,
            'subject': e.subject,
            'from_email': e.from_email,
            'category': e.get_category_display(),
            'summary': e.ai_summary,
            'action_required': e.action_required,
            'auto_resolved': e.auto_resolved,
            'received_at': e.received_at.isoformat() if e.received_at else None,
        } for e in qs.order_by('-received_at')[:50]]
        return Response({'emails': emails, 'claimless': claim is None},
                        status=status.HTTP_200_OK)


class ZendeskClientUpdatesView(APIView):
    """POST /api/integrations/zd/updates/  Body: {ticket_id, action, kind, id, body}

    The Zendesk-side surface for client progress updates: a timeline of the
    initial "what we did" message + the day-2/5/11/21 follow-ups, with prepare/
    send/skip actions. Reads/writes the SAME LORA data the claim page uses (one
    store, two views). Auth: ZendeskSidebarAuth. Draft-for-approval — send always
    posts a PUBLIC reply only when the agent triggers it."""

    permission_classes = [AllowAny]

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed updates auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        if not claim:
            return Response({'claim': False, 'items': []}, status=status.HTTP_200_OK)

        action = (request.data.get('action') or 'list').strip()
        message = ''
        if action in ('send', 'prepare', 'skip', 'start'):
            message = self._act(request, claim, action)

        return Response({**self._timeline(claim), 'message': message}, status=status.HTTP_200_OK)

    def _act(self, request, claim, action) -> str:
        from django.utils import timezone
        from apps.communications import client_updates as cu

        if action == 'start':
            return ('Client updates started — the initial draft is ready and follow-ups scheduled.'
                    if cu.start_client_updates(claim) else 'Updates already started for this claim.')

        kind = (request.data.get('kind') or '').strip()
        body = (request.data.get('body') or '').strip()

        if kind == 'initial':
            if action == 'prepare':
                from apps.communications.client_report import build_client_update_message
                claim.client_report_draft = build_client_update_message(claim, polish=True)
                claim.save(update_fields=['client_report_draft', 'updated_at'])
                return 'Initial update regenerated.'
            if action == 'send':
                if claim.client_report_sent_at:
                    return 'The initial update was already sent.'
                if not body or not claim.zd_ticket_id:
                    return 'Nothing to send.'
                from apps.integrations.services import post_zendesk_comment
                if post_zendesk_comment(claim.zd_ticket_id, body, is_internal=False) is None:
                    return 'Could not post the reply to Zendesk.'
                claim.client_report_draft = body
                claim.client_report_sent_at = timezone.now()
                claim.save(update_fields=['client_report_draft', 'client_report_sent_at', 'updated_at'])
                return 'Initial update sent as a public reply.'
            return ''

        # follow-up
        update = claim.follow_up_updates.filter(id=request.data.get('id')).first()
        if not update:
            return 'Update not found.'
        if action == 'prepare':
            cu.prepare_follow_up(update)
            return f'{update.label} update drafted.'
        if action == 'skip':
            cu.skip_follow_up(update)
            return f'{update.label} update skipped.'
        if action == 'send':
            if update.state == 'SENT':
                return 'That update was already sent.'
            if cu.send_follow_up(update, body):
                return f'{update.label} update sent as a public reply.'
            return 'Could not post the reply to Zendesk.'
        return ''

    def _timeline(self, claim) -> dict:
        from django.utils import timezone
        now = timezone.now()
        items = []
        if claim.client_report_draft or claim.client_report_sent_at:
            items.append({
                'kind': 'initial', 'label': 'Initial update', 'due_label': 'On submission',
                'state': 'sent' if claim.client_report_sent_at else 'drafted',
                'body': claim.client_report_draft,
                'has_news': True,
                'sent_at': claim.client_report_sent_at.isoformat() if claim.client_report_sent_at else None,
                'can_send': bool(claim.zd_ticket_id),
            })
        for fu in claim.follow_up_updates.all().order_by('due_at'):
            items.append({
                'kind': 'followup', 'id': fu.id, 'label': fu.label,
                'milestone': fu.milestone, 'state': fu.state.lower(),
                'due_at': fu.due_at.isoformat(),
                'is_due': fu.state == 'SCHEDULED' and fu.due_at <= now,
                'has_news': fu.has_news, 'body': fu.draft_body,
                'sent_at': fu.sent_at.isoformat() if fu.sent_at else None,
                'can_send': bool(claim.zd_ticket_id),
            })
        return {'claim': True, 'alf_id': claim.alf_claim_id or '', 'items': items}
