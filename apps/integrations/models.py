from django.conf import settings
from django.db import models


class FormFill(models.Model):
    """One institution-form fill attempt, driven by Browser Use, recorded per claim.

    The durable audit trail (what form, when, by whom, the result + screenshot) and
    the source of truth the Zendesk tab / claim page read from."""

    STATUS_STARTED = 'STARTED'
    STATUS_FILLED = 'FILLED'
    STATUS_SUBMITTING = 'SUBMITTING'
    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_STARTED, 'Started'),
        (STATUS_FILLED, 'Filled — awaiting approval'),
        (STATUS_SUBMITTING, 'Submitting'),
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_FAILED, 'Failed'),
    ]

    IMAGE_SOURCE_NONE = 'none'
    IMAGE_SOURCE_TICKET = 'ticket'
    IMAGE_SOURCE_UPLOAD = 'upload'
    IMAGE_SOURCE_CHOICES = [
        (IMAGE_SOURCE_NONE, 'No image'),
        (IMAGE_SOURCE_TICKET, 'From ticket attachment'),
        (IMAGE_SOURCE_UPLOAD, 'Agent uploaded'),
    ]

    claim = models.ForeignKey('claims.Claim', on_delete=models.CASCADE,
                              related_name='form_fills')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    form_url = models.URLField(max_length=1000)
    browser_use_session_id = models.CharField(max_length=128, blank=True, default='')
    browser_use_workspace_id = models.CharField(max_length=128, blank=True, default='')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_STARTED)

    image_source = models.CharField(max_length=8, choices=IMAGE_SOURCE_CHOICES,
                                    default=IMAGE_SOURCE_NONE)
    image_name = models.CharField(max_length=255, blank=True, default='')
    image = models.FileField(upload_to='form_fill_images/', null=True, blank=True)
    confirmation_screenshot = models.FileField(upload_to='form_fill_screenshots/',
                                               null=True, blank=True)

    result_output = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    posted_to_ticket = models.BooleanField(default=False)
    post_screenshot = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    filled_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['claim', '-created_at'])]

    def __str__(self):
        return f"FormFill #{self.pk} ({self.status}) for claim {self.claim_id}"
