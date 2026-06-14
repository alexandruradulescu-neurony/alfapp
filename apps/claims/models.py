from django.db import models
from django.conf import settings


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
        default='Investigation initiated',
        help_text='Zendesk custom status name (agent view), mirrored verbatim from the ticket'
    )
    status_category = models.CharField(
        max_length=10,
        choices=STATUS_FAMILIES,
        default='open',
        blank=True,
        help_text="Zendesk status family — drives grouping/colors; '' when unknown"
    )
    status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the current status was set (from the Zendesk webhook)'
    )
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

    def __str__(self):
        return f"Claim #{self.id} ({self.alf_claim_id}) - {self.client_email} ({self.status})"
    
    @property
    def has_refund(self):
        """Check if claim has any refunds."""
        return self.refunds.exists()
    
    @property
    def refund_total(self):
        """Calculate total refunded amount."""
        from django.db.models import Sum
        result = self.refunds.filter(status='COMPLETED').aggregate(total=Sum('amount'))
        return result['total'] or 0
    
    @property
    def latest_refund(self):
        """Get the most recent refund."""
        return self.refunds.order_by('-created_at').first()
    
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
    
    def __str__(self):
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

    def __str__(self):
        return f"Evidence for Claim #{self.claim.id} - {self.description}"
