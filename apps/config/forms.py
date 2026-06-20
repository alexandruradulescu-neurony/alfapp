"""
Forms for SystemSettings configuration.
"""

from django import forms
from apps.config.models import SystemSettings


class SystemSettingsForm(forms.ModelForm):
    """
    ModelForm for SystemSettings.

    Sensitive fields (passwords, tokens) are handled specially:
    - They are NOT pre-filled in the form (security)
    - They are only updated if a new value is provided
    
    All fields are optional to allow partial configuration updates.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make all fields optional to allow partial saves
        for field in self.fields.values():
            field.required = False

    def clean_service_length_days(self):
        # Never let a blank/zero value null out the NOT NULL column — fall back
        # to the configured default.
        from apps.communications.constants import DEFAULT_SERVICE_LENGTH_DAYS
        return self.cleaned_data.get('service_length_days') or DEFAULT_SERVICE_LENGTH_DAYS

    # Sensitive fields that should NOT be in the form
    # They are handled separately in the view to preserve values
    SENSITIVE_FIELDS = [
        'ai_api_key',
        'imap_pass',
        'zd_token',
        'paypal_secret',
        'sidebar_secret_token',
        'woocommerce_consumer_key',
        'woocommerce_consumer_secret',
        'oblio_secret',
    ]

    class Meta:
        model = SystemSettings
        # IMPORTANT: every field listed here MUST be rendered on the settings page.
        # A field that is in the form but not on the page is wiped to blank on every
        # Save (it's absent from POST -> cleaned to '' -> saved). The behaviour
        # switches (client_updates_autosend, email_sweep_autorun) are deliberately
        # NOT here — they are instant AJAX toggles, persisted via their own endpoint.
        # ai_prompt_template was removed: it is dead (referenced nowhere in code).
        fields = [
            # AI Configuration (non-sensitive)
            'ai_provider',
            'ai_api_base',
            'ai_api_model',
            # Email / IMAP (non-sensitive)
            'imap_host',
            'imap_user',
            'email_domain',
            'zd_alias_custom_field_id',
            # Zendesk (non-sensitive)
            'zd_subdomain',
            'zd_email',
            # PayPal (non-sensitive)
            'paypal_client_id',
            'paypal_webhook_id',
            'paypal_mode',
            # WooCommerce (non-sensitive part)
            'woocommerce_store_url',
            # Oblio invoicing (non-sensitive part; secret is in SENSITIVE_FIELDS)
            'oblio_email',
            'oblio_cif',
            # Client update automation
            'client_report_trigger_status',
            'client_report_trigger_status_id',
            'service_length_days',
        ]
        widgets = {
            # AI Configuration
            'ai_provider': forms.TextInput(attrs={'class': 'form-control'}),
            'ai_api_base': forms.TextInput(attrs={'class': 'form-control'}),
            'ai_api_key': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'ai_api_model': forms.TextInput(attrs={'class': 'form-control'}),
            # Password fields for sensitive data
            'imap_pass': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'zd_token': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'paypal_secret': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'sidebar_secret_token': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            # Regular text inputs
            'imap_host': forms.TextInput(attrs={'class': 'form-control'}),
            'imap_user': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_subdomain': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_email': forms.TextInput(attrs={'class': 'form-control'}),
            'paypal_client_id': forms.TextInput(attrs={'class': 'form-control'}),
            'paypal_webhook_id': forms.TextInput(attrs={'class': 'form-control'}),
            'email_domain': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_alias_custom_field_id': forms.TextInput(attrs={'class': 'form-control'}),
        }
