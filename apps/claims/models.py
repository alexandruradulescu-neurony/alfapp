from django.db import models
from django.conf import settings


class Claim(models.Model):
    """
    Represents a lost object claim submitted by a client.
    """

    STATUS_CHOICES = [
        ('Received', 'Received'),
        ('Searching', 'Searching'),
        ('Found', 'Found'),
        ('Shipped', 'Shipped'),
        ('Disputed', 'Disputed'),
        ('REFUND_REQUESTED', 'Refund Requested'),
        ('REFUNDED', 'Refunded'),
        ('PARTIALLY_REFUNDED', 'Partially Refunded'),
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

    # Claim Details
    flight_details = models.TextField(
        blank=True,
        help_text='Flight information (number, date, route)'
    )
    object_description = models.TextField(
        blank=True,
        help_text='Description of lost item'
    )

    # Workflow
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='Received',
        help_text='Current claim status'
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
        help_text='AI-generated summary from Zendesk ticket analysis (generated once at creation)'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status', '-created_at']),
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
        return f"Update for Claim #{self.claim_id} - {self.update_type} ({self.created_at|date:'M d, Y'})"


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
    description = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"Evidence for Claim #{self.claim.id} - {self.description}"
