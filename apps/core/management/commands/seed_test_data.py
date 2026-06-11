"""
Django management command to seed test data for the LORA application.

Usage:
    python manage.py seed_test_data
    python manage.py seed_test_data --flush  # Just delete data without creating new
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.payments.models import Refund, Dispute


class Command(BaseCommand):
    help = "Seed test data for the LORA application"

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Only delete existing data without creating new test data",
        )

    def handle(self, *args, **options):
        self.stdout.write("Starting test data seeding...")

        flush_only = options.get("flush", False)

        try:
            with transaction.atomic():
                # Delete existing data (except users)
                deleted_counts = self._delete_existing_data()

                if flush_only:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"\nFlush complete! Deleted data:\n"
                            f"  - Claims: {deleted_counts['claims']}\n"
                            f"  - Email Logs: {deleted_counts['emails']}\n"
                            f"  - Disputes: {deleted_counts['disputes']}\n"
                            f"  - Refunds: {deleted_counts['refunds']}"
                        )
                    )
                    return

                # Create new test data
                created_counts = self._create_test_data()

                # Print summary
                self.stdout.write(self.style.SUCCESS("\n" + "=" * 50))
                self.stdout.write(self.style.SUCCESS("Test data seeding complete!"))
                self.stdout.write(self.style.SUCCESS("=" * 50))
                self.stdout.write(self.style.SUCCESS(f"\nDeleted:"))
                self.stdout.write(self.style.SUCCESS(f"  - {deleted_counts['claims']} claims"))
                self.stdout.write(self.style.SUCCESS(f"  - {deleted_counts['emails']} emails"))
                self.stdout.write(self.style.SUCCESS(f"  - {deleted_counts['disputes']} disputes"))
                self.stdout.write(self.style.SUCCESS(f"  - {deleted_counts['refunds']} refunds"))
                self.stdout.write(self.style.SUCCESS(f"\nCreated:"))
                self.stdout.write(self.style.SUCCESS(f"  - {created_counts['claims']} claims"))
                self.stdout.write(self.style.SUCCESS(f"  - {created_counts['emails']} emails"))
                self.stdout.write(self.style.SUCCESS(f"  - {created_counts['disputes']} disputes"))
                self.stdout.write(self.style.SUCCESS(f"  - {created_counts['refunds']} refunds"))

        except Exception as e:
            raise CommandError(f"Error seeding test data: {str(e)}")

    def _delete_existing_data(self):
        """Delete all existing data except users."""
        counts = {
            "refunds": 0,
            "disputes": 0,
            "emails": 0,
            "claims": 0,
        }

        # Delete in order of dependencies (refunds/disputes first, then emails, then claims)
        counts["refunds"] = Refund.objects.count()
        Refund.objects.all().delete()

        counts["disputes"] = Dispute.objects.count()
        Dispute.objects.all().delete()

        counts["emails"] = EmailLog.objects.count()
        EmailLog.objects.all().delete()

        counts["claims"] = Claim.objects.count()
        Claim.objects.all().delete()

        self.stdout.write(f"Deleted existing data: {counts}")
        return counts

    def _create_test_data(self):
        """Create realistic test data."""
        now = timezone.now()

        # Create 4 claims with different statuses
        claims = self._create_claims(now)

        # Create 10 email logs linked to claims
        emails = self._create_email_logs(claims, now)

        # Create 2 disputes linked to claims
        disputes = self._create_disputes(claims, now)

        # Create 3 refunds linked to claims
        refunds = self._create_refunds(claims, now)

        return {
            "claims": len(claims),
            "emails": len(emails),
            "disputes": len(disputes),
            "refunds": len(refunds),
        }

    def _create_claims(self, now):
        """Create 4 claims with different statuses."""
        claims_data = [
            {
                # Claim 1: active/open — initial investigation
                "alf_claim_id": "ALF1000001",
                "zd_ticket_id": "1001",
                "client_email": "john.smith@example.com",
                "phone": "+1-555-0101",
                "alternate_email": "",
                "flight_details": "Flight AA1234 from JFK to LAX on 2024-01-15",
                "object_description": "Black leather wallet with brown interior, contains driver's license and credit cards",
                "status": "Investigation initiated",
                "status_category": "open",
                "llm_extraction_failed": False,
                "created_offset": -10,
            },
            {
                # Claim 2: open — claim submitted, being worked
                "alf_claim_id": "ALF1000002",
                "zd_ticket_id": "1002",
                "client_email": "sarah.johnson@gmail.com",
                "phone": "+1-555-0102",
                "alternate_email": "s.johnson@work.com",
                "flight_details": "Flight UA5678 from ORD to SFO on 2024-01-18",
                "object_description": "Silver MacBook Pro 14-inch with apple logo sticker, in grey sleeve",
                "status": "Claim submitted",
                "status_category": "open",
                "llm_extraction_failed": False,
                "created_offset": -8,
            },
            {
                # Claim 3: open — object located, pending shipment
                "alf_claim_id": "ALF1000003",
                "zd_ticket_id": "1003",
                "client_email": "michael.chen@outlook.com",
                "phone": "+1-555-0103",
                "alternate_email": "",
                "flight_details": "Flight DL9012 from ATL to SEA on 2024-01-20",
                "object_description": "Blue Samsonite carry-on suitcase with TSA lock, baggage tag attached",
                "status": "Object Found",
                "status_category": "open",
                "llm_extraction_failed": False,
                "created_offset": -6,
            },
            {
                # Claim 4: solved — closed with full refund
                "alf_claim_id": "ALF1000004",
                "zd_ticket_id": "1004",
                "client_email": "emma.williams@yahoo.com",
                "phone": "+1-555-0104",
                "alternate_email": "emma.w@personal.com",
                "flight_details": "Flight BA2468 from LHR to BOS on 2024-01-22",
                "object_description": "Canon EOS R5 camera body with 24-70mm lens, black camera bag included",
                "status": "Closed - Refunded",
                "status_category": "solved",
                "llm_extraction_failed": False,
                "created_offset": -4,
            },
        ]

        claims = []
        for data in claims_data:
            created_at = now - timedelta(days=data.pop("created_offset"))
            claim = Claim.objects.create(
                **data,
                created_at=created_at,
                updated_at=created_at,
            )
            claims.append(claim)
            self.stdout.write(f"  Created claim: {claim.alf_claim_id} ({claim.status})")

        return claims

    def _create_email_logs(self, claims, now):
        """Create 10 email logs linked to claims (2-3 per claim)."""
        email_templates = [
            {
                "subject": "Claim Submission Confirmation - ALF{claim_id}",
                "body": "Dear {client_email},\n\nThank you for submitting your claim regarding your lost item on flight {flight}. We have received your submission and assigned it reference number ALF{claim_id}.\n\nOur team will begin searching for your {object} immediately. You can expect an update within 3-5 business days.\n\nBest regards,\nLost & Found Team",
                "category": "SUBMISSION_CONFIRMATION",
                "action_required": False,
                "auto_resolved": True,
            },
            {
                "subject": "Update on Your Claim - ALF{claim_id}",
                "body": "Dear {client_email},\n\nWe are writing to provide an update on your claim ALF{claim_id}.\n\nOur team has completed the initial search phase and we are currently reviewing all found items that match your description.\n\nWe will notify you as soon as we have more information.\n\nBest regards,\nLost & Found Team",
                "category": "GENERAL_CORRESPONDENCE",
                "action_required": False,
                "auto_resolved": True,
            },
            {
                "subject": "Possible Match Found - Claim ALF{claim_id}",
                "body": "Dear {client_email},\n\nGood news! We may have found your {object}.\n\nAn item matching your description has been located at {location}. To proceed with verification and return, please confirm the following details:\n\n1. Color and brand\n2. Any distinguishing features\n3. Contents (if applicable)\n\nPlease respond within 48 hours to confirm.\n\nBest regards,\nLost & Found Team",
                "category": "OBJECT_FOUND",
                "action_required": True,
                "auto_resolved": False,
            },
            {
                "subject": "Additional Information Required - ALF{claim_id}",
                "body": "Dear {client_email},\n\nRegarding your claim ALF{claim_id}, we require additional information to proceed with the search.\n\nCould you please provide:\n- More detailed description of the item\n- Photos if available\n- Approximate value\n\nThis information will help us locate your item more efficiently.\n\nBest regards,\nLost & Found Team",
                "category": "GENERAL_CORRESPONDENCE",
                "action_required": True,
                "auto_resolved": False,
            },
            {
                "subject": "Search Complete - Item Not Found - ALF{claim_id}",
                "body": "Dear {client_email},\n\nWe have completed an extensive search for your {object} but unfortunately have not been able to locate it.\n\nWe understand this is disappointing. If you would like to discuss next steps or have any questions, please don't hesitate to contact us.\n\nBest regards,\nLost & Found Team",
                "category": "OBJECT_NOT_FOUND",
                "action_required": False,
                "auto_resolved": True,
            },
        ]

        # Distribution: Claim 1: 2 emails, Claim 2: 3 emails, Claim 3: 3 emails, Claim 4: 2 emails
        distribution = [2, 3, 3, 2]

        emails = []
        email_count = 0

        for claim_idx, claim in enumerate(claims):
            num_emails = distribution[claim_idx]
            for i in range(num_emails):
                template = email_templates[email_count % len(email_templates)]

                subject = template["subject"].format(
                    claim_id=claim.alf_claim_id.replace("ALF", ""),
                    client_email=claim.client_email,
                    flight=claim.flight_details,
                    object=claim.object_description[:30],
                    location="Terminal 3, Gate A12",
                )

                body = template["body"].format(
                    claim_id=claim.alf_claim_id.replace("ALF", ""),
                    client_email=claim.client_email,
                    flight=claim.flight_details,
                    object=claim.object_description[:30],
                    location="Terminal 3, Gate A12",
                )

                received_at = now - timedelta(days=random.randint(1, 15), hours=random.randint(0, 23))

                email = EmailLog.objects.create(
                    claim=claim,
                    subject=subject,
                    body=body,
                    ai_summary=f"Email regarding claim {claim.alf_claim_id} - {template['category']}",
                    action_required=template["action_required"],
                    received_at=received_at,
                    from_email=f"support@lostfound.airline.com",
                    to_email=claim.client_email,
                    delivered_to=claim.client_email,
                    alias_matched="",
                    zd_ticket_id=claim.zd_ticket_id,
                    category=template["category"],
                    auto_resolved=template["auto_resolved"],
                    raw_headers=f"From: support@lostfound.airline.com\nTo: {claim.client_email}\nSubject: {subject}\nDate: {received_at}",
                )
                emails.append(email)
                email_count += 1

        self.stdout.write(f"  Created {len(emails)} email logs")
        return emails

    def _create_disputes(self, claims, now):
        """Create 2 disputes linked to claims."""
        disputes_data = [
            {
                # Dispute 1: Linked to Claim 2, status=RECEIVED
                "claim_index": 1,
                "paypal_dispute_id": "PP-D-10001",
                "paypal_case_id": "CASE-10001",
                "status": "RECEIVED",
                "dispute_reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                "dispute_amount": "150.00",
                "dispute_currency": "USD",
                "buyer_email": "sarah.johnson@gmail.com",
                "buyer_name": "Sarah Johnson",
                "transaction_id": "TXN-5678-ABCD",
                "transaction_days_ago": 7,
                "seller_response_due_days": 3,
                "notes": "Customer claims item was never received. Awaiting evidence gathering.",
            },
            {
                # Dispute 2: Linked to Claim 4, status=EVIDENCE_SENT
                "claim_index": 3,
                "paypal_dispute_id": "PP-D-10002",
                "paypal_case_id": "CASE-10002",
                "status": "EVIDENCE_SENT",
                "dispute_reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                "dispute_amount": "200.00",
                "dispute_currency": "USD",
                "buyer_email": "emma.williams@yahoo.com",
                "buyer_name": "Emma Williams",
                "transaction_id": "TXN-9012-EFGH",
                "transaction_days_ago": 12,
                "seller_response_due_days": -2,  # Already passed
                "notes": "Customer claims item condition not as described. Evidence package sent to PayPal on 2024-02-01.",
            },
        ]

        disputes = []
        for data in disputes_data:
            claim = claims[data.pop("claim_index")]
            transaction_days_ago = data.pop("transaction_days_ago")
            seller_response_due_days = data.pop("seller_response_due_days")

            transaction_date = now - timedelta(days=transaction_days_ago)
            seller_response_due = now + timedelta(days=seller_response_due_days) if seller_response_due_days > 0 else now - timedelta(days=abs(seller_response_due_days))

            dispute = Dispute.objects.create(
                claim=claim,
                zd_ticket_id=claim.zd_ticket_id,
                transaction_date=transaction_date,
                seller_response_due=seller_response_due,
                raw_webhook_payload={
                    "dispute_id": data["paypal_dispute_id"],
                    "status": data["status"],
                    "reason": data["dispute_reason"],
                },
                **data,
            )
            disputes.append(dispute)
            self.stdout.write(f"  Created dispute: {dispute.paypal_dispute_id} linked to {claim.alf_claim_id} ({dispute.status})")

        return disputes

    def _create_refunds(self, claims, now):
        """Create 3 refunds linked to claims."""
        refunds_data = [
            {
                # Refund 1: Linked to Claim 3, status=REQUESTED, amount=$50.00
                "claim_index": 2,
                "paypal_refund_id": "REF-10001-ABC",
                "paypal_capture_id": "CAP-30001",
                "amount": "50.00",
                "currency": "USD",
                "status": "REQUESTED",
                "refund_type": "PARTIAL",
                "external_source": "LORA",
                "reason": "Partial refund requested due to delayed response. Customer agreed to partial compensation.",
                "created_days_ago": 2,
                "processed_at_offset": None,
            },
            {
                # Refund 2: Linked to Claim 3, status=REQUESTED, amount=$25.00 (partial)
                "claim_index": 2,
                "paypal_refund_id": "REF-10002-DEF",
                "paypal_capture_id": "CAP-30001",
                "amount": "25.00",
                "currency": "USD",
                "status": "REQUESTED",
                "refund_type": "PARTIAL",
                "external_source": "MANUAL",
                "reason": "Additional partial refund for inconvenience caused during claim process.",
                "created_days_ago": 1,
                "processed_at_offset": None,
            },
            {
                # Refund 3: Linked to Claim 4, status=COMPLETED, amount=$100.00
                "claim_index": 3,
                "paypal_refund_id": "REF-10003-GHI",
                "paypal_capture_id": "CAP-40001",
                "amount": "100.00",
                "currency": "USD",
                "status": "COMPLETED",
                "refund_type": "FULL",
                "external_source": "LORA",
                "reason": "Full refund issued as item could not be located after extensive search.",
                "created_days_ago": 5,
                "processed_at_offset": 3,
            },
        ]

        refunds = []
        for data in refunds_data:
            claim = claims[data.pop("claim_index")]
            created_days_ago = data.pop("created_days_ago")
            processed_at_offset = data.pop("processed_at_offset")

            created_at = now - timedelta(days=created_days_ago)
            processed_at = None
            if processed_at_offset is not None:
                processed_at = created_at + timedelta(days=processed_at_offset)

            refund = Refund.objects.create(
                claim=claim,
                created_at=created_at,
                updated_at=created_at,
                processed_at=processed_at,
                metadata={
                    "refund_id": data["paypal_refund_id"],
                    "capture_id": data["paypal_capture_id"],
                    "initiated_by": "system_seed",
                },
                **data,
            )
            refunds.append(refund)
            self.stdout.write(f"  Created refund: {refund.paypal_refund_id} - ${refund.amount} ({refund.status}) linked to {claim.alf_claim_id}")

        return refunds
