from django.contrib import admin

from apps.config.models import SystemSettings, ServiceStatus


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
        ('Flight Data (AeroDataBox)', {
            'fields': ('aerodatabox_api_key',),
            'description': 'RapidAPI key for the AeroDataBox flight lookups.'
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


@admin.register(ServiceStatus)
class ServiceStatusAdmin(admin.ModelAdmin):
    list_display = ['service', 'status', 'is_enabled', 'last_checked', 'last_error']
    list_filter = ['status', 'is_enabled', 'service']
    search_fields = ['service', 'last_error']
    readonly_fields = ['last_checked']
    
    fieldsets = (
        ('Service Information', {
            'fields': ('service', 'status', 'is_enabled')
        }),
        ('Status Details', {
            'fields': ('last_checked', 'last_error', 'metadata')
        }),
    )
    
    actions = ['enable_services', 'disable_services']
    
    def enable_services(self, request, queryset):
        queryset.update(is_enabled=True)
    enable_services.short_description = 'Enable selected services'
    
    def disable_services(self, request, queryset):
        queryset.update(is_enabled=False)
    disable_services.short_description = 'Disable selected services'
