"""Tunable durations for the communications domain — every hard-coded day/week
value the client-update cadence and the email sweep depend on lives here, so the
business rules are in one place and easy to audit or tweak.

Anything that is operator-configurable at runtime (e.g. the service length) has
its DEFAULT here and its live value on SystemSettings."""

# --- Email ingestion ---------------------------------------------------------
# How far back the IMAP search looks (UNSEEN + SINCE this many days). The
# unread flag alone can't guarantee once-only processing, so this bounds the
# window; Message-ID dedup does the rest.
EMAIL_LOOKBACK_DAYS = 2

# --- Client update cadence ---------------------------------------------------
# Default length of the concierge service in days, measured from claim/ticket
# creation. Configurable per-deployment via SystemSettings.service_length_days;
# this is only the fallback when that is unset. Drives the final email timing
# and how far the update cadence tail extends.
DEFAULT_SERVICE_LENGTH_DAYS = 30

# Fixed early progress updates, in days after the claim was SUBMITTED.
EARLY_UPDATE_OFFSETS = [2, 5, 11, 21]

# After the early updates, keep checking in every TAIL_STEP days starting at
# TAIL_START, for as long as the day is still inside the service window.
TAIL_START_DAY = 31
TAIL_STEP_DAYS = 10

# Milestone key for the single end-of-service email (sent only if the object
# was never found). Anchored to claim CREATION, not submission.
FINAL_MILESTONE = 'FINAL'


def cadence_offsets(service_length_days):
    """Day offsets (from the submission moment) for every progress update that
    falls strictly inside the service window. Returns an ascending list, e.g.
    L=30 -> [2, 5, 11, 21]; L=45 -> [2, 5, 11, 21, 31, 41]; L=55 -> [..., 51].

    The FINAL end-of-service email is NOT in this list — it is anchored to
    creation and handled separately."""
    offsets = [d for d in EARLY_UPDATE_OFFSETS if d < service_length_days]
    day = TAIL_START_DAY
    while day < service_length_days:
        offsets.append(day)
        day += TAIL_STEP_DAYS
    return offsets
