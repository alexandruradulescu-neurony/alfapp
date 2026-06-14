# Dispute back-and-forth — design spec (2026-06-14)

**Status:** DRAFT for review. No code yet. Builds on the dispute pipeline
(`apps/payments/`), the narrative evidence report (`document_service.py`), and
the AI trust boundary (`apps/ai/`).

## Goal

Turn a dispute from "generate one PDF and submit once" into a **two-way case
you can work over time**: write a persuasive evidence narrative (AI-drafted,
human-edited), attach manager-supplied context and images, submit it to PayPal,
and keep submitting follow-up info while the case is open — with the whole
exchange visible as a timeline.

Four parts (the user's list): **A** AI narrative, **B** manager text+images
input, **C** follow-up submissions, **D** reply timeline.

## What PayPal gives us (from real payloads)

- **First response** (state `REQUIRED_ACTION`, status `WAITING_FOR_SELLER_RESPONSE`):
  `…/provide-evidence` — multipart: an `evidence_type`, free-text `notes`, and
  document files. (Already implemented as `provide_evidence`.)
- **After it's under review** (state `UNDER_PAYPAL_REVIEW`): the dispute exposes
  `…/provide-supporting-info` instead — same shape (notes + documents), used to
  add more info to an open case. **Not yet implemented.**
- **`evidences[]`** on the dispute records every submission (date, `evidence_type`,
  `notes`, `documents[]`, `source` = SUBMITTED_BY_SELLER / REQUESTED_FROM_SELLER).
  This is the authoritative history for the timeline.
- `adjudications[]`, `money_movements[]` show PayPal's decisions / money moved —
  useful context to show, read-only.
- The `notes` field is the persuasive text PayPal's reviewer actually reads — the
  thing feature **A** must generate. Example from a real submission: a first-person
  "We are formally contesting… 1. Proof of Authorization… 2. Service Delivery…
  request the case be closed in our favor" narrative.

## A. AI-written evidence narrative (the `notes` text)

- New generator `build_dispute_narrative_notes(dispute, *, manager_note='', use_ai=True)`
  in `document_service.py`. Produces the first-person ALF narrative in the style
  of the PayPal example: reason-specific opening, proof-of-authorization (verified
  name/email/address + the highly-specific data the client entered: flight, loss
  location, item description), service-delivery (fee, ticket id, calls, outreach
  to airline/airport, status updates), terms/refund-window acceptance, and a
  closing request. Reuses the report's `_narrative_fields`, `_identity_context`
  (IP match), `_build_timeline`, `CATEGORY_FRAMING`, and `SECTION_PRIORITY_BY_REASON`.
- **AI path:** via `AIClient` (call_site `dispute_narrative_notes`, new schema
  `DisputeNarrative` in `apps/ai/schemas.py`). PII handling — IMPORTANT and
  different from the report's grouping AI:
  - When CALLING the LLM, mask client name/email/address/phone (`_known_pii_for`)
    so the provider never sees raw PII.
  - The final `notes` submitted to PayPal **must contain the real values** (the
    example cites the real name + address — that's the whole point, and PayPal is
    inside the trust zone). So the AIClient `untokenize` step restores them before
    the text is stored/shown. Net: provider sees tokens, PayPal sees reals.
  - Graceful fallback: no AI key / error → deterministic template narrative
    (same structure, filled from claim fields), like the report's fallback.
- Stored editable (see model below). Manager reviews/edits before any submit.

## B. Manager input — text + images

- A box on the dispute page: a free-text **"Notes to add"** field + an image
  uploader (drag/drop or file picker).
- The text feeds the AI narrative prompt as an extra trusted instruction
  ("incorporate: …") so the generated `notes` reflect what the manager wants
  emphasized. Manager text is treated as trusted (staff), not fenced.
- Uploaded images are stored and become **attachments** on the submission
  (PayPal accepts document files). Also embeddable into the evidence PDF.
- Images go through the existing size guard; total submission payload kept under
  PayPal's ~10 MB cap (we already warn at 9.5 MB).

## C. Follow-up submissions (provide-supporting-info)

- New `provide_supporting_info(dispute_id, notes, files)` in
  `paypal_disputes_service.py` — mirrors `provide_evidence`'s multipart encoder,
  POSTs to `…/provide-supporting-info`.
- A **submit** action chooses the endpoint by the dispute's current PayPal state:
  `REQUIRED_ACTION`/chargeback-stage → `provide-evidence`; `UNDER_PAYPAL_REVIEW`
  → `provide-supporting-info`. (Server picks; the manager just clicks "Submit to
  PayPal".) Stage gating from the existing `can_submit_evidence` still applies to
  the first-evidence path.
- Each submit records a `DisputeSubmission` row + a `DisputeActivityLog` entry,
  and re-syncs the dispute from PayPal afterwards (so state + `evidences[]` update).

## D. Reply timeline

- The dispute page shows a chronological timeline merging:
  1. **Our submissions** — `DisputeSubmission` rows (notes preview, attachments,
     who/when, submitted vs draft vs failed).
  2. **PayPal's record** — entries from `evidences[]` in the stored payload
     (incl. `REQUESTED_FROM_SELLER` asks and any buyer/PayPal notes).
  3. Key events from `DisputeActivityLog` (matched, resolved, money moved).
- **Manual reply:** a "Add reply" box where the manager pastes text and submits
  it as a supporting-info submission without the AI (kind=MANUAL). Same path as C.

## Data model

New model `DisputeSubmission` (apps/payments/models.py):

| field | type | notes |
|---|---|---|
| dispute | FK→Dispute (CASCADE) | |
| kind | choice: EVIDENCE / SUPPORTING_INFO / MESSAGE | which PayPal endpoint |
| source | choice: AI / AI_EDITED / MANUAL | provenance of the notes |
| notes | TextField | the narrative text submitted (editable while DRAFT) |
| manager_note | TextField blank | the extra context the manager typed (feeds AI) |
| evidence_type | CharField blank | PayPal evidence_type — defaulted per dispute reason via EVIDENCE_TYPE_BY_REASON (Decision #6) |
| status | choice: DRAFT / SUBMITTED / FAILED | |
| submitted_at | DateTime null | |
| submitted_by | FK→User null | |
| paypal_response | JSONField | raw response for audit |
| created_at / updated_at | | |

Images: a dedicated **`DisputeSubmissionImage`** model (Decision #3) — FK→DisputeSubmission
(CASCADE), `file` FileField, `caption` blank, `uploaded_by` FK→User null, `uploaded_at`.

The existing `DisputeDocument` (EVIDENCE_REPORT PDF) remains — a submission can
attach both the generated PDF and the manager's images.

## UI (templates/manager/dispute_detail.html)

- Replace the single "Generate Documents → Send Evidence" flow with a
  **"Prepare submission"** panel: AI-draft narrative (editable textarea) +
  manager notes + image upload + attachment checklist (images always; the
  evidence PDF only if the manager ticks it — Decision #2) + one
  **"Submit to PayPal"** button (endpoint auto-chosen).
- A **Timeline** section (feature D) below it.
- "Raw PayPal data" viewer stays.

## PII / security

- LLM never sees raw client PII (mask → tokenize → untokenize), per
  [[project-llm-trust-boundary]]. PayPal gets the real values (inside trust zone).
- Manager-uploaded images fetched/stored only from our own infra; submissions go
  only to the configured PayPal mode (sandbox/live).
- All actions manager-only; live mode still gated by `paypal_mode`.

## Phasing (suggested build order)

1. **A** — narrative generator + schema + fallback + tests (no UI yet; command/preview).
2. **B** — manager note + image upload model/UI; wire note into A's prompt, images as attachments.
3. **C** — `provide_supporting_info` + endpoint-by-state submit + `DisputeSubmission` records + re-sync.
4. **D** — timeline (our submissions + PayPal evidences + activity) + manual reply.

Each phase ships + tests independently.

## Decisions (resolved 2026-06-14)

1. **Narrative length/tone:** follow PayPal dispute-evidence **best practices** —
   a structured, factual, first-person case (clear sections; the specific
   authorization proof + service-delivery proof; an explicit request to resolve
   in our favour); complete but no padding. Not locked to the example's exact
   length/headings.
2. **Auto-attach the evidence PDF:** NO — attach it only when the manager ticks it.
3. **Images:** a dedicated **`DisputeSubmissionImage`** model (NOT folded into
   `DisputeDocument`).
4. **Endpoint:** LORA **auto-picks** `provide-evidence` vs `provide-supporting-info`
   by the dispute's current PayPal state.
5. **Manual reply:** posts via the **follow-up channel** (`provide-supporting-info`).
   The buyer-facing `send-message` channel is NOT exposed.
6. **Evidence_type:** **varies by dispute reason** — a per-reason map (e.g.
   PROOF_OF_FULFILLMENT for not-received / service cases; appropriate types for
   UNAUTHORISED etc.), via an `EVIDENCE_TYPE_BY_REASON` constant.

## Testing

Per phase: narrative generator (AI mock + deterministic fallback + PII mask/restore),
`provide_supporting_info` (multipart shape, mocked), endpoint-by-state selection,
submission records + timeline assembly, manager-note-into-prompt, image attach +
size cap. All against the existing dispute test suite.
