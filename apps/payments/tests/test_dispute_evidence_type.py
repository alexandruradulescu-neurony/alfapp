"""Red-phase spec — pick a PayPal-ACCEPTED evidence_type per dispute.

PayPal restricts which `evidence_type` labels it will accept for a given dispute
and LISTS the accepted ones in the stored payload: each
``raw_webhook_payload['evidences']`` entry with ``source == 'REQUESTED_FROM_SELLER'``
carries an ``evidence_type``, and that set is what PayPal will take. Today LORA
hardcodes ``PROOF_OF_FULFILLMENT`` for every dispute (evidence_type_for_reason),
which PayPal rejects for "not as described" (SNAD) disputes with
``EVIDENCE_TYPE_IS_NOT_ALLOWED``.

This file pins the NEW per-dispute evidence-type selection (deliberately
unimplemented — these tests must FAIL until it lands):

1. ``pds.allowed_evidence_types(dispute) -> set`` — the REQUESTED_FROM_SELLER
   types PayPal will accept (upper-cased; blanks + non-seller sources excluded).
2. ``pds.preferred_evidence_type(dispute) -> str`` — a reason-aware pick from the
   allowed set (never auto-returning PROOF_OF_REFUND); PayPal's list wins over the
   reason bias; empty list falls back to the reason preference's first entry.
3. ``submit_dispute_response`` auto-corrects a wrong stored evidence_type to an
   accepted one at send time (the safety net), while honouring a manager's
   already-valid explicit choice.
4. ``pds.fix_dispute_evidence_types(*, dry_run=False) -> dict`` — a backfill that
   corrects stored DRAFT submissions on OPEN disputes to the preferred type.
5. A thin ``fix_dispute_evidence_types`` management command with ``--dry-run``.
6. The dispute detail page surfaces the preferred default + the allowed list.

The new service functions are reached as module attributes
(``pds.allowed_evidence_types`` / ``pds.preferred_evidence_type`` /
``pds.fix_dispute_evidence_types``) so a missing one is a clean AttributeError;
the management command via call_command (missing → CommandError); the detail-page
context keys / rendered hint via the login'd Client.

Nothing here talks to PayPal: the multipart transport
(``_post_dispute_action_multipart``) and the post-submit re-sync
(``sync_dispute_from_paypal``) are patched.
"""

import itertools
from contextlib import contextmanager
from datetime import datetime, timezone as dt_tz
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute, DisputeSubmission

User = get_user_model()

# Globally-unique paypal_dispute_id per created dispute so the service's
# get-by-id lookups never collide across tests.
_ids = itertools.count(1)


def _dispute(payload=None, **kw):
    """A normal, non-terminal, non-manual dispute (SNAD by default) with a
    guaranteed-unique paypal_dispute_id unless one is passed explicitly."""
    base = dict(
        paypal_dispute_id=f'PP-D-ET{next(_ids)}',
        buyer_email='b@example.com',
        transaction_id='TX',
        transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
        dispute_reason='MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
        status=Dispute.STATUS_MATCHED,
        raw_webhook_payload=payload if payload is not None else {},
    )
    base.update(kw)
    return Dispute.objects.create(**base)


def _requested_payload(*evidence_types, state='REQUIRED_ACTION'):
    """A stored payload where PayPal is REQUESTING seller evidence and lists the
    accepted types — one REQUESTED_FROM_SELLER entry per type, same timestamp
    (exactly how PayPal records a single request offering several options)."""
    return {
        'dispute_state': state,
        'evidences': [
            {'source': 'REQUESTED_FROM_SELLER', 'evidence_type': t,
             'date': '2026-07-29T00:00:00.000Z'}
            for t in evidence_types
        ],
    }


def _reason_dispute(reason, *allowed, **kw):
    """A CHARGEBACK / REQUIRED_ACTION dispute (so submit_endpoint ==
    'provide-evidence') whose PayPal-accepted evidence set is exactly `allowed`."""
    return _dispute(dispute_reason=reason,
                    dispute_life_cycle_stage='CHARGEBACK',
                    payload=_requested_payload(*allowed), **kw)


def _snad_dispute(*allowed, **kw):
    """A SNAD ("not as described") dispute with the given PayPal-accepted set."""
    return _reason_dispute('MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED', *allowed, **kw)


def _draft(dispute, evidence_type):
    """A DRAFT submission carrying a stored evidence_type, no attachments (so
    _build_submission_files stays empty — no terms/invoice/report I/O)."""
    return DisputeSubmission.objects.create(
        dispute=dispute, notes='our case', evidence_type=evidence_type,
        status=DisputeSubmission.STATUS_DRAFT,
        attach_evidence_pdf=False, attach_terms=False, attach_invoice=False)


