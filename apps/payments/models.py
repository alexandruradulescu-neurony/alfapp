from datetime import timedelta

from django.db import models
from django.conf import settings
from django.utils import timezone


class Refund(models.Model):
    """
    Represents a refund transaction linked to a Claim.
    Tracks refunds initiated via LORA, WooCommerce, or manually.
    """
    
    # Status values — reference these constants (not bare strings) everywhere so
    # a rename is a NameError, not a silently-broken comparison.
    STATUS_REQUESTED = 'REQUESTED'
    STATUS_PENDING = 'PENDING'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [
        (STATUS_REQUESTED, 'Requested'),
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    TYPE_FULL = 'FULL'
    TYPE_PARTIAL = 'PARTIAL'
    TYPE_CHOICES = [
        (TYPE_FULL, 'Full Refund'),
        (TYPE_PARTIAL, 'Partial Refund'),
    ]

    SOURCE_LORA = 'LORA'
    SOURCE_WOOCOMMERCE = 'WOOCOMMERCE'
    SOURCE_MANUAL = 'MANUAL'
    SOURCE_CHOICES = [
        (SOURCE_LORA, 'LORA Initiated'),
        (SOURCE_WOOCOMMERCE, 'WooCommerce/WordPress'),
        (SOURCE_MANUAL, 'Manual Entry'),
    ]

    # paypal_refund_id prefixes for WooCommerce-origin rows (a placeholder id is
    # used until/unless a real PayPal refund id is known).
    WC_PREFIX = 'WC-'
    WC_PENDING_PREFIX = 'WC-PENDING-'
    
    # Relationship to Claim (One Claim can have multiple Refunds)
    claim = models.ForeignKey(
        'claims.Claim',
        on_delete=models.PROTECT,  # Don't allow deleting claim with refunds
        related_name='refunds',
        null=True,
        blank=True,
        help_text='Claim associated with this refund'
    )
    
    # PayPal Data
    paypal_refund_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text='PayPal refund transaction ID'
    )
    paypal_capture_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Original PayPal capture ID'
    )
    
    # Amount & Currency
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='Refund amount'
    )
    currency = models.CharField(
        max_length=3,
        default='USD',
        help_text='Currency code (e.g., USD, EUR)'
    )
    
    # Status Tracking
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_REQUESTED,
        help_text='Current refund status'
    )
    
    # Refund Type
    refund_type = models.CharField(
        max_length=10,
        choices=TYPE_CHOICES,
        help_text='Full or Partial refund'
    )
    
    # Source Tracking
    external_source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_LORA,
        help_text='Origin of the refund'
    )
    
    # Metadata
    reason = models.TextField(
        help_text='Reason for the refund'
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='PayPal API response and additional data'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the refund was processed/completed'
    )
    
    # Audit
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='refunds_created',
        help_text='User who initiated the refund'
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Refund'
        verbose_name_plural = 'Refunds'
        indexes = [
            models.Index(fields=['claim', 'status']),
            models.Index(fields=['paypal_refund_id']),
            models.Index(fields=['external_source', 'created_at']),
        ]
    
    def __str__(self):
        return f'Refund {self.id} - {self.currency} {self.amount} ({self.status})'
    
    def mark_completed(self):
        """Mark refund as completed. Writes only the touched columns so a
        concurrent writer (e.g. a PayPal webhook vs. an admin action) can't
        clobber unrelated fields with a stale full-row save."""
        self.status = self.STATUS_COMPLETED
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at', 'updated_at'])

    def mark_failed(self, error_message=''):
        """Mark refund as failed (touched columns only — see mark_completed)."""
        self.status = self.STATUS_FAILED
        if error_message:
            self.metadata['error_message'] = error_message
        self.save(update_fields=['status', 'metadata', 'updated_at'])

    def mark_processing(self):
        """Mark refund as processing (touched columns only — see mark_completed)."""
        self.status = self.STATUS_PROCESSING
        self.save(update_fields=['status', 'updated_at'])

    def mark_cancelled(self):
        """Mark refund as cancelled (touched columns only — see mark_completed)."""
        self.status = self.STATUS_CANCELLED
        self.save(update_fields=['status', 'updated_at'])

    @property
    def is_completed(self):
        return self.status == self.STATUS_COMPLETED

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING


