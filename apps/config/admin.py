from django.contrib import admin

from apps.config.models import SystemSettings


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ('updated_at', 'created_at')
    readonly_fields = ('created_at', 'updated_at')
    
    fieldsets = (
        ('AI Configuration', {
            'fields': ('ai_prompt_template',),
            'description': 'Template used for AI analysis of claims.'
        }),
        ('IMAP Settings', {
            'fields': ('imap_host', 'imap_user', 'imap_pass'),
            'description': 'Email server configuration for fetching claim notifications.'
        }),
        ('Zendesk Settings', {
            'fields': ('zd_subdomain', 'zd_token', 'zd_email'),
            'description': 'Zendesk integration configuration.'
        }),
        ('PayPal Settings', {
            'fields': ('paypal_client_id', 'paypal_secret', 'paypal_webhook_id'),
            'description': 'PayPal payment processing configuration.'
        }),
        ('Sidebar Authentication', {
            'fields': ('sidebar_secret_token',),
            'description': 'Secret token for Zendesk sidebar authentication.'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def has_add_permission(self, request):
        # Prevent adding more than one instance
        return not SystemSettings.objects.exists()
    
    def has_delete_permission(self, request, obj=None):
        # Prevent deletion of the singleton instance
        return False
