"""Red-phase spec — dispute-reply editing safety, round 3 (deliberately
unimplemented; these tests must fail until the behaviour lands).

Second-review findings driving this round:

  * in-flight guard scope — dispute_prepare_submission's double-submit guard
    (added for round 2's item 5, DoubleSubmitSafetyTests) only ever checks
    `action == 'send'`. Posting action=save or action=generate while a
    submission is mid-flight (SUBMITTING) is not guarded at all today:
    _working_draft only ever looks at DRAFT rows, so with a submission
    already claimed it just falls through to "no draft" and quietly creates
    a brand new, divergent DisputeSubmission — the same silent-duplicate
    failure mode round 2 fixed for action=send, still open here.

  * stuck SUBMITTING rows — the existing guard also has no time bound: once
    a row is claimed (DRAFT -> SUBMITTING) it blocks the dispute forever if
    nothing ever resolves it (a worker crash, a dead process, an infra
    hiccup between the claim and the PayPal call). A SUBMITTING row must
    stop counting as "in flight" once it is stale, or a single stuck row
    would permanently lock the composer. This round treats anything older
    than 10 minutes as stale.

1. A RECENT SUBMITTING row (updated_at = now) must refuse both action=save
   and action=generate on the composer: an ERROR-level flash message, no new
   DisputeSubmission row created, build_dispute_narrative_notes never called
   for generate, and a redirect back to the detail page. NOT implemented
   yet — today only action=send is guarded, so save/generate happily create
   a second, divergent submission.
2. A STALE SUBMITTING row (updated_at 30 minutes in the past, older than the
   10-minute cutoff) must NOT lock the dispute: action=save creates/updates
   a DRAFT normally, and action=send proceeds and calls
   submit_dispute_response exactly once. NOT implemented yet — today's send
   guard has no time bound, so it wrongly blocks the stale row too.
3. (Regression pin, expected to already pass) A RECENT SUBMITTING row
   (updated 1 minute ago) still blocks action=send — round 2's
   DoubleSubmitSafetyTests behaviour, unchanged by this round.
4. (Expected to already pass; pin) Vision (_narrate_image_evidence) is
   called exactly once when generating a report for an image-only case.
5. (Expected to already pass; pin) After a failed draft restore (an
   exception cloning a rejected draft's images onto the fresh DRAFT, as in
   round 2's RejectedSendCloneFailureTests), no DRAFT row survives — the
   atomic clone rolls back completely instead of leaving a half-cloned one.
"""

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.payments import document_service as ds
from apps.payments import frontend_views as fv
from apps.payments.models import Dispute, DisputeSubmission, DisputeSubmissionImage

User = get_user_model()

# A SUBMITTING row older than this must no longer count as "in flight" (see
# the module docstring's "stuck SUBMITTING rows" finding).
STALE_AFTER_MINUTES = 10


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-EDITSAFE3', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED',
                raw_webhook_payload=payload or {})
    base.update(kw)
    return Dispute.objects.create(**base)


def _evidence_open_dispute(**kw):
    """A dispute PayPal accepts a first-evidence submission for
    (dispute.submit_endpoint == 'provide-evidence')."""
    return _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                    dispute_life_cycle_stage='CHARGEBACK', **kw)


def _age_submission(submission, minutes):
    """Back-date a SUBMITTING row's updated_at by `minutes`. Must go through
    .update() on the queryset, not .save() — auto_now overwrites updated_at
    to "now" on every save() regardless of what's assigned in memory."""
    DisputeSubmission.objects.filter(pk=submission.pk).update(
        updated_at=timezone.now() - timedelta(minutes=minutes))


def _fake_paypal_rejection(submission, performed_by=None):
    """Mirrors what the real submit_dispute_response does on a clean PayPal
    rejection: mark the submission FAILED, persist it, report failure."""
    submission.status = DisputeSubmission.STATUS_FAILED
    submission.save(update_fields=['status', 'updated_at'])
    return False


def _fake_paypal_success(submission, performed_by=None):
    """Mirrors what the real submit_dispute_response does on a clean PayPal
    acceptance: mark the submission SUBMITTED, persist it, report success."""
    submission.status = DisputeSubmission.STATUS_SUBMITTED
    submission.save(update_fields=['status', 'updated_at'])
    return True


class _LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='editsafe3_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)


# ---------------------------------------------------------------------------
# 1. The in-flight guard must also cover action=save and action=generate
# ---------------------------------------------------------------------------

