from django.db import models
from django.conf import settings
from django.utils import timezone


class Refund(models.Model):
    """
    Represents a refund transaction linked to a Claim.
    Tracks refunds initiated via LORA, WooCommerce, or manually.
    """
    
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    TYPE_CHOICES = [
        ('FULL', 'Full Refund'),
        ('PARTIAL', 'Partial Refund'),
    ]
    
    SOURCE_CHOICES = [
        ('LORA', 'LORA Initiated'),
        ('WOOCOMMERCE', 'WooCommerce/WordPress'),
        ('MANUAL', 'Manual Entry'),
    ]
    
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
        default='PENDING',
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
        default='LORA',
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
        """Mark refund as completed."""
        self.status = 'COMPLETED'
        self.processed_at = timezone.now()
        self.save()
    
    def mark_failed(self, error_message=''):
        """Mark refund as failed."""
        self.status = 'FAILED'
        if error_message:
            self.metadata['error_message'] = error_message
        self.save()
    
    def mark_processing(self):
        """Mark refund as processing."""
        self.status = 'PROCESSING'
        self.save()
    
    @property
    def is_completed(self):
        return self.status == 'COMPLETED'
    
    @property
    def is_pending(self):
        return self.status == 'PENDING'


class Dispute(models.Model):
    """
    Core dispute entity representing a PayPal dispute case.
    Tied to a Claim (required) with its own lifecycle.
    """

    STATUS_CHOICES = [
        ('RECEIVED', 'Received'),
        ('MATCHED', 'Matched to Zendesk Ticket'),
        ('GATHERING_DATA', 'Gathering Data'),
        ('DOCUMENTS_READY', 'Documents Ready'),
        ('UNDER_REVIEW', 'Under Review'),
        ('EVIDENCE_SENT', 'Evidence Sent to PayPal'),
        ('RESOLVED_WON', 'Resolved - Won'),
        ('RESOLVED_LOST', 'Resolved - Lost'),
        ('ACCEPTED', 'Accepted/Refunded'),
    ]

    REASON_CHOICES = [
        ('MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'Merchandise/Service Not Received'),
        ('MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED', 'Merchandise/Service Not As Described'),
        ('UNAUTHORIZED_TRANSACTION', 'Unauthorized Transaction'),
        ('CREDIT_NOT_PROCESSED', 'Credit Not Processed'),
        ('DUPLICATE_TRANSACTION', 'Duplicate Transaction'),
        ('INCORRECT_AMOUNT', 'Incorrect Amount'),
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
        default='RECEIVED',
        db_index=True,
    )

    # Dispute details
    dispute_reason = models.CharField(
        max_length=50,
        choices=REASON_CHOICES,
        blank=True,
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

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['buyer_email', '-created_at']),
            models.Index(fields=['transaction_id']),
            models.Index(fields=['claim', '-created_at']),
        ]
        constraints = [
            # Ensure buyer_email is set when claim is linked
            models.CheckConstraint(
                check=~models.Q(claim__isnull=False, buyer_email=''),
                name='dispute_claim_requires_buyer_email',
            ),
        ]

    def __str__(self):
        return f"Dispute #{self.id} - {self.buyer_email} ({self.status})"


class DisputeDocument(models.Model):
    """
    Response letters and evidence reports for disputes.
    """

    DOC_TYPE_CHOICES = [
        ('RESPONSE_LETTER', 'Response Letter'),
        ('EVIDENCE_REPORT', 'Evidence Report'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('REVIEW', 'Under Review'),
        ('ACCEPTED', 'Accepted'),
        ('SENT', 'Sent to PayPal'),
    ]

    GENERATED_BY_CHOICES = [
        ('AI', 'AI Generated'),
        ('MANUAL', 'Manually Created'),
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
        default='DRAFT',
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
        default='AI',
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


class DisputeScreenshot(models.Model):
    """
    Browser-captured Zendesk screenshots for disputes.
    """

    dispute = models.ForeignKey(
        Dispute,
        on_delete=models.CASCADE,
        related_name='screenshots',
    )
    image = models.ImageField(upload_to='dispute_screenshots/')
    description = models.CharField(max_length=500, blank=True)
    page_url = models.URLField(max_length=500, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['dispute', '-captured_at']),
        ]

    def __str__(self):
        return f"Screenshot #{self.id} for Dispute #{self.dispute_id}"


class DisputeActivityLog(models.Model):
    """
    Audit trail for dispute actions.
    """

    ACTION_CHOICES = [
        ('DISPUTE_CREATED', 'Dispute Created'),
        ('DISPUTE_MATCHED', 'Dispute Matched to Ticket'),
        ('SCREENSHOTS_CAPTURED', 'Screenshots Captured'),
        ('DOCUMENT_GENERATED', 'Document Generated'),
        ('DOCUMENT_ACCEPTED', 'Document Accepted'),
        ('EVIDENCE_SENT', 'Evidence Sent to PayPal'),
        ('STATUS_CHANGED', 'Status Changed'),
        ('NOTE_ADDED', 'Note Added'),
        ('DISPUTE_RESOLVED', 'Dispute Resolved'),
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


class ProcessedWebhookEvent(models.Model):
    """
    Tracks processed PayPal webhook events for idempotency.
    Prevents duplicate processing of the same webhook event.
    """

    STATUS_CHOICES = [
        ('processed', 'Processed'),
        ('failed', 'Failed'),
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
        default='processed',
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

    @classmethod
    def is_already_processed(cls, event_id: str) -> bool:
        """Check if an event has already been processed."""
        return cls.objects.filter(event_id=event_id, status='processed').exists()

    @classmethod
    def mark_as_processed(
        cls,
        event_id: str,
        event_type: str,
        resource_type: str = '',
        resource_id: str = '',
    ) -> 'ProcessedWebhookEvent':
        """Mark an event as processed."""
        obj, created = cls.objects.get_or_create(
            event_id=event_id,
            defaults={
                'event_type': event_type,
                'resource_type': resource_type,
                'resource_id': resource_id,
                'status': 'processed',
            }
        )
        return obj

    @classmethod
    def mark_as_failed(
        cls,
        event_id: str,
        event_type: str,
        error_message: str,
        resource_type: str = '',
        resource_id: str = '',
    ) -> 'ProcessedWebhookEvent':
        """Mark an event as failed."""
        obj, created = cls.objects.get_or_create(
            event_id=event_id,
            defaults={
                'event_type': event_type,
                'resource_type': resource_type,
                'resource_id': resource_id,
                'status': 'failed',
                'error_message': error_message,
            }
        )
        return obj
