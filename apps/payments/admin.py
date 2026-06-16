from django.contrib import admin
from django.utils import timezone
from apps.payments.models import Dispute, DisputeDocument, DisputeActivityLog, ProcessedWebhookEvent, Refund


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'paypal_dispute_id', 'claim', 'buyer_email', 'dispute_amount', 'status', 'assigned_to', 'created_at')
    list_filter = ('status', 'dispute_reason', 'created_at')
    search_fields = ('paypal_dispute_id', 'buyer_email', 'transaction_id', 'claim__client_email')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'raw_webhook_payload',
                       'outbound_in_flight', 'outbound_in_flight_at')
    actions = ['reset_outbound_lock']

    @admin.action(description='Reset stuck outbound (in-flight) lock')
    def reset_outbound_lock(self, request, queryset):
        """Manual escape hatch for a dispute whose outbound lock was left set by a
        crashed worker (it also auto-expires after Dispute.OUTBOUND_INFLIGHT_TTL)."""
        updated = queryset.update(outbound_in_flight=False, outbound_in_flight_at=None)
        self.message_user(request, f"Cleared the outbound lock on {updated} dispute(s).")

    fieldsets = (
        ('PayPal Information', {
            'fields': ('paypal_dispute_id', 'paypal_case_id', 'dispute_reason', 'dispute_amount', 'dispute_currency')
        }),
        ('Links', {
            'fields': ('claim', 'zd_ticket_id')
        }),
        ('Buyer Information', {
            'fields': ('buyer_email', 'buyer_name')
        }),
        ('Transaction Information', {
            'fields': ('transaction_id', 'transaction_date', 'seller_response_due')
        }),
        ('Status', {
            'fields': ('status', 'assigned_to', 'notes')
        }),
        ('Raw Payload', {
            'fields': ('raw_webhook_payload',),
            'classes': ('collapse',)
        }),
        ('Outbound lock', {
            'fields': ('outbound_in_flight', 'outbound_in_flight_at'),
            'classes': ('collapse',),
            'description': ('In-flight guard for PayPal accept/reply. A worker that '
                            'crashed mid-call can leave this stuck True; it auto-expires '
                            'after the TTL, or use the "Reset stuck outbound lock" action.'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(DisputeDocument)
class DisputeDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'dispute', 'doc_type', 'status', 'version', 'generated_by', 'created_at')
    list_filter = ('doc_type', 'status', 'generated_by')
    search_fields = ('dispute__paypal_dispute_id', 'dispute__buyer_email')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Document Information', {
            'fields': ('dispute', 'doc_type', 'version', 'generated_by')
        }),
        ('Status', {
            'fields': ('status', 'accepted_by', 'accepted_at')
        }),
        ('Content', {
            'fields': ('file_path', 'content_html')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(DisputeActivityLog)
class DisputeActivityLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'dispute', 'action', 'performed_by', 'performed_at')
    list_filter = ('action', 'performed_at')
    search_fields = ('dispute__paypal_dispute_id', 'performed_by__username')
    ordering = ('-performed_at',)
    readonly_fields = ('performed_at',)

    fieldsets = (
        ('Activity Information', {
            'fields': ('dispute', 'action', 'details', 'performed_by')
        }),
        ('Timestamp', {
            'fields': ('performed_at',),
            'classes': ('collapse',)
        }),
    )


@admin.register(ProcessedWebhookEvent)
class ProcessedWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'event_id', 'event_type', 'resource_type', 'resource_id', 'status', 'processed_at')
    list_filter = ('status', 'event_type', 'processed_at')
    search_fields = ('event_id', 'event_type', 'resource_id')
    ordering = ('-processed_at',)
    readonly_fields = ('processed_at',)

    fieldsets = (
        ('Event Information', {
            'fields': ('event_id', 'event_type', 'resource_type', 'resource_id')
        }),
        ('Status', {
            'fields': ('status', 'error_message')
        }),
        ('Timestamp', {
            'fields': ('processed_at',),
            'classes': ('collapse',)
        }),
    )


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ('id', 'claim', 'amount', 'currency', 'status', 'refund_type', 'external_source', 'created_at', 'created_by')
    list_filter = ('status', 'refund_type', 'external_source', 'created_at')
    search_fields = ('claim__client_email', 'paypal_refund_id', 'claim__id')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'processed_at', 'metadata')

    fieldsets = (
        ('Refund Information', {
            'fields': ('claim', 'paypal_refund_id', 'paypal_capture_id', 'amount', 'currency')
        }),
        ('Status & Type', {
            'fields': ('status', 'refund_type', 'external_source')
        }),
        ('Details', {
            'fields': ('reason', 'metadata')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'processed_at', 'created_by')
        }),
    )

    actions = ['mark_completed', 'mark_failed', 'mark_cancelled']

    def mark_completed(self, request, queryset):
        count = queryset.update(status='COMPLETED', processed_at=timezone.now())
        self.message_user(request, f'{count} refunds marked as completed.')
    mark_completed.short_description = 'Mark selected refunds as completed'

    def mark_failed(self, request, queryset):
        count = queryset.update(status='FAILED')
        self.message_user(request, f'{count} refunds marked as failed.')
    mark_failed.short_description = 'Mark selected refunds as failed'

    def mark_cancelled(self, request, queryset):
        count = queryset.update(status='CANCELLED')
        self.message_user(request, f'{count} refunds marked as cancelled.')
    mark_cancelled.short_description = 'Mark selected refunds as cancelled'
