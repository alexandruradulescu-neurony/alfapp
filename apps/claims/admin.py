from django.contrib import admin

from apps.claims.models import Claim, ClaimEvidence


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ('id', 'client_email', 'status', 'zd_ticket_id', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at')
    search_fields = ('client_email', 'zd_ticket_id', 'flight_details')
    ordering = ('-created_at',)
    readonly_fields = ('status', 'status_category', 'status_changed_at', 'created_at', 'updated_at')

    fieldsets = (
        ('Claim Information', {
            'fields': ('client_email', 'status', 'status_category', 'status_changed_at', 'zd_ticket_id', 'flight_details')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ClaimEvidence)
class ClaimEvidenceAdmin(admin.ModelAdmin):
    list_display = ('id', 'claim', 'description', 'uploaded_at')
    list_filter = ('uploaded_at',)
    search_fields = ('description', 'claim__client_email')
    ordering = ('-uploaded_at',)
    readonly_fields = ('uploaded_at',)

    fieldsets = (
        ('Evidence', {
            'fields': ('claim', 'image', 'description')
        }),
        ('Timestamp', {
            'fields': ('uploaded_at',),
            'classes': ('collapse',)
        }),
    )