class InFlightGuardScopeTests(_LoggedInTestCase):
    """Item 1 — a RECENT SUBMITTING row must refuse action=save and
    action=generate too, not just action=send. Today _working_draft only
    ever looks at DRAFT rows, so with a submission mid-flight both actions
    fall through to "no draft" and quietly create a brand new, divergent
    DisputeSubmission — exactly the silent-duplicate failure mode round 2
    fixed for action=send, still open for the other two actions."""

    def test_save_refused_while_recent_submission_in_flight(self):
        dispute = _evidence_open_dispute()
        inflight = DisputeSubmission.objects.create(
            dispute=dispute, notes='Already going out.', source=DisputeSubmission.SOURCE_MANUAL,
            status=DisputeSubmission.STATUS_SUBMITTING)

        resp = self.web.post(
            reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
            {'action': 'save', 'notes': 'Trying to edit while it is sending.'})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp.url, reverse('disputes:dispute_detail', args=[dispute.id]),
            "must redirect back to the detail page")
        self.assertEqual(
            dispute.submissions.count(), 1,
            "no new DisputeSubmission row may be created while a recent submission is in flight")
        self.assertEqual(dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).count(), 0)
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, DisputeSubmission.STATUS_SUBMITTING,
                         "the in-flight submission must be left completely alone")

        resp2 = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        msgs = list(resp2.context['messages'])
        self.assertTrue(
            any(m.level_tag == 'error' for m in msgs),
            f"expected an ERROR-level flash message; got: {[(m.level_tag, str(m)) for m in msgs]}")

    def test_generate_refused_while_recent_submission_in_flight(self):
        dispute = _evidence_open_dispute()
        inflight = DisputeSubmission.objects.create(
            dispute=dispute, notes='Already going out.', source=DisputeSubmission.SOURCE_MANUAL,
            status=DisputeSubmission.STATUS_SUBMITTING)

        with patch.object(fv, 'build_dispute_narrative_notes',
                          return_value={'notes': 'AI drafted narrative.', 'source': 'AI',
                                        'sections': {}}) as gen:
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                {'action': 'generate', 'manager_note': 'Stress the IP match.'})
            gen.assert_not_called()

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp.url, reverse('disputes:dispute_detail', args=[dispute.id]),
            "must redirect back to the detail page")
        self.assertEqual(
            dispute.submissions.count(), 1,
            "no new DisputeSubmission row may be created while a recent submission is in flight")
        self.assertEqual(dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).count(), 0)
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, DisputeSubmission.STATUS_SUBMITTING,
                         "the in-flight submission must be left completely alone")

        resp2 = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        msgs = list(resp2.context['messages'])
        self.assertTrue(
            any(m.level_tag == 'error' for m in msgs),
            f"expected an ERROR-level flash message; got: {[(m.level_tag, str(m)) for m in msgs]}")


# ---------------------------------------------------------------------------
# 2. A stale SUBMITTING row must not lock the dispute
# ---------------------------------------------------------------------------

class StaleInFlightRowDoesNotLockTests(_LoggedInTestCase):
    """Item 2 — a SUBMITTING row stuck for longer than the staleness cutoff
    (10 minutes) must stop counting as "in flight": save must behave
    normally, and send must go ahead and actually call
    submit_dispute_response instead of bouncing off a guard that could never
    be satisfied again (nothing ever moves a truly stuck row out of
    SUBMITTING on its own)."""

    def test_stale_submitting_does_not_block_save(self):
        dispute = _evidence_open_dispute()
        stale = DisputeSubmission.objects.create(
            dispute=dispute, notes='Stuck mid-send.', source=DisputeSubmission.SOURCE_MANUAL,
            status=DisputeSubmission.STATUS_SUBMITTING)
        _age_submission(stale, minutes=30)

        resp = self.web.post(
            reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
            {'action': 'save', 'notes': 'Editing normally despite the stale row.'})

        self.assertEqual(resp.status_code, 302)
        draft_qs = dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT)
        self.assertEqual(
            draft_qs.count(), 1,
            "a stale (>10 min) SUBMITTING row must not block a normal save")
        self.assertEqual(draft_qs.get().notes, 'Editing normally despite the stale row.')
        stale.refresh_from_db()
        self.assertEqual(stale.status, DisputeSubmission.STATUS_SUBMITTING,
                         "the stale row itself is left alone, just no longer treated as blocking")

    def test_stale_submitting_does_not_block_send(self):
        dispute = _evidence_open_dispute()
        stale = DisputeSubmission.objects.create(
            dispute=dispute, notes='Stuck mid-send.', source=DisputeSubmission.SOURCE_MANUAL,
            status=DisputeSubmission.STATUS_SUBMITTING)
        _age_submission(stale, minutes=30)

        with patch.object(fv, 'submit_dispute_response', side_effect=_fake_paypal_success) as submit:
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                {'action': 'send', 'notes': 'Sending despite the stale row.'})
            submit.assert_called_once()

        self.assertEqual(resp.status_code, 302)
        submitted_qs = dispute.submissions.filter(status=DisputeSubmission.STATUS_SUBMITTED)
        self.assertEqual(
            submitted_qs.count(), 1,
            "a stale (>10 min) SUBMITTING row must not block a send from going through")
        self.assertEqual(submitted_qs.get().notes, 'Sending despite the stale row.')


