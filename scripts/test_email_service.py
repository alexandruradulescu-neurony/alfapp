"""
Test script for the IMAP email processing service.
Tests the service functions without actually connecting to IMAP.
"""

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.communications.services import (
    decode_mime_header,
    extract_email_body,
    extract_from_email,
    parse_ai_response,
)
from apps.config.models import SystemSettings

print("=" * 60)
print("Testing IMAP Email Processing Service")
print("=" * 60)

# Test 1: SystemSettings
print("\n1. Testing SystemSettings...")
try:
    settings = SystemSettings.get_instance()
    print(f"   ✓ SystemSettings loaded: {settings}")
    print(f"   ✓ AI Prompt Template length: {len(settings.ai_prompt_template)} chars")
    print(f"   ✓ IMAP Host: {settings.imap_host}")
except Exception as e:
    print(f"   ✗ Error loading SystemSettings: {e}")

# Test 2: MIME Header Decoding
print("\n2. Testing MIME Header Decoding...")
test_headers = [
    ('John Doe <john@example.com>', 'John Doe <john@example.com>'),
    ('=?UTF-8?B?VGVzdA==?=', 'Test'),  # Base64 encoded "Test"
    ('simple@example.com', 'simple@example.com'),
]
for input_val, expected_contains in test_headers:
    result = decode_mime_header(input_val)
    print(f"   Input: {input_val[:30]}... -> Output: {result[:30]}...")

# Test 3: Email Extraction
print("\n3. Testing Email Extraction from headers...")
import email

test_from_headers = [
    'John Doe <john@example.com>',
    'jane@test.org',
    '=?UTF-8?B?VGVzdA==?= <test@example.com>',
]
for header in test_from_headers:
    msg = email.message.EmailMessage()
    msg['From'] = header
    result = extract_from_email(msg)
    print(f"   From: {header[:40]}... -> Email: {result}")

# Test 4: AI Response Parsing
print("\n4. Testing AI Response Parsing...")
test_responses = [
    '{"summary": "Customer lost laptop", "sentiment": "Urgent", "action_required": true}',
    '{"Summary": "Bag found", "Sentiment": "Positive", "Action_Required": false}',
    'The customer is frustrated and needs immediate assistance.',  # Non-JSON fallback
    'Invalid JSON response from AI',
]
for response in test_responses:
    result = parse_ai_response(response)
    print(f"   Summary: {result['summary'][:30]}... | Sentiment: {result['sentiment']} | Action: {result['action_required']}")

# Test 5: Email Body Extraction
print("\n5. Testing Email Body Extraction...")
test_email = """From: test@example.com
To: support@lora.com
Subject: Lost Item
Content-Type: text/plain

Hello,
I lost my laptop on flight AA123.
Please help me recover it.

Thank you,
John
"""
msg = email.message_from_string(test_email)
body = extract_email_body(msg)
print(f"   Extracted body length: {len(body)} chars")
print(f"   Body preview: {body[:50]}...")

print("\n" + "=" * 60)
print("Service tests completed!")
print("=" * 60)

# Show how to run the actual processing
print("\nTo run the actual email processing:")
print("  from apps.communications.services import process_incoming_emails")
print("  stats = process_incoming_emails()")
print("  print(stats)")
