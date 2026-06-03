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

    # Sensitive fields that should NOT be in the form
    # They are handled separately in the view to preserve values
    SENSITIVE_FIELDS = [
        'ai_api_key',
        'imap_pass',
        'zd_token',
        'paypal_secret',
        'sidebar_secret_token',
        'zd_agent_password',
    ]

    class Meta:
        model = SystemSettings
        fields = [
            # AI Configuration (non-sensitive)
            'ai_provider',
            'ai_api_base',
            'ai_api_model',
            # AI Prompt Templates
            'ai_prompt_template',
            # IMAP Configuration (non-sensitive)
            'imap_host',
            'imap_user',
            # Zendesk Configuration (non-sensitive)
            'zd_subdomain',
            'zd_email',
            # PayPal Configuration (non-sensitive)
            'paypal_client_id',
            'paypal_webhook_id',
            # Zendesk Sidebar Authentication (non-sensitive)
            # Email Configuration
            'email_domain',
            'zd_alias_custom_field_id',
            # Zendesk Browser Authentication (non-sensitive)
            'zd_agent_email',
            # AI Prompt Templates
            'dispute_response_prompt',
            'email_analysis_prompt',
        ]
        widgets = {
            # AI Configuration
            'ai_provider': forms.TextInput(attrs={'class': 'form-control'}),
            'ai_api_base': forms.TextInput(attrs={'class': 'form-control'}),
            'ai_api_key': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'ai_api_model': forms.TextInput(attrs={'class': 'form-control'}),
            # Text areas for prompts
            'ai_prompt_template': forms.Textarea(attrs={'rows': 10, 'class': 'form-control'}),
            'dispute_response_prompt': forms.Textarea(attrs={'rows': 10, 'class': 'form-control'}),
            'email_analysis_prompt': forms.Textarea(attrs={'rows': 10, 'class': 'form-control'}),
            # Password fields for sensitive data
            'imap_pass': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'zd_token': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'paypal_secret': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'sidebar_secret_token': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            'zd_agent_password': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'placeholder': '••••••••••••'}),
            # Regular text inputs
            'imap_host': forms.TextInput(attrs={'class': 'form-control'}),
            'imap_user': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_subdomain': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_email': forms.TextInput(attrs={'class': 'form-control'}),
            'paypal_client_id': forms.TextInput(attrs={'class': 'form-control'}),
            'paypal_webhook_id': forms.TextInput(attrs={'class': 'form-control'}),
            'email_domain': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_alias_custom_field_id': forms.TextInput(attrs={'class': 'form-control'}),
            'zd_agent_email': forms.TextInput(attrs={'class': 'form-control'}),
        }