# ---------------------------------------------------------------------------
# 3. Regression pin — a recent SUBMITTING row still blocks send
# ---------------------------------------------------------------------------

class RecentSubmittingStillBlocksSendTests(_LoggedInTestCase):
    """Item 3 (regression pin, expected to already pass) — a SUBMITTING row
    updated a minute ago is well inside the staleness cutoff and must still
    block action=send exactly like round 2's DoubleSubmitSafetyTests."""

    def test_recent_one_minute_old_submitting_still_blocks_send(self):
        dispute = _evidence_open_dispute()
        inflight = DisputeSubmission.objects.create(
            dispute=dispute, notes='Already going out.', source=DisputeSubmission.SOURCE_MANUAL,
            status=DisputeSubmission.STATUS_SUBMITTING)
        _age_submission(inflight, minutes=1)

        with patch.object(fv, 'submit_dispute_response') as submit:
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                {'action': 'send', 'notes': 'Trying to send again.'})
            submit.assert_not_called()

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).count(), 0,
            "no new DRAFT may be created while a recent submission is in flight")
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, DisputeSubmission.STATUS_SUBMITTING,
                         "the in-flight submission must be left completely alone")

        resp2 = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        msgs = [str(m) for m in resp2.context['messages']]
        self.assertTrue(
            any('already being sent' in m for m in msgs),
            f"expected the existing 'already being sent' error message; got: {msgs}")


# ---------------------------------------------------------------------------
# 4. Vision is called exactly once for an image-only case
# ---------------------------------------------------------------------------

class ImageOnlyVisionCallCountTests(TestCase):
    """Item 4 (expected to already pass; pin) — extends round 2's
    ImageOnlyProvenanceTests setup: when the case record is made entirely of
    image-only internal notes, _narrate_image_evidence (vision) must be
    called exactly once, never skipped and never re-invoked per image."""

    IMAGE_ONLY_COMMENTS = [
        {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': False,
         'created_at': '2026-02-03T21:14:00Z', 'body': 'FRONTIER',
         'attachments': [{'content_type': 'image/png', 'content_url': 'https://zd/f.png',
                          'file_name': 'f.png'}]},
    ]

    def _dispute_with_claim(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     alf_claim_id='ALF1R3', zd_ticket_id='97003')
        return Dispute.objects.create(
            paypal_dispute_id='PP-D-IMGCOUNT3', buyer_email='b@example.com', transaction_id='TX',
            transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc), dispute_reason='UNAUTHORISED',
            claim=claim, zd_ticket_id='97003')

    def test_vision_called_exactly_once_for_image_only_case(self):
        d = self._dispute_with_claim()
        placements = {1: {'section': 'SUBMISSIONS', 'explanation': 'We reported the loss to Frontier.'}}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': self.IMAGE_ONLY_COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'), \
             patch.object(ds, '_render_to_pdf', return_value=b'%PDF-1.4 fake'), \
             patch.object(ds, '_narrate_evidence') as narrate_evidence, \
             patch.object(ds, '_narrate_image_evidence', return_value=placements) as narrate_image:
            doc = ds.generate_evidence_report(d.id)
        narrate_evidence.assert_not_called()
        self.assertIsNotNone(doc, "report generation must succeed")
        self.assertEqual(
            narrate_image.call_count, 1,
            f"vision must be called exactly once for an image-only case; got {narrate_image.call_count} calls")


# ---------------------------------------------------------------------------
# 5. No DRAFT rows survive a failed clone of a rejected draft
# ---------------------------------------------------------------------------

class FailedDraftRestoreLeavesNoDraftRowTests(_LoggedInTestCase):
    """Item 5 (expected to already pass; pin) — narrower, explicit pin of
    round 2's RejectedSendCloneFailureTests: when cloning a rejected draft's
    images blows up, the atomic block must roll back completely, so no
    DRAFT row (partial or otherwise) survives the failure."""

    def test_no_draft_rows_survive_a_clone_failure(self):
        dispute = _evidence_open_dispute()
        draft = DisputeSubmission.objects.create(
            dispute=dispute, notes='Our full narrative for PayPal.',
            source=DisputeSubmission.SOURCE_MANUAL, status=DisputeSubmission.STATUS_DRAFT)
        DisputeSubmissionImage.objects.create(submission=draft, file='shot.png', uploaded_by=self.user)

        with patch.object(fv, 'submit_dispute_response', side_effect=_fake_paypal_rejection), \
             patch.object(DisputeSubmissionImage.objects, 'create', side_effect=Exception('boom')):
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                {'action': 'send', 'notes': 'Our full narrative for PayPal.'})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).count(), 0,
            "a failed clone must leave no half-cloned DRAFT behind (atomic rollback)")
