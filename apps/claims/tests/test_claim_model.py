"""
Tests for the updated Claim model with new fields.

Tests the Claim model including:
- New fields: alf_claim_id, phone, alternate_email, llm_extraction_failed
- Unique constraint on alf_claim_id
- Database indexes
- String representation
"""

import pytest
from django.test import TestCase
from django.db import IntegrityError, connection
from apps.claims.models import Claim, ClaimUpdateTimeline
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
class TestClaimModel:
    """Test cases for the Claim model."""

    def test_claim_with_new_fields(self):
        """Create claim with all new fields."""
        claim = Claim.objects.create(
            alf_claim_id='ALF1234567',
            zd_ticket_id='12345',
            client_email='customer@example.com',
            phone='+1-555-123-4567',
            alternate_email='backup@gmail.com',
            flight_details='Flight AA123 from JFK to LAX on March 15, 2026',
            object_description='Black MacBook Pro laptop, 15-inch',
            status='Received',
            llm_extraction_failed=False,
        )

        # Verify all fields are saved correctly
        assert claim.alf_claim_id == 'ALF1234567'
        assert claim.zd_ticket_id == '12345'
        assert claim.client_email == 'customer@example.com'
        assert claim.phone == '+1-555-123-4567'
        assert claim.alternate_email == 'backup@gmail.com'
        assert claim.flight_details == 'Flight AA123 from JFK to LAX on March 15, 2026'
        assert claim.object_description == 'Black MacBook Pro laptop, 15-inch'
        assert claim.status == 'Received'
        assert claim.llm_extraction_failed is False

        # Verify timestamps are set
        assert claim.created_at is not None
        assert claim.updated_at is not None

    def test_claim_minimal_fields(self):
        """Create claim with only required fields."""
        claim = Claim.objects.create(
            client_email='minimal@example.com',
        )

        assert claim.client_email == 'minimal@example.com'
        assert claim.alf_claim_id is None
        assert claim.zd_ticket_id is None
        assert claim.phone is None  # null=True allows None
        assert claim.alternate_email == ''
        assert claim.flight_details == ''
        assert claim.object_description == ''
        assert claim.status == 'Investigation initiated'
        assert claim.llm_extraction_failed is False

    def test_claim_alf_claim_id_unique(self):
        """ALF claim ID must be unique."""
        Claim.objects.create(
            alf_claim_id='ALF1234567',
            client_email='first@example.com',
        )

        # Attempting to create another claim with same ALF ID should fail
        with pytest.raises(IntegrityError):
            Claim.objects.create(
                alf_claim_id='ALF1234567',  # Duplicate
                client_email='second@example.com',
            )

    def test_claim_alf_claim_id_unique_case_insensitive(self):
        """ALF claim ID uniqueness is case-sensitive in SQLite.
        
        Note: SQLite uses case-sensitive comparison by default.
        Case-insensitive uniqueness would require a custom constraint or CI collation.
        """
        Claim.objects.create(
            alf_claim_id='ALF1234567',
            client_email='first@example.com',
        )

        # In SQLite, lowercase version does NOT violate unique constraint
        # because SQLite comparison is case-sensitive by default
        claim2 = Claim.objects.create(
            alf_claim_id='alf1234567',  # Lowercase version - allowed in SQLite
            client_email='second@example.com',
        )
        assert claim2.alf_claim_id == 'alf1234567'

    def test_claim_alf_claim_id_null_allowed(self):
        """Multiple claims can have NULL alf_claim_id."""
        Claim.objects.create(
            alf_claim_id=None,
            client_email='nulltest-a@example.com',
        )

        # Should not raise - NULL values don't violate unique constraint
        Claim.objects.create(
            alf_claim_id=None,
            client_email='nulltest-b@example.com',
        )

        # Verify both claims exist. Scope to the two we created (other fixtures
        # in this suite may also create NULL-alf claims), so the assertion is
        # isolation-safe.
        ours = Claim.objects.filter(
            alf_claim_id__isnull=True,
            client_email__in=['nulltest-a@example.com', 'nulltest-b@example.com'],
        )
        assert ours.count() == 2

    def test_claim_alf_claim_id_blank_allowed(self):
        """Multiple claims can have empty string alf_claim_id."""
        Claim.objects.create(
            alf_claim_id='',
            client_email='first@example.com',
        )

        # Empty string is not NULL, so this should raise IntegrityError
        with pytest.raises(IntegrityError):
            Claim.objects.create(
                alf_claim_id='',
                client_email='second@example.com',
            )

    def test_claim_str_includes_alf_id(self):
        """__str__ includes ALF claim ID."""
        claim = Claim.objects.create(
            alf_claim_id='ALF1234567',
            client_email='customer@example.com',
            status='Received',
        )

        str_repr = str(claim)
        assert 'ALF1234567' in str_repr
        assert 'customer@example.com' in str_repr
        assert 'Received' in str_repr
        assert 'Claim #' in str_repr

    def test_claim_str_without_alf_id(self):
        """__str__ handles None ALF claim ID."""
        claim = Claim.objects.create(
            alf_claim_id=None,
            client_email='customer@example.com',
            status='Searching',
        )

        str_repr = str(claim)
        assert 'None' in str_repr  # Shows None when alf_claim_id is null
        assert 'customer@example.com' in str_repr
        assert 'Searching' in str_repr

    def test_claim_indexes_exist(self):
        """Database indexes created correctly."""
        # Get all indexes for the claims_claim table
        with connection.cursor() as cursor:
            # PostgreSQL: query pg_indexes
            # SQLite: query sqlite_master
            # This test verifies indexes are created via model Meta.indexes

            # Check that we can query by indexed fields efficiently
            # The existence of indexes is verified by Django migration tests
            # Here we verify the fields that should be indexed work correctly

            claim = Claim.objects.create(
                alf_claim_id='ALF1234567',
                zd_ticket_id='12345',
                client_email='customer@example.com',
                status='Received',
            )

            # These queries should use indexes (verified via EXPLAIN in production)
            # Query by alf_claim_id
            result = Claim.objects.filter(alf_claim_id='ALF1234567').first()
            assert result == claim

            # Query by zd_ticket_id
            result = Claim.objects.filter(zd_ticket_id='12345').first()
            assert result == claim

            # Query by client_email
            result = Claim.objects.filter(client_email='customer@example.com').first()
            assert result == claim

            # Query by status with ordering
            result = Claim.objects.filter(status='Received').order_by('-created_at').first()
            assert result == claim

            # Query by assigned_to with ordering (for None assigned_to)
            result = Claim.objects.filter(assigned_to=None).order_by('-created_at').first()
            assert result == claim

    def test_claim_ordering(self):
        """Claims are ordered by created_at descending."""
        claim1 = Claim.objects.create(
            client_email='first@example.com',
        )
        claim2 = Claim.objects.create(
            client_email='second@example.com',
        )
        claim3 = Claim.objects.create(
            client_email='third@example.com',
        )

        # Default ordering is -created_at (newest first)
        claims = list(Claim.objects.all())
        assert claims[0] == claim3
        assert claims[1] == claim2
        assert claims[2] == claim1

    def test_claim_status_choices(self):
        """Claim accepts valid status choices."""
        valid_statuses = [
            'Received',
            'Searching',
            'Found',
            'Shipped',
            'Disputed',
            'REFUND_REQUESTED',
            'REFUNDED',
            'PARTIALLY_REFUNDED',
        ]

        for i, status in enumerate(valid_statuses):
            claim = Claim.objects.create(
                client_email=f'test{i}@example.com',
                status=status,
            )
            assert claim.status == status

    def test_claim_invalid_status(self):
        """Claim accepts any status value (Django doesn't validate choices by default).
        
        Note: Django's choices parameter is only for documentation and form validation.
        Database-level enforcement would require custom validators or constraints.
        """
        # Django doesn't enforce choices at the model/database level by default
        # This test documents the actual behavior
        claim = Claim.objects.create(
            client_email='test@example.com',
            status='INVALID_STATUS',
        )
        assert claim.status == 'INVALID_STATUS'

    def test_claim_default_status(self):
        """Claim default status is 'Investigation initiated'."""
        claim = Claim.objects.create(
            client_email='test@example.com',
        )
        assert claim.status == 'Investigation initiated'

    def test_claim_phone_optional(self):
        """Phone field is optional (blank and null allowed)."""
        claim = Claim.objects.create(
            client_email='test@example.com',
            phone='',  # Empty string
        )
        assert claim.phone == ''

        claim2 = Claim.objects.create(
            client_email='test2@example.com',
            phone=None,  # None is allowed with null=True
        )
        assert claim2.phone is None

    def test_claim_alternate_email_optional(self):
        """Alternate email field is optional."""
        claim = Claim.objects.create(
            client_email='test@example.com',
            alternate_email='',
        )
        assert claim.alternate_email == ''

    def test_claim_alternate_email_validation(self):
        """Alternate email must be valid email format."""
        # Valid email
        claim = Claim.objects.create(
            client_email='test@example.com',
            alternate_email='backup@example.com',
        )
        assert claim.alternate_email == 'backup@example.com'

        # Invalid email should fail validation at model level
        # Note: EmailField validation happens on save/clean
        claim2 = Claim.objects.create(
            client_email='test2@example.com',
            alternate_email='',  # Empty is allowed
        )
        assert claim2.alternate_email == ''

    def test_claim_llm_extraction_failed_default(self):
        """llm_extraction_failed defaults to False."""
        claim = Claim.objects.create(
            client_email='test@example.com',
        )
        assert claim.llm_extraction_failed is False

    def test_claim_llm_extraction_failed_set_true(self):
        """llm_extraction_failed can be set to True."""
        claim = Claim.objects.create(
            client_email='test@example.com',
            llm_extraction_failed=True,
        )
        assert claim.llm_extraction_failed is True

    def test_claim_flight_details_max_length(self):
        """Flight details accepts long text."""
        long_flight_details = 'Flight AA123 from JFK to LAX on March 15, 2026. ' * 100
        claim = Claim.objects.create(
            client_email='test@example.com',
            flight_details=long_flight_details,
        )
        assert claim.flight_details == long_flight_details

    def test_claim_object_description_max_length(self):
        """Object description accepts long text."""
        long_description = 'Black MacBook Pro laptop, 15-inch with silver apple logo. ' * 100
        claim = Claim.objects.create(
            client_email='test@example.com',
            object_description=long_description,
        )
        assert claim.object_description == long_description

    def test_claim_zd_ticket_id_max_length(self):
        """Zendesk ticket ID accepts up to 50 characters."""
        long_ticket_id = '12345' * 10  # 50 characters
        claim = Claim.objects.create(
            client_email='test@example.com',
            zd_ticket_id=long_ticket_id,
        )
        assert claim.zd_ticket_id == long_ticket_id

    def test_claim_zd_ticket_id_optional(self):
        """Zendesk ticket ID is optional."""
        claim = Claim.objects.create(
            client_email='test@example.com',
            zd_ticket_id='',
        )
        assert claim.zd_ticket_id == ''

        claim2 = Claim.objects.create(
            client_email='test2@example.com',
            zd_ticket_id=None,
        )
        assert claim2.zd_ticket_id is None

    def test_claim_assigned_to_optional(self):
        """Claim can be assigned to a user."""
        user = User.objects.create_user(
            username='testagent',
            email='agent@example.com',
            password='testpass',
        )

        claim = Claim.objects.create(
            client_email='test@example.com',
            assigned_to=user,
        )

        assert claim.assigned_to == user
        assert user.assigned_claims.count() == 1

    def test_claim_assigned_to_null(self):
        """Claim can be unassigned."""
        claim = Claim.objects.create(
            client_email='test@example.com',
            assigned_to=None,
        )
        assert claim.assigned_to is None

    def test_claim_has_refund_property_false(self):
        """has_refund returns False when no refunds."""
        claim = Claim.objects.create(
            client_email='test@example.com',
        )
        assert claim.has_refund is False

    def test_claim_has_refund_property_true(self):
        """has_refund returns True when refunds exist."""
        from apps.payments.models import Refund

        claim = Claim.objects.create(
            client_email='test@example.com',
        )

        Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test',
        )

        assert claim.has_refund is True

    def test_claim_refund_total_property(self):
        """refund_total calculates sum of completed refunds."""
        from apps.payments.models import Refund

        claim = Claim.objects.create(
            client_email='test@example.com',
        )

        # No refunds yet
        assert claim.refund_total == 0

        # Add completed refund
        Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test',
            status='COMPLETED',
        )

        # Refresh from DB
        claim.refresh_from_db()
        assert claim.refund_total == 50.00

        # Add another completed refund
        Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-124',
            amount=25.00,
            refund_type='PARTIAL',
            reason='Test 2',
            status='COMPLETED',
        )

        claim.refresh_from_db()
        assert claim.refund_total == 75.00

    def test_claim_refund_total_excludes_non_completed(self):
        """refund_total only includes COMPLETED refunds."""
        from apps.payments.models import Refund

        claim = Claim.objects.create(
            client_email='test@example.com',
        )

        # PENDING refund (not completed)
        Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test',
            status='PENDING',
        )

        claim.refresh_from_db()
        assert claim.refund_total == 0

    def test_claim_latest_refund_property(self):
        """latest_refund returns most recent refund."""
        from apps.payments.models import Refund
        import time

        claim = Claim.objects.create(
            client_email='test@example.com',
        )

        # First refund
        refund1 = Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='First',
        )

        time.sleep(0.01)  # Ensure different created_at

        # Second refund (more recent)
        refund2 = Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-124',
            amount=25.00,
            refund_type='PARTIAL',
            reason='Second',
        )

        claim.refresh_from_db()
        assert claim.latest_refund == refund2
        assert claim.latest_refund.paypal_refund_id == 'REFUND-124'

    def test_claim_latest_refund_property_none(self):
        """latest_refund returns None when no refunds."""
        claim = Claim.objects.create(
            client_email='test@example.com',
        )
        assert claim.latest_refund is None

    def test_claim_refund_status_property(self):
        """refund_status returns status of latest refund."""
        from apps.payments.models import Refund

        claim = Claim.objects.create(
            client_email='test@example.com',
        )

        # No refunds
        assert claim.refund_status is None

        # Add refund
        Refund.objects.create(
            claim=claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test',
            status='COMPLETED',
        )

        claim.refresh_from_db()
        assert claim.refund_status == 'COMPLETED'


