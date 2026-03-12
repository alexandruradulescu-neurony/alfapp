from django.contrib import admin

from apps.communications.models import EmailLog


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'claim', 'subject', 'sentiment', 'action_required', 'received_at')
    list_filter = ('sentiment', 'action_required', 'received_at')
    search_fields = ('subject', 'body', 'ai_summary', 'claim__client_email')
    ordering = ('-received_at',)
    readonly_fields = ('claim', 'subject', 'body', 'ai_summary', 'sentiment', 'action_required', 'received_at')

    fieldsets = (
        ('Email Information', {
            'fields': ('claim', 'subject', 'body')
        }),
        ('AI Analysis', {
            'fields': ('ai_summary', 'sentiment', 'action_required')
        }),
        ('Timestamp', {
            'fields': ('received_at',),
            'classes': ('collapse',)
        }),
    )
