"""AI-assist sidebar endpoints: briefing, email-draft and chat. Split out of the
integrations views package; classes moved verbatim. The AI client, schemas and
service helpers are imported lazily inside each handler (as before)."""

import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.integrations.briefing import ALF_BUSINESS_CONTEXT
from apps.integrations.views.auth import ZendeskSidebarAuth

logger = logging.getLogger(__name__)

# Number of trailing chat turns replayed into the ticket-only chat context.
CHAT_HISTORY_TURNS = 10


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
                # On an explicit Regenerate, FIRST pull the ticket's current
                # status live from Zendesk and mirror it (same path as the
                # webhook), so a missed status webhook self-heals on demand.
                # When that pull actually changes the status, the mirror has
                # already regenerated the summary from live ticket content, so
                # we don't rebuild it a second time.
                synced = {'outcome': 'skipped'}
                if refresh:
                    try:
                        from apps.integrations.views.webhooks import resync_ticket_status
                        synced = resync_ticket_status(claim)
                    except Exception as e:  # never let a sync hiccup 500 the sidebar
                        logger.warning("Status resync failed for ticket %s: %s", ticket_id, e)
                if synced.get('outcome') == 'updated':
                    claim.refresh_from_db()
                else:
                    ticket_data = {
                        'subject': data.get('subject', ''),
                        'description': data.get('description', ''),
                        'created_at': data.get('ticket_created_at', ''),
                        'comments': data.get('comments') or [],
                    }
                    refresh_claim_summary(claim, ticket_data)  # best-effort; persists
                    claim.refresh_from_db()
                # Rebuild facts so the sidebar shows the freshly-pulled status.
                facts = build_claim_facts(claim)
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
