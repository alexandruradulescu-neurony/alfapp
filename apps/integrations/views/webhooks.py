"""Inbound webhook endpoints: the WooCommerce/Zendesk refund notification and
the Zendesk custom-status-change webhook.

Split out of the integrations views package (untangling refactor); both classes
are moved verbatim. The claim-status logic (_handle_status_change) is
intentionally NOT yet extracted into a service — that is a separate, higher-risk
change deferred for later.
"""

import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.claims.models import Claim, ClaimUpdateTimeline
from apps.payments.refund_service import RefundService
from apps.integrations.services import (
    tag_zendesk_ticket_as_refunded,
    add_refund_comment_to_zendesk,
    fetch_zendesk_ticket,
    fetch_zendesk_comments,
    resolve_custom_status,
)
from apps.integrations.briefing import refresh_claim_summary
from apps.integrations.views.auth import verify_webhook_secret

logger = logging.getLogger(__name__)

# Default currency used when a refund webhook payload omits one.
DEFAULT_CURRENCY = 'USD'

# Timeline update_type value for a mirrored Zendesk status change. Matches a
# member of ClaimUpdateTimeline.UPDATE_TYPE_CHOICES (no TYPE_* const on model).
TIMELINE_TYPE_STATUS_CHANGE = 'STATUS_CHANGE'


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
            secret_error = verify_webhook_secret(request, context='refund webhook')
            if secret_error:
                return secret_error

            data = request.data

            # Validate required fields
            required_fields = ['claim_number', 'refund_id', 'refund_amount']
            for field in required_fields:
                if field not in data:
                    logger.warning("Missing required field: %s", field)
                    return Response(
                        {'error': f'Missing required field: {field}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Process refund
            currency = str(data.get('currency', DEFAULT_CURRENCY))
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
                logger.error("Refund processing failed: %s", result.get('error'))
                return Response(
                    {'error': result.get('error', 'Processing failed')},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        except Exception as e:
            logger.error("Error processing refund webhook: %s", e, exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


def mirror_status_change(claim, custom_status_id) -> dict:
    """Mirror a Zendesk custom-status change onto an existing claim and refresh
    the stored AI summary. Returns a discriminated result dict (no DRF types, so
    this is unit-testable without HTTP); the webhook view maps each outcome to a
    Response:

        {'outcome': 'no_status'}
        {'outcome': 'unresolved', 'claim_id': ...}
        {'outcome': 'no_change',  'claim_id': ..., 'status': ...}
        {'outcome': 'updated',    'claim_id': ..., 'status': ...}

    Load-bearing behaviour (must not change):
    - same-status → no-op (idempotent under Zendesk retries)
    - the timeline entry (llm_summary='') is written in the SAME atomic block as
      the status save so a crash during the later AI call never leaves the claim
      updated without a history entry
    - the AI-summary back-fill runs AFTER the transaction and is deliberately NOT
      wrapped in try/except, so a failure propagates to the caller (the webhook
      returns 500 and Zendesk can retry)
    - an unresolved custom-status id (resolver echoes the raw id) is dropped to
      avoid overwriting a real status name with a number

    Lives in this module (not services.py) on purpose: it references the
    module-level resolve_custom_status / fetch_zendesk_ticket /
    fetch_zendesk_comments / refresh_claim_summary names, which the webhook tests
    patch at apps.integrations.views.webhooks.*.
    """
    if not custom_status_id:
        return {'outcome': 'no_status'}

    resolved = resolve_custom_status(custom_status_id)
    new_status = resolved['name']

    # Fix 4: never overwrite a real named status with a raw numeric id.
    if new_status == str(custom_status_id) and not (claim.status or '').isdigit():
        logger.warning(
            "Claim #%s: custom status %s could not be resolved; keeping '%s'",
            claim.id, custom_status_id, claim.status
        )
        return {'outcome': 'unresolved', 'claim_id': claim.id}

    if new_status == claim.status:
        return {'outcome': 'no_change', 'claim_id': claim.id, 'status': claim.status}

    old_status = claim.status
    old_category = claim.status_category

    # Capture the most-recent timeline note BEFORE we add a new entry, so the
    # AI call can diff against what was written last time.
    previous_note = (claim.updates.first().llm_summary if claim.updates.exists() else '')

    # Fix 3: write the timeline entry in the same atomic block as the claim save
    # so a crash during the subsequent AI call never leaves the status updated
    # without a history entry.
    with transaction.atomic():
        claim.status = new_status
        claim.status_category = resolved['category']
        claim.status_changed_at = timezone.now()
        claim.save(update_fields=['status', 'status_category', 'status_changed_at', 'updated_at'])
        entry = ClaimUpdateTimeline.objects.create(
            claim=claim,
            zendesk_ticket_id=claim.zd_ticket_id or '',
            update_type=TIMELINE_TYPE_STATUS_CHANGE,
            changes_summary=json.dumps({'old_status': old_status, 'new_status': new_status}),
            llm_summary='',
        )
    logger.info("Claim #%s status mirrored: '%s' -> '%s'", claim.id, old_status, new_status)

    # Deterministic status regression: a terminal (Solved) claim reopened to a
    # non-terminal stage is a red flag (e.g. an agent bouncing a refund dispute
    # back to 'Investigation initiated'). Only this unambiguous case is hard-flagged.
    # Compute ONCE and reuse for both the risk call and the transition headline.
    regressed = old_category == 'solved' and resolved['category'] != 'solved'
    if regressed:
        claim.register_risk(
            reasons=['status_regression'], level='at_risk',
            detail=f"Reopened from Solved to '{new_status}'")

    # Client-update cascade (draft initial message + schedule follow-ups on entry
    # into the submitted status; stop the cadence on close). Best-effort: the
    # broad except keeps a failure here from failing the status mirror, exactly as
    # before. The cadence logic itself now lives in the communications service.
    try:
        from apps.communications.client_updates import sync_cadence_for_status
        sync_cadence_for_status(claim, custom_status_id)
    except Exception as e:
        logger.error("Client-update handling failed for claim #%s: %s", claim.id, e)

    # Build transition headline (deterministic, always written).
    transition = (f"Status: {old_status or '—'} → {new_status}"
                  + (" (reopened)" if regressed else ""))
    delta = None
    ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
    if ticket_data:
        ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)
        delta = refresh_claim_summary(claim, ticket_data, previous_note=previous_note)
    entry.llm_summary = f"{transition}. {delta}" if delta else transition
    entry.save(update_fields=['llm_summary'])

    return {'outcome': 'updated', 'claim_id': claim.id, 'status': new_status}


def resync_ticket_status(claim) -> dict:
    """On-demand pull: fetch the ticket's CURRENT custom status live from Zendesk
    and mirror it onto the claim via the same path the webhook uses.

    This is the manual self-heal for a status webhook that was never delivered:
    the agent hits Regenerate in the sidebar, LORA pulls the live status, and
    mirror_status_change applies it (status, category, timeline, risk, cadence
    and — on an actual change — a fresh summary). Returns mirror_status_change's
    result dict, plus two pull-specific outcomes:

        {'outcome': 'no_ticket'}     — claim has no Zendesk ticket id
        {'outcome': 'fetch_failed'}  — the live ticket fetch failed (network/auth)

    Reuses mirror_status_change's no-op-on-same-status guard, so re-syncing an
    already-current claim is a cheap confirmation, not a rewrite.
    """
    if not claim.zd_ticket_id:
        return {'outcome': 'no_ticket'}
    ticket = fetch_zendesk_ticket(claim.zd_ticket_id)
    if not ticket:
        return {'outcome': 'fetch_failed'}
    return mirror_status_change(claim, ticket.get('custom_status_id'))


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

    # Zendesk custom status ID for "Investigation Initiated" — deploy/tenant
    # specific, sourced from settings (env ZENDESK_INVESTIGATION_STATUS_ID) so it
    # isn't a hardcoded literal in code.
    INVESTIGATION_STATUS_ID = settings.ZENDESK_INVESTIGATION_STATUS_ID
    permission_classes = [AllowAny]  # Webhook secret verification

    def post(self, request):
        """
        Process Zendesk claim creation or status-change webhook.
        """
        try:
            # Auth is mandatory: a webhook without the correct shared secret
            # is rejected before the body is parsed or anything is logged.
            secret_error = verify_webhook_secret(request, context='Zendesk webhook')
            if secret_error:
                return secret_error

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
                    "Ignoring webhook for ticket %s: no claim and "
                    "custom_status '%s' is not investigation initiated",
                    ticket_id, custom_status)
                return Response({
                    'message': 'Ignored: no claim and status is not investigation initiated',
                    'custom_status': custom_status,
                }, status=status.HTTP_200_OK)

            # New ticket at investigation-initiated status — delegate creation to
            # the shared service (also used by the on-demand backlog import) and
            # translate its outcome to a response. The view stays thin: secret
            # check, dispatch, and HTTP mapping only.
            from apps.integrations.services import create_claim_from_zendesk_ticket
            result = create_claim_from_zendesk_ticket(
                ticket_id,
                status_id=self.INVESTIGATION_STATUS_ID,
                webhook_requester_email=(data.get('requester') or {}).get('email', '') or '',
                webhook_requester_id=detail_data.get('requester_id') or '',
            )
            outcome = result['outcome']
            if outcome == 'fetch_failed':
                logger.error("Failed to fetch Zendesk ticket %s", ticket_id)
                return Response(
                    {'error': 'Failed to fetch Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            if outcome == 'ignored':
                logger.info(
                    "Ignoring webhook for ticket %s: no ALF claim number "
                    "in subject or Claim # field — not a claim-form ticket", ticket_id)
                return Response({
                    'message': 'Ignored: no ALF claim number — not a claim form ticket',
                }, status=status.HTTP_200_OK)
            if outcome == 'already_exists':
                existing = result['claim']
                logger.info(
                    "Webhook for ticket %s: Claim #%s "
                    "(%s) already exists.", ticket_id, existing.id, existing.alf_claim_id)
                return Response({
                    'message': 'Claim already exists',
                    'claim_id': existing.id,
                    'alf_claim_id': existing.alf_claim_id,
                }, status=status.HTTP_200_OK)

            claim = result['claim']
            logger.info(
                "Created Claim #%s (%s) from Zendesk "
                "ticket %s. LLM failed: %s",
                claim.id, claim.alf_claim_id, ticket_id, claim.llm_extraction_failed)
            return Response({
                'message': 'Claim created successfully',
                'claim_id': claim.id,
                'alf_claim_id': claim.alf_claim_id,
                'zd_ticket_id': claim.zd_ticket_id,
                'llm_extraction_failed': claim.llm_extraction_failed,
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error("Error processing Zendesk claim webhook: %s", e, exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _handle_status_change(self, claim, custom_status_id):
        """Thin HTTP wrapper: run the status mirror and map its result to a
        Response. All the logic (and its transaction/ordering/back-fill
        behaviour) lives in mirror_status_change(); this is wrapped by post()'s
        outer try/except, so an exception in the AI back-fill still yields 500."""
        result = mirror_status_change(claim, custom_status_id)
        outcome = result['outcome']
        if outcome == 'no_status':
            return Response({'message': 'Ignored: no custom status in payload'},
                            status=status.HTTP_200_OK)
        if outcome == 'unresolved':
            return Response({'error': 'Custom status could not be resolved',
                             'claim_id': result['claim_id']},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if outcome == 'no_change':
            return Response({'message': 'No change', 'claim_id': result['claim_id'],
                             'status': result['status']}, status=status.HTTP_200_OK)
        return Response({'message': 'Status updated', 'claim_id': result['claim_id'],
                         'status': result['status']}, status=status.HTTP_200_OK)


def _latest_public_comment_author(comments) -> str:
    """Email (lowercased) of the author of the most recent PUBLIC comment, or ''
    when there is no public comment or no author email. Comments arrive in
    chronological order, so the last public one is the newest."""
    for c in reversed(comments or []):
        if c.get('public'):
            return ((c.get('author') or {}).get('email') or '').strip().lower()
    return ''


def assess_client_reply(claim) -> dict:
    """Re-read the live Zendesk thread for a claim and re-run the EXISTING summary
    + risk assessment, because the client just replied INSIDE Zendesk — the one
    ticket event no other trigger reacts to.

    Reuses refresh_claim_summary, which rewrites the at-a-glance summary AND
    re-scores risk (hostile_language / refund_demanded / dispute_risk /
    negative_sentiment, plus the chargeback/lawyer/BBB keyword backstop). So an
    upset reply raises the at-risk flag — which already pauses automated client
    updates — while a normal reply just refreshes the summary.

    Stores nothing new: the reply is read live and assessed, never mirrored into
    the DB. Guard: only acts when the newest public comment is actually the
    client's (the Zendesk trigger is the primary filter; this defends against a
    misfire). Returns an outcome dict and never raises (the webhook must not make
    Zendesk retry)."""
    if not claim.zd_ticket_id:
        return {'outcome': 'no_ticket'}
    ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
    if not ticket_data:
        return {'outcome': 'fetch_failed'}
    comments = fetch_zendesk_comments(claim.zd_ticket_id) or []
    ticket_data['comments'] = comments
    client_email = (claim.client_email or '').strip().lower()
    author = _latest_public_comment_author(comments)
    # Skip when the newest public comment is clearly NOT the client (an agent or
    # institution reply). If the author email is unknown, proceed rather than risk
    # dropping a real client reply.
    if author and client_email and author != client_email:
        return {'outcome': 'not_client_reply'}
    try:
        refresh_claim_summary(claim, ticket_data)
    except Exception as e:
        logger.error("Client-reply assessment failed for claim #%s: %s", claim.id, e)
        return {'outcome': 'assess_failed', 'claim_id': claim.id}
    return {'outcome': 'assessed', 'claim_id': claim.id,
            'risk_level': claim.risk_level, 'risk_active': claim.risk_active}


class ZendeskClientReplyWebhookView(APIView):
    """Webhook fired by a Zendesk trigger the moment the CLIENT (requester) posts a
    public reply — the one ticket event no other trigger reacts to. LORA re-reads
    the thread and re-runs the existing sentiment/risk assessment, so an upset
    reply is flagged at-risk (which pauses automated client updates) and the
    summary reflects the reply. Nothing is mirrored into the DB.

    Auth: the same X-Webhook-Secret shared secret as the other webhooks.
    Body: {"ticket_id": "<id>"}.
    """

    permission_classes = [AllowAny]  # Webhook secret verification

    def post(self, request):
        try:
            secret_error = verify_webhook_secret(request, context='client-reply webhook')
            if secret_error:
                return secret_error

            data = request.data if isinstance(request.data, dict) else {}
            detail_data = data.get('detail') if isinstance(data.get('detail'), dict) else {}
            ticket_id = detail_data.get('id') or data.get('ticket_id')
            if not ticket_id:
                return Response({'error': 'Missing required field: ticket_id'},
                                status=status.HTTP_400_BAD_REQUEST)
            ticket_id = str(ticket_id)

            claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
            if not claim:
                # No claim for this ticket — nothing to assess. 200 so Zendesk
                # does not retry.
                return Response({'message': 'Ignored: no claim for ticket',
                                 'ticket_id': ticket_id}, status=status.HTTP_200_OK)

            result = assess_client_reply(claim)
            return Response({'message': 'Client reply assessed', **result},
                            status=status.HTTP_200_OK)
        except Exception as e:
            # A best-effort risk check must never trigger a Zendesk retry storm.
            logger.error("Client-reply webhook failed: %s", e)
            return Response({'message': 'Error handled', 'error': str(e)},
                            status=status.HTTP_200_OK)
