"""One-shot mapping of pre-mirror LORA statuses to Zendesk status names.
Used by the data migration; kept importable so it stays unit-tested."""

LEGACY_STATUS_MAP = {
    'Received': ('Investigation initiated', 'open'),
    'Searching': ('Claim submitted', 'open'),
    'Found': ('Object Found', 'open'),
    'Shipped': ('Object Found', 'open'),
    'Disputed': ('Open', 'open'),
    'REFUND_REQUESTED': ('Refund Requested', 'open'),
    'REFUNDED': ('Closed - Refunded', 'solved'),
    'PARTIALLY_REFUNDED': ('Closed - Refunded', 'solved'),
}


def map_legacy_status(old: str) -> tuple[str, str]:
    return LEGACY_STATUS_MAP.get(old, (old, 'open'))
