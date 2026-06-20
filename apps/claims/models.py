from decimal import Decimal

from django.db import models
from django.conf import settings

# Default workflow status for a freshly-created claim. Kept byte-identical to the
# original field literals so referencing these constants triggers no migration.
DEFAULT_STATUS = 'Investigation initiated'
DEFAULT_CATEGORY = 'open'


RISK_LEVELS = [('none', 'None'), ('watch', 'Watch'), ('at_risk', 'At risk')]
RISK_RANK = {'none': 0, 'watch': 1, 'at_risk': 2}
_RANK_LEVEL = {0: 'none', 1: 'watch', 2: 'at_risk'}
RISK_REASON_CHOICES = [
    'hostile_language', 'refund_demanded', 'dispute_risk',
    'status_regression', 'negative_sentiment',
]
RISK_REASON_LABELS = {
    'hostile_language': 'Hostile language',
    'refund_demanded': 'Refund demanded',
    'dispute_risk': 'Dispute/chargeback risk',
    'status_regression': 'Status reopened',
    'negative_sentiment': 'Negative sentiment',
}


class Claim(models.Model):
    """
    Represents a lost object claim submitted by a client.
    """

    # Zendesk custom-status families (status_category in the Zendesk API).
    STATUS_FAMILIES = [
        ('new', 'New'),
        ('open', 'Open'),
        ('pending', 'Pending'),
        ('hold', 'On hold'),
        ('solved', 'Solved'),
    ]

    # Claim Identifiers
    alf_claim_id = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        null=True,  # Allow null for existing claims
        blank=True,
        help_text='ALF claim ID (format: ALF1234567) from Zendesk ticket subject'
    )
    zd_ticket_id = models.CharField(
        max_length=50,
        blank=True,
        db_index=True,
        null=True,
        unique=True,
        help_text='Zendesk ticket ID'
    )

    # Client Information
    client_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Client full name (from Zendesk "Customer Name" field)'
    )
    client_email = models.EmailField(
        db_index=True,
        help_text='Client email address (extracted from Zendesk ticket)'
    )
    phone = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text='Client phone number'
    )
    alternate_email = models.EmailField(
        blank=True,
        help_text='Alternate contact email'
    )
    email_alias = models.EmailField(
        blank=True,
        default='',
        help_text='Per-ticket inbound email alias (Zendesk "Email Alias" field); '
                  'cached here on first email check'
    )
    billing_address = models.TextField(
        blank=True,
        default='',
        help_text='Billing address (from Zendesk "Billing Address" field)'
    )
    shipping_address = models.TextField(
        blank=True,
        default='',
        help_text='Where to ship the recovered object (Zendesk "Shipping Address")'
    )

    # Claim Details
    flight_details = models.TextField(
        blank=True,
        help_text='Flight information (number, date, route)'
    )
    object_description = models.TextField(
        blank=True,
        help_text='Description of lost item'
    )
    incident_details = models.TextField(
        blank=True,
        default='',
        help_text='How/when the item was lost (Zendesk "Incident Details")'
    )
    lost_location = models.TextField(
        blank=True,
        default='',
        help_text='Where the item was lost (Zendesk "Lost Location") — primary search lead'
    )

    # Deadline (30-day claim lifecycle)
    deadline_date = models.DateField(
        null=True,
        blank=True,
        help_text='Claim deadline date (Zendesk "Deadline Date")'
    )
    deadline_time = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text='Claim deadline time-of-day (Zendesk "Deadline Time")'
    )
    deadline_timezone = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='Timezone for the deadline (Zendesk "Deadline Time Zone")'
    )
    deadline_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Computed deadline moment (date + best-effort time/timezone); urgency math uses this'
    )

    # Payment & order
    price_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Concierge fee the client paid (Zendesk "Price Paid")'
    )
    payment_method = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Payment method (Zendesk "Payment Method")'
    )
    payment_status = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Payment status from the storefront (Zendesk "Payment Status")'
    )
    woocommerce_id = models.CharField(
        max_length=50,
        blank=True,
        default='',
        db_index=True,
        help_text='WooCommerce order ID (Zendesk "WooCommerce ID")'
    )
    paypal_transaction_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
        db_index=True,
        help_text='PayPal transaction ID (from Zendesk). Cross-checks dispute '
                  'matching: a dispute links to this claim only if its PayPal '
                  'transaction ID agrees with this one.'
    )

    # Fulfillment
    tracking_info = models.TextField(
        blank=True,
        default='',
        help_text='Return-shipment tracking (Zendesk "3rd Party Tracking Information")'
    )

    # Flight lookup (AeroDataBox via the zd/flight-lookup/ endpoint)
    flight_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Normalized flight lookup result; written only by the flight-lookup endpoint'
    )
    flight_data_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When flight_data was last fetched'
    )

    # Workflow
    status = models.CharField(
        max_length=64,
        default=DEFAULT_STATUS,
        help_text='Zendesk custom status name (agent view), mirrored verbatim from the ticket'
    )
    status_category = models.CharField(
        max_length=10,
        choices=STATUS_FAMILIES,
        default=DEFAULT_CATEGORY,
        blank=True,
        help_text="Zendesk status family — drives grouping/colors; '' when unknown"
    )
    status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the current status was set (from the Zendesk webhook)'
    )

    # --- Client-risk flag (sticky) ---
    risk_level = models.CharField(max_length=10, choices=RISK_LEVELS, default='none', blank=True)
    risk_reasons = models.JSONField(default=list, blank=True)
    risk_detail = models.CharField(max_length=300, blank=True)
    risk_first_flagged_at = models.DateTimeField(null=True, blank=True)
    risk_last_signal_at = models.DateTimeField(null=True, blank=True)
    risk_acknowledged_at = models.DateTimeField(null=True, blank=True)
    risk_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='acknowledged_claim_risks')

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_claims',
        help_text='Agent assigned to this claim',
    )

    # LLM Extraction Tracking
    llm_extraction_failed = models.BooleanField(
        default=False,
        help_text='True if LLM failed to extract data from Zendesk ticket'
    )
    ai_summary = models.TextField(
        blank=True,
        help_text='AI-generated case summary (regenerated at creation, on Zendesk status changes, and on manual refresh)'
    )
    ai_summary_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the AI summary was last regenerated'
    )

    # Client "what we did" update (drafted when the claim enters the configured
    # submitted-status; an agent reviews and sends it as a public Zendesk reply).
    client_report_draft = models.TextField(
        blank=True,
        default='',
        help_text='Drafted client update message awaiting agent review/send'
    )
    client_report_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the client update was sent as a public Zendesk reply (None = not sent)'
    )
    client_report_skipped_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When an agent skipped the initial update (e.g. the claim reached '
                  'LORA late and the client was already updated). None = not skipped.'
    )
    zd_tags = models.JSONField(
        default=list,
        blank=True,
        help_text="The Zendesk ticket's tags as of the last 'Refresh from Zendesk'. "
                  "Used to show client-update milestones done via the manual macros "
                  "(client_update_N tags) without a live API call per page render."
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['status_category', '-created_at']),
            models.Index(fields=['client_email']),
            models.Index(fields=['assigned_to', '-created_at']),
            models.Index(fields=['alf_claim_id']),
            models.Index(fields=['zd_ticket_id']),
        ]

    def __str__(self) -> str:
        return f"Claim #{self.id} ({self.alf_claim_id}) - {self.client_email} ({self.status})"

    @property
    def risk_active(self) -> bool:
        """A raised risk that no one has acknowledged yet — what the badge/filter show."""
        return self.risk_level != 'none' and self.risk_acknowledged_at is None

    @property
    def has_exited(self) -> bool:
        """True once the claim has genuinely left active handling — a Solved or
        Closed Zendesk status. A 'Refund-Denied' ticket sits in the Solved FAMILY
        at Zendesk but is still being worked until it is actually closed, so it is
        NOT counted as exited (it keeps an active badge and stays in the active
        lenses). Mirrors the queryset filter in manager_claims so the badge and the
        lists always agree."""
        if self.status_category not in ('solved', 'closed'):
            return False
        return 'denied' not in (self.status or '').lower()

    @property
    def risk_reason_labels(self) -> list:
        """Human-readable labels for risk_reasons (raw tags -> display strings)."""
        return [RISK_REASON_LABELS.get(r, r) for r in (self.risk_reasons or [])]

    def register_risk(self, *, reasons, level, detail=''):
        """Sticky-merge a risk signal. Only ADDS reasons (union) and RAISES level —
        never downgrades, so a later clean read can't erase a flag. A genuinely new
        signal (a new reason, or the level rising) after an acknowledgement clears the
        acknowledgement so the badge returns. Saves only its own fields."""
        from django.utils import timezone
        reasons = [r for r in (reasons or []) if r]
        if level == 'none' and not reasons:
            return  # nothing to register; never downgrade
        existing = set(self.risk_reasons or [])
        incoming = set(reasons)
        old_rank = RISK_RANK.get(self.risk_level, 0)
        new_rank = max(old_rank, RISK_RANK.get(level, 0))
        is_new_signal = bool(incoming - existing) or new_rank > old_rank

        now = timezone.now()
        self.risk_reasons = sorted(existing | incoming)
        self.risk_level = _RANK_LEVEL[new_rank]
        if detail:
            self.risk_detail = detail[:300]
        if self.risk_first_flagged_at is None and new_rank > 0:
            self.risk_first_flagged_at = now
        self.risk_last_signal_at = now
        fields = ['risk_reasons', 'risk_level', 'risk_detail',
                  'risk_first_flagged_at', 'risk_last_signal_at', 'updated_at']
        if self.risk_acknowledged_at is not None and is_new_signal:
            self.risk_acknowledged_at = None
            self.risk_acknowledged_by = None
            fields += ['risk_acknowledged_at', 'risk_acknowledged_by']
        self.save(update_fields=fields)

    def acknowledge_risk(self, user):
        """Clear the active badge (records who/when). Keeps reasons/level for audit."""
        from django.utils import timezone
        self.risk_acknowledged_at = timezone.now()
        self.risk_acknowledged_by = user
        self.save(update_fields=['risk_acknowledged_at', 'risk_acknowledged_by', 'updated_at'])

    # These refund_* properties read the claim's refund set in Python, so a
    # prefetch_related('refunds') makes ALL of them free — zero extra queries.
    # That makes them safe to render per-row in a list (prefetch refunds on the
    # queryset). Without a prefetch each falls back to a query (no worse than the
    # previous .exists()/.aggregate()/.order_by() form).
    @property
    def has_refund(self) -> bool:
        """Check if claim has any refunds."""
        return bool(self.refunds.all())

    @property
    def refund_total(self) -> Decimal:
        """Total of COMPLETED refunds — always a Decimal."""
        # 'COMPLETED' mirrors apps.payments.models.Refund's status choice value;
        # kept as a literal here to avoid a claims->payments import cycle.
        return sum((r.amount for r in self.refunds.all() if r.status == 'COMPLETED'),
                   Decimal('0.00'))

    @property
    def latest_refund(self):
        """Get the most recent refund (by created_at)."""
        return max(self.refunds.all(), key=lambda r: r.created_at, default=None)

    @property
    def refund_status(self):
        """Get the status of the latest refund."""
        latest = self.latest_refund
        return latest.status if latest else None