@contextmanager
def _capture_evidence_submit():
    """Patch the multipart transport + post-submit re-sync, capturing the
    provide-evidence ``input_json`` that submit_dispute_response builds. For
    provide-evidence that JSON is
    ``{"evidences": [{"evidence_type": ..., "notes": ..., "document_ids": [...]}]}``."""
    captured = {}

    def fake_post(dispute_id, action, input_json, files):
        captured['dispute_id'] = dispute_id
        captured['action'] = action
        captured['input_json'] = input_json
        captured['files'] = list(files or [])
        return True, {'ok': True}

    with patch.object(pds, '_post_dispute_action_multipart', side_effect=fake_post), \
         patch.object(pds, 'sync_dispute_from_paypal'):
        yield captured


def _sent_evidence_type(captured):
    return captured['input_json']['evidences'][0]['evidence_type']


class AllowedEvidenceTypesTests(TestCase):
    """pds.allowed_evidence_types(dispute) -> set of uppercase strings.

    Function does NOT exist yet → AttributeError until it lands."""

    def test_requested_from_seller_types_uppercased(self):
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        self.assertEqual(pds.allowed_evidence_types(d), {'OTHER', 'PROOF_OF_REFUND'})

    def test_only_buyer_evidences_is_empty(self):
        # Evidences exist, but none are REQUESTED_FROM_SELLER → nothing PayPal is
        # asking us to pick from.
        d = _dispute(payload={
            'dispute_state': 'REQUIRED_ACTION',
            'evidences': [{'source': 'SUBMITTED_BY_BUYER',
                           'evidence_type': 'PROOF_OF_FULFILLMENT'}],
        })
        self.assertEqual(pds.allowed_evidence_types(d), set())

    def test_no_evidences_key_is_empty(self):
        d = _dispute(payload={})
        self.assertEqual(pds.allowed_evidence_types(d), set())

    def test_source_match_is_case_insensitive(self):
        d = _dispute(payload={
            'dispute_state': 'REQUIRED_ACTION',
            'evidences': [{'source': 'requested_from_seller',
                           'evidence_type': 'other'}],
        })
        self.assertEqual(pds.allowed_evidence_types(d), {'OTHER'})

    def test_blank_evidence_types_excluded(self):
        d = _dispute(payload={
            'dispute_state': 'REQUIRED_ACTION',
            'evidences': [
                {'source': 'REQUESTED_FROM_SELLER', 'evidence_type': ''},
                {'source': 'REQUESTED_FROM_SELLER', 'evidence_type': 'OTHER'},
            ],
        })
        self.assertEqual(pds.allowed_evidence_types(d), {'OTHER'})


