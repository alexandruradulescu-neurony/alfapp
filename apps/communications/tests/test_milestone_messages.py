"""Per-milestone macro-voice templates (milestone_message) — structural tests.

Written BEFORE the implementation (strict TDD / RED phase).  All tests must
fail (ImportError or assertion failures) until milestone_message is in place
in apps/communications/client_update_templates.py.

Also covers the wiring of milestone_message into prepare_follow_up in
apps/communications/client_updates.py (the wiring tests at the bottom).
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

import pytest
from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import ClientUpdate
from apps.communications import client_updates as cu
from apps.communications.client_update_templates import milestone_message


# ---------------------------------------------------------------------------
# Helpers — in-memory fake claim (no DB for pure template tests)
# ---------------------------------------------------------------------------

def _fake_claim(**kwargs):
    defaults = dict(
        client_name='Jane Smith',
        object_description='Blue Laptop\nExtra detail',
        alf_claim_id='ALF-TEST-001',
        phone='+1-800-555-0100',
        flight_data={'airline': 'Delta Air Lines', 'number': 'DL123'},
        email_alias='',
        zd_ticket_id='',
    )
    defaults.update(kwargs)

    class FakeClaim:
        pass

    obj = FakeClaim()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _full_ticket_data():
    """ticket_data with all custom fields populated — use for 'all present' tests."""
    from apps.integrations.services import (
        ZENDESK_FIELD_AIRPORT,
        ZENDESK_FIELD_AIRLINE,
        ZENDESK_FIELD_FLIGHT,
        ZENDESK_FIELD_DATETIME,
    )
    return {
        'custom_fields': [
            {'id': ZENDESK_FIELD_AIRPORT, 'value': 'JFK'},
            {'id': ZENDESK_FIELD_AIRLINE, 'value': 'American Airlines'},
            {'id': ZENDESK_FIELD_FLIGHT, 'value': 'AA456'},
            {'id': ZENDESK_FIELD_DATETIME, 'value': '2026-05-01'},
        ]
    }


def _empty_ticket_data():
    """ticket_data where every relevant field is absent — for graceful tests."""
    return {'custom_fields': []}


# ---------------------------------------------------------------------------
# 1. Basic content — DAY_2 renders the right substantive details
# ---------------------------------------------------------------------------

class TestDAY2Content:
    def test_contains_lost_item(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_2', _full_ticket_data(), 30)
        assert 'Blue Laptop' in msg

    def test_contains_airport(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_2', _full_ticket_data(), 30)
        assert 'JFK' in msg

    def test_contains_airline(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_2', _full_ticket_data(), 30)
        assert 'American Airlines' in msg

    def test_contains_flight(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_2', _full_ticket_data(), 30)
        assert 'AA456' in msg

    def test_greeting_with_first_name(self):
        claim = _fake_claim(client_name='Jane Smith')
        msg = milestone_message(claim, 'DAY_2', _full_ticket_data(), 30)
        assert msg.startswith('Dear Jane,')

    def test_sign_off_present(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_2', _full_ticket_data(), 30)
        assert 'The Airport Lost & Found team' in msg


# ---------------------------------------------------------------------------
# 2. No em-dash / en-dash in ANY produced message
# ---------------------------------------------------------------------------

MILESTONES_TO_CHECK = ['DAY_2', 'DAY_5', 'DAY_11', 'DAY_21', 'DAY_31', 'FINAL']


class TestNoEmDash:
    def _check_no_dashes(self, milestone):
        claim = _fake_claim()
        msg = milestone_message(claim, milestone, _full_ticket_data(), 30)
        assert '—' not in msg, f"Em-dash found in {milestone}: {msg!r}"
        assert '–' not in msg, f"En-dash found in {milestone}: {msg!r}"

    def test_day2_no_dashes(self):
        self._check_no_dashes('DAY_2')

    def test_day5_no_dashes(self):
        self._check_no_dashes('DAY_5')

    def test_day11_no_dashes(self):
        self._check_no_dashes('DAY_11')

    def test_day21_no_dashes(self):
        self._check_no_dashes('DAY_21')

    def test_day31_no_dashes(self):
        self._check_no_dashes('DAY_31')

    def test_final_no_dashes(self):
        self._check_no_dashes('FINAL')


# ---------------------------------------------------------------------------
# 3. Dynamic service period — period_days=45 appears, "30" does NOT as period
# ---------------------------------------------------------------------------

class TestDynamicPeriod:
    def test_day11_uses_period_days_45(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_11', _full_ticket_data(), 45)
        assert '45' in msg

    def test_day11_does_not_use_30_as_period_with_period_45(self):
        """The word "30" must not appear as the period reference when period_days=45."""
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_11', _full_ticket_data(), 45)
        # "30" could appear in e.g. flight numbers, but any "30 day" / "30-day"
        # phrase in the template is the period reference that must be replaced.
        assert '30-day' not in msg
        assert '30 day' not in msg
        assert '30th day' not in msg

    def test_final_uses_period_days_45(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'FINAL', _full_ticket_data(), 45)
        assert '45' in msg

    def test_final_does_not_use_30_as_period_with_period_45(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'FINAL', _full_ticket_data(), 45)
        assert '30-day' not in msg
        assert '30 day' not in msg
        assert '30th day' not in msg

    def test_day21_uses_elapsed_21_not_period(self):
        """DAY_21: elapsed_days == 21; period_days (30) and 21 are independent."""
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_21', _empty_ticket_data(), 30)
        assert '21' in msg


# ---------------------------------------------------------------------------
# 4. Graceful placeholders — empty fields produce no dangling text
# ---------------------------------------------------------------------------

class TestGracefulPlaceholders:
    def _empty_claim(self):
        return _fake_claim(
            client_name='',
            object_description='',
            alf_claim_id='',
            phone='',
            flight_data={},
        )

    def test_greeting_falls_back_to_there(self):
        msg = milestone_message(self._empty_claim(), 'DAY_2', _empty_ticket_data(), 30)
        assert 'Dear there,' in msg

    def test_no_double_space_artifacts(self):
        for ms in MILESTONES_TO_CHECK:
            msg = milestone_message(self._empty_claim(), ms, _empty_ticket_data(), 30)
            assert '  ' not in msg, f"Double-space in {ms}: {msg!r}"

    def test_no_dangling_to(self):
        """No "to  " or "to ." artifact when fields are empty."""
        for ms in MILESTONES_TO_CHECK:
            msg = milestone_message(self._empty_claim(), ms, _empty_ticket_data(), 30)
            assert 'to  ' not in msg, f"Dangling 'to  ' in {ms}: {msg!r}"

    def test_no_dangling_for(self):
        """No "for  " or "for ." artifact when lost_item is empty."""
        for ms in MILESTONES_TO_CHECK:
            msg = milestone_message(self._empty_claim(), ms, _empty_ticket_data(), 30)
            assert 'for  ' not in msg, f"Dangling 'for  ' in {ms}: {msg!r}"

    def test_still_has_greeting(self):
        for ms in MILESTONES_TO_CHECK:
            msg = milestone_message(self._empty_claim(), ms, _empty_ticket_data(), 30)
            assert msg.startswith('Dear '), f"No greeting in {ms}"

    def test_still_has_disclaimer(self):
        for ms in MILESTONES_TO_CHECK:
            msg = milestone_message(self._empty_claim(), ms, _empty_ticket_data(), 30)
            # Every message must have SOME disclaimer text
            has_standard = '72%' in msg
            has_final = 'service period' in msg.lower()
            assert has_standard or has_final, f"No disclaimer in {ms}"


# ---------------------------------------------------------------------------
# 5. Correct disclaimer per milestone
# ---------------------------------------------------------------------------

class TestDisclaimers:
    def test_non_final_has_72_percent_disclaimer(self):
        claim = _fake_claim()
        for ms in ['DAY_2', 'DAY_5', 'DAY_11', 'DAY_21', 'DAY_31']:
            msg = milestone_message(claim, ms, _full_ticket_data(), 30)
            assert '72%' in msg, f"Standard disclaimer missing in {ms}"

    def test_final_has_service_period_disclaimer(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'FINAL', _full_ticket_data(), 30)
        assert 'service period' in msg.lower()
        # FINAL uses a different disclaimer that does NOT contain the 72% stat
        assert '72%' not in msg

    def test_non_final_does_not_have_service_period_disclaimer(self):
        claim = _fake_claim()
        for ms in ['DAY_2', 'DAY_5', 'DAY_11', 'DAY_21']:
            msg = milestone_message(claim, ms, _full_ticket_data(), 30)
            # "service period" is a FINAL-only phrase in the disclaimer context
            # (it might appear in other copy, so we check that the FINAL disclaimer
            # text "we were unable to recover" is NOT in non-final messages)
            assert 'unable to recover' not in msg.lower(), \
                f"FINAL disclaimer text leaked into {ms}"


# ---------------------------------------------------------------------------
# 6. DAY_31 / tail milestone — still-searching template
# ---------------------------------------------------------------------------

class TestTailMilestone:
    def test_day31_renders_still_searching_template(self):
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_31', _full_ticket_data(), 45)
        assert 'still' in msg.lower()

    def test_day31_contains_elapsed_days(self):
        """DAY_31 elapsed = 31, should appear in the tail template."""
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_31', _full_ticket_data(), 45)
        assert '31' in msg

    def test_day41_also_uses_tail_template(self):
        """Any DAY_<n> beyond 21 uses the still-searching template."""
        claim = _fake_claim()
        msg = milestone_message(claim, 'DAY_41', _full_ticket_data(), 45)
        assert 'still' in msg.lower()
        assert '41' in msg


# ---------------------------------------------------------------------------
# 7. Wiring — prepare_follow_up uses milestone_message for non-final/no-news
# ---------------------------------------------------------------------------

class TestPrepareFollowUpWiring(TestCase):
    """prepare_follow_up for a NON-FINAL update with no safe replies must now
    produce milestone_message output (not the old _no_news_template text).
    Mirrors the existing PrepareSendSkipTests setup/mocking pattern."""

    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='wire@example.com',
            client_name='Lee',
            object_description='iPad',
            zd_ticket_id='98001',
        )
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')

    @patch('apps.integrations.services.fetch_zendesk_ticket')
    def test_prepare_uses_milestone_message_when_no_safe_replies(self, mock_fetch):
        """With no safe replies and ticket_data mocked, prepare_follow_up
        should produce a draft containing milestone_message content."""
        mock_fetch.return_value = {
            'custom_fields': []  # no data, but fetch succeeded
        }
        cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'DRAFTED')
        body = self.update.draft_body
        # milestone_message for DAY_2 must be used — it differs from old template
        # by NOT containing "still actively following up" (old text)
        self.assertNotIn('still actively following up', body)
        # and should contain the greeting
        self.assertIn('Dear Lee,', body)


class TestPrepareFollowUpFinalWiring(TestCase):
    """prepare_follow_up for FINAL must produce milestone_message('FINAL') output."""

    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='wire2@example.com',
            client_name='Lee',
            object_description='iPad',
            zd_ticket_id='98002',
        )
        self.update = ClientUpdate.objects.create(
            claim=self.claim,
            milestone='FINAL',
            state='SCHEDULED',
            due_at=timezone.now() - timedelta(hours=1),
        )

    @patch('apps.integrations.services.fetch_zendesk_ticket')
    def test_prepare_final_uses_milestone_message_closer(self, mock_fetch):
        mock_fetch.return_value = {'custom_fields': []}
        cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'DRAFTED')
        body = self.update.draft_body
        # milestone_message FINAL uses the closer copy
        # The old _final_template said "trusting us" — the new one says "service period"
        self.assertIn('service period', body.lower())
