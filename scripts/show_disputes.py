import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.payments.models import Dispute, DisputeDocument

print("=" * 60)
print("Sample Disputes Created")
print("=" * 60)

print(f"\nTotal Disputes: {Dispute.objects.count()}")
print(f"Total Documents: {DisputeDocument.objects.count()}")

print("\nDispute List:")
for d in Dispute.objects.all():
    print(f"\n  #{d.id}: {d.paypal_dispute_id}")
    print(f"     Status: {d.status}")
    print(f"     Buyer: {d.buyer_name} ({d.buyer_email})")
    print(f"     Amount: ${d.dispute_amount} {d.dispute_currency}")
    print(f"     Zendesk: #{d.zd_ticket_id}")
    print(f"     Documents: {d.documents.count()}")
    print(f"     Activity Logs: {d.activity_log.count()}")

print("\n" + "=" * 60)
print("Access the dispute management UI:")
print("  URL: http://127.0.0.1:8000/manager/disputes/")
print("  Login: admin / admin123")
print("=" * 60)