class Dispute(models.Model):
    """
    Core dispute entity representing a PayPal dispute case.
    Tied to a Claim (required) with its own lifecycle.
    """

    # Status values — reference these constants (not bare strings) in view/service
    # logic so a rename can't silently break a comparison or a log.
    STATUS_RECEIVED = 'RECEIVED'
    STATUS_MATCHED = 'MATCHED'
    STATUS_GATHERING_DATA = 'GATHERING_DATA'
    STATUS_DOCUMENTS_READY = 'DOCUMENTS_READY'
    STATUS_UNDER_REVIEW = 'UNDER_REVIEW'
    STATUS_EVIDENCE_SENT = 'EVIDENCE_SENT'
    STATUS_RESOLVED_WON = 'RESOLVED_WON'
    STATUS_RESOLVED_LOST = 'RESOLVED_LOST'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_CHOICES = [
        (STATUS_RECEIVED, 'Received'),
        (STATUS_MATCHED, 'Matched to Zendesk Ticket'),
        (STATUS_GATHERING_DATA, 'Gathering Data'),
        (STATUS_DOCUMENTS_READY, 'Documents Ready'),
        (STATUS_UNDER_REVIEW, 'Under Review'),
        (STATUS_EVIDENCE_SENT, 'Evidence Sent to PayPal'),
        (STATUS_RESOLVED_WON, 'Resolved - Won'),
        (STATUS_RESOLVED_LOST, 'Resolved - Lost'),
        (STATUS_ACCEPTED, 'Accepted/Refunded'),
    ]

    # Status groupings — the single source of truth for "is this dispute over?".
    # A dispute in a TERMINAL status no longer needs action and no longer voids
    # the client-update cadence; everything else is ACTIVE.
    TERMINAL_STATUSES = (STATUS_RESOLVED_WON, STATUS_RESOLVED_LOST, STATUS_ACCEPTED)
    ACTIVE_STATUSES = (STATUS_RECEIVED, STATUS_MATCHED, STATUS_GATHERING_DATA,
                       STATUS_DOCUMENTS_READY, STATUS_UNDER_REVIEW, STATUS_EVIDENCE_SENT)

    # Must match PayPal's exact `reason` enum (British 'UNAUTHORISED', etc.) —
    # any drift breaks prefill from the webhook. This is the human's category.
    REASON_CHOICES = [
        ('MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'Item/Service Not Received'),
        ('MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED', 'Not As Described'),
        ('UNAUTHORISED', 'Unauthorised Transaction'),
        ('CREDIT_NOT_PROCESSED', 'Credit Not Processed'),
        ('DUPLICATE_TRANSACTION', 'Duplicate Transaction'),
        ('INCORRECT_AMOUNT', 'Incorrect Amount'),
        ('PAYMENT_BY_OTHER_MEANS', 'Paid By Other Means'),
        ('CANCELED_RECURRING_BILLING', 'Cancelled Recurring Billing'),
        ('PROBLEM_WITH_REMITTANCE', 'Problem With Remittance'),
        ('OTHER', 'Other'),
    ]
    VALID_REASONS = dict(REASON_CHOICES)

    # PayPal identifiers
    paypal_dispute_id = models.CharField(max_length=100, unique=True, db_index=True)
    paypal_case_id = models.CharField(max_length=100, blank=True, db_index=True)

    # Links to other systems
    claim = models.ForeignKey(
        'claims.Claim',
        on_delete=models.PROTECT,  # Don't allow deleting claim with dispute
        related_name='disputes',
        null=True,
        blank=True,
    )
    zd_ticket_id = models.CharField(max_length=50, blank=True, db_index=True)

    # Status tracking
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_RECEIVED,
        db_index=True,
    )

    # Dispute details
    dispute_reason = models.CharField(
        max_length=50,
        choices=REASON_CHOICES,
        blank=True,
    )
    # PayPal lifecycle stage (INQUIRY → CHARGEBACK → PRE_ARBITRATION →
    # ARBITRATION). Evidence can only be submitted from CHARGEBACK onward.
    dispute_life_cycle_stage = models.CharField(
        max_length=30,
        blank=True,
        default='',
        help_text='PayPal dispute_life_cycle_stage; gates evidence submission',
    )
    dispute_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    dispute_currency = models.CharField(max_length=3, blank=True)

    # Buyer information
    buyer_email = models.EmailField(db_index=True)
    buyer_name = models.CharField(max_length=255, blank=True)

    # Transaction information
    transaction_id = models.CharField(max_length=100, db_index=True)
    transaction_date = models.DateTimeField()

    # Timeline
    seller_response_due = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Payload and notes
    raw_webhook_payload = models.JSONField(default=dict)
    notes = models.TextField(blank=True)

    # Assignment
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_disputes',
    )

    # Transient mutex: set True (atomic compare-and-set) while an outbound
    # PayPal action (accept-claim, manual supporting-info reply) is mid-flight,
    # so two concurrent clicks can't both call the API. Cleared when the call
    # returns. (Cache locks are per-process here — no shared cache — so the
    # guard must live in the DB.)
    outbound_in_flight = models.BooleanField(default=False)
    # When outbound_in_flight was last set. A worker killed between claiming the
    # flag and the finally-release would otherwise leave it stuck True forever,
    # wedging accept-claim / manual-reply on this dispute. With a timestamp the
    # lock is treated as stale after OUTBOUND_INFLIGHT_TTL and re-claimable.
    # NULL = idle / never claimed.
    outbound_in_flight_at = models.DateTimeField(null=True, blank=True)

    # A held lock older than this is considered abandoned (worker died mid-call).
    OUTBOUND_INFLIGHT_TTL = timedelta(minutes=10)

    def claim_outbound(self, *, exclude_terminal=False) -> bool:
        """Atomically claim this dispute's outbound channel (compare-and-set).
        Wins if the lock is free, stale (older than OUTBOUND_INFLIGHT_TTL), or
        was left set without a timestamp (legacy/crashed). Returns True if this
        caller now holds the lock. Release with release_outbound()."""
        from django.db.models import Q
        now = timezone.now()
        cutoff = now - self.OUTBOUND_INFLIGHT_TTL
        qs = Dispute.objects.filter(pk=self.pk).filter(
            Q(outbound_in_flight=False)
            | Q(outbound_in_flight_at__lt=cutoff)
            | Q(outbound_in_flight_at__isnull=True)
        )
        if exclude_terminal:
            qs = qs.exclude(status__in=self.TERMINAL_STATUSES)
        return bool(qs.update(outbound_in_flight=True, outbound_in_flight_at=now))

    def release_outbound(self) -> None:
        """Release the outbound channel claimed by claim_outbound()."""
        Dispute.objects.filter(pk=self.pk).update(
            outbound_in_flight=False, outbound_in_flight_at=None)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['buyer_email', '-created_at']),
            models.Index(fields=['transaction_id']),
            models.Index(fields=['claim', '-created_at']),
        ]
        # NOTE: previously a CheckConstraint (dispute_claim_requires_buyer_email)
        # required buyer_email whenever a claim was linked. Removed 2026-06-14:
        # PayPal's dispute object does NOT include the buyer email, so disputes
        # are matched to claims by the invoice/order reference instead — a
        # correctly matched dispute can validly have an empty buyer_email.

    def __str__(self):
        return f"Dispute #{self.id} - {self.buyer_email or '(no email)'} ({self.status})"

    # Stages from which PayPal accepts an evidence upload (INQUIRY is
    # message-only — PayPal rejects provide-evidence there).
    EVIDENCE_STAGES = ('CHARGEBACK', 'PRE_ARBITRATION', 'ARBITRATION')
    OPEN_STATUSES = (STATUS_RECEIVED, STATUS_MATCHED, STATUS_GATHERING_DATA,
                     STATUS_DOCUMENTS_READY, STATUS_UNDER_REVIEW, STATUS_EVIDENCE_SENT)

    @property
    def can_submit_evidence(self) -> bool:
        """Evidence upload is allowed only from CHARGEBACK stage onward and
        while the case is still open. Unknown stage → allow (don't block on
        missing data; PayPal is the final gate)."""
        if self.status not in self.OPEN_STATUSES:
            return False
        if not self.dispute_life_cycle_stage:
            return True
        return self.dispute_life_cycle_stage.upper() in self.EVIDENCE_STAGES

    @property
    def paypal_state(self) -> str:
        """PayPal's current `dispute_state` from the stored payload, upper-cased."""
        return ((self.raw_webhook_payload or {}).get('dispute_state') or '').upper()

    @property
    def submit_endpoint(self) -> str:
        """Which PayPal endpoint a NEW submission should target, auto-picked from
        the dispute's current state (the manager just clicks "Submit to PayPal"):

        - UNDER_PAYPAL_REVIEW / status UNDER_REVIEW → 'provide-supporting-info'
          (the case is already under review — add follow-up info).
        - REQUIRED_ACTION / WAITING_FOR_SELLER_RESPONSE / unknown →
          'provide-evidence' (the first seller response), but only when the
          stage gate allows it (INQUIRY is message-only — see can_submit_evidence).
        - resolved/closed → '' (nothing to submit).

        '' means no submission endpoint is available right now.
        """
        # Manually-created disputes carry a synthetic id (no real PayPal case to
        # POST to) — the manager downloads the report and uploads it in PayPal by
        # hand. Refuse the API submit path so it can't 404 against a fake id.
        if (self.paypal_dispute_id or '').startswith('MANUAL-'):
            return ''
        if self.status in self.TERMINAL_STATUSES:
            return ''
        payload = self.raw_webhook_payload or {}
        state = (payload.get('dispute_state') or '').upper()
        pp_status = (payload.get('status') or '').upper()
        if 'RESOLVED' in (state, pp_status):
            return ''
        if state == 'UNDER_PAYPAL_REVIEW' or pp_status == 'UNDER_REVIEW':
            return 'provide-supporting-info'
        return 'provide-evidence' if self.can_submit_evidence else ''

    @property
    def deadline_state(self) -> str:
        """'' | overdue | soon | ok — for colour-coding the response deadline."""
        if not self.seller_response_due or self.status in self.TERMINAL_STATUSES:
            return ''
        days = (self.seller_response_due - timezone.now()).days
        if days < 0:
            return 'overdue'
        if days <= 3:
            return 'soon'
        return 'ok'


