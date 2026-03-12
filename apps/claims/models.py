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
    ]

    client_email = models.EmailField()  # Covered by Meta index
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='Received',
    )  # Covered by Meta composite index (status, -created_at)
    zd_ticket_id = models.CharField(
        max_length=50,
        blank=True,
        db_index=True,
        null=True,  # Allow null for unlinked claims
    )
    flight_details = models.TextField(blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_claims',
        help_text='Agent assigned to this claim',
    )  # Covered by Meta composite index (assigned_to, -created_at)
    created_at = models.DateTimeField(auto_now_add=True)  # Covered by Meta index
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['client_email']),
            models.Index(fields=['assigned_to', '-created_at']),
        ]

    def __str__(self):
        return f"Claim #{self.id} - {self.client_email} ({self.status})"


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
