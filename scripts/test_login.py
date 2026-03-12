import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model

User = get_user_model()

# Test authentication
print("Testing authentication...")

# Test admin user
user = authenticate(username='admin', password='admin123')
if user:
    print(f"✓ admin user authenticated: {user.username} (role: {user.role})")
else:
    print("✗ admin authentication failed")
    # Check if user exists
    try:
        user = User.objects.get(username='admin')
        print(f"  User exists: {user.username}, role: {user.role}, has_usable_password: {user.has_usable_password()}")
    except User.DoesNotExist:
        print("  User does not exist!")

# Test agent1
user = authenticate(username='agent1', password='password123')
if user:
    print(f"✓ agent1 authenticated: {user.username} (role: {user.role})")
else:
    print("✗ agent1 authentication failed")

# Test manager1
user = authenticate(username='manager1', password='password123')
if user:
    print(f"✓ manager1 authenticated: {user.username} (role: {user.role})")
else:
    print("✗ manager1 authentication failed")

print("\nAll users in database:")
for u in User.objects.all():
    print(f"  - {u.username} (role: {u.role}, active: {u.is_active})")
