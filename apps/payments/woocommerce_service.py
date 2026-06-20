"""
WooCommerce REST API client for LORA-initiated refunds.

LORA's reverse refund flow (option B): instead of calling PayPal itself, LORA
asks WooCommerce to refund the order through the original payment method
(`api_refund=true`). WooCommerce moves the money via PayPal, closes the
Zendesk ticket, and notifies LORA's inbound webhook — LORA pulls one lever
and the existing cascade does the rest. WooCommerce is the single executor,
so LORA can never double-pay.
"""

import base64
import json
import logging
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Dict, Any

from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)


class WooCommerceNotConfigured(Exception):
    """Store URL or REST API credentials are missing from SystemSettings."""


def _wc_credentials():
    ss = SystemSettings.get_instance()
    url = (ss.woocommerce_store_url or '').strip().rstrip('/')
    key = (ss.woocommerce_consumer_key or '').strip()
    secret = (ss.woocommerce_consumer_secret or '').strip()
    if not (url and key and secret):
        raise WooCommerceNotConfigured(
            'WooCommerce store URL and REST API credentials are not configured '
            'in System settings.')
    # Credentials are sent via HTTP Basic auth, so the store URL MUST be HTTPS
    # (except explicit local/dev hosts) — refuse to leak them over plaintext.
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or '').lower()
    is_local = host in ('localhost', '127.0.0.1', '::1') or host.endswith('.local')
    if not url.lower().startswith('https://') and not is_local:
        raise WooCommerceNotConfigured(
            'WooCommerce store URL must use HTTPS — credentials are sent via HTTP '
            'Basic auth and must not go over plaintext.')
    return url, key, secret


def create_woocommerce_refund(
    order_id: str,
    amount: Decimal,
    reason: str = '',
    timeout: int = 30,
) -> Dict[str, Any]:
    """Create a refund on a WooCommerce order, pushed through the gateway.

    POST {store}/wp-json/wc/v3/orders/{order_id}/refunds
    body: {amount, reason, api_refund: true}  (api_refund => refund via PayPal)
    Auth: HTTP Basic with the consumer key/secret over HTTPS.

    Returns:
        {'success': True, 'refund_id': <wc id>, 'raw': {...}} on success;
        {'success': False, 'error': <message>} on a definite failure;
        {'success': False, 'error': ..., 'indeterminate': True} when the
        outcome is unknown (timeout / network) — the caller must NOT auto-retry
        (the money may already have moved) and should leave the record pending
        for the inbound webhook to reconcile.

    Raises:
        WooCommerceNotConfigured when credentials are absent.
    """
    base_url, key, secret = _wc_credentials()
    url = f"{base_url}/wp-json/wc/v3/orders/{order_id}/refunds"
    token = base64.b64encode(f"{key}:{secret}".encode('utf-8')).decode('ascii')
    payload = json.dumps({
        'amount': str(amount),
        'reason': reason or '',
        'api_refund': True,  # refund through the original payment gateway (PayPal)
    }).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Authorization': f'Basic {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'LORA-refunds/1.0',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        refund_id = body.get('id')
        if not refund_id:
            return {'success': False, 'error': 'WooCommerce returned no refund id.'}
        logger.info(f"WooCommerce refund {refund_id} created on order {order_id}")
        return {'success': True, 'refund_id': str(refund_id), 'raw': body}
    except urllib.error.HTTPError as e:
        # A definite rejection from WooCommerce (e.g. amount too high, gateway
        # declined). Money did NOT move.
        try:
            detail = json.loads(e.read().decode('utf-8')).get('message', '')
        except Exception:
            detail = ''
        msg = f"WooCommerce refused the refund (HTTP {e.code}){': ' + detail if detail else ''}"
        logger.error(f"WooCommerce refund failed for order {order_id}: {msg}")
        return {'success': False, 'error': msg}
    except Exception as e:
        # Timeout / connection error: the refund MAY have gone through. Do not
        # let the caller retry automatically.
        logger.error(f"WooCommerce refund indeterminate for order {order_id}: {e}",
                     exc_info=True)
        return {'success': False,
                'error': 'Could not confirm the refund with WooCommerce. Check the '
                         'order in WooCommerce before retrying.',
                'indeterminate': True}


def list_woocommerce_refunds(order_id: str, timeout: int = 15) -> Dict[str, Any]:
    """Read the refunds WooCommerce has recorded against an order (read-only).

    GET {store}/wp-json/wc/v3/orders/{order_id}/refunds

    This is the pull side of the reverse flow: when LORA's refund call timed out
    (indeterminate) and left a row PENDING, this lets LORA ask WooCommerce what
    actually happened and reconcile against the truth — no money is moved.

    Returns:
        {'success': True, 'refunds': [{'id', 'amount', 'reason'}, ...]} on success
            (amount is a positive string as WooCommerce returns it);
        {'success': False, 'error': ...} on a definite failure;
        {'success': False, 'error': ..., 'indeterminate': True} on timeout/network.

    Raises:
        WooCommerceNotConfigured when credentials are absent.
    """
    base_url, key, secret = _wc_credentials()
    url = f"{base_url}/wp-json/wc/v3/orders/{order_id}/refunds"
    token = base64.b64encode(f"{key}:{secret}".encode('utf-8')).decode('ascii')
    req = urllib.request.Request(
        url,
        headers={'Authorization': f'Basic {token}', 'User-Agent': 'LORA-refunds/1.0'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        refunds = [
            {'id': str(r.get('id')), 'amount': str(r.get('amount') or '0'),
             'reason': r.get('reason') or ''}
            for r in (body or []) if r.get('id') is not None
        ]
        logger.info(f"WooCommerce order {order_id} has {len(refunds)} refund(s)")
        return {'success': True, 'refunds': refunds}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode('utf-8')).get('message', '')
        except Exception:
            detail = ''
        msg = f"WooCommerce could not list refunds (HTTP {e.code}){': ' + detail if detail else ''}"
        logger.error(f"List refunds failed for order {order_id}: {msg}")
        return {'success': False, 'error': msg}
    except Exception as e:
        logger.error(f"List refunds indeterminate for order {order_id}: {e}", exc_info=True)
        return {'success': False,
                'error': 'Could not reach WooCommerce to check the refund. Try again shortly.',
                'indeterminate': True}
