# AI Agent Chat Documentation

**Version:** 1.6.0  
**Author:** LORA Development Team  
**Last Updated:** March 18, 2026

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How It Works](#how-it-works)
- [Configuration](#configuration)
- [Usage Guide](#usage-guide)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)
- [Security Considerations](#security-considerations)
- [Best Practices](#best-practices)

---

## Overview

The AI Agent Chat is a ChatGPT-like interface that allows LORA users to query claim information using natural language. Instead of navigating through multiple pages and filters, agents can simply ask questions like:

- "What's the status of ALF1234567?"
- "Show me emails for Emma Williamson"
- "Has this claim been refunded?"

The system automatically detects claim IDs, searches by customer name or email, and maintains conversation context across multiple questions.

### Key Features

| Feature | Description |
|---------|-------------|
| **Natural Language** | Ask questions in plain English |
| **Claim Detection** | Auto-detect ALF claim IDs from messages |
| **Customer Search** | Find claims by name or email address |
| **Context Persistence** | Maintain conversation history for follow-ups |
| **Multi-Source Data** | Aggregate data from claims, emails, refunds, Zendesk |
| **LLM Responses** | DeepSeek AI generates natural language answers |
| **Hallucination Prevention** | Uses ONLY provided database data |

### Access Requirements

- **User Role**: MANAGER or AGENT
- **Configuration**: AI API key must be configured
- **URL**: `/agent/chat` or via sidebar navigation

---

## Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (Browser)                        │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  chat.html                                                 │  │
│  │  • Chat interface (message bubbles)                        │  │
│  │  • Loading states                                          │  │
│  │  • Conversation history display                            │  │
│  │  • Claim context indicators                                │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP (AJAX)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Django Backend                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  AgentChatAPIView (views.py)                               │  │
│  │  • POST /api/agent/chat/                                   │  │
│  │  • Authentication & authorization                          │  │
│  │  • Request validation                                      │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  AgentChatService (services.py)                            │  │
│  │  • process_message()                                       │  │
│  │  • detect_claim_ids()                                      │  │
│  │  • detect_name_or_email()                                  │  │
│  │  • fetch_context()                                         │  │
│  │  • build_prompt()                                          │  │
│  │  • _call_llm()                                             │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│              ┌───────────────┼───────────────┐                   │
│              ▼               ▼               ▼                   │
│  ┌────────────────┐ ┌────────────────┐ ┌────────────────┐       │
│  │ Claim Model    │ │ EmailLog Model │ │ Refund Model   │       │
│  │ (apps.claims)  │ │ (apps.comm.)   │ │ (apps.payments)│       │
│  └────────────────┘ └────────────────┘ └────────────────┘       │
│              │                                   │               │
│              ▼                                   ▼               │
│  ┌────────────────────────────────────────────────────────┐     │
│  │              Zendesk Integration Service                │     │
│  │  • fetch_zendesk_ticket()                               │     │
│  │  • fetch_zendesk_comments()                             │     │
│  └────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTPS
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      External Services                           │
│  ┌────────────────┐                              ┌────────────┐ │
│  │ DeepSeek API   │                              │ Zendesk    │ │
│  │ (LLM)          │                              │ API        │ │
│  └────────────────┘                              └────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
User Message
    │
    ▼
┌─────────────────────────────┐
│ 1. Detect Claim IDs         │
│    • Regex: ALF\d{7}        │
│    • Email pattern          │
│    • Name pattern           │
└─────────────────────────────┘
    │
    ├──────┐
    │      │ (if no claim detected)
    │      ▼
    │ ┌─────────────────────────┐
    │ │ 1b. Search by Name/     │
    │ │     Email               │
    │ └─────────────────────────┘
    │      │
    │      ▼
    │ ┌─────────────────────────┐
    │ │ 1c. Check Conversation  │
    │ │     History             │
    │ └─────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 2. Fetch Context            │
│    • Claim details          │
│    • Email history (10)     │
│    • Refund records         │
│    • Timeline updates       │
│    • Zendesk ticket         │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 3. Build Prompt             │
│    • System instructions    │
│    • Context data           │
│    • Conversation history   │
│    • User message           │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 4. Call LLM (DeepSeek)      │
│    • System prompt          │
│    • User prompt            │
│    • Temperature: 0.7       │
│    • Max tokens: 1000       │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 5. Return Response          │
│    • Natural language       │
│    • Sources list           │
│    • Claim data             │
└─────────────────────────────┘
```

---

## How It Works

### Claim Detection

The system uses multiple strategies to detect which claim the user is asking about:

#### 1. ALF ID Pattern Matching

**Pattern:** `ALF[-_]?\d{7}` (case insensitive)

**Matches:**
- `ALF1234567`
- `ALF-1234567`
- `ALF_1234567`
- `alf1234567`

**Implementation:**
```python
import re
claim_id_pattern = re.compile(r'ALF[-_]?\d{7}', re.IGNORECASE)
matches = claim_id_pattern.findall(message)
# Normalize: ALF-1234567 → ALF1234567
```

#### 2. Email Detection

**Pattern:** Standard email regex

**Matches:**
- `customer@example.com`
- `john.doe@company.org`

**Implementation:**
```python
email_pattern = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
emails = email_pattern.findall(message)
```

#### 3. Name Detection

**Pattern:** Two or more consecutive words (potential customer name)

**Keywords:** Looks for names after words like "for", "about", "regarding", "customer"

**Examples:**
- "Find claims **for emma williamson**"
- "Ticket **for john doe**"
- "Search **regarding jane smith**"

**Implementation:**
```python
name_pattern = re.compile(r'\b[a-z]+\s+[a-z]+\b', re.IGNORECASE)
# Search after keywords
for keyword in ['for', 'about', 'regarding']:
    if keyword in message_lower:
        names = name_pattern.findall(after_keyword)
```

#### 4. Conversation History Context

If no claim is detected in the current message, the system checks the last 6 messages in the conversation history for claim references.

**Example:**
```
User: What's the status of ALF1234567?
AI: Claim ALF1234567 is in "Found" status...
User: When was it found?  ← No claim ID, but context maintained
AI: The item was found on March 15, 2026...
```

### Context Fetching

Once claim IDs are identified, the system fetches comprehensive data:

#### Claim Details
```python
claim_data = {
    'id': claim.id,
    'alf_claim_id': claim.alf_claim_id,
    'client_email': claim.client_email,
    'status': claim.get_status_display(),
    'zd_ticket_id': claim.zd_ticket_id,
    'flight_details': claim.flight_details,
    'object_description': claim.object_description,
    'phone': claim.phone,
    'alternate_email': claim.alternate_email,
    'created_at': claim.created_at.strftime('%B %d, %Y'),
    'ai_summary': claim.ai_summary,
}
```

#### Email History (Last 10)
```python
emails = EmailLog.objects.filter(claim=claim).order_by('-received_at')[:10]
# Includes full email body, not just summary
```

#### Refund Records
```python
refunds = Refund.objects.filter(claim=claim).order_by('-created_at')
# All refunds with status, amount, type, reason
```

#### Timeline Updates (Last 10)
```python
timeline = claim.updates.all().order_by('-created_at')[:10]
# Zendesk sync history with LLM summaries
```

#### Zendesk Ticket
```python
ticket = fetch_zendesk_ticket(claim.zd_ticket_id)
comments = fetch_zendesk_comments(claim.zd_ticket_id)[:5]
# Ticket status, subject, recent comments
```

### LLM Prompt Construction

The system builds a comprehensive prompt with:

1. **System Instructions**: Role definition and critical rules
2. **Conversation History**: Last 10 messages
3. **Claim Data**: All fetched context
4. **User Message**: Current question

**Example Prompt:**
```
You are a helpful AI assistant for LORA, a lost luggage recovery service.

You help agents by answering questions about claims using ONLY the data provided below.

CRITICAL RULES:
1. ONLY use information from the "Claim data" section below
2. NEVER make up or invent information
3. If information is not in the data, say "I don't have that information"
4. NEVER output JSON or structured data
5. Respond in natural, conversational English
6. Be specific - cite actual values from the data

Previous conversation:
User: What's the status of ALF1234567?
Assistant: Claim ALF1234567 is currently in "Found" status...

Claim data:
CLAIM: ALF1234567
Email: emma.williamson@example.com | Phone: +1-555-123-4567
Status: Found
Zendesk: #12345
Flight: BA2492 from London to New York on March 10, 2026
Object: Black Samsonite carry-on suitcase with red ribbon
Created: March 11, 2026

EMAIL [March 12, 2026]: Lost Item Claim
  Summary: Customer reporting lost black Samsonite suitcase
  Category: OBJECT_NOT_FOUND | Action Required: Yes
  Body: [Full email content...]

REFUND: USD 50.00 (COMPLETED) - Customer request - item found damaged

ZENDESK #12345: Solved - Lost Item - ALF1234567
  Agent Sarah: "Item located in baggage claim area"

User: Show me the email history

Assistant (use ONLY the claim data above):
```

### LLM Response Generation

**API Call:**
```python
from openai import OpenAI

client = OpenAI(api_key=api_key, base_url=api_base)

response = client.chat.completions.create(
    model=model,  # e.g., "deepseek-chat"
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ],
    temperature=0.7,
    max_tokens=1000,
)

answer = response.choices[0].message.content.strip()
```

**Safety Checks:**
- Detect and reject JSON responses
- Handle empty or error responses
- Log full prompt for debugging

---

## Configuration

### Environment Variables

Add to your `.env` file:

```bash
# AI Provider Configuration
AI_PROVIDER=DeepSeek
AI_API_BASE=https://api.deepseek.com/v1
AI_API_KEY=sk-your-api-key-here
AI_API_MODEL=deepseek-chat
```

| Variable | Description | Required | Example |
|----------|-------------|----------|---------|
| `AI_PROVIDER` | Provider name (for reference) | Yes | `DeepSeek` |
| `AI_API_BASE` | API endpoint URL | Yes | `https://api.deepseek.com/v1` |
| `AI_API_KEY` | API key (encrypted at rest) | Yes | `sk-...` |
| `AI_API_MODEL` | Model name to use | Yes | `deepseek-chat` |

### System Settings (Database)

Configure via **Manager → Configuration** or Django admin (`/admin/`):

1. Navigate to **Manager → Configuration**
2. Find the **AI Settings** section
3. Enter your credentials:
   - **AI API Key**: Your DeepSeek API key
   - **AI API Base**: `https://api.deepseek.com/v1`
   - **AI API Model**: `deepseek-chat`
4. Click **Save Settings**

| Setting | Type | Description | Encrypted |
|---------|------|-------------|-----------|
| `ai_api_key` | TextField | AI API key | Yes |
| `ai_api_base` | CharField | API endpoint URL | No |
| `ai_api_model` | CharField | Model name | No |

### Getting a DeepSeek API Key

1. Visit [DeepSeek Platform](https://platform.deepseek.com/)
2. Sign up or log in
3. Navigate to **API Keys** section
4. Create a new API key
5. Copy the key and store it securely
6. Add to `.env` and SystemSettings

### Verification

After configuration, verify the setup:

1. Go to **Manager → Configuration**
2. Check that AI settings are populated
3. Navigate to **AI Agent** in sidebar
4. Send a test message: "Test"
5. You should receive a response (or a message if no claim detected)

---

## Usage Guide

### Getting Started

1. **Access the Chat Interface**
   - Log in as Agent or Manager
   - Click **AI Agent** in the sidebar
   - Or navigate to `/agent/chat`

2. **Start a Conversation**
   - Type your question in the message box
   - Press Enter or click Send
   - Wait for the AI response (typically 2-5 seconds)

3. **Follow Up**
   - Ask additional questions without repeating the claim ID
   - The AI maintains context from previous messages

### Query Types

#### By Claim ID

**Format:** Include ALF claim ID in your message

**Examples:**
```
"What's the status of ALF1234567?"
"Show me emails for ALF-7654321"
"Has ALF1234567 been refunded?"
"Tell me about ALF_9876543"
```

#### By Customer Name

**Format:** Use customer's full name

**Examples:**
```
"Find claims for emma williamson"
"Search for john doe"
"Show me tickets regarding jane smith"
```

**Note:** If multiple claims are found, the AI will list them and ask you to specify.

#### By Email Address

**Format:** Include email address in your message

**Examples:**
```
"Search for customer@example.com"
"Find claims for john.doe@company.com"
```

#### Follow-up Questions

Once a claim context is established, you can ask follow-ups:

```
User: What's the status of ALF1234567?
AI: Claim ALF1234567 is in "Found" status...

User: When was it found?
AI: The item was found on March 15, 2026...

User: Show me the emails
AI: Here are the emails for ALF1234567...

User: Any refunds?
AI: Yes, there is 1 refund record...
```

### Best Practices

#### Do's

✅ **Be specific with claim IDs:**
```
"What's the status of ALF1234567?"
```

✅ **Use natural language:**
```
"Has this been refunded yet?"
```

✅ **Ask follow-up questions:**
```
"Show me the emails" (after asking about a claim)
```

✅ **Include context:**
```
"Find claims for emma williamson and show me the most recent one"
```

#### Don'ts

❌ **Vague questions:**
```
"Tell me about it" (without prior context)
```

❌ **Multiple claims in one question:**
```
"What about ALF1234567 and ALF7654321?" (ask separately)
```

❌ **Questions outside claim data:**
```
"What's the weather like?" (AI only knows about claims)
```

### Example Session

```
╔══════════════════════════════════════════════════════════════╗
║  AI Agent Chat                                    [Clear]    ║
╠══════════════════════════════════════════════════════════════╣
║                                                               ║
║  👤 You: What's the status of ALF1234567?                    ║
║                                                               ║
║  🤖 AI: Claim ALF1234567 is currently in "Found" status.     ║
║      The item was located on March 15, 2026.                 ║
║                                                               ║
║      Customer: emma.williamson@example.com                   ║
║      Flight: BA2492 from London to New York on March 10      ║
║      Object: Black Samsonite carry-on with red ribbon        ║
║                                                               ║
║      The claim has 3 email exchanges and 1 refund record.    ║
║                                                               ║
║  👤 You: Show me the email history                           ║
║                                                               ║
║  🤖 AI: Here are the emails for ALF1234567:                  ║
║                                                               ║
║      1. March 12, 2026 - "Lost Item Claim"                   ║
║         Summary: Customer reporting lost suitcase            ║
║         Category: OBJECT_NOT_FOUND | Action: Yes             ║
║                                                               ║
║      2. March 13, 2026 - "Re: Lost Item Claim"               ║
║         Summary: Follow-up asking for update                 ║
║         Category: GENERAL_CORRESPONDENCE | Action: No        ║
║                                                               ║
║      3. March 15, 2026 - "Great news!"                       ║
║         Summary: Customer notified item was found            ║
║         Category: OBJECT_FOUND | Action: No                  ║
║                                                               ║
║  👤 You: Has it been refunded?                               ║
║                                                               ║
║  🤖 AI: Yes, ALF1234567 has 1 refund record:                 ║
║                                                               ║
║      Amount: USD 50.00 (FULL)                                ║
║      Status: COMPLETED                                       ║
║      Reason: Customer request - item found damaged           ║
║      Processed: March 16, 2026                               ║
║                                                               ║
╚══════════════════════════════════════════════════════════════╝
```

---

## API Reference

### Endpoints

#### POST /api/agent/chat/

Process a chat message and return an AI-generated response.

**Authentication:** Required (Session or Token)  
**Permissions:** MANAGER or AGENT role

**Request Headers:**
```
Content-Type: application/json
X-CSRFToken: <csrf-token>  (for session auth)
```

**Request Body:**
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
| `message` | string | Yes | User's chat message |
| `conversationHistory` | array | No | Array of previous messages (max 10) |

**Response (Success):**
```json
{
  "answer": "Claim ALF1234567 is currently in 'Found' status...",
  "sources": ["LORA", "EmailLog", "Refund", "Zendesk"],
  "claims": [
    {
      "id": 123,
      "alf_claim_id": "ALF1234567",
      "client_email": "customer@example.com",
      "status": "Found",
      "zd_ticket_id": "12345",
      "flight_details": "BA2492 from London to New York",
      "object_description": "Black Samsonite carry-on",
      "phone": "+1-555-123-4567",
      "alternate_email": "alt@example.com",
      "created_at": "March 11, 2026",
      "ai_summary": "Customer reported lost item..."
    }
  ],
  "success": true
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `answer` | string | AI-generated natural language response |
| `sources` | array | List of data sources used |
| `claims` | array | Claim data dictionaries referenced |
| `success` | boolean | Always `true` for successful requests |

**Response (Error - Bad Request):**
```json
{
  "error": "Message is required"
}
```

**Response (Error - Server Error):**
```json
{
  "error": "Failed to process message",
  "details": "Error details...",
  "message": "An unexpected error occurred..."
}
```

**HTTP Status Codes:**

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Bad Request (missing message) |
| 401 | Unauthorized (not logged in) |
| 403 | Forbidden (insufficient permissions) |
| 500 | Internal Server Error |

#### GET /agent/chat/

Render the AI agent chat page.

**Authentication:** Required  
**Permissions:** MANAGER or AGENT role

**Response:** HTML page with chat interface

---

## Troubleshooting

### Common Issues

#### "AI Not Configured" Message

**Symptom:**
```
⚠️ AI Not Configured

The AI API key is not configured in SystemSettings.
```

**Cause:** AI API key is missing from SystemSettings.

**Solution:**
1. Go to **Manager → Configuration**
2. Find **AI Settings** section
3. Enter your **AI API Key**
4. Save settings
5. Refresh the chat page

#### No Claims Detected

**Symptom:**
```
I couldn't detect a claim ID in your message.
```

**Cause:** Message doesn't contain a recognizable claim ID, name, or email.

**Solution:**
- Include claim ID in format: `ALF1234567`, `ALF-1234567`, `ALF_1234567`
- Or search by customer name: "Find claims for emma williamson"
- Or search by email: "Search for customer@example.com"

#### Multiple Claims Found

**Symptom:**
```
I found 2 claims matching "john.doe@company.com":
- ALF7654321 - john.doe@company.com (Status: Searching)
- ALF9876543 - john.doe@company.com (Status: Shipped)

Please specify which claim you'd like to know more about.
```

**Cause:** Customer has multiple claims in the system.

**Solution:** Specify which claim by using the ALF ID:
```
"Show me ALF7654321"
```

#### Slow Responses

**Symptom:** AI takes 10+ seconds to respond.

**Possible Causes:**
1. Slow AI API connectivity
2. Large context (many claims, emails, refunds)
3. Network latency

**Solutions:**
1. Check AI API status in **Manager → Configuration**
2. Test AI connection using the **Test** button
3. Verify network connectivity to AI API endpoint
4. Consider reducing context size (fewer emails/timeline entries)

#### AI Returns JSON Instead of Natural Language

**Symptom:**
```
I apologize, but I encountered an error processing that request.
```

**Cause:** LLM returned JSON despite instructions not to.

**Solution:**
- This is caught by the system automatically
- Try rephrasing your question
- Check server logs for the actual JSON response
- Consider adjusting the system prompt if this happens frequently

#### Claim Not Found in Database

**Symptom:**
```
Claim ALF1234567 not found in LORA
```

**Cause:** Claim ID doesn't exist in the database.

**Solution:**
- Verify the claim ID is correct
- Check if claim was created from Zendesk webhook
- Manually create claim if needed

### Debug Mode

Enable debug logging to troubleshoot issues:

**1. Add to `.env`:**
```bash
DEBUG=True
```

**2. Configure logging in `lora_app/settings.py`:**
```python
LOGGING = {
    'version': 1,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'apps.agent': {'level': 'DEBUG'},
    },
}
```

**3. Check server logs:**
```
=== LLM PROMPT ===
[Full prompt sent to LLM]
=== END PROMPT ===

LLM response (first 500 chars): [Response preview]...
```

### Server Logs

Key log messages to watch for:

```
INFO - Agent chat request - User: username, Authenticated: True
INFO - === LLM PROMPT ===
INFO - LLM response (first 500 chars): ...
ERROR - Error in agent chat: [error details]
ERROR - LLM call failed: [error details]
```

### Testing AI Connectivity

**Via UI:**
1. Go to **Manager → Configuration**
2. Find **AI Settings** section
3. Click **Test Connection**
4. Check status indicator

**Via API:**
```bash
curl -X POST http://localhost:8000/api/services/AI/test/ \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: <token>"
```

**Expected Response:**
```json
{
  "status": "connected",
  "message": "AI API is reachable",
  "response_time_ms": 245
}
```

---

## Security Considerations

### Prompt Injection Prevention

**Risk:** Users might try to inject malicious prompts to bypass restrictions.

**Mitigation:**
1. **User content in user role only:**
   ```python
   messages=[
       {"role": "system", "content": system_prompt},  # Trusted
       {"role": "user", "content": prompt},  # User content here
   ]
   ```

2. **System prompt emphasizes rules:**
   ```
   CRITICAL RULES:
   1. ONLY use information from the "Claim data" section
   2. NEVER make up or invent information
   3. NEVER output JSON or structured data
   ```

3. **Response validation:**
   ```python
   if raw_response.strip().startswith('{') and 'summary' in raw_response.lower():
       # Reject JSON responses
       return "I apologize, but I encountered an error..."
   ```

### Data Access Control

**Authentication Required:**
- Only logged-in users can access the chat
- Session authentication with CSRF protection

**Role-Based Access:**
- MANAGER and AGENT roles only
- Enforced at view level:
  ```python
  permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
  ```

**Data Scope:**
- AI only accesses data the user already has permission to view
- No additional data exposure through AI

### Hallucination Prevention

**Risk:** LLM might invent information not in the database.

**Mitigation:**
1. **Explicit system prompt instructions:**
   ```
   CRITICAL RULES:
   1. ONLY use information from the "Claim data" section below
   2. NEVER make up or invent information
   3. If information is not in the data, say "I don't have that information"
   ```

2. **Context-only responses:**
   - All claim data is fetched from database
   - LLM only formats and summarizes, never invents

3. **Source attribution:**
   - Response includes `sources` list
   - Users can verify data origin

### API Key Security

**Encryption at Rest:**
- AI API key encrypted in database using Fernet
- Separate `ENCRYPTION_KEY` from `SECRET_KEY`

**Environment Variable:**
- Store in `.env` (not committed to version control)
- Use secrets management in production

**Access Control:**
- Only SystemSettings can access decrypted key
- Logged when used (but key itself not logged)

### Rate Limiting

**Current State:** No rate limiting on chat endpoint.

**Recommendation:** Implement rate limiting in production:
```python
from rest_framework.throttling import UserRateThrottle

class AgentChatThrottle(UserRateThrottle):
    rate = '10/minute'

class AgentChatAPIView(APIView):
    throttle_classes = [AgentChatThrottle]
```

### Logging and Audit

**What is logged:**
- User ID making request
- Full prompt sent to LLM (for debugging)
- LLM response (first 500 chars)
- Errors with stack traces

**What is NOT logged:**
- AI API key (encrypted, never logged)
- Full LLM response (only first 500 chars)
- User passwords or credentials

**Audit Trail:**
- Consider adding audit log for AI queries
- Track which claims were accessed via AI
- Useful for compliance and debugging

---

## Best Practices

### For Users

1. **Be Specific**
   - Include claim IDs when possible
   - Ask one question at a time
   - Use natural language

2. **Use Follow-ups**
   - The AI remembers context
   - No need to repeat claim IDs
   - Ask related questions in sequence

3. **Verify Critical Information**
   - AI summarizes database data
   - Double-check important details
   - Use AI for quick info, not final decisions

4. **Provide Feedback**
   - Report incorrect or confusing responses
   - Help improve the system

### For Developers

1. **Monitor AI Usage**
   - Track API calls and costs
   - Monitor response times
   - Set up alerts for failures

2. **Test Claim Detection**
   - Test various claim ID formats
   - Test name and email detection
   - Test conversation context

3. **Optimize Prompts**
   - Keep prompts concise but complete
   - Test different system prompts
   - Balance detail vs. token usage

4. **Handle Errors Gracefully**
   - Always provide fallback messages
   - Log errors for debugging
   - Don't expose internal errors to users

5. **Secure the Endpoint**
   - Enforce authentication
   - Implement rate limiting
   - Validate all inputs

### For Administrators

1. **Configure AI Properly**
   - Use production API keys
   - Set appropriate models
   - Test before deploying

2. **Monitor Costs**
   - Track AI API usage
   - Set budget alerts
   - Optimize token usage

3. **Train Users**
   - Provide usage guidelines
   - Share example queries
   - Collect feedback

4. **Review Logs Regularly**
   - Check for errors
   - Identify common issues
   - Improve based on usage patterns

---

## Appendix

### Supported Claim ID Formats

| Format | Example | Detected |
|--------|---------|----------|
| Standard | `ALF1234567` | ✅ |
| With hyphen | `ALF-1234567` | ✅ |
| With underscore | `ALF_1234567` | ✅ |
| Lowercase | `alf1234567` | ✅ |
| Mixed case | `AlF1234567` | ✅ |
| Missing digits | `ALF123456` | ❌ |
| Extra digits | `ALF12345678` | ❌ |

### Data Sources Summary

| Source | Data Included | Limit |
|--------|---------------|-------|
| **LORA Claim** | All claim fields | 1 claim |
| **EmailLog** | Subject, body, summary, category | Last 10 emails |
| **Refund** | Amount, status, type, reason | All refunds |
| **Timeline** | Update type, LLM summary | Last 10 updates |
| **Zendesk** | Ticket status, subject, comments | 1 ticket, 5 comments |

### Conversation Context Limits

| Aspect | Limit |
|--------|-------|
| **Message History** | Last 10 messages |
| **Claim Context Search** | Last 6 messages |
| **Email History** | Last 10 emails per claim |
| **Timeline Updates** | Last 10 updates per claim |
| **Zendesk Comments** | Last 5 comments per ticket |

### Glossary

| Term | Definition |
|------|------------|
| **ALF Claim ID** | Unique identifier for claims (format: ALF + 7 digits) |
| **Claim Context** | Claim data fetched for AI reference |
| **Conversation History** | Previous messages in current chat session |
| **Hallucination** | LLM inventing information not in source data |
| **LLM** | Large Language Model (DeepSeek in this case) |
| **Prompt** | Input text sent to LLM for response generation |
| **System Prompt** | Instructions that define AI behavior |
| **Token** | Unit of text for LLM processing |

---

**End of Document**
