from django.db import models
from django.utils import timezone

from apps.config.encrypted_fields import EncryptedCharField

__all__ = ['SystemSettings', 'ServiceStatus']


class ServiceStatus(models.Model):
    """Track status of external service connections and background services."""
    
    SERVICE_CHOICES = [
        ('AI', 'AI Provider'),
        ('IMAP', 'IMAP Email'),
        ('ZENDESK', 'Zendesk'),
        ('PAYPAL', 'PayPal'),
        ('WOOCOMMERCE', 'WooCommerce'),
        ('SCHEDULER', 'Email Scheduler'),
    ]
    
    STATUS_CONNECTED = 'connected'
    STATUS_DISCONNECTED = 'disconnected'
    STATUS_ERROR = 'error'
    STATUS_RUNNING = 'running'
    STATUS_STOPPED = 'stopped'
    STATUS_CHOICES = [
        (STATUS_CONNECTED, 'Connected'),
        (STATUS_DISCONNECTED, 'Disconnected'),
        (STATUS_ERROR, 'Error'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_STOPPED, 'Stopped'),
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
        default=STATUS_DISCONNECTED,
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
    
    def get_status_color(self) -> str:
        """Return DaisyUI status color class."""
        color_map = {
            self.STATUS_CONNECTED: 'success',
            self.STATUS_DISCONNECTED: 'neutral',
            self.STATUS_ERROR: 'error',
            self.STATUS_RUNNING: 'primary',
            self.STATUS_STOPPED: 'warning',
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
    # Anthropic (Claude) — used ONLY for the dispute zone (better understanding of
    # the case). Everything else stays on the default provider above. When the
    # key is blank, disputes fall back to the default provider too.
    anthropic_api_base = models.CharField(
        max_length=255,
        default='https://api.anthropic.com',
        help_text='Anthropic API base URL (rarely changed)'
    )
    anthropic_api_key = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Anthropic (Claude) API key — used only for dispute reports (encrypted at rest)'
    )
    anthropic_model = models.CharField(
        max_length=100,
        default='claude-sonnet-4-6',
        choices=[
            ('claude-sonnet-4-6', 'Claude Sonnet 4.6 (recommended for disputes)'),
            ('claude-opus-4-8', 'Claude Opus 4.8 (most capable)'),
            ('claude-haiku-4-5', 'Claude Haiku 4.5 (fastest, cheapest)'),
        ],
        help_text='Claude model used for dispute report generation'
    )
    pii_tokenization_salt = EncryptedCharField(
        # 4580 is the user-facing (plaintext) limit; EncryptedCharField inflates
        # the actual DB column to hold the larger Fernet ciphertext. It's sized
        # to comfortably fit a long hex-encoded salt with room to spare — not a
        # magic constant tied to any one credential's length.
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
    paypal_mode = models.CharField(
        max_length=10,
        choices=[('sandbox', 'Sandbox (test — no real money)'), ('live', 'Live (real money)')],
        default='sandbox',
        help_text='Which PayPal environment dispute/refund API calls hit. '
                  'Defaults to SANDBOX — set to Live only when ready to move real money.'
    )

    # Client "what we did" update: the Zendesk custom-status name that, when a
    # claim enters it, drafts the client update. Blank = feature off.
    # LEGACY name-match fallback — prefer client_report_trigger_status_id below,
    # which matches the Zendesk custom-status ID (names can be renamed/duplicated).
    client_report_trigger_status = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='LEGACY: Zendesk status NAME that triggers the client update. '
                  'Prefer the status-ID field below. Leave blank to disable name matching.'
    )
    client_report_trigger_status_id = models.CharField(
        max_length=32,
        blank=True,
        default='',
        help_text='Zendesk custom-status ID that triggers drafting the client "what we did" '
                  'update + the follow-up cadence (e.g. the "Claim submitted" status ID). '
                  'This is the authoritative trigger. Leave blank to disable the feature.'
    )

    # Client update cadence: length of the concierge service in days (from
    # claim creation). Drives how far the update tail extends and when the
    # final end-of-service email goes out. Default in apps.communications.constants.
    service_length_days = models.PositiveIntegerField(
        default=30,
        help_text='Length of the concierge service in days, measured from claim creation. '
                  'Drives the client-update cadence tail and the final end-of-service email.'
    )
    # Autonomous client updates: OFF means LORA only SCHEDULES updates for an
    # agent to prepare/send manually. ON means the run_client_updates command
    # may draft AND send due updates as public Zendesk replies without an agent.
    client_updates_autosend = models.BooleanField(
        default=False,
        help_text='When ON, the run_client_updates job auto-drafts and sends due client '
                  'progress updates as public Zendesk replies. When OFF (default), updates '
                  'are only scheduled for an agent to prepare and send manually.'
    )
    # Global inbox sweep: dormant by design until the AI categorisation is proven.
    # When ON, the scheduled dispatcher polls the shared inbox and auto-categorises
    # institution mail. OFF (default) keeps email ingestion button-driven per ticket.
    email_sweep_autorun = models.BooleanField(
        default=False,
        help_text='When ON, the scheduled job sweeps the shared inbox for institution '
                  'replies and auto-categorises them (acts on live tickets). When OFF '
                  '(default), email is only checked per-ticket via the manual button.'
    )
    # Backlog-transition helper: while only some historic claims are mirrored
    # into LORA, an institution email may match a Zendesk ticket that has no
    # local claim yet. When ON, that email imports the existing claim from
    # Zendesk on the spot (never fabricates one). Rides on the inbox sweep, so
    # it only fires when email_sweep_autorun is also ON. Turn OFF once the full
    # backlog is in LORA.
    import_claims_from_email = models.BooleanField(
        default=False,
        help_text='When ON, an inbound institution email that matches a Zendesk ticket '
                  'with no local claim imports that existing claim from Zendesk on the '
                  'spot. Never creates a new claim. Requires the inbox sweep to be ON. '
                  'Intended as a temporary backlog-transition helper.'
    )
    recover_orphan_emails = models.BooleanField(
        default=False,
        help_text='When ON, each scheduled run re-routes orphaned emails (already swept, '
                  'no ticket match at the time) to their tickets. Idempotent — turn ON to '
                  'clear the backlog, watch one run, then turn OFF.')

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

    # --- Browser Use Cloud (Zendesk "Form filling" feature) ---
    browser_use_api_key = EncryptedCharField(
        max_length=255, blank=True, default='',
        help_text='Browser Use Cloud API key (bu_...), encrypted at rest. Powers the '
                  'Zendesk Form filling tab.')
    browser_use_model = models.CharField(
        max_length=64, blank=True, default='claude-sonnet-4.6',
        help_text='Model Browser Use runs the form-filling agent on.')
    form_filling_enabled = models.BooleanField(
        default=False,
        help_text='When ON, the Zendesk Form filling tab can drive Browser Use to fill '
                  'institution forms from a claim. OFF by default.')

    # WooCommerce store (for LORA-initiated refunds → WooCommerce → PayPal → Zendesk)
    woocommerce_store_url = models.URLField(
        blank=True,
        default='',
        help_text='WooCommerce store base URL, e.g. https://store.example.com (no trailing path)'
    )
    woocommerce_consumer_key = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='WooCommerce REST API consumer key (ck_…, Read/Write; encrypted at rest)'
    )
    woocommerce_consumer_secret = EncryptedCharField(
        max_length=255,
        blank=True,
        default='',
        help_text='WooCommerce REST API consumer secret (cs_…; encrypted at rest)'
    )

    # Dispute documents
    terms_conditions_pdf = models.FileField(
        upload_to='settings/',
        blank=True,
        null=True,
        help_text='Terms & Conditions PDF, attached to PayPal dispute first responses'
    )

    # Oblio invoicing (fallback for fetching the customer invoice when the
    # WooCommerce order has no stored invoice link). All optional — the primary
    # path reads the link already saved on the WooCommerce order.
    oblio_email = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Oblio API client id (the account email)'
    )
    oblio_secret = EncryptedCharField(
        max_length=255, blank=True, default='',
        help_text='Oblio API secret (encrypted at rest)'
    )
    oblio_cif = models.CharField(
        max_length=32, blank=True, default='',
        help_text='Your company tax id / CIF, required by the Oblio invoice API'
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

    # AI Prompt Templates
    email_analysis_prompt = models.TextField(
        default="""You are analyzing an incoming email for a lost object recovery service.

Email Content:
- Subject: {subject}
- Body: {body}

Analyze this email and provide:
1. Summary: Brief summary of the email content
2. Category: OBJECT_FOUND, OBJECT_NOT_FOUND, RESUBMISSION_REQUIRED, SUBMISSION_CONFIRMATION, GENERAL_CORRESPONDENCE, or UNKNOWN
3. Action Required: true/false - Does this email require human agent attention?
4. Auto Resolvable: true/false - Can this be automatically resolved without human intervention?

Respond with JSON in this format:
{
  "summary": "...",
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
