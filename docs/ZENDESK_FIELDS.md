# Zendesk Field Reference

**Purpose:** the authoritative map of every Zendesk ticket field LORA can read, what it means, and whether the app currently uses it. When building a feature that needs ticket data, start here — find the field, copy its ID, and check the "Wired into LORA?" column before adding extraction code.

**Source of truth for IDs in code:** `apps/integrations/services.py` (the `ZENDESK_FIELD_*` constants). Field values are read from a ticket's `custom_fields` list via `_get_custom_field_value(custom_fields, ZENDESK_FIELD_X)`.

**Last confirmed against live Zendesk:** 2026-06-10.

---

## How to use this file

- **"Wired into LORA?"** = the app reads this field at claim creation and stores it.
  - ✅ Wired — there's a `ZENDESK_FIELD_*` constant and it populates a `Claim` field.
  - 📄 Documented only — known and listed here, but not read yet. To wire it: add a `ZENDESK_FIELD_*` constant in `services.py`, read it in `analyze_zendesk_ticket_for_claim`, add a `Claim` model field + migration, and pass it through `views.py`.
  - ⚙️ Zendesk-managed — a standard Zendesk field (status, assignee, etc.) LORA reads live from the ticket API when needed, not stored on the Claim.

---

## Client identity & contact

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Customer Name | 13737514170140 | Text | ✅ `Claim.client_name` | Client's full name. We submit claims in the client's name. |
| Customer Email | 13737499349020 | Text | ✅ `Claim.client_email` | The client's **real** email (PII; tokenized before any LLM call). |
| Email used for submissions | 13606076120860 | Text | ✅ alias (known_pii) | The per-case **alias** our agents mint on our domain. Used to route institutional replies back to the right ticket. Tokenized as ALIAS, not EMAIL. |
| Phone Number | 11761070082844 | Text | ✅ `Claim.phone` | Client phone (PII; tokenized). |
| Customer IP Address | 14438419565340 | Text | 📄 Documented only | Submission IP. Useful only for fraud/abuse signals — no consumer yet. |
| Billing Address | 13737449416988 | Multi-line | ✅ `Claim.billing_address` | Used for refund/dispute paperwork. |
| Shipping Address | 11949784750236 | Multi-line | ✅ `Claim.shipping_address` | Where to ship the recovered object (drives the "Shipped" status flow). |

## Object & loss details

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Lost Object | 11761123532444 | Text | ✅ `Claim.object_description` | The item itself. Composed with Object Details. |
| Object Details | 13737436477852 | Multi-line | ✅ `Claim.object_description` | Extra detail about the item. |
| Incident Details | 13737603591964 | Multi-line | ✅ `Claim.incident_details` | Narrative of how/when it was lost. |
| Lost Location | 16314445118492 | Multi-line | ✅ `Claim.lost_location` | Where the item was lost — primary search lead. |
| Client Uploaded Images | 12286658769052 | Multi-line | 📄 Documented only | Image references/URLs the client uploaded. LORA has a separate `ClaimEvidence` model for images; wiring this would mean importing those refs. |
| Description | 11688561324188 | Multi-line | ⚙️ Zendesk-managed | Standard ticket description; read live as free-text context for the LLM. |
| Subject | 11688533475612 | Text | ⚙️ Zendesk-managed | Standard subject; fallback source for the ALF claim ID. |

## Claim identity

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Claim # | 11688794648732 | Text | ✅ `Claim.alf_claim_id` | Authoritative ALF id source; falls back to subject-line parsing. |

## Flight details

All five compose into `Claim.flight_details` (one labeled string).

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Flight Number | 13737630819996 | Text | ✅ `Claim.flight_details` | |
| Airline | 11761080032028 | Text | ✅ `Claim.flight_details` | |
| Airport | 11761104069276 | Text | ✅ `Claim.flight_details` | |
| Seat Number | 13737646294940 | Text | ✅ `Claim.flight_details` | |
| Date & Time | 13737598795292 | Text | ✅ `Claim.flight_details` | Flight date/time. |

## Deadlines (30-day lifecycle)

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Deadline Date | 14394267216668 | Date | ✅ `Claim.deadline_date` | Drives the 30-day claim window and the planned update cadence (2/5/11/20-day). |
| Deadline Time | 14394267218972 | Text | ✅ `Claim.deadline_time` | Time-of-day portion of the deadline. |
| Deadline Time Zone | 14394267222684 | Text | ✅ `Claim.deadline_timezone` | Timezone for the deadline. |