class DisputeDocument(models.Model):
    """
    Response letters and evidence reports for disputes.
    """

    DOC_TYPE_RESPONSE_LETTER = 'RESPONSE_LETTER'
    DOC_TYPE_EVIDENCE_REPORT = 'EVIDENCE_REPORT'
    DOC_TYPE_CHOICES = [
        (DOC_TYPE_RESPONSE_LETTER, 'Response Letter'),
        (DOC_TYPE_EVIDENCE_REPORT, 'Evidence Report'),
    ]

    STATUS_DRAFT = 'DRAFT'
    STATUS_REVIEW = 'REVIEW'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_SENT = 'SENT'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_REVIEW, 'Under Review'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_SENT, 'Sent to PayPal'),
    ]

    GENERATED_BY_AI = 'AI'
    GENERATED_BY_MANUAL = 'MANUAL'
    GENERATED_BY_CHOICES = [
        (GENERATED_BY_AI, 'AI Generated'),
        (GENERATED_BY_MANUAL, 'Manually Created'),
    ]

    dispute = models.ForeignKey(
        Dispute,
        on_delete=models.CASCADE,
        related_name='documents',
    )
    doc_type = models.CharField(
        max_length=20,
        choices=DOC_TYPE_CHOICES,
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )
    file_path = models.FileField(
        upload_to='dispute_documents/',
        null=True,
        blank=True,
    )
    content_html = models.TextField(
        blank=True,
        help_text='Inline HTML content for editing',
    )
    version = models.IntegerField(default=1)
    generated_by = models.CharField(
        max_length=10,
        choices=GENERATED_BY_CHOICES,
        default=GENERATED_BY_AI,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='accepted_documents',
        db_index=True,  # Add index for queries filtering by accepted_by
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['dispute', '-created_at']),
            models.Index(fields=['doc_type', 'status']),
            models.Index(fields=['accepted_by', '-created_at']),  # Composite index for audit queries
        ]

    def __str__(self):
        return f"Document #{self.id} - {self.get_doc_type_display()} (v{self.version})"


