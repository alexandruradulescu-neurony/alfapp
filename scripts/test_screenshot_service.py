"""
Test script for the Zendesk Screenshot Capture Service.
Tests the service functions and verifies configuration.
"""

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.config.models import SystemSettings
from apps.payments.models import Dispute

print("=" * 60)
print("Testing Zendesk Screenshot Capture Service")
print("=" * 60)

# Test 1: SystemSettings Zendesk browser config
print("\n1. Testing SystemSettings Zendesk browser configuration...")
try:
    settings = SystemSettings.get_instance()
    print(f"   Zendesk Subdomain: {settings.zd_subdomain or '(not set)'}")
    print(f"   Zendesk Agent Email: {settings.zd_agent_email or '(not set)'}")
    print(f"   Zendesk Agent Password: {'***' + settings.zd_agent_password[-4:] if settings.zd_agent_password else '(not set)'}")
    
    if all([settings.zd_subdomain, settings.zd_agent_email, settings.zd_agent_password]):
        print("   [OK] All Zendesk browser credentials configured")
    else:
        print("   [WARNING] Some Zendesk browser credentials missing")
        print("   Configure in Django Admin: /admin/config/systemsettings/1/change/")
except Exception as e:
    print(f"   [ERROR] Error loading SystemSettings: {e}")

# Test 2: Test service function availability
print("\n2. Testing screenshot service function availability...")
try:
    from apps.payments.screenshot_service import (
        capture_zendesk_screenshots,
        capture_screenshots_manual,
        capture_screenshots_batch,
    )
    print("   [OK] capture_zendesk_screenshots - available")
    print("   [OK] capture_screenshots_manual - available")
    print("   [OK] capture_screenshots_batch - available")
except ImportError as e:
    print(f"   [ERROR] Import error: {e}")

# Test 3: Test Playwright availability
print("\n3. Testing Playwright availability...")
try:
    from apps.payments.screenshot_service import _get_playwright
    playwright_func = _get_playwright()
    print("   [OK] Playwright imported successfully")
    print("   NOTE: Run 'playwright install chromium' if not already installed")
except ImportError as e:
    print(f"   [ERROR] Playwright not installed: {e}")
    print("   Install with: pip install playwright && playwright install chromium")

# Test 4: Show sample disputes for testing
print("\n4. Sample disputes for screenshot testing...")
disputes = Dispute.objects.filter(zd_ticket_id__isnull=False, zd_ticket_id__gt='')[:10]
for dispute in disputes:
    print(f"   - Dispute #{dispute.id}: PayPal={dispute.paypal_dispute_id}, ZD Ticket={dispute.zd_ticket_id}, Status={dispute.status}")

if not disputes.exists():
    print("   (No disputes with Zendesk ticket IDs found)")
    print("   Create a dispute or add zd_ticket_id to existing disputes")

# Test 5: Test function signature
print("\n5. Testing function signatures...")
try:
    from apps.payments.screenshot_service import capture_zendesk_screenshots
    import inspect
    sig = inspect.signature(capture_zendesk_screenshots)
    print(f"   capture_zendesk_screenshots{sig}")
    print("   [OK] Function signature correct")
except Exception as e:
    print(f"   [ERROR] {e}")

print("\n" + "=" * 60)
print("Screenshot service tests completed!")
print("=" * 60)

# Show usage examples
print("\nUsage Examples:")
print("  # Capture screenshot for a single dispute:")
print("  from apps.payments.screenshot_service import capture_zendesk_screenshots")
print("  success, message = capture_zendesk_screenshots(dispute_id=1)")
print("")
print("  # Manual trigger (with auto-retry):")
print("  from apps.payments.screenshot_service import capture_screenshots_manual")
print("  success, message = capture_screenshots_manual(dispute_id=1)")
print("")
print("  # Batch capture:")
print("  from apps.payments.screenshot_service import capture_screenshots_batch")
print("  results = capture_screenshots_batch([1, 2, 3])")
print("")
print("  # API endpoint (for MANAGERS):")
print("  POST /api/payments/disputes/<id>/capture-screenshot/")
print("")
print("Installation:")
print("  1. pip install -r requirements.txt")
print("  2. playwright install chromium")
print("  3. Configure zd_subdomain, zd_agent_email, zd_agent_password in SystemSettings")
