"""Named constants for the users app."""

# Login brute-force throttle — see apps.users.views.rate_limit_logins / login_view.
# The counter records FAILED attempts per client IP and is cleared on success.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 60  # rolling window for the failed-attempt counter