class DisputeActivityLog(models.Model):
    """
    Audit trail for dispute actions.
    """

    ACTION_DISPUTE_CREATED = 'DISPUTE_CREATED'
    ACTION_DISPUTE_MATCHED = 'DISPUTE_MATCHED'
    ACTION_SCREENSHOTS_CAPTURED = 'SCREENSHOTS_CAPTURED'
    ACTION_DOCUMENT_GENERATED = 'DOCUMENT_GENERATED'
    ACTION_DOCUMENT_ACCEPTED = 'DOCUMENT_ACCEPTED'
    ACTION_EVIDENCE_SENT = 'EVIDENCE_SENT'
    ACTION_STATUS_CHANGED = 'STATUS_CHANGED'
    ACTION_NOTE_ADDED = 'NOTE_ADDED'
    ACTION_DISPUTE_RESOLVED = 'DISPUTE_RESOLVED'
    ACTION_CHOICES = [
        (ACTION_DISPUTE_CREATED, 'Dispute Created'),
        (ACTION_DISPUTE_MATCHED, 'Dispute Matched to Ticket'),
        (ACTION_SCREENSHOTS_CAPTURED, 'Screenshots Captured'),
        (ACTION_DOCUMENT_GENERATED, 'Document Generated'),
        (ACTION_DOCUMENT_ACCEPTED, 'Document Accepted'),
        (ACTION_EVIDENCE_SENT, 'Evidence Sent to PayPal'),
        (ACTION_STATUS_CHANGED, 'Status Changed'),
        (ACTION_NOTE_ADDED, 'Note Added'),
        (ACTION_DISPUTE_RESOLVED, 'Dispute Resolved'),
    ]

    dispute = models.ForeignKey(
        Dispute,
        on_delete=models.CASCADE,
        related_name='activity_log',
    )
    action = models.CharField(
        max_length=50,
        choices=ACTION_CHOICES,
    )
    details = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='dispute_activities',
    )
    performed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-performed_at']
        indexes = [
            models.Index(fields=['dispute', '-performed_at']),
        ]

    def __str__(self):
        return f"Activity #{self.id} - Dispute #{self.dispute_id} - {self.action}"


