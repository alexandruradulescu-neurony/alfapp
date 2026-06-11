"""
Test script for the Zendesk integration service.
Tests the service functions without actually calling Zendesk API.
"""

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.config.models import SystemSettings
from apps.claims.models import Claim
from apps.communications.models import EmailLog

print("=" * 60)
print("Testing Zendesk Integration Service")
print("=" * 60)

# Test 1: SystemSettings Zendesk config
print("\n1. Testing SystemSettings Zendesk configuration...")
try:
    settings = SystemSettings.get_instance()
    print(f"   ✓ Zendesk Subdomain: {settings.zd_subdomain or '(not set)'}")
    print(f"   ✓ Zendesk Email: {settings.zd_email or '(not set)'}")
    print(f"   ✓ Zendesk Token: {'***' + settings.zd_token[-4:] if settings.zd_token else '(not set)'}")
    print(f"   ✓ Sidebar Secret Token: {'***' + settings.sidebar_secret_token[-4:] if settings.sidebar_secret_token else '(not set)'}")
except Exception as e:
    print(f"   ✗ Error loading SystemSettings: {e}")

# Test 2: Test Zendesk auth header generation
print("\n2. Testing Zendesk auth header generation...")
try:
    from apps.integrations.services import _get_zendesk_auth_headers, _get_zendesk_base_url
    
    base_url = _get_zendesk_base_url()
    print(f"   ✓ Base URL: {base_url}")
    
    headers = _get_zendesk_auth_headers()
    print(f"   ✓ Auth header generated: {headers.get('Authorization', '')[:30]}...")
    print(f"   ✓ Content-Type: {headers.get('Content-Type')}")
except ValueError as e:
    print(f"   ⚠ Configuration error (expected if not set up): {e}")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 3: Test service function signatures
print("\n3. Testing service function availability...")
try:
    from apps.integrations.services import (
        post_zendesk_comment,
        fetch_zendesk_comments,
        fetch_zendesk_ticket,
        create_zendesk_ticket,
    )
    print("   ✓ post_zendesk_comment - available")
    print("   ✓ fetch_zendesk_comments - available")
    print("   ✓ fetch_zendesk_ticket - available")
    print("   ✓ create_zendesk_ticket - available")
except ImportError as e:
    print(f"   ✗ Import error: {e}")

# Test 4: Test ZendeskSidebarView auth
print("\n4. Testing ZendeskSidebarView authentication...")
try:
    from apps.integrations.views import ZendeskSidebarAuth
    
    # Test with invalid token
    class MockRequest:
        headers = {'Authorization': 'Bearer wrong-token'}
    
    result = ZendeskSidebarAuth.authenticate(MockRequest())
    print(f"   ✓ Invalid token rejected: {not result}")
    
    # Test with valid token (if configured)
    system_settings = SystemSettings.get_instance()
    if system_settings.sidebar_secret_token:
        class MockRequestValid:
            headers = {'Authorization': f'Bearer {system_settings.sidebar_secret_token}'}
        
        result = ZendeskSidebarAuth.authenticate(MockRequestValid())
        print(f"   ✓ Valid token accepted: {result}")
    else:
        print("   ⚠ Sidebar token not configured, skipping valid token test")
        
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 5: Show sample claim data for sidebar testing
print("\n5. Sample claims for sidebar testing...")
claims = Claim.objects.all()[:5]
for claim in claims:
    emails_count = EmailLog.objects.filter(claim=claim).count()
    print(f"   - {claim.client_email}: status={claim.status}, emails={emails_count}, zd_ticket={claim.zd_ticket_id or 'None'}")

if not claims.exists():
    print("   (No claims found)")

print("\n" + "=" * 60)
print("Zendesk integration tests completed!")
print("=" * 60)

# Show API endpoints
print("\nZendesk API Endpoints:")
print("  GET  /api/integrations/zd/info/?email=<customer_email>")
print("       Auth: Authorization: Bearer <sidebar_secret_token>")
print("       Returns: claim_status, emails_processed, found")
print("")
print("  POST /api/integrations/zd/sync/")
print("       Body: {\"claim_id\": <id>}")
print("       Creates Zendesk ticket for claim")
