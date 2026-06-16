"""
Create sample disputes for testing the LORA dispute management system.
"""

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from datetime import datetime, timedelta
from decimal import Decimal
from django.utils import timezone

from apps.payments.models import Dispute, DisputeDocument, DisputeScreenshot, DisputeActivityLog
from apps.claims.models import Claim
from apps.users.models import User

print("=" * 60)
print("Creating Sample Disputes for Testing")
print("=" * 60)

# Get or create a manager user for assignment
manager = User.objects.filter(role='MANAGER').first()
if not manager:
    print("Creating manager user...")
    manager = User.objects.create_user(
        username='dispute_manager',
        email='manager@example.com',
        password='password123',
        role='MANAGER',
        first_name='Dispute',
        last_name='Manager'
    )
    print(f"✓ Created manager: {manager.username}")

# Get existing claims or create sample ones
claims = Claim.objects.all()[:3]

if not claims.exists():
    print("\nCreating sample claims...")
    from apps.claims.models import Claim
    claims_data = [
        {'client_email': 'john.doe@example.com', 'status': 'Investigation initiated', 'status_category': 'open', 'flight_details': 'Flight AA123 from JFK to LAX on 2024-01-15'},
        {'client_email': 'jane.smith@example.com', 'status': 'Claim submitted', 'status_category': 'open', 'flight_details': 'Flight UA456 from ORD to SFO on 2024-01-16'},
    ]
    for data in claims_data:
        claim, _ = Claim.objects.get_or_create(client_email=data['client_email'], defaults=data)
        claims = claims | Claim.objects.filter(id=claim.id)
    print(f"✓ Created {len(claims_data)} sample claims")

print(f"\nFound {claims.count()} existing claims")

# Sample dispute data
disputes_data = [
    {
        'paypal_dispute_id': 'PP-D-2026-001',
        'paypal_case_id': 'CASE-12345',
        'claim': None,  # Will be set from claims queryset
        'zd_ticket_id': '12345',
        'status': 'GATHERING_DATA',
        'dispute_reason': 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
        'dispute_amount': Decimal('249.99'),
        'dispute_currency': 'USD',
        'buyer_email': 'john.doe@example.com',
        'buyer_name': 'John Doe',
        'transaction_id': 'PAY-1234567890',
        'transaction_date': timezone.now() - timedelta(days=10),
        'seller_response_due': timezone.now() + timedelta(days=5),
        'notes': 'Customer claims item never arrived. Tracking shows delivered to wrong address.',
        'activity_log': [
            ('DISPUTE_CREATED', 'Dispute received from PayPal'),
            ('DISPUTE_MATCHED', 'Matched to Zendesk ticket #12345'),
            ('SCREENSHOTS_CAPTURED', 'Captured 3 screenshots from Zendesk'),
        ]
    },
    {
        'paypal_dispute_id': 'PP-D-2026-002',
        'paypal_case_id': 'CASE-12346',
        'claim': None,
        'zd_ticket_id': '12346',
        'status': 'DOCUMENTS_READY',
        'dispute_reason': 'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
        'dispute_amount': Decimal('599.00'),
        'dispute_currency': 'USD',
        'buyer_email': 'jane.smith@example.com',
        'buyer_name': 'Jane Smith',
        'transaction_id': 'PAY-0987654321',
        'transaction_date': timezone.now() - timedelta(days=15),
        'seller_response_due': timezone.now() + timedelta(days=2),
        'notes': 'Customer received damaged item. Photos provided. Awaiting response letter.',
        'activity_log': [
            ('DISPUTE_CREATED', 'Dispute received from PayPal'),
            ('DISPUTE_MATCHED', 'Matched to Zendesk ticket #12346'),
            ('SCREENSHOTS_CAPTURED', 'Captured 5 screenshots from Zendesk'),
            ('DOCUMENT_GENERATED', 'Generated response letter v1'),
            ('DOCUMENT_GENERATED', 'Generated evidence report v1'),
        ]
    },
]

print("\n" + "=" * 60)
print("Creating Disputes")
print("=" * 60)

