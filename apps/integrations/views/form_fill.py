"""Zendesk sidebar 'Form filling' endpoints: drive Browser Use to fill an
institution form from a claim, with a human approval gate before submit. Every
attempt is a FormFill row. Auth: ZendeskSidebarAuth (bearer token)."""
import base64
import logging

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
    build_form_secrets, build_fill_task, SUBMIT_TASK, form_host)
from apps.integrations.models import FormFill
from apps.integrations.services import (
    post_zendesk_comment, fetch_zendesk_comments, fetch_zendesk_attachment)
from apps.integrations.views.auth import ZendeskSidebarAuth

logger = logging.getLogger(__name__)


def _claim_for(ticket_id):
    return Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None


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
        ctype = r.headers.get('Content-Type', 'image/png')
        b64 = base64.b64encode(r.content).decode()
        return f'data:{ctype};base64,{b64}'
    except Exception as e:
        logger.warning('Screenshot proxy failed: %s', e)
        return ''


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
        host = form_host(url)
        secrets = build_form_secrets(claim, host)
        task = build_fill_task(url, secrets)

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
            ff.save(update_fields=['form_url', 'status', 'updated_at'])
            if ff.image:
                image_bytes = ff.image.read()
                image_ctype = 'application/octet-stream'
        else:
            ff = FormFill.objects.create(
                claim=claim, form_url=url, status=FormFill.STATUS_STARTED,
                created_by=request.user if request.user.is_authenticated else None,
                posted_to_ticket=False)
            if image_url:
                try:
                    image_bytes, image_ctype = fetch_zendesk_attachment(image_url)
                    ff.image_source = FormFill.IMAGE_SOURCE_TICKET
                    ff.image_name = image_filename
                    ff.image.save(image_filename, ContentFile(image_bytes), save=True)
                except Exception as e:
                    logger.warning('Ticket attachment fetch failed: %s', e)

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
        if ff and ff.status == FormFill.STATUS_STARTED:
            if bu_status == 'idle':
                ff.status = FormFill.STATUS_FILLED
                ff.filled_at = timezone.now()
                ff.result_output = str(st.get('output', ''))[:5000]
                ff.save(update_fields=['status', 'filled_at', 'result_output', 'updated_at'])
            elif bu_status in ('error', 'failed', 'timed_out', 'stopped'):
                ff.status = FormFill.STATUS_FAILED
                ff.error = str(st.get('output', '') or 'Session ended before the fill completed.')[:2000]
                ff.save(update_fields=['status', 'error', 'updated_at'])
        screenshot = _proxy_screenshot(session_id)
        return Response({'status': ff.status if ff else bu_status, 'bu_status': bu_status,
                         'screenshot': screenshot}, status=status.HTTP_200_OK)


class FormFillSubmitView(APIView):
    """POST {session_id, ticket_id, post_screenshot} — continue the session to submit."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-submit')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ticket_id = str(request.data.get('ticket_id', '')).strip()
        post_screenshot = bool(request.data.get('post_screenshot', False))
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        try:
            browser_use.continue_session(session_id, task=SUBMIT_TASK)
            st = browser_use.get_session(session_id)
        except browser_use.BrowserUseError as e:
            if ff:
                ff.status = FormFill.STATUS_FAILED
                ff.error = str(e)
                ff.save()
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        screenshot = _proxy_screenshot(session_id)
        if ff:
            ff.status = FormFill.STATUS_SUBMITTED
            ff.submitted_at = timezone.now()
            ff.result_output = str(st.get('output', ''))[:5000]
            ff.save(update_fields=['status', 'submitted_at', 'result_output', 'updated_at'])
        if post_screenshot and screenshot and ticket_id:
            note = (f'<p>\U0001F4DD <strong>Form filled &amp; submitted via LORA</strong></p>'
                    f'<p><img src="{screenshot}" alt="form submission confirmation" /></p>')
            try:
                post_zendesk_comment(ticket_id, comment_body='', is_internal=True, html_body=note)
                if ff:
                    ff.posted_to_ticket = True
                    ff.save(update_fields=['posted_to_ticket', 'updated_at'])
            except Exception as e:
                logger.warning('Form-fill note post failed for ticket %s: %s', ticket_id, e)
        return Response({'status': 'submitted', 'screenshot': screenshot}, status=status.HTTP_200_OK)


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