class PreferredEvidenceTypeTests(TestCase):
    """pds.preferred_evidence_type(dispute) -> str (uppercase).

    Function does NOT exist yet → AttributeError until it lands."""

    def test_snad_prefers_other_over_refund(self):
        # SNAD order is [OTHER, PROOF_OF_FULFILLMENT]; OTHER is accepted → OTHER.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        self.assertEqual(pds.preferred_evidence_type(d), 'OTHER')

    def test_never_auto_returns_proof_of_refund(self):
        # PROOF_OF_REFUND is accepted for this dispute but must never be the
        # AUTO pick (it asserts we refunded, which we did not).
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        self.assertNotEqual(pds.preferred_evidence_type(d), 'PROOF_OF_REFUND')

    def test_inr_with_only_fulfillment_returns_fulfillment(self):
        d = _reason_dispute('MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'PROOF_OF_FULFILLMENT')
        self.assertEqual(pds.preferred_evidence_type(d), 'PROOF_OF_FULFILLMENT')

    def test_inr_list_overrides_reason_bias(self):
        # INR biases toward PROOF_OF_FULFILLMENT, but PayPal only accepts OTHER
        # here — the per-dispute list wins over the reason bias.
        d = _reason_dispute('MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'OTHER')
        self.assertEqual(pds.preferred_evidence_type(d), 'OTHER')

    def test_unauthorised_prefers_fulfillment(self):
        d = _reason_dispute('UNAUTHORISED', 'OTHER', 'PROOF_OF_FULFILLMENT', 'PROOF_OF_REFUND')
        self.assertEqual(pds.preferred_evidence_type(d), 'PROOF_OF_FULFILLMENT')

    def test_falls_back_to_allowed_non_refund_type(self):
        # None of our preferences are accepted, but a non-refund type is → use it.
        d = _snad_dispute('PROOF_OF_SOURCE')
        result = pds.preferred_evidence_type(d)
        self.assertEqual(result, 'PROOF_OF_SOURCE')
        self.assertNotEqual(result, 'PROOF_OF_REFUND')

    def test_empty_allowed_snad_falls_back_to_other(self):
        # PayPal lists nothing (UNDER_PAYPAL_REVIEW, no REQUESTED_FROM_SELLER) →
        # fall back to the reason preference's FIRST entry. SNAD → OTHER.
        d = _dispute(dispute_reason='MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
                     payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        self.assertEqual(pds.preferred_evidence_type(d), 'OTHER')

    def test_empty_allowed_inr_falls_back_to_fulfillment(self):
        d = _dispute(dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
                     payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        self.assertEqual(pds.preferred_evidence_type(d), 'PROOF_OF_FULFILLMENT')

    def test_empty_allowed_other_reason_falls_back_to_other(self):
        d = _dispute(dispute_reason='CREDIT_NOT_PROCESSED',
                     payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        self.assertEqual(pds.preferred_evidence_type(d), 'OTHER')


class SubmitAutoCorrectsEvidenceTypeTests(TestCase):
    """submit_dispute_response must send an ACCEPTED evidence_type (the safety
    net): auto-correct a wrong stored value, keep an already-valid one, honour a
    manager's explicit valid choice, and fill a blank with the preferred type.

    NOTE: two of these are guards that PASS TODAY because LORA currently sends the
    stored value verbatim — see the per-test comments."""

    def test_snad_wrong_hardcoded_value_autocorrected_to_allowed(self):
        # The wrong hardcoded PROOF_OF_FULFILLMENT on a SNAD dispute that only
        # accepts {OTHER, PROOF_OF_REFUND} must be corrected to OTHER before send.
        # FAILS TODAY: LORA sends the raw stored PROOF_OF_FULFILLMENT.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        sub = _draft(d, 'PROOF_OF_FULFILLMENT')
        with _capture_evidence_submit() as captured:
            result = pds.submit_dispute_response(sub)
        self.assertEqual(captured['action'], 'provide-evidence')
        self.assertEqual(_sent_evidence_type(captured), 'OTHER')
        self.assertTrue(result)
        sub.refresh_from_db()
        self.assertEqual(sub.status, DisputeSubmission.STATUS_SUBMITTED)

    def test_inr_already_valid_type_unchanged(self):
        # PROOF_OF_FULFILLMENT is accepted here → sent unchanged.
        # PASSES TODAY (stored value is already valid and sent verbatim).
        d = _reason_dispute('MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'PROOF_OF_FULFILLMENT')
        sub = _draft(d, 'PROOF_OF_FULFILLMENT')
        with _capture_evidence_submit() as captured:
            pds.submit_dispute_response(sub)
        self.assertEqual(_sent_evidence_type(captured), 'PROOF_OF_FULFILLMENT')

    def test_manager_override_valid_choice_honored(self):
        # A manager deliberately chose PROOF_OF_REFUND, which IS accepted for this
        # dispute → the auto-correct must not clobber it.
        # PASSES TODAY (no auto-correct exists; the stored value is sent verbatim).
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        sub = _draft(d, 'PROOF_OF_REFUND')
        with _capture_evidence_submit() as captured:
            pds.submit_dispute_response(sub)
        self.assertEqual(_sent_evidence_type(captured), 'PROOF_OF_REFUND')

    def test_blank_stored_type_uses_preferred(self):
        # Blank stored type → send the preferred type (OTHER for this SNAD).
        # FAILS TODAY: blank falls to evidence_type_for_reason → PROOF_OF_FULFILLMENT.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        sub = _draft(d, '')
        with _capture_evidence_submit() as captured:
            pds.submit_dispute_response(sub)
        self.assertEqual(_sent_evidence_type(captured), 'OTHER')  # == preferred_evidence_type(d)


class FixDisputeEvidenceTypesBackfillTests(TestCase):
    """pds.fix_dispute_evidence_types(*, dry_run=False) -> dict.

    Function does NOT exist yet → AttributeError until it lands."""

    def test_returns_checked_and_updated_ints(self):
        _draft(_snad_dispute('OTHER', 'PROOF_OF_REFUND'), 'PROOF_OF_FULFILLMENT')
        summary = pds.fix_dispute_evidence_types()
        self.assertIn('checked', summary)
        self.assertIn('updated', summary)
        self.assertIsInstance(summary['checked'], int)
        self.assertIsInstance(summary['updated'], int)

    def test_wrong_hardcoded_draft_is_corrected(self):
        sub = _draft(_snad_dispute('OTHER', 'PROOF_OF_REFUND'), 'PROOF_OF_FULFILLMENT')
        summary = pds.fix_dispute_evidence_types()
        sub.refresh_from_db()
        self.assertEqual(sub.evidence_type, 'OTHER')
        self.assertGreaterEqual(summary['updated'], 1)

    def test_draft_already_preferred_not_counted(self):
        sub = _draft(_snad_dispute('OTHER', 'PROOF_OF_REFUND'), 'OTHER')
        summary = pds.fix_dispute_evidence_types()
        sub.refresh_from_db()
        self.assertEqual(sub.evidence_type, 'OTHER')
        self.assertEqual(summary['updated'], 0)

    def test_draft_on_resolved_dispute_untouched(self):
        # A resolved/terminal dispute is not open — leave its draft alone.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND', status=Dispute.STATUS_RESOLVED_WON)
        sub = _draft(d, 'PROOF_OF_FULFILLMENT')
        summary = pds.fix_dispute_evidence_types()
        sub.refresh_from_db()
        self.assertEqual(sub.evidence_type, 'PROOF_OF_FULFILLMENT')
        self.assertEqual(summary['updated'], 0)

    def test_dry_run_counts_but_persists_nothing(self):
        sub = _draft(_snad_dispute('OTHER', 'PROOF_OF_REFUND'), 'PROOF_OF_FULFILLMENT')
        summary = pds.fix_dispute_evidence_types(dry_run=True)
        sub.refresh_from_db()
        self.assertEqual(sub.evidence_type, 'PROOF_OF_FULFILLMENT')  # nothing persisted
        self.assertGreaterEqual(summary['updated'], 1)              # counts what WOULD change

    def test_submitted_and_failed_not_modified(self):
        # Only DRAFT submissions are corrected; sent/failed history is immutable.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        submitted = DisputeSubmission.objects.create(
            dispute=d, notes='n', evidence_type='PROOF_OF_FULFILLMENT',
            status=DisputeSubmission.STATUS_SUBMITTED)
        failed = DisputeSubmission.objects.create(
            dispute=d, notes='n', evidence_type='PROOF_OF_FULFILLMENT',
            status=DisputeSubmission.STATUS_FAILED)
        summary = pds.fix_dispute_evidence_types()
        submitted.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual(submitted.evidence_type, 'PROOF_OF_FULFILLMENT')
        self.assertEqual(failed.evidence_type, 'PROOF_OF_FULFILLMENT')
        self.assertEqual(summary['updated'], 0)


class FixEvidenceTypesCommandTests(TestCase):
    """The thin `fix_dispute_evidence_types` management command honours --dry-run.

    Command does NOT exist yet → CommandError until it lands."""

    def test_command_dry_run_reports_and_persists_nothing(self):
        sub = _draft(_snad_dispute('OTHER', 'PROOF_OF_REFUND'), 'PROOF_OF_FULFILLMENT')
        out = StringIO()
        call_command('fix_dispute_evidence_types', '--dry-run', stdout=out)
        sub.refresh_from_db()
        self.assertEqual(sub.evidence_type, 'PROOF_OF_FULFILLMENT')  # dry-run persists nothing
        val = out.getvalue()
        self.assertTrue(
            'updated' in val.lower() or any(c.isdigit() for c in val),
            f"the command must surface how many drafts would change; got {val!r}")


class DisputeDetailEvidenceTypeTests(TestCase):
    """The detail page surfaces the PayPal-accepted evidence types + the preferred
    default (not the hardcoded PROOF_OF_FULFILLMENT)."""

    def setUp(self):
        self.user = User.objects.create_user(username='et_detail_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)

    def _get(self, dispute):
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_context_default_is_preferred_not_hardcoded(self):
        # SNAD accepting {OTHER, PROOF_OF_REFUND} → preferred is OTHER.
        # FAILS TODAY: evidence_type_default == evidence_type_for_reason(SNAD)
        # == 'PROOF_OF_FULFILLMENT'.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        resp = self._get(d)
        self.assertEqual(resp.context['evidence_type_default'], 'OTHER')

    def test_context_exposes_allowed_types(self):
        # FAILS TODAY: the context has no 'evidence_type_allowed' key.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        resp = self._get(d)
        self.assertIn('evidence_type_allowed', resp.context)
        allowed = list(resp.context['evidence_type_allowed'])
        self.assertIn('OTHER', allowed)
        self.assertIn('PROOF_OF_REFUND', allowed)

    def test_html_hints_allowed_types(self):
        # The allowed-types hint must render the accepted enums for the manager.
        # The raw-payload debug block already echoes the payload's single
        # PROOF_OF_REFUND once, so a genuine hint makes the enum appear at least
        # TWICE. (A bare `in` check would false-green off that raw echo alone.)
        # FAILS TODAY: only the raw echo exists → count == 1.
        d = _snad_dispute('OTHER', 'PROOF_OF_REFUND')
        html = self._get(d).content.decode()
        self.assertGreaterEqual(
            html.count('PROOF_OF_REFUND'), 2,
            "the allowed-types hint (PROOF_OF_REFUND) must render outside the raw "
            "PayPal debug block")