class ClaimUpdateTimeline(models.Model):
    """
    Tracks updates from Zendesk ticket sync.
    Creates a timeline of changes when ticket is updated from Zendesk.
    """
    
    UPDATE_TYPE_CHOICES = [
        ('STATUS_CHANGE', 'Status Change'),
        ('NEW_COMMENT', 'New Comment'),
        ('INFO_UPDATED', 'Information Updated'),
        ('LLM_ANALYSIS', 'LLM Analysis'),
    ]
    
    claim = models.ForeignKey(
        Claim,
        on_delete=models.CASCADE,
        related_name='updates',
        help_text='Claim this update belongs to'
    )
    zendesk_ticket_id = models.CharField(
        max_length=50,
        db_index=True,
        help_text='Zendesk ticket ID'
    )
    update_type = models.CharField(
        max_length=20,
        choices=UPDATE_TYPE_CHOICES,
        help_text='Type of update'
    )
    changes_summary = models.TextField(
        blank=True,
        help_text='JSON summary of what changed'
    )
    llm_summary = models.TextField(
        blank=True,
        help_text='LLM-generated summary of changes'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['claim', '-created_at']),
            models.Index(fields=['zendesk_ticket_id']),
        ]
    
    def __str__(self) -> str:
        return f"Update for Claim #{self.claim_id} - {self.update_type} ({self.created_at:%b %d, %Y})"


class ClaimEvidence(models.Model):
    """
    Evidence (images) attached to a claim.
    """

    claim = models.ForeignKey(
        Claim,
        on_delete=models.CASCADE,
        related_name='evidence',
    )
    image = models.ImageField(upload_to='evidence/')
    description = models.TextField(
        blank=True,
        default='',
        help_text='Agent description of the evidence image'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self) -> str:
        return f"Evidence for Claim #{self.claim.id} - {self.description}"