## Payment, price & refund

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Price Paid | 19736734259996 | Numeric | ✅ `Claim.price_paid` | Concierge fee the client paid; used for refund/dispute amounts. |
| Price | 12286577491484 | Text | 📄 Documented only | Legacy text price field; superseded by Price Paid (numeric). |
| Payment Method | 14495509913244 | Text | ✅ `Claim.payment_method` | How the client paid (card, PayPal, etc.). |
| Payment Status | 11761180893980 | Text | ✅ `Claim.payment_status` | Payment state from the storefront. |
| WooCommerce ID | 13484164181916 | Text | ✅ `Claim.woocommerce_id` | Links the claim to the WooCommerce order (refund webhooks reference this). |
| Refund Requested | 13111739347868 | Drop-down | 📄 Documented only | LORA derives refund state from its own `Refund` model + webhooks; the Zendesk drop-down is not authoritative. |
| Refund Approval Status | 13737596403100 | Drop-down | 📄 Documented only | Same reasoning as Refund Requested. |

## Shipping & fulfillment

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| 3rd Party Tracking Information | 11949753094556 | Multi-line | ✅ `Claim.tracking_info` | Carrier tracking for the return shipment. |
| Shipping Address | 11949784750236 | Multi-line | ✅ `Claim.shipping_address` | (Also listed under Client contact.) |

## Workflow & status (Zendesk-managed / standard)

These are standard Zendesk fields. LORA reads ticket status/type live from the API when needed; it does not mirror them onto the Claim.

| Display name | Field ID | Type | Wired into LORA? | Notes |
|---|---|---|---|---|
| Ticket status | 11688533475740 | Drop-down | ⚙️ Zendesk-managed | Drives the webhook trigger (custom status "Investigation Initiated"). Note: casing here is informational — LORA resolves the live label via the custom-statuses API at runtime (id `11688538967068`), so renamed statuses flow through automatically without code changes. |
| Assignee | 11688538946332 | Drop-down | ⚙️ Zendesk-managed | |
| Group | 11688546130204 | Drop-down | ⚙️ Zendesk-managed | |
| Priority | 11688523489308 | Drop-down | ⚙️ Zendesk-managed | |
| Type | 11688538945948 | Drop-down | ⚙️ Zendesk-managed | |
| Channel group | 27446823174812 | Drop-down | ⚙️ Zendesk-managed | Newer standard field (2026). |
| Resolution tier | 27446816886300 | Drop-down | ⚙️ Zendesk-managed | Newer standard field (2026). |
| Resolution type | 23414012323612 | Drop-down | ⚙️ Zendesk-managed | Newer standard field (2026). |
| Disputed | 12684920226972 | Drop-down | 📄 Documented only | Relevant to the future PayPal dispute pipeline; not wired yet. |
| Security Status | 14485135280540 | Drop-down | 📄 Documented only | Operational/screening status. |
| Search Authorization | 11761161367580 | Checkbox | 📄 Documented only | Whether the client authorized the search. |

## Consent, comms & misc (documented only)

Low immediate value — no consumer in the app today. Listed for completeness.

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| Privacy Policy | 11761096442652 | Checkbox | Consent flag at submission. |
| Send SMS? | 12939497004828 | Checkbox | Whether the client opted into SMS updates. |
| Recover Link | 19147402757276 | Text | Self-service recovery link. |
| VIP | 19736607388060 | Checkbox | Customer tier flag. |
| VIP Status | 19737405485596 | Drop-down | Customer tier detail. |
| Tickler_Data | 11971193171740 | Multi-line | Internal scheduling/reminder data. |
| [Ticket Merge] Target | 12323865046428 | Checkbox | Zendesk merge mechanics. |

---

## Adding a new field to LORA (checklist)

1. Add the constant in `apps/integrations/services.py`:
   ```python
   ZENDESK_FIELD_MY_THING: int = <id>  # "My Thing"
   ```
2. Read it in `analyze_zendesk_ticket_for_claim` and add it to the returned dict.
3. Add a `Claim` model field in `apps/claims/models.py` + run `makemigrations claims`.
4. Pass it through in `apps/integrations/views.py` (`Claim.objects.create(...)`).
5. Add a test in `apps/integrations/tests/test_zendesk_services.py`.
6. Update the table above (flip 📄 → ✅).
