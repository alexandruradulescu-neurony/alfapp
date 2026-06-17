"""
Zendesk integration views for LORA.
Provides API endpoints for Zendesk sidebar widget.
"""

import hmac
import json
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from django.db import transaction
from django.db.models import Count, Q

from apps.claims.models import Claim, ClaimUpdateTimeline
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Dispute
from apps.core.utils import get_client_ip
from django.utils import timezone

from apps.integrations.briefing import ALF_BUSINESS_CONTEXT

# Endpoint classes split into their own modules (views-untangling refactor).
# Re-exported so `from apps.integrations.views import X` and urls.py keep working.
from apps.integrations.views.auth import ZendeskSidebarAuth
from apps.integrations.views.flight import ZendeskFlightLookupView
from apps.integrations.views.webhooks import RefundWebhookView, ZendeskClaimWebhookView
from apps.integrations.views.email import ZendeskEmailCheckView, ZendeskTicketEmailsView

logger = logging.getLogger(__name__)

# Number of trailing chat turns replayed into the ticket-only chat context.
CHAT_HISTORY_TURNS = 10



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

        logger.info("Sidebar data request - email: %s, ticket_id: %s", customer_email or 'N/A', ticket_id or 'N/A')

        # Validate that at least one parameter is provided
        if not customer_email and not ticket_id:
            return Response(
                {'error': 'Either email or ticket_id parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Authenticate using sidebar secret token (per-IP brute-force throttle).
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(
            request, context=f"email: {customer_email or 'N/A'}, ticket_id: {ticket_id or 'N/A'}")
        if auth_error:
            return auth_error

        # Lookup claim
        claim = None
        try:
            if ticket_id:
                # First try to lookup by ticket_id
                claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
                if claim:
                    # Extract requester email from the claim
                    customer_email = claim.client_email
                    logger.info("Found claim #%s via ticket_id: %s", claim.id, ticket_id)
            
            # If no claim found yet, try lookup by email
            if not claim and customer_email:
                claim = Claim.objects.filter(client_email=customer_email).first()
                if claim:
                    logger.info("Found claim #%s via email: %s", claim.id, customer_email)

            if not claim:
                logger.info("No claim found for email: %s, ticket_id: %s", customer_email, ticket_id)
                return Response(
                    {'error': 'No claim found for this email or ticket_id'},
                    status=status.HTTP_404_NOT_FOUND
                )

        except Exception as e:
            logger.error("Error fetching claim for email: %s, ticket_id: %s: %s", customer_email, ticket_id, e)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            # Build enriched response data
            response_data = self._build_enriched_sidebar_data(claim, customer_email)
            
            logger.info("Sidebar data returned for claim #%s, email: %s", claim.id, customer_email)
            return Response(response_data)

        except Exception as e:
            logger.error("Error building sidebar data for claim #%s, email: %s: %s", claim.id, customer_email, e)
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
        # Total + unresolved (action_required=True AND auto_resolved=False) in a
        # single aggregation pass instead of two separate COUNT round-trips.
        counts = EmailLog.objects.filter(claim=claim).aggregate(
            total=Count('id'),
            unresolved=Count('id', filter=Q(action_required=True, auto_resolved=False)),
        )
        total_count = counts['total']
        unresolved_count = counts['unresolved']

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
        
        # Active disputes: any dispute not in a resolved state — single source
        # of truth on the model (don't duplicate the status list here).
        active_disputes_qs = Dispute.objects.filter(
            claim=claim,
            status__in=Dispute.ACTIVE_STATUSES
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
            category=EmailLog.CATEGORY_SUBMISSION_CONFIRMATION
        ).count()

        # Count GENERAL_CORRESPONDENCE emails (responses)
        general_correspondence = EmailLog.objects.filter(
            claim=claim,
            category=EmailLog.CATEGORY_GENERAL_CORRESPONDENCE
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
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='briefing')
        if auth_error:
            return auth_error

        from apps.ai.client import AIClient
        from apps.ai.schemas import BriefingSummary, NextSteps
        from apps.ai.exceptions import AIResponseValidationError
        from apps.claims.models import Claim
        from apps.integrations.services import build_claim_facts, build_ticket_thread

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        mode = str(data.get('mode', 'summary')).strip() or 'summary'
        logger.info("Briefing request for ticket_id: %s (mode=%s)", ticket_id or 'N/A', mode)

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
            logger.warning("Briefing AI validation failed for ticket %s (mode=%s): %s", ticket_id, mode, e)
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
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='draft')
        if auth_error:
            return auth_error

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
        logger.info("Draft request for ticket_id: %s (type=%s)", ticket_id or 'N/A', draft_type)

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
            logger.warning("Draft AI validation failed for ticket %s (%s): %s", ticket_id, draft_type, e)
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
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='chat')
        if auth_error:
            return auth_error

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

        logger.info("Sidebar chat for ticket_id: %s, claim: %s", ticket_id, claim.alf_claim_id)
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
            for msg in history[-CHAT_HISTORY_TURNS:]:
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
            logger.warning("Ticket-only chat AI validation failed for ticket %s: %s", ticket_id, e)
            return Response(
                {'answer': "I couldn't process that just now — please try again.",
                 'sources': []},
                status=status.HTTP_200_OK,
            )

        logger.info("Ticket-only sidebar chat for ticket_id: %s (no linked claim)", ticket_id)
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
        # Authenticate (now consistent with the other sidebar endpoints: per-IP
        # brute-force throttle, not a bare 403).
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='ticket-sync')
        if auth_error:
            return auth_error

        claim_id = request.data.get('claim_id')
        
        if not claim_id:
            return Response(
                {'error': 'claim_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from apps.integrations.services import create_zendesk_ticket_for_claim

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

            # Create Zendesk ticket (subject/comment/tags composed in the service)
            ticket_data = create_zendesk_ticket_for_claim(claim)
            
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
            logger.error("Error syncing claim %s to Zendesk: %s", claim_id, e)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ZendeskClientUpdatesView(APIView):
    """POST /api/integrations/zd/updates/  Body: {ticket_id, action, kind, id, body}

    The Zendesk-side surface for client progress updates: a timeline of the
    initial "what we did" message + the day-2/5/11/21 follow-ups, with prepare/
    send/skip actions. Reads/writes the SAME LORA data the claim page uses (one
    store, two views). Auth: ZendeskSidebarAuth. Draft-for-approval — send always
    posts a PUBLIC reply only when the agent triggers it."""

    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='updates')
        if auth_error:
            return auth_error

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
