# LORA API Documentation

**Version:** 1.6.0  
**Base URL:** `http://localhost:8000` (development)  
**Framework:** Django REST Framework

---

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [AI Agent Chat API](#ai-agent-chat-api)
- [Claims API](#claims-api)
- [Refunds API](#refunds-api)
- [Communications API](#communications-api)
- [Service Control API](#service-control-api)
- [Webhook Endpoints](#webhook-endpoints)
- [Error Handling](#error-handling)
- [Rate Limiting](#rate-limiting)

---

## Overview

The LORA API provides programmatic access to the Lost Object Recovery Automation platform. All API endpoints return JSON responses and require authentication.

### Base URL

**Development:**
```
http://localhost:8000
```

**Production:**
```
https://your-lora-domain.com
```

### API Versioning

Current API version: **v1** (implicit, no version prefix in URLs)

### Response Format

All responses are JSON unless otherwise specified:

```json
{
  "success": true,
  "data": {...},
  "message": "Optional message"
}
```

### HTTP Verbs

| Verb | Description |
|------|-------------|
| `GET` | Retrieve resource(s) |
| `POST` | Create resource or trigger action |
| `PUT` | Full update of resource |
| `PATCH` | Partial update of resource |
| `DELETE` | Delete resource |

---

## Authentication

### Session Authentication (Primary)

Most API endpoints use Django session authentication.

**Requirements:**
- User must be logged in via `/login/`
- Session cookie must be included in requests
- CSRF token required for POST/PUT/PATCH/DELETE

**Example:**
```bash
curl -X GET http://localhost:8000/api/claims/ \
  -H "Cookie: sessionid=abc123..." \
  -H "X-CSRFToken: xyz789..."
```

### Obtaining CSRF Token

**From HTML:**
```html
<meta name="csrf-token" content="xyz789...">
```

**From Cookie:**
```javascript
const csrftoken = document.cookie
  .split('; ')
  .find(row => row.startsWith('csrftoken='))
  .split('=')[1];
```

### Token Authentication (Future)

Token authentication is planned for future versions to support third-party integrations.

---

## AI Agent Chat API

### POST /api/agent/chat/

Process a chat message and return an AI-generated response about claims.

**Authentication:** Required (Session)  
**Permissions:** MANAGER or AGENT role

#### Request

**Headers:**
```
Content-Type: application/json
X-CSRFToken: <csrf-token>
Cookie: sessionid=<session-id>
```

**Body:**
```json
{
  "message": "What's the status of ALF1234567?",
  "conversationHistory": [
    {
      "role": "user",
      "content": "Previous user message"
    },
    {
      "role": "assistant",
      "content": "Previous AI response"
    }
  ]
}
```

**Request Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | Yes | User's chat message (natural language) |
| `conversationHistory` | array | No | Previous messages for context (max 10) |

**Conversation History Item:**

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | Either "user" or "assistant" |
| `content` | string | Message content |

#### Response

**Success (200 OK):**
```json
{
  "answer": "Claim ALF1234567 is currently in 'Found' status. The item was located on March 15, 2026.\n\nCustomer: emma.williamson@example.com\nFlight: BA2492 from London to New York on March 10, 2026\nObject: Black Samsonite carry-on suitcase with red ribbon\n\nThe claim has 3 email exchanges and 1 refund record.",
  "sources": ["LORA", "EmailLog", "Refund", "Zendesk"],
  "claims": [
    {
      "id": 123,
      "alf_claim_id": "ALF1234567",
      "client_email": "emma.williamson@example.com",
      "status": "Found",
      "zd_ticket_id": "12345",
      "flight_details": "BA2492 from London to New York on March 10, 2026",
      "object_description": "Black Samsonite carry-on suitcase with red ribbon",
      "phone": "+1-555-123-4567",
      "alternate_email": null,
      "created_at": "March 11, 2026",
      "ai_summary": "Customer reported lost black Samsonite suitcase on flight BA2492."
    }
  ],
  "success": true
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `answer` | string | AI-generated natural language response |
| `sources` | string[] | Data sources used (LORA, EmailLog, Refund, Zendesk, Timeline) |
| `claims` | object[] | Claim data dictionaries referenced in response |
| `success` | boolean | Always `true` for successful requests |

**Claim Object Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Database claim ID |
| `alf_claim_id` | string | ALF claim ID (format: ALF1234567) |
| `client_email` | string | Customer email address |
| `status` | string | Claim status display value |
| `zd_ticket_id` | string | Zendesk ticket ID |
| `flight_details` | string | Flight information |
| `object_description` | string | Lost item description |
| `phone` | string | Customer phone number |
| `alternate_email` | string | Alternate email address |
| `created_at` | string | Creation date (formatted) |
| `ai_summary` | string | AI-generated summary |

**Error - Bad Request (400):**
```json
{
  "error": "Message is required"
}
```

**Error - Server Error (500):**
```json
{
  "error": "Failed to process message",
  "details": "Specific error details",
  "message": "An unexpected error occurred. Please check the server logs and try again."
}
```

**Error - AI Not Configured:**
```json
{
  "answer": "⚠️ **AI Not Configured**\n\nThe AI API key is not configured...",
  "sources": [],
  "claims": [],
  "success": true
}
```

#### HTTP Status Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Bad Request (missing required field) |
| 401 | Unauthorized (not logged in) |
| 403 | Forbidden (insufficient permissions) |
| 500 | Internal Server Error |

#### Example Request (JavaScript)

```javascript
async function chatWithAgent(message, conversationHistory = []) {
  const response = await fetch('/api/agent/chat/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCookie('csrftoken'),
    },
    body: JSON.stringify({
      message: message,
      conversationHistory: conversationHistory,
    }),
  });

  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }

  return await response.json();
}

// Usage
const result = await chatWithAgent('What\'s the status of ALF1234567?');
console.log(result.answer);
```

#### Example Request (Python)

```python
import requests

def chat_with_agent(message, conversation_history=None):
    session = requests.Session()
    # Login first to get session cookie
    session.post('http://localhost:8000/login/', {
        'username': 'agent',
        'password': 'password'
    })
    
    # Get CSRF token
    response = session.get('http://localhost:8000/agent/chat/')
    csrf_token = response.cookies.get('csrftoken')
    
    # Send chat message
    response = session.post(
        'http://localhost:8000/api/agent/chat/',
        json={
            'message': message,
            'conversationHistory': conversation_history or []
        },
        headers={
            'X-CSRFToken': csrf_token,
            'Content-Type': 'application/json'
        }
    )
    
    return response.json()

# Usage
result = chat_with_agent('What\'s the status of ALF1234567?')
print(result['answer'])
```

#### Example Request (cURL)

```bash
# First, login to get session cookie
curl -c cookies.txt -b cookies.txt \
  -X POST http://localhost:8000/login/ \
  -d "username=agent&password=password"

# Get CSRF token from cookie
CSRF_TOKEN=$(grep csrftoken cookies.txt | awk '{print $7}')

# Send chat message
curl -c cookies.txt -b cookies.txt \
  -X POST http://localhost:8000/api/agent/chat/ \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $CSRF_TOKEN" \
  -d '{
    "message": "What'\''s the status of ALF1234567?",
    "conversationHistory": []
  }'
```

---

## Claims API

### GET /api/claims/

List all claims with optional filtering.

**Authentication:** Required

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by status |
| `assigned_to` | integer | Filter by assigned user ID |
| `search` | string | Search by email, ALF ID, or Zendesk ID |
| `page` | integer | Page number for pagination |

**Response:**
```json
{
  "count": 50,
  "next": "http://localhost:8000/api/claims/?page=2",
  "previous": null,
  "results": [
    {
      "id": 123,
      "alf_claim_id": "ALF1234567",
      "client_email": "customer@example.com",
      "status": "Found",
      "assigned_to": 5,
      "created_at": "2026-03-11T10:30:00Z"
    }
  ]
}
```

### POST /api/claims/

Create a new claim.

**Request Body:**
```json
{
  "alf_claim_id": "ALF1234567",
  "client_email": "customer@example.com",
  "status": "Received",
  "flight_details": "BA2492 from London to New York",
  "object_description": "Black suitcase"
}
```

### GET /api/claims/{id}/

Get claim details.

**Response:**
```json
{
  "id": 123,
  "alf_claim_id": "ALF1234567",
  "client_email": "customer@example.com",
  "status": "Found",
  "flight_details": "BA2492 from London to New York",
  "object_description": "Black suitcase",
  "zd_ticket_id": "12345",
  "ai_summary": "Customer reported lost item..."
}
```

### PUT /api/claims/{id}/

Update claim (full update).

### PATCH /api/claims/{id}/

Update claim (partial update).

**Request Body:**
```json
{
  "status": "Shipped"
}
```

### DELETE /api/claims/{id}/

Delete claim.

### POST /api/claims/{id}/update-from-zendesk/

Update claim data from Zendesk ticket.

**Response:**
```json
{
  "message": "Claim updated successfully",
  "updates": {
    "phone": "+1-555-123-4567",
    "alternate_email": "alt@example.com"
  },
  "llm_summary": "Customer provided phone number in recent comment",
  "timeline_entry_id": 15
}
```

---

## Refunds API

### GET /api/payments/refunds/

List all refunds.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by status |
| `claim` | integer | Filter by claim ID |
| `refund_type` | string | Filter by type (FULL, PARTIAL) |

### POST /api/payments/refunds/

Create manual refund record.

**Request Body:**
```json
{
  "claim_id": 123,
  "amount": "50.00",
  "currency": "USD",
  "refund_type": "FULL",
  "reason": "Customer request"
}
```

### POST /api/payments/refunds/process/

Process refund via PayPal API.

**Request Body:**
```json
{
  "claim_id": 123,
  "amount": "50.00",
  "currency": "USD",
  "refund_type": "FULL",
  "reason": "Customer request - item not found"
}
```

**Response:**
```json
{
  "message": "Refund initiated successfully",
  "refund": {
    "id": 42,
    "paypal_refund_id": "REF-123456789",
    "amount": "50.00",
    "status": "REQUESTED"
  }
}
```

### GET /api/payments/refunds/stats/

Get refund statistics.

**Response:**
```json
{
  "total_refunds": 25,
  "total_amount": 1250.00,
  "by_status": {
    "REQUESTED": 5,
    "PENDING": 3,
    "PROCESSING": 2,
    "COMPLETED": 15,
    "FAILED": 2,
    "CANCELLED": 1
  },
  "by_type": {
    "FULL": 20,
    "PARTIAL": 5
  },
  "by_source": {
    "LORA": 10,
    "WOOCOMMERCE": 10,
    "MANUAL": 5
  }
}
```

---

## Communications API

### GET /api/communications/email-logs/

List email logs.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `claim` | integer | Filter by claim ID |
| `category` | string | Filter by category |
| `action_required` | boolean | Filter by action required |

### POST /api/communications/email-logs/

Create email log entry.

---

## Service Control API

### GET /api/services/status/

List all service statuses.

**Response:**
```json
[
  {
    "service": "AI",
    "status": "connected",
    "is_enabled": true,
    "last_checked": "2026-03-18T10:30:00Z",
    "last_error": null
  },
  {
    "service": "IMAP",
    "status": "connected",
    "is_enabled": true,
    "last_checked": "2026-03-18T10:30:00Z",
    "last_error": null
  }
]
```

### POST /api/services/{service}/test/

Test service connection.

**Example:**
```bash
curl -X POST http://localhost:8000/api/services/AI/test/ \
  -H "X-CSRFToken: xyz789..."
```

**Response:**
```json
{
  "status": "connected",
  "message": "AI API is reachable",
  "response_time_ms": 245
}
```

### POST /api/services/{service}/toggle/

Toggle service enabled state.

**Request Body:**
```json
{
  "enabled": false
}
```

### POST /api/services/scheduler/start/

Start email processing scheduler.

### POST /api/services/scheduler/stop/

Stop email processing scheduler.

---

## Webhook Endpoints

### POST /api/integrations/zd/claim-webhook/

Create claim from Zendesk ticket.

**Headers:**
```
X-Webhook-Secret: your-sidebar-secret-token
```

**Body:**
```json
{
  "ticket_id": "12345",
  "subject": "Lost Item - ALF1234567",
  "requester": {
    "email": "customer@example.com"
  },
  "status": "investigation_initiated"
}
```

**Response:**
```json
{
  "message": "Claim created successfully",
  "claim_id": 42,
  "alf_claim_id": "ALF1234567",
  "zd_ticket_id": "12345",
  "llm_extraction_failed": false
}
```

### POST /api/integrations/zd/refund-webhook/

Handle refund notifications from PayPal/WooCommerce.

### POST /api/integrations/zd/status-webhook/

Handle Zendesk ticket status changes.

### POST /api/payments/paypal/webhook/

Handle PayPal webhook events.

---

## Error Handling

### Standard Error Response Format

```json
{
  "error": "Error type or message",
  "details": "Detailed error description (optional)",
  "field": "field_name (optional, for validation errors)"
}
```

### HTTP Status Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 201 | Created |
| 204 | No Content (successful delete) |
| 400 | Bad Request (invalid input) |
| 401 | Unauthorized (not logged in) |
| 403 | Forbidden (insufficient permissions) |
| 404 | Not Found |
| 409 | Conflict (duplicate resource) |
| 500 | Internal Server Error |

### Validation Errors

```json
{
  "error": "Validation failed",
  "fields": {
    "email": ["Enter a valid email address."],
    "amount": ["Ensure this value is greater than 0."]
  }
}
```

---

## Rate Limiting

### Current Implementation

Rate limiting is not currently enforced but is planned for future versions.

### Planned Limits

| Endpoint | Limit |
|----------|-------|
| `/api/agent/chat/` | 10 requests/minute |
| `/api/claims/` | 60 requests/minute |
| `/api/payments/refunds/` | 30 requests/minute |
| Webhooks | 100 requests/minute |

### Rate Limit Response

```json
{
  "error": "Rate limit exceeded. Try again in 45 seconds.",
  "retry_after": 45
}
```

**HTTP Header:**
```
Retry-After: 45
```

---

## Appendix

### Claim Statuses

| Status | Description |
|--------|-------------|
| `Received` | Claim received, initial review |
| `Searching` | Actively searching for item |
| `Found` | Item located |
| `Shipped` | Item shipped to customer |
| `Disputed` | PayPal dispute opened |
| `REFUND_REQUESTED` | Refund requested |
| `REFUNDED` | Full refund completed |
| `PARTIALLY_REFUNDED` | Partial refund completed |

### Refund Statuses

| Status | Description |
|--------|-------------|
| `REQUESTED` | Refund requested, not yet processed |
| `PENDING` | Awaiting approval |
| `PROCESSING` | Being processed via PayPal |
| `COMPLETED` | Refund completed successfully |
| `FAILED` | Refund processing failed |
| `CANCELLED` | Refund cancelled |

### Email Categories

| Category | Description |
|----------|-------------|
| `OBJECT_FOUND` | Customer found item |
| `OBJECT_NOT_FOUND` | Item not found (auto-resolvable) |
| `RESUBMISSION_REQUIRED` | Need more information |
| `SUBMISSION_CONFIRMATION` | Form submission acknowledgment |
| `GENERAL_CORRESPONDENCE` | Other communication |
| `UNKNOWN` | Cannot categorize |

---

**End of API Documentation**
