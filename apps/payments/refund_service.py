"""
Refund Service for LORA.

Handles refund processing via PayPal API:
- Initiate refunds for claims
- Process webhook notifications from PayPal
- Sync with WooCommerce/WordPress refunds
- Idempotency protection for duplicate prevention
"""

import logging
from typing import Dict, Any, Optional
from decimal import Decimal
from django.db import transaction
from apps.payments.models import Refund
from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.paypal_disputes_service import get_paypal_access_token

logger = logging.getLogger(__name__)


class RefundService:
    """
    Service for processing refunds via PayPal API.
    
    Usage:
        service = RefundService()
        result = service.initiate_refund(claim, amount, reason, user)
    """
    
    def __init__(self):
        self.paypal_base_url = "https://api.paypal.com"
    
    @transaction.atomic
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
            
            # Create refund record in PENDING state
            refund = Refund.objects.create(
                claim=claim,
                paypal_refund_id='',  # Will be set after PayPal confirms
                paypal_capture_id=capture_id or '',
                amount=amount,
                currency='USD',  # TODO: Get from claim or PayPal
                status='PENDING',
                refund_type=refund_type,
                external_source='LORA',
                reason=reason,
                created_by=user,
            )
            
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
            
            refund.paypal_refund_id = paypal_refund_id
            refund.metadata = paypal_result.get('metadata', {})
            refund.mark_processing()

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
                if status == 'COMPLETED':
                    existing_refund.mark_completed()
                elif status == 'FAILED':
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
            currency = resource.get('amount', {}).get('currency_code', 'USD')
            
            # Try to find associated claim
            # This depends on your webhook payload structure
            claim = None
            capture_id = resource.get('seller_payable_breakdown', {}).get(
                'payable_version', {}
            ).get('id')  # May need adjustment based on actual payload
            
            refund = Refund.objects.create(
                paypal_refund_id=refund_id,
                paypal_capture_id=capture_id or '',
                amount=amount,
                currency=currency,
                status='COMPLETED',
                refund_type='FULL',  # May need to determine from amount
                external_source='LORA',
                reason='PayPal webhook notification',
                metadata=event_data,
            )
            
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
    
    @transaction.atomic
    def process_woocommerce_refund(
        self,
        claim_number: str,
        refund_amount: Decimal,
        refund_id: str,
        order_id: str,
        reason: str = '',
    ) -> Dict[str, Any]:
        """
        Process a refund notification from WooCommerce/WordPress.
        
        Args:
            claim_number: Claim ID/reference from WordPress
            refund_amount: Refund amount
            refund_id: WooCommerce refund ID
            order_id: WooCommerce order ID
            reason: Refund reason
        
        Returns:
            Dict with success status and refund object
        """
        try:
            # Find claim by claim number (may need to adjust based on your schema)
            try:
                claim = Claim.objects.get(id=claim_number)
            except Claim.DoesNotExist:
                return {
                    'success': False,
                    'error': f'Claim {claim_number} not found',
                }
            
            # Check for existing refund (idempotency)
            existing_refund = Refund.objects.filter(
                paypal_refund_id=f'WC-{refund_id}'
            ).first()
            
            if existing_refund:
                logger.info(f"WooCommerce refund {refund_id} already processed")
                return {
                    'success': True,
                    'refund': existing_refund,
                    'message': 'Refund already processed',
                }
            
            # Create refund record
            refund = Refund.objects.create(
                claim=claim,
                paypal_refund_id=f'WC-{refund_id}',  # Prefix to distinguish from PayPal
                amount=refund_amount,
                currency='USD',
                status='COMPLETED',
                refund_type='FULL',  # May need to determine from amount
                external_source='WOOCOMMERCE',
                reason=reason,
                metadata={
                    'woocommerce_order_id': order_id,
                    'woocommerce_refund_id': refund_id,
                },
            )
            
            logger.info(f"Processed WooCommerce refund {refund_id} for Claim #{claim.id}")
            
            return {
                'success': True,
                'refund': refund,
                'message': 'WooCommerce refund processed',
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
