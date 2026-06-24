"""Re-anchoring corrects reminders scheduled off the IMPORT date. The error is the
gap (created_at - submitted_at): mis-anchored claims have their open reminders shifted
earlier (past ones skipped); correctly-anchored claims are left alone even if overdue."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import ClientUpdate
from apps.communications.client_updates import reanchor_client_updates


def _claim(slug, *, created_at, submitted_at):
    c = Claim.objects.create(client_email='c@e.com', alf_claim_id=slug, zd_ticket_id=slug)
    # created_at is auto_now_add; override both via update() to simulate import timing.
    Claim.objects.filter(id=c.id).update(created_at=created_at, submitted_at=submitted_at)
    return Claim.objects.get(id=c.id)


@pytest.mark.django_db
def test_mis_anchored_past_reminder_is_skipped():
    base = timezone.now()
    # Paid May (60d ago) but imported now → gap 60d; its DAY_11 shifts into the deep past.
    claim = _claim('RA1', created_at=base, submitted_at=base - timedelta(days=60))
    cu = ClientUpdate.objects.create(claim=claim, milestone='DAY_11',
                                     due_at=base + timedelta(days=5),
                                     state=ClientUpdate.STATE_SCHEDULED)
    reanchor_client_updates()
    cu.refresh_from_db()
    assert cu.state == ClientUpdate.STATE_SKIPPED          # window long past → never sent


@pytest.mark.django_db
def test_mis_anchored_future_reminder_is_shifted_earlier():
    base = timezone.now()
    # Paid 5d before import (gap 5d) → DAY_11 reminder shifts 5d earlier, still future.
    claim = _claim('RA2', created_at=base, submitted_at=base - timedelta(days=5))
    cu = ClientUpdate.objects.create(claim=claim, milestone='DAY_11',
                                     due_at=base + timedelta(days=11),
                                     state=ClientUpdate.STATE_SCHEDULED)
    reanchor_client_updates()
    cu.refresh_from_db()
    assert cu.state == ClientUpdate.STATE_SCHEDULED
    assert abs((cu.due_at - (base + timedelta(days=6))).total_seconds()) < 5   # 11d - 5d gap


@pytest.mark.django_db
def test_overdue_but_correctly_anchored_claim_is_left_alone():
    # THE regression guard: a recent live claim imported the same day it was paid (gap 0)
    # whose Day-2 is overdue only because autosend was off. Must NOT be cancelled.
    base = timezone.now()
    claim = _claim('RA3', created_at=base - timedelta(days=10), submitted_at=base - timedelta(days=10))
    cu = ClientUpdate.objects.create(claim=claim, milestone='DAY_2',
                                     due_at=base - timedelta(days=8),   # overdue
                                     state=ClientUpdate.STATE_SCHEDULED)
    reanchor_client_updates()
    cu.refresh_from_db()
    assert cu.state == ClientUpdate.STATE_SCHEDULED        # gap≈0 → untouched, still live


@pytest.mark.django_db
def test_same_day_future_reminder_unchanged():
    base = timezone.now()
    claim = _claim('RA4', created_at=base, submitted_at=base)
    due = base + timedelta(days=2)
    cu = ClientUpdate.objects.create(claim=claim, milestone='DAY_2', due_at=due,
                                     state=ClientUpdate.STATE_SCHEDULED)
    reanchor_client_updates()
    cu.refresh_from_db()
    assert cu.state == ClientUpdate.STATE_SCHEDULED
    assert abs((cu.due_at - due).total_seconds()) < 5     # due_at unchanged
