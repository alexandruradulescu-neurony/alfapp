import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.claims.models import Claim

# Set all empty zd_ticket_id to None
Claim.objects.filter(zd_ticket_id='').update(zd_ticket_id=None)
print("Set empty zd_ticket_id to None")

# Check for any remaining duplicates
from django.db.models import Count
duplicates = Claim.objects.values('zd_ticket_id').annotate(
    count=Count('zd_ticket_id')
).filter(count__gt=1, zd_ticket_id__isnull=False)

if duplicates:
    print(f"Found duplicates: {list(duplicates)}")
    # Clear all duplicates
    for dup in duplicates:
        if dup['zd_ticket_id']:
            Claim.objects.filter(zd_ticket_id=dup['zd_ticket_id']).update(zd_ticket_id=None)
    print("Cleared all duplicates")
else:
    print("No duplicates found")
