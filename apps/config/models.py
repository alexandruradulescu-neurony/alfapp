from django.db import models
from django.utils import timezone

from apps.config.encrypted_fields import EncryptedCharField, EncryptedTextField

__all__ = ['SystemSettings', 'ServiceStatus']


class ServiceStatus(models.Model):
    """Track status of external service connections and background services."""
    
    SERVICE_CHOICES = [
        ('AI', 'AI Provider'),
        ('IMAP', 'IMAP Email'),
        ('ZENDESK', 'Zendesk'),
        ('PAYPAL', 'PayPal'),
        ('SCHEDULER', 'Email Scheduler'),
        ('SCREENSHOT', 'Screenshot Service'),
    ]
    
    STATUS_CHOICES = [
        ('connected', 'Connected'),
        ('disconnected', 'Disconnected'),
        ('error', 'Error'),
        ('running', 'Running'),
        ('stopped', 'Stopped'),
    ]
    
    service = models.CharField(
        max_length=20,
        choices=SERVICE_CHOICES,
        unique=True,
        help_text='The service this status entry represents'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='disconnected',
        help_text='Current connection/operational status'
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text='Whether this service is enabled (toggle control)'
    )
    last_checked = models.DateTimeField(
        auto_now_add=True,
        help_text='Last time the connection was tested'
    )
    last_error = models.TextField(
        blank=True,
        default='',
        help_text='Last error message if status is error'
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional service-specific metadata'
    )
    
    class Meta:
        ordering = ['service']
        verbose_name = 'Service Status'
        verbose_name_plural = 'Service Statuses'
    
    def __str__(self):
        service_name = dict(self.SERVICE_CHOICES).get(self.service, self.service)
        return f'{service_name} - {self.get_status_display()}'
    
    def mark_connected(self):
        """Mark service as connected."""
        self.status = 'connected'
        self.last_checked = timezone.now()
        self.last_error = ''
        self.save()
    
    def mark_disconnected(self):
        """Mark service as disconnected."""
        self.status = 'disconnected'
        self.last_checked = timezone.now()
        self.last_error = ''
        self.save()
    
    def mark_error(self, error_message):
        """Mark service as having an error."""
        self.status = 'error'
        self.last_checked = timezone.now()
        self.last_error = error_message
        self.save()
    
    def get_status_color(self):
        """Return DaisyUI status color class."""
        color_map = {
            'connected': 'success',
            'disconnected': 'neutral',
            'error': 'error',
            'running': 'primary',
            'stopped': 'warning',
        }
        return color_map.get(self.status, 'neutral')


