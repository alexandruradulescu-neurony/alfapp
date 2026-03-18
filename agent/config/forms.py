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

    class Meta:
        model = SystemSettings
        fields = [
            # AI Configuration
            'ai_provider',
            'ai_api_base',
            'ai_api_key',
            'ai_api_model',
            # AI Prompt Templates
            'ai_prompt_template',
            # IMAP Configuration
            'imap_host',
            'imap_user',
            'imap_pass',
            # Zendesk Configuration
            'zd_subdomain',
            'zd_token',
            'zd_email',
            # PayPal Configuration
            'paypal_client_id',
            'paypal_secret',
            'paypal_webhook_id',
            # Zendesk Sidebar Authentication
            'sidebar_secret_token',
            # Email Configuration
            'email_domain',
            'zd_alias_custom_field_id',
            # Zendesk Browser Authentication
            'zd_agent_email',
            'zd_agent_password',
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
