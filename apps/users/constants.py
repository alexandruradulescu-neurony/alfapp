"""Named constants for the users app."""

# Login brute-force throttle — see apps.users.views.rate_limit_logins / login_view.
# The counter records FAILED attempts per client IP and is cleared on success.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 60  # rolling window for the failed-attempt counter

# Evidence upload validation — see apps.users.views.agent_upload_evidence.
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024  # 10MB ceiling for an uploaded image
EVIDENCE_ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'gif', 'webp']
EVIDENCE_ALLOWED_MIME_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
MAGIC_SNIFF_BYTES = 1024  # bytes read for libmagic magic-number detection

# Claim list / dashboard tuning.
CLAIM_STUCK_DAYS = 14  # days in one status before a claim is flagged "stuck"
LIST_PAGE_SIZE = 20    # rows per page in the paginated list views

# Deadline display thresholds (days from now) — see _annotate_deadline.
DEADLINE_OVERDUE_DAYS = 0  # < this many days remaining → overdue
DEADLINE_DUE_TODAY_DAYS = 0  # exactly this many days remaining → due today
DEADLINE_SOON_DAYS = 7  # <= this many days remaining → due soon
