"""
Test script for Phase 6 - Dashboard UI.
"""

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from django.contrib.auth import get_user_model
from apps.claims.models import Claim
from apps.communications.models import EmailLog

User = get_user_model()

print("=" * 60)
print("Phase 6 - Dashboard UI Tests")
print("=" * 60)

# Test 1: Check decorators
print("\n1. Testing decorators...")
try:
    from apps.users.decorators import (
        role_required,
        agent_required,
        manager_required,
        login_redirect,
    )
    print("   ✓ role_required - available")
    print("   ✓ agent_required - available")
    print("   ✓ manager_required - available")
    print("   ✓ login_redirect - available")
except ImportError as e:
    print(f"   ✗ Import error: {e}")

# Test 2: Check views
print("\n2. Testing views...")
try:
    from apps.users.views import (
        login_view,
        logout_view,
        dashboard_redirect,
        agent_dashboard,
        agent_claims,
        agent_claim_detail,
        agent_upload_evidence,
        manager_dashboard,
        manager_claims,
        manager_settings,
        manager_users,
    )
    print("   ✓ login_view - available")
    print("   ✓ logout_view - available")
    print("   ✓ agent_dashboard - available")
    print("   ✓ agent_claims - available")
    print("   ✓ agent_claim_detail - available")
    print("   ✓ agent_upload_evidence - available")
    print("   ✓ manager_dashboard - available")
    print("   ✓ manager_claims - available")
    print("   ✓ manager_settings - available")
    print("   ✓ manager_users - available")
except ImportError as e:
    print(f"   ✗ Import error: {e}")

# Test 3: Check URLs
print("\n3. Testing URL configuration...")
try:
    from django.urls import reverse
    
    urls_to_test = [
        ('login', []),
        ('logout', []),
        ('dashboard', []),
        ('agent_dashboard', []),
        ('agent_claims', []),
        ('manager_dashboard', []),
        ('manager_claims', []),
        ('manager_settings', []),
        ('manager_users', []),
    ]
    
    for url_name, args in urls_to_test:
        try:
            url = reverse(url_name, args=args)
            print(f"   ✓ {url_name} -> {url}")
        except Exception as e:
            print(f"   ✗ {url_name} -> Error: {e}")
            
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 4: Check templates exist
print("\n4. Checking templates...")
import os
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
templates_to_check = [
    'base.html',
    'base_auth.html',
    'login.html',
    'agent/dashboard.html',
    'agent/claims.html',
    'agent/claim_detail.html',
    'manager/dashboard.html',
    'manager/claims.html',
    'manager/settings.html',
    'manager/users.html',
]

for template in templates_to_check:
    template_path = os.path.join(template_dir, template)
    if os.path.exists(template_path):
        print(f"   ✓ {template}")
    else:
        print(f"   ✗ {template} - NOT FOUND")

# Test 5: User stats
print("\n5. User statistics...")
total_users = User.objects.count()
managers = User.objects.filter(role='MANAGER').count()
agents = User.objects.filter(role='AGENT').count()
print(f"   Total users: {total_users}")
print(f"   Managers: {managers}")
print(f"   Agents: {agents}")

# List users
print("\n   Existing users:")
for user in User.objects.all():
    print(f"   - {user.username} ({user.role}) - {user.email}")

# Test 6: Claim stats
print("\n6. Claim statistics...")
total_claims = Claim.objects.count()
print(f"   Total claims: {total_claims}")

print("\n" + "=" * 60)
print("Phase 6 tests completed!")
print("=" * 60)

# Show frontend URLs
print("\nFrontend URLs:")
print("  /login/          - Login page")
print("  /                - Dashboard redirect (based on role)")
print("  /agent/          - Agent dashboard")
print("  /agent/claims/   - Agent claims list")
print("  /agent/claims/<id>/ - Claim detail")
print("  /manager/        - Manager dashboard")
print("  /manager/claims/ - Manager claims overview")
print("  /manager/settings/ - System settings")
print("  /manager/users/  - User management")

print("\nTest Credentials:")
print("  admin / admin123     (Superuser/Manager)")
print("  agent1 / password123 (Agent)")
print("  manager1 / password123 (Manager)")

print("\nTo test the frontend:")
print("  py -3.10 manage.py runserver")
print("  Then visit: http://127.0.0.1:8000/login/")
