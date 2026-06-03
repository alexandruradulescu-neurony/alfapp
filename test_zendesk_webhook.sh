#!/bin/bash

# Test Zendesk Webhook - Simulate webhook payload from Zendesk
# Usage: ./test_zendesk_webhook.sh [ticket_id] [subject]

# Configuration
WEBHOOK_URL="https://neurozen.ngrok.dev/api/integrations/zd/claim-webhook/"
# WEBHOOK_URL="http://localhost:8000/api/integrations/zd/claim-webhook/"

# Custom status ID for "Investigation Initiated"
INVESTIGATION_STATUS_ID="11688538967068"

# Generate ticket ID (use provided or random)
TICKET_ID="${1:-$(date +%s)}"
SUBJECT="${2:-Lost Item - ALF$(date +%s | tail -c 8)}"

# Current timestamp
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "==================================="
echo "  Zendesk Webhook Test"
echo "==================================="
echo ""
echo "Ticket ID: $TICKET_ID"
echo "Subject: $SUBJECT"
echo "Status ID: $INVESTIGATION_STATUS_ID"
echo "URL: $WEBHOOK_URL"
echo ""

# Create the webhook payload (matching Zendesk format)
PAYLOAD=$(cat <<EOF
{
  "account_id": 22243063,
  "detail": {
    "actor_id": "8645878250110",
    "assignee_id": null,
    "brand_id": "8475923139582",
    "created_at": "$TIMESTAMP",
    "custom_status": "$INVESTIGATION_STATUS_ID",
    "description": "Customer reported losing their luggage during flight. Please investigate.",
    "external_id": null,
    "form_id": "8475923126142",
    "group_id": "8475923147902",
    "id": "$TICKET_ID",
    "is_public": true,
    "organization_id": null,
    "priority": null,
    "requester_id": "8645878250110",
    "status": "OPEN",
    "subject": "$SUBJECT",
    "submitter_id": "8645878250110",
    "tags": [
      "lost_item",
      "flight_ba2492"
    ],
    "type": "TASK",
    "updated_at": "$TIMESTAMP",
    "via": {
      "channel": "web_service"
    }
  },
  "event": {
    "current": "$INVESTIGATION_STATUS_ID",
    "meta": {
      "sequence": {
        "id": 39313930383633353634323835,
        "position": 1
      }
    },
    "previous": null
  },
  "id": "$(uuidgen 2>/dev/null || echo "test-$(date +%s)")",
  "subject": "zen:ticket:$TICKET_ID",
  "time": "${TIMESTAMP%.000000000Z}.000000000Z",
  "type": "zen:event-type:ticket.custom_status_changed",
  "zendesk_event_version": "2022-11-06"
}
EOF
)

echo "Payload:"
echo "$PAYLOAD" | python3 -m json.tool 2>/dev/null || echo "$PAYLOAD"
echo ""
echo "Sending webhook..."
echo ""

# Send the webhook
RESPONSE=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: ${SIDEBAR_SECRET_TOKEN:-}" \
  -d "$PAYLOAD")

# Extract body and status code
BODY=$(echo "$RESPONSE" | sed '$d')
HTTP_CODE=$(echo "$RESPONSE" | grep "HTTP_CODE:" | cut -d: -f2)

echo "Response (HTTP $HTTP_CODE):"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
echo ""

# Check result
if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ Webhook accepted successfully"
elif [ "$HTTP_CODE" = "201" ]; then
    echo "✅ Claim created successfully"
else
    echo "❌ Webhook failed (HTTP $HTTP_CODE)"
fi

echo ""
echo "==================================="
