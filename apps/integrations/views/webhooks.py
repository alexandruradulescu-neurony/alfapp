"""Inbound webhook endpoints: the WooCommerce/Zendesk refund notification and
the Zendesk custom-status-change webhook.

Split out of the integrations views package (untangling refactor); both classes
are moved verbatim. The claim-status logic (_handle_status_change) is
intentionally NOT yet extracted into a service — that is a separate, higher-risk
change deferred for later.
"""

import hmac
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
from apps.config.models import SystemSettings
from apps.payments.refund_service import RefundService
from apps.integrations.services import (
    tag_zendesk_ticket_as_refunded,
    add_refund_comment_to_zendesk,
    fetch_zendesk_ticket,
    fetch_zendesk_comments,
    resolve_custom_status,
)
from apps.integrations.briefing import refresh_claim_summary

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
                "Claim #%s: custom status %s could not be resolved; "
                "keeping '%s'",
                claim.id, custom_status_id, claim.status
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
                update_type=TIMELINE_TYPE_STATUS_CHANGE,
                changes_summary=json.dumps({'old_status': old_status, 'new_status': new_status}),
                llm_summary='',
            )
        logger.info("Claim #%s status mirrored: '%s' -> '%s'", claim.id, old_status, new_status)

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
                logger.info("Client update drafted + first follow-up scheduled for claim #%s", claim.id)
            # Stop the cadence when the claim is voided — solved, an open
            # dispute, or an actual refund (claim_is_closed covers all three).
            from apps.communications.client_updates import claim_is_closed, cancel_open_follow_ups
            if claim_is_closed(claim):
                cancel_open_follow_ups(claim)
        except Exception as e:
            logger.error("Client-update handling failed for claim #%s: %s", claim.id, e)

        ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        if ticket_data:
            ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)
            if refresh_claim_summary(claim, ticket_data):
                entry.llm_summary = claim.ai_summary
                entry.save(update_fields=['llm_summary'])

        return Response({'message': 'Status updated', 'claim_id': claim.id,
                         'status': new_status}, status=status.HTTP_200_OK)