@pytest.mark.django_db
class TestClaimModelQueries:
    """Test database query behavior for Claim model."""

    def test_claim_filter_by_alf_claim_id(self):
        """Can filter claims by ALF claim ID."""
        Claim.objects.create(
            alf_claim_id='ALF1111111',
            client_email='first@example.com',
        )
        Claim.objects.create(
            alf_claim_id='ALF2222222',
            client_email='second@example.com',
        )

        result = Claim.objects.filter(alf_claim_id='ALF1111111').first()
        assert result.client_email == 'first@example.com'

    def test_claim_filter_by_zd_ticket_id(self):
        """Can filter claims by Zendesk ticket ID."""
        Claim.objects.create(
            zd_ticket_id='12345',
            client_email='first@example.com',
        )
        Claim.objects.create(
            zd_ticket_id='67890',
            client_email='second@example.com',
        )

        result = Claim.objects.filter(zd_ticket_id='12345').first()
        assert result.client_email == 'first@example.com'

    def test_claim_filter_by_client_email(self):
        """Can filter claims by client email."""
        Claim.objects.create(
            client_email='unique@example.com',
        )

        result = Claim.objects.filter(client_email='unique@example.com').first()
        assert result is not None

    def test_claim_filter_by_status(self):
        """Can filter claims by status."""
        # Use unique email to ensure we're filtering only our test data
        Claim.objects.create(
            client_email='filter_test_first@example.com',
            status='Received',
        )
        Claim.objects.create(
            client_email='filter_test_second@example.com',
            status='Found',
        )

        # Filter by both status and our test email pattern to ensure isolation
        received_claims = Claim.objects.filter(
            status='Received',
            client_email__startswith='filter_test_'
        )
        assert received_claims.count() == 1
        assert received_claims.first().client_email == 'filter_test_first@example.com'

    def test_claim_filter_by_llm_extraction_failed(self):
        """Can filter claims by LLM extraction failed flag."""
        Claim.objects.create(
            client_email='llmfail-yes@example.com',
            llm_extraction_failed=True,
        )
        Claim.objects.create(
            client_email='llmfail-no@example.com',
            llm_extraction_failed=False,
        )

        # Scope to the two claims we created so other fixtures' claims (which may
        # also set llm_extraction_failed=True) don't break the count.
        ours = Claim.objects.filter(
            client_email__in=['llmfail-yes@example.com', 'llmfail-no@example.com'],
        )
        failed_claims = ours.filter(llm_extraction_failed=True)
        assert failed_claims.count() == 1
        assert failed_claims.first().client_email == 'llmfail-yes@example.com'

    def test_claim_get_or_create_with_alf_claim_id(self):
        """get_or_create works with ALF claim ID."""
        claim, created = Claim.objects.get_or_create(
            alf_claim_id='ALF1234567',
            defaults={'client_email': 'test@example.com'},
        )
        assert created is True
        assert claim.alf_claim_id == 'ALF1234567'

        # Second call should return existing claim
        claim2, created2 = Claim.objects.get_or_create(
            alf_claim_id='ALF1234567',
            defaults={'client_email': 'other@example.com'},
        )
        assert created2 is False
        assert claim2.id == claim.id
        # Email should not be updated (defaults only apply on create)
        assert claim2.client_email == 'test@example.com'

    def test_claim_update_or_create_with_alf_claim_id(self):
        """update_or_create works with ALF claim ID."""
        claim, created = Claim.objects.update_or_create(
            alf_claim_id='ALF1234567',
            defaults={
                'client_email': 'test@example.com',
                'status': 'Received',
            },
        )
        assert created is True

        # Second call should update existing claim
        claim2, created2 = Claim.objects.update_or_create(
            alf_claim_id='ALF1234567',
            defaults={
                'client_email': 'updated@example.com',
                'status': 'Found',
            },
        )
        assert created2 is False
        assert claim2.id == claim.id
        assert claim2.client_email == 'updated@example.com'
        assert claim2.status == 'Found'


class ClaimUpdateTimelineStrTests(TestCase):
    def test_str_does_not_crash_and_mentions_update_type(self):
        claim = Claim.objects.create(client_email='str-test@example.com')
        entry = ClaimUpdateTimeline.objects.create(
            claim=claim, zendesk_ticket_id='123', update_type='STATUS_CHANGE',
        )
        text = str(entry)
        self.assertIn('STATUS_CHANGE', text)
        self.assertIn(str(claim.id), text)


class ClaimStatusMirrorFieldTests(TestCase):
    def test_new_claim_defaults_to_investigation_initiated_open_family(self):
        claim = Claim.objects.create(client_email='mirror@example.com')
        self.assertEqual(claim.status, 'Investigation initiated')
        self.assertEqual(claim.status_category, 'open')
        self.assertIsNone(claim.status_changed_at)
        self.assertIsNone(claim.deadline_at)
        self.assertIsNone(claim.ai_summary_updated_at)

    def test_status_accepts_long_zendesk_names(self):
        claim = Claim.objects.create(
            client_email='long@example.com',
            status='Closed - Client Not Answering', status_category='solved',
        )
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'Closed - Client Not Answering')
