import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.claims.models import Claim, ClaimEvidence
from apps.communications.models import EmailLog
from django.contrib.auth import get_user_model

User = get_user_model()

# Create sample users
if not User.objects.filter(username='agent1').exists():
    agent = User.objects.create_user('agent1', 'agent1@example.com', 'password123', role='AGENT')
    print(f"Created agent user: {agent}")

if not User.objects.filter(username='manager1').exists():
    manager = User.objects.create_user('manager1', 'manager1@example.com', 'password123', role='MANAGER')
    print(f"Created manager user: {manager}")

# Create sample claims
claims_data = [
    {'client_email': 'john.doe@example.com', 'status': 'Received', 'flight_details': 'Flight AA123 from JFK to LAX on 2024-01-15'},
    {'client_email': 'jane.smith@example.com', 'status': 'Searching', 'flight_details': 'Flight UA456 from ORD to SFO on 2024-01-16'},
    {'client_email': 'bob.wilson@example.com', 'status': 'Found', 'zd_ticket_id': '12345', 'flight_details': 'Flight DL789 from ATL to SEA on 2024-01-17'},
]

for data in claims_data:
    claim, created = Claim.objects.get_or_create(client_email=data['client_email'], defaults=data)
    if created:
        print(f"Created claim: {claim}")

# Create sample email logs
claims = list(Claim.objects.all())
if claims:
    email_data = [
        {'subject': 'Lost item inquiry', 'body': 'I left my laptop on the plane...', 'sentiment': 'Urgent', 'action_required': True},
        {'subject': 'Follow-up on claim', 'body': 'Any update on my claim?', 'sentiment': 'Frustrated', 'action_required': True},
        {'subject': 'Thank you', 'body': 'Thank you for finding my bag!', 'sentiment': 'Positive', 'action_required': False},
    ]
    
    for i, data in enumerate(email_data):
        if i < len(claims):
            EmailLog.objects.create(claim=claims[i], **data)
            print(f"Created email log: {data['subject']}")

print("\nSample data created successfully!")
print(f"Total claims: {Claim.objects.count()}")
print(f"Total email logs: {EmailLog.objects.count()}")
