"""Zendesk sidebar 'Form filling' endpoints: drive Browser Use to fill an
institution form from a claim, with a human approval gate before submit. Every
attempt is a FormFill row. Auth: ZendeskSidebarAuth (bearer token)."""
import base64
import hashlib
import hmac
import json
import logging
from urllib.parse import urlparse

import requests
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.integrations import browser_use
from apps.integrations.form_fill_service import (
    build_agent_context, build_form_secrets, build_fill_task, SUBMIT_TASK, form_host,
    MAX_FILL_STEPS)
from apps.integrations.models import FormFill
from apps.integrations.services import (
    post_zendesk_comment, fetch_zendesk_ticket, fetch_zendesk_comments,
    fetch_zendesk_attachment, get_ticket_email_alias)
from apps.integrations.views.auth import ZendeskSidebarAuth

logger = logging.getLogger(__name__)


def _claim_for(ticket_id):
    return Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None


def _is_allowed_attachment_url(url: str) -> bool:
    """Only fetch attachment URLs that point at our own Zendesk tenant over https
    (Zendesk serves attachments from <subdomain>.zendesk.com and *.zdusercontent.com).
    Blocks SSRF / credential-leak via an arbitrary image_url."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != 'https' or not p.hostname:
        return False
    host = p.hostname.lower()
    sub = (SystemSettings.get_instance().zd_subdomain or '').lower()
    allowed = set()
    if sub:
        allowed.add(f'{sub}.zendesk.com')
    return (host in allowed) or host.endswith('.zdusercontent.com')


def _proxy_screenshot(session_id: str) -> str:
    """Fetch the latest screenshot from Browser Use and return it as a data: URL so
    the sidebar loads it same-origin (no CSP/whitelist change). '' if none."""
    try:
        src = browser_use.latest_screenshot_url(session_id)
        if not src:
            return ''
        r = requests.get(src, timeout=30)
        if r.status_code >= 400:
            return ''
        content = r.content[:6 * 1024 * 1024]   # cap ~6MB
        ctype = r.headers.get('Content-Type', 'image/png')
        if not ctype.startswith('image/'):
            ctype = 'image/png'
        b64 = base64.b64encode(content).decode()
        return f'data:{ctype};base64,{b64}'
    except Exception as e:
        logger.warning('Screenshot proxy failed: %s', e)
        return ''


def _post_status_note(ff, headline, screenshot=''):
    """Post an internal Zendesk note so the agent is told ON THE TICKET that a fill needs
    attention — even if they never opened (or have since closed) the Form filling tab."""
    if not ff.claim.zd_ticket_id:
        return
    img = f'<p><img src="{screenshot}" alt="filled form" /></p>' if screenshot else ''
    note = f'<p>\U0001F4DD <strong>{headline}</strong></p>{img}'
    try:
        post_zendesk_comment(ff.claim.zd_ticket_id, comment_body='', is_internal=True, html_body=note)
        ff.posted_to_ticket = True
        ff.save(update_fields=['posted_to_ticket', 'updated_at'])
    except Exception as e:
        logger.warning('Form-fill status note post failed for ticket %s: %s', ff.claim.zd_ticket_id, e)


def _finalize_form_fill(ff, st, screenshot=''):
    """Apply a Browser Use session state to the FormFill AND notify the ticket. Shared by
    the status poll and the webhook so a fill is recorded the same way regardless of what
    triggered the check. Only transitions OUT of non-terminal states, so it is safe to
    call repeatedly (e.g. a poll and a webhook racing on the same session)."""
    bu_status = st.get('status', '')
    if ff.status == FormFill.STATUS_STARTED:
        if bu_status == 'idle':
            ff.status = FormFill.STATUS_FILLED
            ff.filled_at = timezone.now()
            ff.result_output = str(st.get('output', ''))[:5000]
            ff.save(update_fields=['status', 'filled_at', 'result_output', 'updated_at'])
            # Notify on the ticket that it needs review (the screenshot only if opted in).
            _post_status_note(ff, 'LORA filled a form — review it, then Approve &amp; submit in the '
                                  'Form filling tab.', screenshot if ff.post_screenshot else '')
        elif bu_status in ('error', 'failed', 'timed_out', 'stopped'):
            ff.status = FormFill.STATUS_FAILED
            ff.error = str(st.get('output', '') or 'Session ended before the fill completed.')[:2000]
            ff.save(update_fields=['status', 'error', 'updated_at'])
            _post_status_note(ff, 'LORA could not finish filling a form — open the Form filling tab '
                                  'to take over.')
    elif ff.status == FormFill.STATUS_SUBMITTING:
        # The submit follow-up runs without keep_alive, so it ends 'stopped' (not idle).
        if bu_status in ('stopped', 'idle'):
            ff.status = FormFill.STATUS_SUBMITTED
            ff.submitted_at = timezone.now()
            ff.result_output = str(st.get('output', ''))[:5000]
            ff.save(update_fields=['status', 'submitted_at', 'result_output', 'updated_at'])
            if ff.post_screenshot and screenshot and ff.claim.zd_ticket_id:
                note = (f'<p>\U0001F4DD <strong>Form filled &amp; submitted via LORA</strong></p>'
                        f'<p><img src="{screenshot}" alt="form submission confirmation" /></p>')
                try:
                    post_zendesk_comment(ff.claim.zd_ticket_id, comment_body='',
                                         is_internal=True, html_body=note)
                    ff.posted_to_ticket = True
                    ff.save(update_fields=['posted_to_ticket', 'updated_at'])
                except Exception as e:
                    logger.warning('Form-fill note post failed for ticket %s: %s',
                                   ff.claim.zd_ticket_id, e)
        elif bu_status in ('error', 'failed', 'timed_out'):
            ff.status = FormFill.STATUS_FAILED
            ff.error = str(st.get('output', '') or 'Session ended before the submit completed.')[:2000]
            ff.save(update_fields=['status', 'error', 'updated_at'])
            _post_status_note(ff, 'LORA could not complete the form submission — open the Form '
                                  'filling tab.')


def _verify_webhook_signature(secret: str, body: bytes, signature: str, timestamp: str) -> bool:
    """Verify Browser Use's webhook signature (per their docs). The signed message is
    '{timestamp}.{canonical_json}', where canonical_json is the parsed body re-serialized
    with sorted keys and compact separators — NOT the raw body. HMAC-SHA256, hex digest.
    Headers: X-Browser-Use-Signature and X-Browser-Use-Timestamp (unix seconds).

    We deliberately do NOT enforce their 300s freshness window: _finalize_form_fill is
    idempotent (it only transitions out of non-terminal states), so a replay is harmless,
    and skipping the window lets legitimate retries / dashboard resends through."""
    if not secret or not signature or not timestamp:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    message = f"{timestamp}.{json.dumps(payload, separators=(',', ':'), sort_keys=True)}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class FormFillStartView(APIView):
    """POST /api/integrations/zd/form-fill/start — start a fill (does NOT submit)."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-start')
        if auth_error:
            return auth_error
        if not SystemSettings.get_instance().form_filling_enabled:
            return Response({'error': 'Form filling is turned off in Settings.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ticket_id = str(request.data.get('ticket_id', '')).strip()
        url = str(request.data.get('url', '')).strip()
        post_screenshot = bool(request.data.get('post_screenshot', False))
        if not url:
            return Response({'error': 'Paste the form URL first.'}, status=status.HTTP_400_BAD_REQUEST)
        claim = _claim_for(ticket_id)
        if not claim:
            return Response({'error': 'Link a LORA claim to this ticket to use form filling.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Fetch ticket context once — used for both the alias cache and the agent context.
        ticket_data = {}
        try:
            ticket_data = fetch_zendesk_ticket(ticket_id) or {}
            ticket_data['comments'] = fetch_zendesk_comments(ticket_id)
        except Exception as e:
            logger.warning('Form-fill: could not fetch ticket %s context: %s', ticket_id, e)
        if not claim.email_alias:
            alias = ''
            try:
                alias = get_ticket_email_alias(ticket_data) if ticket_data else ''
            except Exception:
                alias = ''
            if alias:
                claim.email_alias = alias
                claim.save(update_fields=['email_alias', 'updated_at'])

        host = form_host(url)
        secrets = build_form_secrets(claim, host)
        context = build_agent_context(claim, ticket_data)
        task = build_fill_task(url, secrets, context)

        form_fill_id = request.data.get('form_fill_id')
        image_url = str(request.data.get('image_url', '')).strip()
        image_filename = str(request.data.get('image_filename', '')).strip() or 'attachment'
        image_bytes = None
        image_ctype = 'application/octet-stream'

        if form_fill_id:
            ff = FormFill.objects.filter(id=form_fill_id, claim=claim).first()
            if not ff:
                return Response({'error': 'Uploaded image not found for this claim.'},
                                status=status.HTTP_400_BAD_REQUEST)
            ff.form_url = url
            ff.status = FormFill.STATUS_STARTED
            ff.post_screenshot = post_screenshot
            ff.save(update_fields=['form_url', 'status', 'post_screenshot', 'updated_at'])
            if ff.image:
                image_bytes = ff.image.read()
                image_ctype = 'application/octet-stream'
        else:
            ff = FormFill.objects.create(
                claim=claim, form_url=url, status=FormFill.STATUS_STARTED,
                created_by=request.user if request.user.is_authenticated else None,
                posted_to_ticket=False, post_screenshot=post_screenshot)
            if image_url and _is_allowed_attachment_url(image_url):
                try:
                    image_bytes, image_ctype = fetch_zendesk_attachment(image_url)
                    ff.image_source = FormFill.IMAGE_SOURCE_TICKET
                    ff.image_name = image_filename
                    ff.image.save(image_filename, ContentFile(image_bytes), save=True)
                except Exception as e:
                    logger.warning('Ticket attachment fetch failed: %s', e)
            elif image_url:
                logger.warning('Rejected non-Zendesk image_url for form fill: %s', image_url)

        if image_bytes and ff.image_name:
            task += f"\nA file named '{ff.image_name}' has been uploaded to this session — attach it to the form's photo/file upload field."

        try:
            session = browser_use.create_session(task=task, secrets=secrets, allowed_domains=[host])
        except browser_use.BrowserUseError as e:
            ff.status = FormFill.STATUS_FAILED
            ff.error = str(e)
            ff.save()
            return Response({'error': str(e), 'form_fill_id': ff.id},
                            status=status.HTTP_502_BAD_GATEWAY)

        ff.browser_use_session_id = session.get('id', '')
        ff.save(update_fields=['browser_use_session_id', 'updated_at'])

        if image_bytes:
            try:
                browser_use.upload_file(session.get('id', ''), filename=ff.image_name,
                                        content=image_bytes, content_type=image_ctype)
            except browser_use.BrowserUseError as e:
                logger.warning('Form-fill image upload to session failed: %s', e)

        return Response({'form_fill_id': ff.id, 'session_id': session.get('id', ''),
                         'live_url': session.get('live_url', ''), 'status': 'started',
                         'post_screenshot': post_screenshot}, status=status.HTTP_200_OK)


class FormFillStatusView(APIView):
    """POST {session_id} -> {status, screenshot(dataURL|''), bu_status}."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-status')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        try:
            st = browser_use.get_session(session_id)
        except browser_use.BrowserUseError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        bu_status = st.get('status', '')
        step_count = st.get('step_count')
        screenshot = _proxy_screenshot(session_id)
        if ff and ff.status == FormFill.STATUS_STARTED and bu_status == 'running' \
                and isinstance(step_count, int) and step_count > MAX_FILL_STEPS:
            # Cost guard: every step is a billed LLM call. A fill still running past the
            # budget is grinding (e.g. stuck on a control) — stop it and let the human
            # finish in the live view rather than keep paying.
            try:
                browser_use.stop_session(session_id)
            except browser_use.BrowserUseError:
                pass
            ff.status = FormFill.STATUS_FAILED
            ff.error = (f'Stopped after {step_count} steps (budget {MAX_FILL_STEPS}) to limit '
                        f'cost. Open the live view to finish it by hand.')[:2000]
            ff.save(update_fields=['status', 'error', 'updated_at'])
            bu_status = 'stopped'
        elif ff:
            _finalize_form_fill(ff, st, screenshot)
        return Response({'status': ff.status if ff else bu_status, 'bu_status': bu_status,
                         'screenshot': screenshot, 'step_count': step_count},
                        status=status.HTTP_200_OK)


class FormFillSubmitView(APIView):
    """POST {session_id} — kick off the submit (SUBMITTING); the status poll finalizes
    it to SUBMITTED and captures the confirmation. Returns {status: 'submitting'}."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-submit')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        if ff is None or ff.status not in (FormFill.STATUS_FILLED,):
            if ff and ff.status in (FormFill.STATUS_SUBMITTING, FormFill.STATUS_SUBMITTED):
                return Response({'status': ff.status.lower()}, status=status.HTTP_200_OK)
            return Response({'error': 'This fill is not ready to submit.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ff.status = FormFill.STATUS_SUBMITTING
        ff.save(update_fields=['status', 'updated_at'])
        try:
            browser_use.continue_session(session_id, task=SUBMIT_TASK)
        except browser_use.BrowserUseError as e:
            ff.status = FormFill.STATUS_FAILED
            ff.error = str(e)
            ff.save(update_fields=['status', 'error', 'updated_at'])
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'status': 'submitting'}, status=status.HTTP_200_OK)


class FormFillCancelView(APIView):
    """POST {session_id} — stop the session, mark the FormFill cancelled."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-cancel')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        try:
            browser_use.stop_session(session_id)
        except browser_use.BrowserUseError as e:
            logger.warning('Form-fill cancel stop failed: %s', e)
        if ff and ff.status != FormFill.STATUS_SUBMITTED:
            ff.status = FormFill.STATUS_CANCELLED
            ff.save(update_fields=['status', 'updated_at'])
        return Response({'status': 'cancelled'}, status=status.HTTP_200_OK)


class FormFillAttachmentsView(APIView):
    """POST {ticket_id} -> {attachments: [{filename, content_type, url}]} (images only)."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-attachments')
        if auth_error:
            return auth_error
        ticket_id = str(request.data.get('ticket_id', '')).strip()
        out = []
        try:
            for c in fetch_zendesk_comments(ticket_id):
                for a in (c.get('attachments') or []):
                    if str(a.get('content_type', '')).startswith('image/'):
                        out.append({'filename': a.get('file_name', ''),
                                    'content_type': a.get('content_type', ''),
                                    'url': a.get('content_url', '')})
        except Exception as e:
            logger.warning('Form-fill attachments list failed for ticket %s: %s', ticket_id, e)
        return Response({'attachments': out}, status=status.HTTP_200_OK)


class FormFillUploadView(APIView):
    """POST multipart {ticket_id, image} -> stores the image on a pending FormFill,
    returns {form_fill_id}. (The fill is started later via /start with this id.)"""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-upload')
        if auth_error:
            return auth_error
        ticket_id = str(request.data.get('ticket_id', '')).strip()
        upload = request.FILES.get('image')
        if not upload:
            return Response({'error': 'No image received.'}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > 10 * 1024 * 1024:
            return Response({'error': 'Image is larger than 10 MB.'}, status=status.HTTP_400_BAD_REQUEST)
        if not str(upload.content_type or '').startswith('image/'):
            return Response({'error': 'That file is not an image.'}, status=status.HTTP_400_BAD_REQUEST)
        claim = _claim_for(ticket_id)
        if not claim:
            return Response({'error': 'Link a LORA claim to this ticket first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ff = FormFill.objects.create(
            claim=claim, form_url='', status=FormFill.STATUS_STARTED,
            created_by=request.user if request.user.is_authenticated else None,
            image_source=FormFill.IMAGE_SOURCE_UPLOAD, image_name=upload.name)
        ff.image.save(upload.name, ContentFile(upload.read()), save=True)
        return Response({'form_fill_id': ff.id}, status=status.HTTP_200_OK)


class FormFillWebhookView(APIView):
    """Browser Use posts task status changes here so a fill is finalized server-side
    even with the Zendesk tab closed (no polling needed). Secured by the
    X-Browser-Use-Signature HMAC, NOT a bearer token. Configure the URL + signing code
    in Browser Use's dashboard; paste the code into Settings (browser_use_webhook_secret)."""
    permission_classes = [AllowAny]

    def post(self, request):
        body = request.body
        try:
            event = json.loads(body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return Response({'error': 'invalid json'}, status=status.HTTP_400_BAD_REQUEST)
        etype = str(event.get('type', ''))

        # The 'test' event fires when the webhook is created — before the signing code
        # can be pasted into Settings — and carries no data, so just acknowledge it.
        if etype == 'test':
            return Response({'ok': True}, status=status.HTTP_200_OK)

        secret = SystemSettings.get_instance().browser_use_webhook_secret or ''
        sig = request.META.get('HTTP_X_BROWSER_USE_SIGNATURE', '')
        ts = request.META.get('HTTP_X_BROWSER_USE_TIMESTAMP', '')
        if not _verify_webhook_signature(secret, body, sig, ts):
            logger.warning('Form-fill webhook: signature verification failed (type=%s)', etype)
            return Response({'error': 'invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)

        # Any status-change event carries a session_id (Browser Use sends
        # 'session.status.update'; the exact name varies, so don't gate on it). Read the
        # authoritative session state and finalize the same way the status poll does —
        # don't trust the event's own status string blindly.
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        session_id = str(payload.get('session_id') or event.get('session_id') or '').strip()
        if session_id:
            ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
            if ff and ff.status in (FormFill.STATUS_STARTED, FormFill.STATUS_SUBMITTING):
                try:
                    st = browser_use.get_session(session_id)
                    screenshot = _proxy_screenshot(session_id)
                    _finalize_form_fill(ff, st, screenshot)
                except browser_use.BrowserUseError as e:
                    logger.warning('Form-fill webhook: could not read session %s: %s', session_id, e)
        return Response({'ok': True}, status=status.HTTP_200_OK)
