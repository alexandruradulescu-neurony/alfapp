from django.db import models


class EmailLog(models.Model):
    """
    Logs incoming/outgoing emails related to claims and Zendesk tickets.
    Used for tracking communication history and AI analysis.
    
    Note: claim ForeignKey is nullable - emails may link to Zendesk tickets
    without a corresponding Claim record.
    """

    SENTIMENT_CHOICES = [
        ('Positive', 'Positive'),
        ('Neutral', 'Neutral'),
        ('Frustrated', 'Frustrated'),
        ('Urgent', 'Urgent'),
    ]

    CATEGORY_CHOICES = [
        ('OBJECT_FOUND', 'Object Found'),
        ('OBJECT_NOT_FOUND', 'Object Not Found'),
        ('RESUBMISSION_REQUIRED', 'Resubmission Required'),
        ('SUBMISSION_CONFIRMATION', 'Submission Confirmation'),
        ('GENERAL_CORRESPONDENCE', 'General Correspondence'),
        ('UNKNOWN', 'Unknown'),
    ]

    claim = models.ForeignKey(
        'claims.Claim',
        on_delete=models.PROTECT,  # Preserve audit trail
        related_name='emails',
        null=True,  # Allow emails without claims (Zendesk-only)
        blank=True,
        db_index=True,
    )
    subject = models.CharField(max_length=500, db_index=True)
    body = models.TextField()
    ai_summary = models.TextField(blank=True)
    sentiment = models.CharField(
        max_length=20,
        choices=SENTIMENT_CHOICES,
        blank=True,
        db_index=True,
    )
    action_required = models.BooleanField(default=False, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Email routing information
    from_email = models.EmailField(db_index=True, default='')
    to_email = models.EmailField(blank=True, db_index=True, default='')
    delivered_to = models.EmailField(blank=True, db_index=True, default='', help_text='Actual delivery address (for alias matching)')
    alias_matched = models.CharField(max_length=255, blank=True, db_index=True, default='', help_text='Matched alias if any')
    zd_ticket_id = models.CharField(max_length=50, blank=True, db_index=True, default='')

    # AI categorization
    category = models.CharField(
        max_length=30,
        choices=CATEGORY_CHOICES,
        default='UNKNOWN',
        db_index=True,
    )
    auto_resolved = models.BooleanField(default=False, db_index=True, help_text='Auto-resolved by AI')

    # Raw data for debugging
    raw_headers = models.TextField(blank=True, default='', help_text='Full email headers')

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['-received_at']),
            models.Index(fields=['claim', '-received_at']),
            models.Index(fields=['sentiment', 'action_required']),
            models.Index(fields=['category', 'auto_resolved']),
            models.Index(fields=['from_email', '-received_at']),
        ]

    def __str__(self):
        return f"EmailLog #{self.id} - {self.subject[:50]} (Claim #{self.claim_id if self.claim else 'None'})"
