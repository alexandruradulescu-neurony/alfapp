"""Diagnose evidence-report image embedding for a Zendesk ticket.

Fetches the ticket's real comments and, for every candidate image (inline
markdown, html_body <img>, and attachments), reports whether it fetches and
embeds — so a missing report image is observable instead of guessed at.

    python manage.py debug_report_images <zd_ticket_id>

Read-only; prints no secrets (attachment tokens are redacted)."""

from urllib.parse import urlparse

from django.core.management.base import BaseCommand

from apps.integrations.services import fetch_zendesk_comments
from apps.payments import document_service as ds


def _redact(url: str) -> str:
    p = urlparse(url or '')
    return f"{p.scheme}://{p.hostname}{p.path[:48]}{'…' if len(p.path) > 48 else ''}"


class Command(BaseCommand):
    help = "Trace evidence-report image embedding for a Zendesk ticket."

    def add_arguments(self, parser):
        parser.add_argument('ticket', help='Zendesk ticket id')

    def handle(self, *args, ticket=None, **options):
        comments = fetch_zendesk_comments(ticket)
        self.stdout.write(f"ticket {ticket}: {len(comments)} comments")
        embeddable = 0
        for i, c in enumerate(comments):
            inline = ds._comment_inline_image_urls(c)
            atts = [a for a in (c.get('attachments') or [])
                    if (a.get('content_type') or '').startswith('image/')]
            if not inline and not atts:
                continue
            self.stdout.write(f"--- comment {i} public={c.get('public')} "
                              f"body[:50]={c.get('body', '')[:50]!r} ---")
            for u in inline:
                data = ds._fetch_zendesk_image_bytes(u)
                uri = ds._embed_image_data_uri(data) if data else None
                if uri:
                    embeddable += 1
                self.stdout.write(
                    f"  inline  host={urlparse(u).hostname} url={_redact(u)} "
                    f"fetch={('%d bytes' % len(data)) if data else 'None'} "
                    f"embed={'OK' if uri else 'skipped'}")
            for a in atts:
                url = a.get('content_url', '')
                data = ds._fetch_zendesk_image_bytes(url)
                uri = ds._embed_image_data_uri(data) if data else None
                if uri:
                    embeddable += 1
                self.stdout.write(
                    f"  attach  {a.get('content_type')} inline={a.get('inline')} "
                    f"url={_redact(url)} "
                    f"fetch={('%d bytes' % len(data)) if data else 'None'} "
                    f"embed={'OK' if uri else 'skipped'}")
        self.stdout.write(self.style.SUCCESS(f"embeddable images on this ticket: {embeddable}"))
