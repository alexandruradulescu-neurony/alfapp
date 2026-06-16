from django.db import models


class EmailLog(models.Model):
    """
    Logs incoming/outgoing emails related to claims and Zendesk tickets.
    Used for tracking communication history and AI analysis.

    Note: claim ForeignKey is nullable - emails may link to Zendesk tickets
    without a corresponding Claim record.
    """

    # Category values. Reference these constants (not bare strings) in filters so
    # a rename can't silently break a count or a categorization branch.
    CATEGORY_OBJECT_FOUND = 'OBJECT_FOUND'
    CATEGORY_OBJECT_NOT_FOUND = 'OBJECT_NOT_FOUND'
    CATEGORY_RESUBMISSION_REQUIRED = 'RESUBMISSION_REQUIRED'
    CATEGORY_SUBMISSION_CONFIRMATION = 'SUBMISSION_CONFIRMATION'
    CATEGORY_GENERAL_CORRESPONDENCE = 'GENERAL_CORRESPONDENCE'
    CATEGORY_UNKNOWN = 'UNKNOWN'
    CATEGORY_CHOICES = [
        (CATEGORY_OBJECT_FOUND, 'Object Found'),
        (CATEGORY_OBJECT_NOT_FOUND, 'Object Not Found'),
        (CATEGORY_RESUBMISSION_REQUIRED, 'Resubmission Required'),
        (CATEGORY_SUBMISSION_CONFIRMATION, 'Submission Confirmation'),
        (CATEGORY_GENERAL_CORRESPONDENCE, 'General Correspondence'),
        (CATEGORY_UNKNOWN, 'Unknown'),
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

    # RFC 5322 Message-ID — the dedup key: an email is processed at most once,
    # ever, regardless of its read/unread flag in the mailbox.
    message_id = models.CharField(max_length=512, blank=True, default='', db_index=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['-received_at']),
            models.Index(fields=['claim', '-received_at']),
            models.Index(fields=['category', 'auto_resolved']),
            models.Index(fields=['from_email', '-received_at']),
        ]
        constraints = [
            # The database itself refuses a second row for the same email —
            # the check-then-create dedup alone loses a same-second race
            # between two button presses. Blank ids (old rows) are exempt.
            models.UniqueConstraint(
                fields=['message_id'],
                condition=~models.Q(message_id=''),
                name='uniq_emaillog_message_id',
            ),
        ]

    def __str__(self):
        return f"EmailLog #{self.id} - {self.subject[:50]} (Claim #{self.claim_id if self.claim else 'None'})"


class ClientUpdate(models.Model):
    """A scheduled client progress update (the day-2/5/11/21 follow-ups after a
    claim is submitted). The INITIAL "what we did" update lives on the Claim
    itself; these are the follow-up cadence. Each is drafted for an agent to
    review and send as a public Zendesk reply (draft-for-approval)."""

    # The early cadence. The tail (DAY_31, DAY_41, …) and the end-of-service
    # FINAL are scheduled dynamically from the configured service length, so
    # they are not all enumerated here — `label` renders any milestone key.
    MILESTONE_CHOICES = [
        ('DAY_2', 'Day 2'),
        ('DAY_5', 'Day 5'),
        ('DAY_11', 'Day 11'),
        ('DAY_21', 'Day 21'),
        ('FINAL', 'Final update'),
    ]
    # State machine values. Use these constants (not bare strings) in filters
    # and updates so a typo is a NameError, not a silent no-op filter.
    STATE_SCHEDULED = 'SCHEDULED'   # due_at in the future / not yet prepared
    STATE_DRAFTED = 'DRAFTED'       # prepared, awaiting agent review/send
    STATE_SENT = 'SENT'
    STATE_SKIPPED = 'SKIPPED'       # agent chose not to send, or claim closed
    STATE_CHOICES = [
        (STATE_SCHEDULED, 'Scheduled'),
        (STATE_DRAFTED, 'Drafted'),
        (STATE_SENT, 'Sent'),
        (STATE_SKIPPED, 'Skipped'),
    ]
    # The set of "open" states — exactly one is open at a time (the cascade).
    OPEN_STATES = (STATE_SCHEDULED, STATE_DRAFTED)

    claim = models.ForeignKey(
        'claims.Claim', on_delete=models.CASCADE, related_name='follow_up_updates', db_index=True,
    )
    milestone = models.CharField(max_length=10)
    due_at = models.DateTimeField(db_index=True)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default='SCHEDULED', db_index=True)
    draft_body = models.TextField(blank=True, default='')
    has_news = models.BooleanField(
        default=False, help_text='True if the draft reflects new developments (vs a "still searching" note)')
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_at']
        constraints = [
            # One row per milestone per claim — the schedule is fixed.
            models.UniqueConstraint(fields=['claim', 'milestone'], name='uniq_clientupdate_claim_milestone'),
        ]
        indexes = [
            models.Index(fields=['state', 'due_at']),
        ]

    def __str__(self):
        return f"ClientUpdate {self.milestone} (Claim #{self.claim_id}, {self.state})"

    @property
    def label(self) -> str:
        """Human label for any milestone key, including the dynamic tail
        (DAY_31, DAY_41, …) and the end-of-service FINAL."""
        if self.milestone == 'FINAL':
            return 'Final update'
        if self.milestone.startswith('DAY_'):
            return f'Day {self.milestone[4:]}'
        return self.milestone
