"""
Refund Service for LORA.

Handles refund processing via PayPal API:
- Initiate refunds for claims
- Process webhook notifications from PayPal
- Sync with WooCommerce/WordPress refunds
- Idempotency protection for duplicate prevention
"""

import logging
import uuid
from datetime import timedelta
from typing import Dict, Any, Optional
from decimal import Decimal, InvalidOperation
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from apps.payments.models import Refund
from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.paypal_disputes_service import get_paypal_access_token, paypal_api_base
from apps.payments.woocommerce_service import (
    WooCommerceNotConfigured,
    create_woocommerce_refund,
)

logger = logging.getLogger(__name__)


def _capture_id_from_refund_resource(resource: dict) -> str:
    """Best-effort parent-capture id from a PayPal refund resource. Prefers the
    'up' HATEOAS link (which points at the capture, e.g.
    .../v2/payments/captures/{id}), then an older breakdown shape. '' if none."""
    for link in (resource.get('links') or []):
        if (link.get('rel') or '').lower() in ('up', 'capture'):
            seg = (link.get('href') or '').rstrip('/').rsplit('/', 1)[-1]
            if seg:
                return seg
    breakdown = resource.get('seller_payable_breakdown') or {}
    return (breakdown.get('payable_version') or {}).get('id') or ''


