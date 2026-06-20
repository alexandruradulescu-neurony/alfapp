"""Fetch the customer invoice (PDF) to attach to a PayPal dispute first response.

Primary path: the invoice link the WooCommerce order already stores
(`oblio_invoice_link`) — no extra credentials needed. Fallback: the Oblio API
(authorize → get document → download), used only if its credentials are set in
SystemSettings and the order has the invoice series + number. Read-only; never
moves money. Any failure returns None (the submit proceeds without it, logged).
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from apps.config.models import SystemSettings
from apps.payments.woocommerce_service import (
    WooCommerceNotConfigured, get_woocommerce_order_meta)

logger = logging.getLogger(__name__)

_OBLIO_BASE = 'https://www.oblio.eu/api'
_TIMEOUT = 15


def _download_pdf(url: str) -> Optional[bytes]:
    """GET a URL and return the bytes, or None on failure."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'LORA/1.0'}, method='GET')
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except Exception as e:
        logger.error("Invoice download failed for %s: %s", url[:80], e)
        return None


def _oblio_api_link(series, number) -> Optional[str]:
    """Resolve an invoice PDF link via the Oblio API. None if creds/inputs are
    missing or anything fails."""
    ss = SystemSettings.get_instance()
    email = (ss.oblio_email or '').strip()
    secret = (ss.oblio_secret or '').strip()
    cif = (ss.oblio_cif or '').strip()
    series = str(series or '').strip()
    number = str(number or '').strip()
    if not (email and secret and cif and series and number):
        return None
    try:
        data = urllib.parse.urlencode({'client_id': email, 'client_secret': secret}).encode('utf-8')
        req = urllib.request.Request(f'{_OBLIO_BASE}/authorize/token', data=data, method='POST')
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            token = json.loads(resp.read().decode('utf-8')).get('access_token')
        if not token:
            return None
        q = urllib.parse.urlencode({'cif': cif, 'seriesName': series, 'number': number})
        req = urllib.request.Request(f'{_OBLIO_BASE}/docs/invoice?{q}',
                                     headers={'Authorization': f'Bearer {token}'}, method='GET')
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        return (payload.get('data') or {}).get('link') or payload.get('link')
    except Exception as e:
        logger.error("Oblio API invoice lookup failed (series=%s number=%s): %s", series, number, e)
        return None


def _oblio_configured() -> bool:
    ss = SystemSettings.get_instance()
    return bool((ss.oblio_email or '').strip() and (ss.oblio_secret or '').strip()
                and (ss.oblio_cif or '').strip())


def _as_file(order_id: str, pdf: bytes) -> Dict[str, Any]:
    name = f'invoice_{order_id}.pdf'
    return {'name': name, 'filename': name, 'content': pdf, 'content_type': 'application/pdf'}


def fetch_invoice_for_claim(claim) -> Dict[str, Any]:
    """Fetch the claim's customer invoice and report exactly what happened, so
    the manager can VERIFY it (not blind-tick a box). Tries the link stored on
    the WooCommerce order first, then the Oblio API.

    Returns {'ok': bool, 'file': <multipart dict|None>, 'source': str, 'reason': str}.
    'source' (on success) names where it came from; 'reason' (on failure) is a
    plain-English explanation safe to show the manager.
    """
    order_id = str(getattr(claim, 'woocommerce_id', '') or '').strip()
    if not order_id:
        return {'ok': False, 'file': None, 'source': '',
                'reason': 'This claim has no WooCommerce order id, so there is no invoice to fetch.'}
    try:
        meta = get_woocommerce_order_meta(order_id)
    except WooCommerceNotConfigured:
        return {'ok': False, 'file': None, 'source': '',
                'reason': 'WooCommerce is not configured in Settings.'}

    link = str(meta.get('oblio_invoice_link') or '').strip()
    if link:
        pdf = _download_pdf(link)
        if pdf and pdf[:5].startswith(b'%PDF'):
            return {'ok': True, 'file': _as_file(order_id, pdf), 'reason': '',
                    'source': "the invoice link saved on the WooCommerce order"}

    api_link = _oblio_api_link(meta.get('oblio_invoice_series_name'),
                               meta.get('oblio_invoice_number'))
    if api_link:
        pdf = _download_pdf(api_link)
        if pdf and pdf[:5].startswith(b'%PDF'):
            return {'ok': True, 'file': _as_file(order_id, pdf), 'reason': '',
                    'source': "the Oblio API"}

    # Build a precise failure reason.
    if not link and not _oblio_configured():
        reason = ("No invoice link is saved on this WooCommerce order, and Oblio API "
                  "credentials aren't set in Settings — add them to enable the fallback.")
    elif link:
        reason = ("The invoice link on the order didn't return a usable PDF"
                  + (", and the Oblio API fallback didn't either."
                     if _oblio_configured() else " (no Oblio fallback is configured)."))
    else:
        reason = ("Couldn't fetch the invoice from Oblio — check the email/secret/CIF and "
                  "that this order has an invoice series & number.")
    logger.warning("Invoice fetch failed for order %s (claim #%s): %s",
                   order_id, getattr(claim, 'id', '?'), reason)
    return {'ok': False, 'file': None, 'source': '', 'reason': reason}


def fetch_invoice_pdf_for_claim(claim) -> Optional[Dict[str, Any]]:
    """Multipart-ready invoice file dict for the claim, or None. Thin wrapper
    over fetch_invoice_for_claim (used by the submission file builder)."""
    return fetch_invoice_for_claim(claim).get('file')