class DisputeSubmission(models.Model):
    """One submission of evidence / supporting info to PayPal for a dispute.

    A dispute is a back-and-forth: the first response uses provide-evidence, and
    while the case is under PayPal review we can keep adding provide-supporting-info.
    Each attempt is one row — the editable narrative (`notes`), the manager's extra
    context, the chosen attachments, and (after sending) PayPal's response. The
    rows form the "our side" half of the dispute timeline.
    """

    KIND_EVIDENCE = 'EVIDENCE'
    KIND_SUPPORTING_INFO = 'SUPPORTING_INFO'
    KIND_MESSAGE = 'MESSAGE'
    KIND_CHOICES = [
        (KIND_EVIDENCE, 'First evidence'),
        (KIND_SUPPORTING_INFO, 'Supporting info'),
        (KIND_MESSAGE, 'Message to buyer'),
    ]
    SOURCE_AI = 'AI'
    SOURCE_AI_EDITED = 'AI_EDITED'
    SOURCE_MANUAL = 'MANUAL'
    SOURCE_CHOICES = [
        (SOURCE_AI, 'AI-drafted'),
        (SOURCE_AI_EDITED, 'AI-drafted, edited'),
        (SOURCE_MANUAL, 'Manually written'),
    ]
    STATUS_DRAFT = 'DRAFT'
    STATUS_SUBMITTING = 'SUBMITTING'   # transient: atomically claimed for an in-flight PayPal POST
    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_SUBMITTING, 'Submitting'),
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_FAILED, 'Failed'),
    ]
    # Endpoint each kind maps to (used to label the PayPal action taken).
    KIND_TO_ENDPOINT = {
        KIND_EVIDENCE: 'provide-evidence',
        KIND_SUPPORTING_INFO: 'provide-supporting-info',
        KIND_MESSAGE: 'send-message',
    }

    dispute = models.ForeignKey(
        Dispute,
        on_delete=models.CASCADE,
        related_name='submissions',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_EVIDENCE)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_AI)
    notes = models.TextField(
        blank=True,
        help_text='The narrative text submitted to PayPal (editable while DRAFT)',
    )
    manager_note = models.TextField(
        blank=True,
        default='',
        help_text='Extra context the manager typed; feeds the AI narrative, not sent verbatim',
    )
    evidence_type = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="PayPal evidence_type (provide-evidence only); defaulted per dispute reason",
    )
    attach_evidence_pdf = models.BooleanField(
        default=False,
        help_text='Whether to attach the latest evidence-report PDF to this submission',
    )
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispute_submissions',
    )
    paypal_response = models.JSONField(
        default=dict,
        blank=True,
        help_text='Raw PayPal response (or error detail) for audit',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['dispute', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f"Submission #{self.id} - Dispute #{self.dispute_id} - {self.kind} ({self.status})"


class DisputeSubmissionImage(models.Model):
    """An image the manager attached to a dispute submission (drag/drop or file
    picker). Sent to PayPal as a document file and embeddable into the evidence
    PDF. Kept separate from DisputeDocument (which holds the generated PDFs)."""

    submission = models.ForeignKey(
        DisputeSubmission,
        on_delete=models.CASCADE,
        related_name='images',
    )
    file = models.FileField(upload_to='dispute_submission_images/')
    caption = models.CharField(max_length=255, blank=True, default='')
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispute_submission_images',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    def __str__(self):
        return f"Image #{self.id} for Submission #{self.submission_id}"

    @property
    def filename(self) -> str:
        """Bare filename (no upload path) for display."""
        return self.file.name.rsplit('/', 1)[-1] if self.file else ''

    @property
    def is_pdf(self) -> bool:
        """A PDF attachment renders as a document chip, not an <img> thumbnail."""
        return self.filename.lower().endswith('.pdf')


class ProcessedWebhookEvent(models.Model):
    """
    Tracks processed PayPal webhook events for idempotency.
    Prevents duplicate processing of the same webhook event.
    """

    STATUS_PROCESSED = 'processed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PROCESSED, 'Processed'),
        (STATUS_FAILED, 'Failed'),
    ]

    event_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text='PayPal event ID (e.g., WH-EVENT-XXXXX)',
    )
    event_type = models.CharField(
        max_length=100,
        help_text='PayPal event type (e.g., CUSTOMER.DISPUTE.CREATED)',
    )
    resource_type = models.CharField(
        max_length=100,
        blank=True,
        help_text='Resource type (e.g., dispute)',
    )
    resource_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Resource ID from PayPal',
    )
    processed_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text='When the event was processed',
    )
    status = models.CharField(
        max_length=20,
        default=STATUS_PROCESSED,
        choices=STATUS_CHOICES,
        help_text='Processing status',
    )
    error_message = models.TextField(
        blank=True,
        help_text='Error message if processing failed',
    )

    class Meta:
        verbose_name = 'Processed Webhook Event'
        verbose_name_plural = 'Processed Webhook Events'
        ordering = ['-processed_at']
        indexes = [
            models.Index(fields=['-processed_at']),
            models.Index(fields=['event_type', '-processed_at']),
        ]

    def __str__(self):
        return f"{self.event_type} - {self.event_id} ({self.status})"

    # NB: idempotency lives in ONE place — the webhook view does an atomic
    # get_or_create on event_id directly (PayPalDisputeWebhookView), which also
    # gives it the `created` flag and the release-on-failure semantics a helper
    # can't. The former is_already_processed / mark_as_processed / mark_as_failed
    # classmethods were unused (a second, divergent idempotency implementation)
    # and were removed.
