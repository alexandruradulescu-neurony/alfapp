from django.contrib import admin

from apps.communications.models import EmailLog


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'claim', 'subject', 'category', 'action_required', 'received_at')
    list_filter = ('category', 'action_required', 'auto_resolved', 'received_at')
    search_fields = ('subject', 'body', 'ai_summary', 'claim__client_email', 'from_email')
    ordering = ('-received_at',)
    readonly_fields = (
        'claim', 'subject', 'body', 'ai_summary', 'action_required',
        'category', 'auto_resolved', 'received_at',
        'from_email', 'to_email', 'delivered_to', 'alias_matched', 'zd_ticket_id',
        'raw_headers',
    )

    fieldsets = (
        ('Email Information', {
            'fields': (
                'claim', 'subject', 'body',
                'from_email', 'to_email', 'delivered_to', 'alias_matched', 'zd_ticket_id'
            )
        }),
        ('AI Analysis', {
            'fields': ('ai_summary', 'category', 'action_required', 'auto_resolved')
        }),
        ('Debug Info', {
            'fields': ('raw_headers',),
            'classes': ('collapse',)
        }),
        ('Timestamp', {
            'fields': ('received_at',),
            'classes': ('collapse',)
        }),
    )