class SystemSettings(models.Model):
    """
    Singleton model for system-wide configuration.
    Only one instance should exist (pk=1).

    Sensitive fields are encrypted at rest using EncryptedCharField/EncryptedTextField.
    """

    # AI Configuration
    ai_provider = models.CharField(
        max_length=50,
        default='DeepSeek',
        help_text='AI provider name (e.g., DeepSeek, Qwen)'
    )
    ai_api_base = models.CharField(
        max_length=255,
        default='https://api.deepseek.com/v1',
        help_text='AI API base URL (e.g., https://api.deepseek.com/v1)'
    )
    ai_api_key = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='AI API key (encrypted at rest)'
    )
    ai_api_model = models.CharField(
        max_length=100,
        default='deepseek-chat',
        help_text='AI model name (e.g., deepseek-chat, qwen-plus)'
    )
    pii_tokenization_salt = EncryptedCharField(
        max_length=4580,
        blank=True,
        default='',
        help_text=(
            'HMAC-SHA256 key for deterministic PII placeholder generation. '
            'If empty, falls back to the PII_TOKENIZATION_SALT env var. '
            'Set a long random value (32+ bytes hex-encoded) for production.'
        ),
    )

    # AI Prompt Templates
    ai_prompt_template = models.TextField(
        default="""You are an assistant for the Lost Object Recovery Automation (LORA) system.
Your task is to analyze lost object claims and help determine the appropriate action.

Claim Details:
- Item: {item_description}
- Location: {location}
- Date Lost: {date_lost}
- Claimant: {claimant_name}
- Status: {status}

Please analyze this claim and provide:
1. Recommended action (approve, reject, needs_review)
2. Confidence level (high, medium, low)
3. Reasoning for your recommendation
4. Any additional information needed""",
        help_text="Template for AI analysis of claims. Use {variable} placeholders."
    )

    # IMAP Configuration (ENCRYPTED - sensitive credentials)
    imap_host = models.CharField(
        max_length=255,
        default='imap.gmail.com',
        help_text='IMAP server hostname'
    )
    imap_user = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='IMAP username/email'
    )
    imap_pass = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='IMAP password or app-specific password (encrypted at rest)'
    )

    # Zendesk Configuration (ENCRYPTED - sensitive credentials)
    zd_subdomain = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Zendesk subdomain (e.g., "company" in company.zendesk.com)'
    )
    zd_token = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Zendesk API token (encrypted at rest)'
    )
    zd_email = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Zendesk account email'
    )

    # PayPal Configuration (ENCRYPTED - sensitive credentials)
    paypal_client_id = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='PayPal API Client ID (encrypted at rest)'
    )
    paypal_secret = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='PayPal API Secret (encrypted at rest)'
    )
    paypal_webhook_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='PayPal Webhook ID for event notifications'
    )

    # Zendesk Sidebar Authentication (ENCRYPTED - sensitive credential)
    sidebar_secret_token = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Secret token for Zendesk sidebar authentication (encrypted at rest)'
    )

    # Flight data provider (ENCRYPTED - sensitive credential)
    aerodatabox_api_key = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='AeroDataBox (RapidAPI) key for flight lookups (encrypted at rest)'
    )

    # Email Configuration
    email_domain = models.CharField(
        max_length=255,
        blank=True,
        help_text='Domain for alias matching (e.g., mydomain.com)'
    )
    zd_alias_custom_field_id = models.CharField(
        max_length=50,
        blank=True,
        help_text='Zendesk custom field ID storing the email alias'
    )

    # Zendesk Browser Authentication (ENCRYPTED - for screenshot capture)
    zd_agent_email = models.CharField(
        max_length=255,
        blank=True,
        help_text='Zendesk agent email for browser authentication (encrypted)'
    )
    zd_agent_password = EncryptedCharField(
        max_length=255,
        blank=True,
        help_text='Zendesk agent password for browser authentication (encrypted at rest)'
    )

    # AI Prompt Templates
    dispute_response_prompt = models.TextField(
        default="""You are drafting a professional response letter for a PayPal dispute.

Dispute Information:
- Reason: {dispute_reason}
- Amount: {dispute_amount} {dispute_currency}
- Buyer: {buyer_name} ({buyer_email})
- Transaction: {transaction_id} on {transaction_date}

Zendesk Ticket Information:
- Ticket ID: {zd_ticket_id}
- Subject: {ticket_subject}
- Status: {ticket_status}

Based on the Zendesk ticket data and communication history, draft a professional response letter that:
1. Acknowledges the customer's concern
2. Presents the facts from the ticket history
3. Explains any resolution actions taken
4. Maintains a courteous and professional tone

Response Letter:""",
        help_text='Template for AI-generated dispute response letters'
    )
    email_analysis_prompt = models.TextField(
        default="""You are analyzing an incoming email for a lost object recovery service.

Email Content:
- Subject: {subject}
- Body: {body}

Analyze this email and provide:
1. Summary: Brief summary of the email content
2. Sentiment: Positive, Neutral, Frustrated, or Urgent
3. Category: OBJECT_FOUND, OBJECT_NOT_FOUND, RESUBMISSION_REQUIRED, SUBMISSION_CONFIRMATION, GENERAL_CORRESPONDENCE, or UNKNOWN
4. Action Required: true/false - Does this email require human agent attention?
5. Auto Resolvable: true/false - Can this be automatically resolved without human intervention?

Respond with JSON in this format:
{
  "summary": "...",
  "sentiment": "...",
  "category": "...",
  "action_required": true/false,
  "auto_resolvable": true/false
}""",
        help_text='Template for AI email categorization and analysis'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'System Settings'
        verbose_name_plural = 'System Settings'

    def __str__(self):
        return 'System Settings'

    def save(self, *args, **kwargs):
        """
        Enforce singleton pattern:
        - Always set pk=1
        - Never insert a second row
        """
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_instance(cls):
        """Get or create the singleton instance."""
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance
    
    def get_masked_value(self, field_name):
        """
        Get a masked version of a sensitive field for display.
        Shows first 4 and last 4 characters, masks the rest.
        """
        value = getattr(self, field_name, '')
        if not value:
            return ''
        if len(value) <= 8:
            return '•' * len(value)
        return value[:4] + '•' * (len(value) - 8) + value[-4:]