for i, data in enumerate(disputes_data):
    # Link to claim
    if i < len(claims):
        data['claim'] = claims[i]
    
    # Create dispute
    dispute, created = Dispute.objects.get_or_create(
        paypal_dispute_id=data['paypal_dispute_id'],
        defaults={
            'paypal_case_id': data['paypal_case_id'],
            'claim': data['claim'],
            'zd_ticket_id': data['zd_ticket_id'],
            'status': data['status'],
            'dispute_reason': data['dispute_reason'],
            'dispute_amount': data['dispute_amount'],
            'dispute_currency': data['dispute_currency'],
            'buyer_email': data['buyer_email'],
            'buyer_name': data['buyer_name'],
            'transaction_id': data['transaction_id'],
            'transaction_date': data['transaction_date'],
            'seller_response_due': data['seller_response_due'],
            'notes': data['notes'],
            'assigned_to': manager,
        }
    )
    
    if created:
        print(f"\n✓ Created Dispute #{dispute.id}: {dispute.paypal_dispute_id}")
        print(f"  - Buyer: {dispute.buyer_name} ({dispute.buyer_email})")
        print(f"  - Amount: ${dispute.dispute_amount} {dispute.dispute_currency}")
        print(f"  - Status: {dispute.status}")
        print(f"  - Zendesk Ticket: #{dispute.zd_ticket_id}")
        
        # Create activity log entries
        for action, details in data['activity_log']:
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action=action,
                details=details,
                performed_by=manager
            )
        print(f"  - Added {len(data['activity_log'])} activity log entries")
        
        # Create sample documents for second dispute
        if i == 1:
            from apps.payments.models import DisputeDocument

            # (The response-letter PDF was dropped — the written argument to PayPal
            # is now plain text on a DisputeSubmission. Only the evidence report is
            # a generated document.)

            # Evidence report
            evidence_report = DisputeDocument.objects.create(
                dispute=dispute,
                doc_type='EVIDENCE_REPORT',
                status='DRAFT',
                content_html=f'''
                    <h1>Evidence Report</h1>
                    
                    <h2>Dispute Information</h2>
                    <table>
                        <tr><td>PayPal Dispute ID:</td><td>{data['paypal_dispute_id']}</td></tr>
                        <tr><td>Transaction ID:</td><td>{data['transaction_id']}</td></tr>
                        <tr><td>Amount:</td><td>${data['dispute_amount']} {data['dispute_currency']}</td></tr>
                        <tr><td>Date:</td><td>{data['transaction_date'].strftime('%Y-%m-%d')}</td></tr>
                    </table>
                    
                    <h2>Timeline</h2>
                    <ul>
                        <li>Item shipped with tracking</li>
                        <li>Delivery confirmed</li>
                        <li>Customer reported damage</li>
                    </ul>
                    
                    <h2>Evidence</h2>
                    <p>See attached screenshots and documentation.</p>
                ''',
                generated_by='MANUAL',
                version=1
            )
            print(f"  - Created evidence report (DRAFT)")
    else:
        print(f"\n⚠ Dispute {dispute.paypal_dispute_id} already exists")

print("\n" + "=" * 60)
print("Sample Data Summary")
print("=" * 60)

print(f"\nDisputes created: {Dispute.objects.count()}")
print(f"Dispute documents: {DisputeDocument.objects.count()}")
print(f"Dispute activity logs: {DisputeActivityLog.objects.count()}")

print("\nDispute Status Breakdown:")
for status, count in Dispute.objects.values_list('status').distinct():
    print(f"  - {status}: {Dispute.objects.filter(status=status).count()}")

print("\n" + "=" * 60)
print("Sample disputes created successfully!")
print("=" * 60)

print("\nAccess the dispute management UI:")
print("  URL: http://127.0.0.1:8000/manager/disputes/")
print("  Login: admin / admin123 (MANAGER)")
print("         manager1 / password123 (MANAGER)")
print("         dispute_manager / password123 (MANAGER)")

print("\nTest the following:")
print("  1. View dispute list with filters")
print("  2. View dispute detail with activity log")
print("  3. Edit response letter (HTML editor)")
print("  4. Accept documents")
print("  5. Generate new documents")
print("  6. Capture screenshots (requires Zendesk credentials)")
print("  7. Send evidence to PayPal (requires PayPal credentials)")
