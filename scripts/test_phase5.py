"""
Test script for Phase 5 - PayPal Webhook and PDF Generation.
"""

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.config.models import SystemSettings
from apps.claims.models import Claim
from apps.communications.models import EmailLog

print("=" * 60)
print("Phase 5 - PayPal Webhook & PDF Generation Tests")
print("=" * 60)

# Test 1: SystemSettings PayPal config
print("\n1. Testing SystemSettings PayPal configuration...")
try:
    settings = SystemSettings.get_instance()
    print(f"   ✓ PayPal Client ID: {'***' + settings.paypal_client_id[-4:] if settings.paypal_client_id else '(not set)'}")
    print(f"   ✓ PayPal Secret: {'***' + settings.paypal_secret[-4:] if settings.paypal_secret else '(not set)'}")
    print(f"   ✓ PayPal Webhook ID: {settings.paypal_webhook_id or '(not set)'}")
    print(f"   ✓ PayPal Mode: {settings.paypal_mode if hasattr(settings, 'paypal_mode') else 'sandbox'}")
except Exception as e:
    print(f"   ✗ Error loading SystemSettings: {e}")

# Test 2: Test utility function availability
print("\n2. Testing utility function availability...")
try:
    from apps.payments.utils import generate_proof_of_work_pdf, _get_weasyprint
    print("   ✓ generate_proof_of_work_pdf - available")
    print("   ✓ _get_weasyprint - available")
    
    # Test WeasyPrint availability
    HTML, CSS = _get_weasyprint()
    if HTML and CSS:
        print("   ✓ WeasyPrint library - loaded successfully")
    else:
        print("   ⚠ WeasyPrint library - not available (GTK+ not installed)")
        print("      Install from: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html")
except ImportError as e:
    print(f"   ✗ Import error: {e}")

# Test 3: Test PayPal dispute webhook view
print("\n3. Testing PayPal dispute webhook view...")
try:
    from apps.payments.views import PayPalDisputeWebhookView
    print("   ✓ PayPalDisputeWebhookView - available")
except ImportError as e:
    print(f"   ✗ Import error: {e}")

# Test 4: Test proof of work endpoint in ClaimViewSet
print("\n4. Testing ClaimViewSet proof-of-work action...")
try:
    from apps.claims.views import ClaimViewSet
    viewset = ClaimViewSet()
    if hasattr(viewset, 'proof_of_work'):
        print("   ✓ proof_of_work action - available on ClaimViewSet")
    else:
        print("   ✗ proof_of_work action - NOT found on ClaimViewSet")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 5: Sample claims for PDF testing
print("\n5. Sample claims for PDF testing...")
claims = Claim.objects.all()[:5]
for claim in claims:
    evidence_count = claim.evidence.count()
    emails_count = EmailLog.objects.filter(claim=claim).count()
    print(f"   - Claim #{claim.id}: {claim.client_email}, status={claim.status}, evidence={evidence_count}, emails={emails_count}, zd_ticket={claim.zd_ticket_id or 'None'}")

if not claims.exists():
    print("   (No claims found)")

# Test 6: Test PDF generation (if WeasyPrint available)
print("\n6. Testing PDF generation...")
HTML, CSS = _get_weasyprint()
if HTML and CSS and claims.exists():
    test_claim = claims.first()
    print(f"   Attempting to generate PDF for claim #{test_claim.id}...")
    
    try:
        pdf_bytes = generate_proof_of_work_pdf(test_claim)
        if pdf_bytes:
            print(f"   ✓ PDF generated successfully ({len(pdf_bytes)} bytes)")
        else:
            print("   ✗ PDF generation returned None")
    except Exception as e:
        print(f"   ✗ Error generating PDF: {e}")
else:
    print("   ⚠ Skipping PDF generation test (WeasyPrint not available or no claims)")

print("\n" + "=" * 60)
print("Phase 5 tests completed!")
print("=" * 60)

# Show API endpoints
print("\nPayPal & PDF API Endpoints:")
print("  POST /api/payments/paypal/dispute-webhook/")
print("       PayPal webhook for dispute notifications")
print("       Handles: CUSTOMER.DISPUTE.CREATED")
print("")
print("  GET /api/claims/{id}/proof-of-work/")
print("       Download proof of work PDF (MANAGER only)")
print("       Returns: application/pdf file download")

# WeasyPrint installation instructions
print("\n" + "=" * 60)
print("WeasyPrint Installation (for PDF generation):")
print("=" * 60)
print("""
On Windows:
  1. Download GTK+ installer from:
     https://github.com/tschoonhoven/GTK-for-Windows/releases
  2. Install GTK+
  3. pip install weasyprint (already done)

On macOS:
  brew install pango glib

On Linux:
  apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0

Then restart the Django server.
""")