class RefundService:
    """
    Service for processing refunds via PayPal API.
    
    Usage:
        service = RefundService()
        result = service.initiate_refund(claim, amount, reason, user)
    """
    
    def __init__(self):
        # Mode-aware (SANDBOX by default) — never hit LIVE PayPal unless
        # SystemSettings.paypal_mode is explicitly 'live'. Was hardcoded to live.
        self.paypal_base_url = paypal_api_base()

    def initiate_refund(
        self,
        claim: Claim,
        amount: Decimal,
        reason: str,
        user,
        refund_type: str = 'FULL',
        capture_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Initiate a refund for a claim via PayPal API.
        
        Args:
            claim: The Claim to refund
            amount: Refund amount
            reason: Reason for the refund
            user: User initiating the refund
            refund_type: 'FULL' or 'PARTIAL'
            capture_id: PayPal capture ID (if known)
        
        Returns:
            Dict with success status, refund object, and message
        """
        try:
            # Get PayPal credentials
            settings = SystemSettings.get_instance()
            
            if not settings.paypal_client_id or not settings.paypal_secret:
                return {
                    'success': False,
                    'error': 'PayPal credentials not configured',
                }

            # A capture id is required to refund against — without it the PayPal
            # URL is malformed. Fail fast BEFORE creating a row or calling PayPal.
            # (The 'process' action does not yet source one; wiring the capture id
            # from the WooCommerce order / Zendesk is still pending — see review.)
            if not capture_id:
                return {
                    'success': False,
                    'error': 'No PayPal capture id available to refund against',
                }

            # Reserve under the over-refund cap (row-locked) BEFORE calling PayPal,
            # so two refunds for the same claim can't exceed price_paid. Shared
            # with issue_woocommerce_refund via _reserve_refund.
            refund, err = self._reserve_refund(
                claim, amount, external_source=Refund.SOURCE_LORA, reason=reason, user=user,
                pending_prefix='PENDING-', paypal_capture_id=capture_id or '',
                currency='USD', refund_type=refund_type,
            )
            if err:
                return err
            
            # Call PayPal API to process refund
            paypal_result = self._process_paypal_refund(
                capture_id=capture_id,
                amount=amount,
                currency='USD',
                note_to_payer=reason,
            )
            
            if not paypal_result.get('success'):
                refund.mark_failed(paypal_result.get('error', 'Unknown error'))
                return {
                    'success': False,
                    'error': paypal_result.get('error'),
                    'refund': refund,
                }
            
            # Update refund with PayPal response
            paypal_refund_id = paypal_result.get('refund_id')
            if not paypal_refund_id:
                refund.mark_failed('No refund ID from PayPal')
                return {
                    'success': False,
                    'error': 'No refund ID from PayPal',
                    'refund': refund,
                }
            
            # Persist the real PayPal refund id + response AND the PROCESSING
            # status in ONE save. (mark_processing() is field-scoped to
            # ['status','updated_at'], so calling it here would drop the two
            # assignments above — leaving the row on its placeholder PENDING-<uuid>
            # id, which the later PAYMENT.CAPTURE.REFUNDED webhook can't match,
            # creating a duplicate refund. This must write all four columns.)
            refund.paypal_refund_id = paypal_refund_id
            refund.metadata = paypal_result.get('metadata', {})
            refund.status = Refund.STATUS_PROCESSING
            refund.save(update_fields=['paypal_refund_id', 'metadata', 'status', 'updated_at'])

            logger.info(f"Refund initiated for Claim #{claim.id}: {paypal_refund_id}")
            
            return {
                'success': True,
                'refund': refund,
                'paypal_refund_id': paypal_refund_id,
                'message': f'Refund {paypal_refund_id} initiated successfully',
            }
            
        except Exception as e:
            logger.error(f"Error initiating refund: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
            }
    
    def _process_paypal_refund(
        self,
        capture_id: str,
        amount: Decimal,
        currency: str,
        note_to_payer: str,
    ) -> Dict[str, Any]:
        """
        Call PayPal API to process a refund.
        
        Args:
            capture_id: PayPal capture ID to refund
            amount: Refund amount
            currency: Currency code
            note_to_payer: Note to include with refund
        
        Returns:
            Dict with refund_id, status, and metadata
        """
        import urllib.request
        import urllib.error
        import json
        
        try:
            # Get access token
            access_token = get_paypal_access_token()
            if not access_token:
                return {
                    'success': False,
                    'error': 'Failed to get PayPal access token',
                }
            
            # Build refund request
            # Note: This is a simplified example - actual implementation depends on
            # whether you're refunding a capture, payment, or order
            url = f"{self.paypal_base_url}/v2/payments/captures/{capture_id}/refund"
            
            payload = {
                'amount': {
                    'currency_code': currency,
                    'value': str(amount),
                },
                'note_to_payer': note_to_payer,
            }
            
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {access_token}',
                },
                method='POST'
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                
                return {
                    'success': True,
                    'refund_id': result.get('id'),
                    'status': result.get('status'),
                    'metadata': result,
                }
                
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ''
            logger.error(f"PayPal API error: {e.code} - {error_body}")
            return {
                'success': False,
                'error': f'PayPal API error: {e.code}',
                'details': error_body,
            }
        except Exception as e:
            logger.error(f"PayPal refund error: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
            }
    
    @transaction.atomic
    def process_webhook_refund(
        self,
        event_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Process a refund webhook from PayPal.
        
        Handles PAYMENT.CAPTURE.REFUNDED and similar events.
        Implements idempotency to prevent duplicate processing.
        
        Args:
            event_data: Webhook event data from PayPal
        
        Returns:
            Dict with success status and refund object
        """
        try:
            # Extract refund ID from webhook
            refund_id = event_data.get('resource', {}).get('id')
            if not refund_id:
                return {
                    'success': False,
                    'error': 'No refund ID in webhook',
                }
            
            # Check for existing refund (idempotency)
            existing_refund = Refund.objects.filter(
                paypal_refund_id=refund_id
            ).first()
            
            if existing_refund:
                # Update existing refund with webhook data
                status = event_data.get('resource', {}).get('status', '').upper()
                if status == Refund.STATUS_COMPLETED:
                    existing_refund.mark_completed()
                elif status == Refund.STATUS_FAILED:
                    existing_refund.mark_failed('PayPal reported failure')
                
                logger.info(f"Updated existing refund {refund_id} from webhook")
                return {
                    'success': True,
                    'refund': existing_refund,
                    'message': 'Existing refund updated',
                }
            
            # Create new refund from webhook
            resource = event_data.get('resource', {})
            amount = Decimal(resource.get('amount', {}).get('value', '0'))
            currency = (resource.get('amount', {}).get('currency_code') or 'USD')

            # Resolve the capture id from the documented PAYMENT.CAPTURE.REFUNDED
            # payload: the refund resource's `links` carry an 'up' link to the
            # parent capture. Fall back to the older breakdown guess, then flag the
            # row for reconciliation rather than silently storing a blank capture.
            capture_id = _capture_id_from_refund_resource(resource)
            metadata = dict(event_data)
            if not capture_id:
                logger.warning(
                    "PayPal refund webhook %s has no resolvable capture id — "
                    "marking the refund for reconciliation", refund_id)
                metadata['needs_reconciliation'] = True

            # Atomic create guarded against the check-then-create race: two
            # concurrent deliveries of the same refund_id can both pass the
            # existence check above, so the second create() would hit the unique
            # paypal_refund_id constraint. Catch that and adopt the winner's row
            # (idempotent success, not a 500). The nested savepoint keeps the
            # enclosing @transaction.atomic usable for the re-fetch.
            try:
                with transaction.atomic():
                    refund = Refund.objects.create(
                        paypal_refund_id=refund_id,
                        paypal_capture_id=capture_id or '',
                        amount=amount,
                        currency=currency.upper()[:3],
                        status=Refund.STATUS_COMPLETED,
                        refund_type=Refund.TYPE_FULL,  # PayPal refund payload doesn't state full/partial
                        external_source=Refund.SOURCE_LORA,
                        reason='PayPal webhook notification',
                        metadata=metadata,
                    )
            except IntegrityError:
                existing = Refund.objects.filter(paypal_refund_id=refund_id).first()
                if existing is None:
                    raise  # unique violation on some other field — surface it
                logger.info(
                    f"PayPal refund {refund_id} created concurrently; "
                    f"adopting existing row #{existing.id}"
                )
                return {
                    'success': True,
                    'refund': existing,
                    'message': 'Existing refund updated',
                }

            logger.info(f"Created refund {refund_id} from webhook")

            return {
                'success': True,
                'refund': refund,
                'message': 'Refund created from webhook',
            }
            
        except Exception as e:
            logger.error(f"Error processing webhook refund: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
            }
    
    # Refund states that "reserve" money for the over-refund cap (everything
    # except an outright failure counts against the claim's remaining amount).
    RESERVING_STATUSES = (Refund.STATUS_PENDING, Refund.STATUS_PROCESSING,
                          Refund.STATUS_COMPLETED)

    def create_manual_refund(self, *, claim, amount, currency, refund_type, reason,
                             user, dedup_window_seconds: int = 60) -> Refund:
        """Record a MANUAL refund (one already issued out-of-band) as COMPLETED.
        Money-writing logic lives here, not in the view. Idempotent within a short
        window: a repeated identical submit (same claim/amount/reason) returns the
        existing row instead of inserting a duplicate money record — best-effort
        protection against a double-click (a manual single-user action)."""
        window_start = timezone.now() - timedelta(seconds=dedup_window_seconds)
        existing = Refund.objects.filter(
            claim=claim, amount=amount, reason=reason,
            external_source=Refund.SOURCE_MANUAL,
            created_at__gte=window_start,
        ).order_by('-created_at').first()
        if existing:
            logger.info(
                f"Manual refund de-duplicated within {dedup_window_seconds}s "
                f"window — returning existing row #{existing.id}")
            return existing
        return Refund.objects.create(
            claim=claim,
            paypal_refund_id=f'MANUAL-{uuid.uuid4().hex[:12]}',
            amount=amount,
            currency=currency or 'USD',
            status=Refund.STATUS_COMPLETED,
            refund_type=refund_type,
            external_source=Refund.SOURCE_MANUAL,
            reason=reason,
            created_by=user,
        )

    def _complete_woocommerce_refund(self, refund, wc_refund_id) -> None:
        """Stamp a reserved/created refund row as the COMPLETED WooCommerce refund
        (one shared place for the success-finalise both WC paths need): the real
        WC-{id} so the inbound webhook reconciles to it, status, processed_at, and
        the refund id in metadata."""
        refund.paypal_refund_id = f"{Refund.WC_PREFIX}{wc_refund_id}"
        refund.status = Refund.STATUS_COMPLETED
        refund.processed_at = timezone.now()
        refund.metadata['woocommerce_refund_id'] = wc_refund_id
        refund.save(update_fields=['paypal_refund_id', 'status',
                                   'processed_at', 'metadata', 'updated_at'])

    def _reserve_refund(self, claim, amount, *, external_source, reason, user,
                        pending_prefix, paypal_capture_id='', currency='USD',
                        refund_type=None, metadata=None):
        """Reserve `amount` against the claim's remaining refundable amount and
        create a PENDING Refund — atomically, under a row lock, so the cap can't
        be raced. Returns (refund, None) on success or (None, error_dict) if the
        over-refund cap would be breached or the claim vanished.

        Shared by both money-moving paths (initiate_refund / PayPal-direct and
        issue_woocommerce_refund) so the cap is enforced identically. The
        external gateway call MUST happen after this, OUTSIDE the transaction,
        so a timeout can't roll back (and hide) a refund the gateway made.
        """
        try:
            with transaction.atomic():
                locked = Claim.objects.select_for_update().get(pk=claim.pk)
                reserved = locked.refunds.filter(
                    status__in=self.RESERVING_STATUSES
                ).aggregate(t=Sum('amount'))['t'] or Decimal('0')
                if locked.price_paid:
                    remaining = locked.price_paid - reserved
                    if amount > remaining:
                        return None, {
                            'success': False,
                            'error': (f'Refund of {amount} exceeds the remaining '
                                      f'refundable amount ({remaining}).'),
                        }
                    resolved_type = refund_type or (
                        Refund.TYPE_PARTIAL if amount < locked.price_paid else Refund.TYPE_FULL)
                else:
                    resolved_type = refund_type or Refund.TYPE_FULL
                refund = Refund.objects.create(
                    claim=locked,
                    paypal_refund_id=f'{pending_prefix}{uuid.uuid4().hex[:12]}',
                    paypal_capture_id=paypal_capture_id,
                    amount=amount,
                    currency=currency,
                    status=Refund.STATUS_PENDING,
                    refund_type=resolved_type,
                    external_source=external_source,
                    reason=reason,
                    created_by=user,
                    metadata=metadata or {},
                )
        except Claim.DoesNotExist:
            return None, {'success': False, 'error': 'Claim not found.'}
        return refund, None

    def issue_woocommerce_refund(
        self,
        claim: Claim,
        amount: Decimal,
        reason: str,
        user,
    ) -> Dict[str, Any]:
        """LORA-initiated refund (the reverse lever, option B).

        Asks WooCommerce to refund the claim's order through PayPal; the
        existing cascade then closes Zendesk and notifies LORA's inbound
        webhook. Safe by construction:
        - hard cap: amount cannot exceed the claim's remaining (price_paid
          minus everything already reserved/paid);
        - a PENDING row is reserved inside a row-locked transaction BEFORE the
          external call, so two concurrent clicks cannot both pass the cap;
        - the external call runs OUTSIDE the transaction, so a timeout can
          never roll back (and thus hide) a refund the gateway actually made;
        - the reserved row carries the WooCommerce refund id on success, so
          the cascade's inbound webhook reconciles to it (one record).
        """
        order_id = (claim.woocommerce_id or '').strip()
        if not order_id:
            return {'success': False,
                    'error': 'This claim has no WooCommerce order id — cannot issue a refund.'}
        try:
            amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            return {'success': False, 'error': 'Invalid refund amount.'}
        if amount <= 0:
            return {'success': False, 'error': 'Refund amount must be positive.'}

        # Reserve atomically under a row lock so the cap can't be raced (shared
        # with the PayPal-direct path via _reserve_refund).
        refund, err = self._reserve_refund(
            claim, amount, external_source=Refund.SOURCE_WOOCOMMERCE, reason=reason, user=user,
            pending_prefix=Refund.WC_PENDING_PREFIX, currency='USD',
            metadata={'woocommerce_order_id': order_id, 'initiated_by': 'LORA'},
        )
        if err:
            return err

        # External call OUTSIDE the transaction.
        try:
            wc = create_woocommerce_refund(order_id, amount, reason)
        except WooCommerceNotConfigured as e:
            refund.mark_failed(str(e))
            return {'success': False, 'error': str(e), 'refund': refund}

        if not wc.get('success'):
            if wc.get('indeterminate'):
                # Money may have moved — keep the row PENDING (still counts
                # against the cap) for the inbound webhook to reconcile.
                refund.metadata['last_error'] = wc.get('error', '')
                refund.save(update_fields=['metadata', 'updated_at'])
                return {'success': False, 'error': wc.get('error'),
                        'indeterminate': True, 'refund': refund}
            refund.mark_failed(wc.get('error', 'WooCommerce refund failed'))
            return {'success': False, 'error': wc.get('error'), 'refund': refund}

        # Success — stamp the real WooCommerce refund id so the inbound webhook
        # (WC-{id}) reconciles to this row instead of creating a duplicate.
        self._complete_woocommerce_refund(refund, wc['refund_id'])
        logger.info(f"LORA issued WooCommerce refund {refund.paypal_refund_id} "
                    f"for Claim #{claim.id}")
        return {'success': True, 'refund': refund,
                'message': f'Refund issued via WooCommerce ({refund.paypal_refund_id})'}

    def _find_claim_for_refund(self, claim_number: str) -> Optional[Claim]:
        """Resolve the claim a refund notification refers to.

        Robust to whichever identifier WordPress sends as `claim_number`:
        the business ALF claim id ('ALF1234567') OR LORA's internal row id.
        ALF is tried first (it's the real cross-system identifier); a purely
        numeric value falls back to the internal pk.
        """
        if not claim_number:
            return None
        # alf_claim_id is unique + db_indexed. __iexact can't use that btree index,
        # but this runs once per inbound refund webhook (low volume), and the
        # case-insensitive match guards against any case drift in the id WordPress
        # sends — an acceptable trade-off here; don't promote it to a hot path.
        claim = Claim.objects.filter(alf_claim_id__iexact=claim_number).first()
        if claim:
            return claim
        if str(claim_number).isdigit():
            return Claim.objects.filter(id=int(claim_number)).first()
        return None

    def process_woocommerce_refund(
        self,
        claim_number: str,
        refund_amount: Decimal,
        refund_id: str,
        order_id: str,
        reason: str = '',
        currency: str = 'USD',
        refund_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a refund notification from WooCommerce/WordPress.

        Args:
            claim_number: Claim reference from WordPress (ALF id or internal id)
            refund_amount: Refund amount
            refund_id: WooCommerce refund ID
            order_id: WooCommerce order ID
            reason: Refund reason
            currency: Currency code from the payload (defaults USD)
            refund_type: 'FULL'/'PARTIAL' if WordPress states it; otherwise
                inferred from amount vs the claim's price_paid

        Returns:
            Dict with success status and refund object
        """
        try:
            claim = self._find_claim_for_refund(claim_number)
            if claim is None:
                return {
                    'success': False,
                    'error': f'Claim {claim_number} not found',
                }

            # Coerce the webhook amount (a str off request.data) to Decimal ONCE,
            # then use that single value for the reservation match, the price_paid
            # comparison, and the create — never persist the unvalidated raw value.
            try:
                amount = Decimal(str(refund_amount))
            except (InvalidOperation, TypeError, ValueError):
                return {'success': False,
                        'error': f'Invalid refund amount: {refund_amount!r}'}

            wc_id = f'{Refund.WC_PREFIX}{refund_id}'

            # Check for existing refund (idempotency under webhook retries)
            existing_refund = Refund.objects.filter(paypal_refund_id=wc_id).first()

            if existing_refund:
                logger.info(f"WooCommerce refund {refund_id} already processed")
                return {
                    'success': True,
                    'refund': existing_refund,
                    'message': 'Refund already processed',
                    'already_processed': True,
                }

            # Reconcile a LORA-initiated reservation: if this same refund was
            # issued from LORA (a PENDING WC-PENDING-* row for this claim and
            # amount), adopt it instead of creating a duplicate.
            reservation = Refund.objects.filter(
                claim=claim, external_source=Refund.SOURCE_WOOCOMMERCE,
                status__in=(Refund.STATUS_PENDING, Refund.STATUS_PROCESSING),
                paypal_refund_id__startswith=Refund.WC_PENDING_PREFIX,
                amount=amount,
            ).order_by('created_at').first()
            if reservation:
                self._complete_woocommerce_refund(reservation, refund_id)
                logger.info(f"Reconciled LORA reservation to WooCommerce refund {refund_id}")
                return {
                    'success': True,
                    'refund': reservation,
                    'message': 'WooCommerce refund reconciled',
                    'already_processed': False,
                }

            # Determine full vs partial: trust an explicit payload value,
            # else compare the refunded amount to what the client paid.
            resolved_type = (refund_type or '').upper()
            if resolved_type not in (Refund.TYPE_FULL, Refund.TYPE_PARTIAL):
                if claim.price_paid and amount < claim.price_paid:
                    resolved_type = Refund.TYPE_PARTIAL
                else:
                    resolved_type = Refund.TYPE_FULL

            # Atomic create guarded against the check-then-create race: two
            # concurrent deliveries of the same refund_id can both pass the
            # existence/reservation checks above, so the second create() would
            # hit the unique paypal_refund_id constraint. Catch that and adopt
            # the row the winner created — an idempotent success, not a 500.
            # The savepoint keeps any enclosing transaction usable for the
            # re-fetch after the IntegrityError.
            try:
                with transaction.atomic():
                    refund = Refund.objects.create(
                        claim=claim,
                        paypal_refund_id=wc_id,  # Prefix to distinguish from PayPal
                        amount=amount,
                        currency=(currency or 'USD').upper()[:3],
                        status=Refund.STATUS_COMPLETED,
                        refund_type=resolved_type,
                        external_source=Refund.SOURCE_WOOCOMMERCE,
                        reason=reason,
                        metadata={
                            'woocommerce_order_id': order_id,
                            'woocommerce_refund_id': refund_id,
                        },
                    )
            except IntegrityError:
                existing = Refund.objects.filter(paypal_refund_id=wc_id).first()
                if existing is None:
                    raise  # unique violation on some other field — surface it
                logger.info(
                    f"WooCommerce refund {refund_id} created concurrently; "
                    f"adopting existing row #{existing.id}"
                )
                return {
                    'success': True,
                    'refund': existing,
                    'message': 'Refund already processed',
                    'already_processed': True,
                }

            logger.info(f"Processed WooCommerce refund {refund_id} for Claim #{claim.id}")

            return {
                'success': True,
                'refund': refund,
                'message': 'WooCommerce refund processed',
                'already_processed': False,
            }

        except Exception as e:
            logger.error(f"Error processing WooCommerce refund: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
            }
    
    def get_refund_status(self, refund_id: str) -> Optional[Dict[str, Any]]:
        """
        Check refund status with PayPal API.
        
        Args:
            refund_id: PayPal refund ID
        
        Returns:
            Dict with status and details, or None if not found
        """
        import urllib.request
        import urllib.error
        import json
        
        try:
            access_token = get_paypal_access_token()
            if not access_token:
                return None
            
            url = f"{self.paypal_base_url}/v2/payments/refunds/{refund_id}"
            
            req = urllib.request.Request(
                url,
                headers={
                    'Authorization': f'Bearer {access_token}',
                },
                method='GET'
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                return {
                    'id': result.get('id'),
                    'status': result.get('status'),
                    'amount': result.get('amount'),
                    'metadata': result,
                }
                
        except Exception as e:
            logger.error(f"Error checking refund status: {e}")
            return None
