"""Generate a PayPal dispute evidence report (narrative, Zendesk-styled) as a PDF.

Two modes:

  # Preview from any Zendesk ticket — no DB writes. Use this to eyeball fidelity.
  python manage.py generate_dispute_report --zd-ticket 12345 [--reason UNAUTHORISED] [--out /tmp/r.pdf]

  # Render for an existing dispute (preview by default; --save persists a DisputeDocument).
  python manage.py generate_dispute_report --dispute 42 [--save]

The preview modes fetch live Zendesk data and embed pasted attachment images, so
they must run where the Zendesk API credentials are configured (production).
"""

from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from apps.claims.models import Claim
from apps.payments.models import Dispute
from apps.payments import document_service as docsvc


class Command(BaseCommand):
    help = "Generate a narrative PayPal dispute evidence report as a PDF."

    def add_arguments(self, parser):
        parser.add_argument('--dispute', type=int, help='Existing Dispute primary key.')
        parser.add_argument('--zd-ticket', type=str, help='Zendesk ticket ID to preview from its matched claim.')
        parser.add_argument('--reason', type=str, default='',
                            help='Override dispute reason for framing (e.g. UNAUTHORISED). Preview only.')
        parser.add_argument('--out', type=str, default='', help='Output PDF path (default: /tmp/...).')
        parser.add_argument('--save', action='store_true',
                            help='With --dispute: persist a DisputeDocument via the real generator.')
        parser.add_argument('--no-attachments', action='store_true',
                            help='Skip downloading/embedding Zendesk attachment images (faster preview).')

    def handle(self, *args, **opts):
        if not opts['dispute'] and not opts['zd_ticket']:
            raise CommandError("Provide --dispute <id> or --zd-ticket <id>.")

        # --- real-dispute, persist path ---
        if opts['dispute'] and opts['save']:
            doc = docsvc.generate_evidence_report(opts['dispute'])
            if not doc:
                raise CommandError(f"Failed to generate report for dispute {opts['dispute']} (see logs).")
            self.stdout.write(self.style.SUCCESS(
                f"Saved DisputeDocument #{doc.id} (v{doc.version}) → {doc.file_path.name}"))
            return

        # --- preview path (no DB writes) ---
        if opts['dispute']:
            dispute = Dispute.objects.select_related('claim').filter(pk=opts['dispute']).first()
            if not dispute:
                raise CommandError(f"Dispute {opts['dispute']} not found.")
            key = f"dispute_{dispute.id}"
        else:
            ticket = opts['zd_ticket']
            claim = Claim.objects.filter(zd_ticket_id=ticket).first()
            if not claim:
                raise CommandError(f"No claim found for Zendesk ticket {ticket}.")
            dispute = Dispute(  # transient, unsaved
                claim=claim,
                zd_ticket_id=ticket,
                paypal_dispute_id=f"PREVIEW-{ticket}",
                dispute_reason=opts['reason'] or '',
                dispute_amount=claim.price_paid,
                dispute_currency='USD',
                buyer_email=claim.client_email or 'preview@example.com',
                buyer_name=claim.client_name or '',
                transaction_id='PREVIEW',
                transaction_date=claim.created_at,
                status='RECEIVED',
            )
            key = f"ticket_{ticket}"

        bundle = docsvc.build_dispute_evidence_bundle(
            dispute, embed_attachments=not opts['no_attachments'])
        self.stdout.write(
            f"Bundle: {len(bundle['panels'])} panels, "
            f"{sum(len(p['images']) for p in bundle['panels'])} embedded images, "
            f"flight_card={'yes' if bundle['flight_card'] else 'no'}, "
            f"framing='{bundle['framing']['headline']}'")

        html_string = render_to_string(docsvc.report_template_for(dispute), bundle)
        pdf_bytes = docsvc._render_to_pdf(html_string, f"Preview {key}")
        if not pdf_bytes:
            raise CommandError("PDF rendering failed (is WeasyPrint installed in this environment?).")

        out_path = opts['out'] or f"/tmp/dispute_report_{key}.pdf"
        with open(out_path, 'wb') as f:
            f.write(pdf_bytes)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(pdf_bytes)} bytes → {out_path}"))
