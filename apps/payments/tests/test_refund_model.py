from django.test import TestCase
from django.db import IntegrityError
from django.contrib.auth import get_user_model
from apps.payments.models import Refund
from apps.claims.models import Claim

User = get_user_model()


class RefundModelTest(TestCase):
    """Test cases for the Refund model."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testmanager',
            password='testpass',
            email='manager@test.com'
        )
        self.claim = Claim.objects.create(
            client_email='test@example.com',
            flight_details='Flight AA123'
        )
    
    def test_create_refund(self):
        """Test creating a refund instance."""
        refund = Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Customer request',
            created_by=self.user
        )

        self.assertEqual(refund.status, 'REQUESTED')
        self.assertEqual(refund.external_source, 'LORA')
        self.assertEqual(refund.currency, 'USD')
        # Note: Decimal formatting may vary
        self.assertIn('Refund', str(refund))
        self.assertIn('50', str(refund))
        self.assertIn('REQUESTED', str(refund))
    
    def test_unique_paypal_refund_id(self):
        """Test that paypal_refund_id is unique."""
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test'
        )
        
        with self.assertRaises(IntegrityError):
            Refund.objects.create(
                claim=self.claim,
                paypal_refund_id='REFUND-123',  # Duplicate
                amount=75.00,
                refund_type='PARTIAL',
                reason='Test 2'
            )
    
    def test_claim_refunds_relationship(self):
        """Test that claim can have multiple refunds."""
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test 1'
        )
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-124',
            amount=25.00,
            refund_type='PARTIAL',
            reason='Test 2'
        )
        
        self.assertTrue(self.claim.has_refund)
        self.assertEqual(self.claim.refunds.count(), 2)
        self.assertEqual(self.claim.refund_total, 0)  # None completed yet
    
    def test_refund_total_with_completed_refunds(self):
        """Test refund total calculation with completed refunds."""
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test 1',
            status='COMPLETED'
        )
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-124',
            amount=25.00,
            refund_type='PARTIAL',
            reason='Test 2',
            status='COMPLETED'
        )
        
        # Refresh claim to get updated properties
        claim = Claim.objects.get(id=self.claim.id)
        self.assertEqual(claim.refund_total, 75.00)
    
    def test_mark_completed(self):
        """Test marking refund as completed."""
        refund = Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test'
        )
        
        self.assertIsNone(refund.processed_at)
        refund.mark_completed()
        
        refund.refresh_from_db()
        self.assertEqual(refund.status, 'COMPLETED')
        self.assertIsNotNone(refund.processed_at)
        self.assertTrue(refund.is_completed)
    
    def test_mark_failed(self):
        """Test marking refund as failed."""
        refund = Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test'
        )
        
        refund.mark_failed('Payment gateway error')
        refund.refresh_from_db()
        
        self.assertEqual(refund.status, 'FAILED')
        self.assertEqual(refund.metadata['error_message'], 'Payment gateway error')
    
    def test_mark_processing(self):
        """Test marking refund as processing."""
        refund = Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='Test'
        )
        
        refund.mark_processing()
        refund.refresh_from_db()
        
        self.assertEqual(refund.status, 'PROCESSING')
    
    def test_refund_type_choices(self):
        """Test refund type choices."""
        for choice_value, _ in Refund.TYPE_CHOICES:
            refund = Refund.objects.create(
                claim=self.claim,
                paypal_refund_id=f'REFUND-{choice_value}',
                amount=50.00,
                refund_type=choice_value,
                reason='Test'
            )
            self.assertEqual(refund.refund_type, choice_value)
    
    def test_external_source_choices(self):
        """Test external source choices."""
        for choice_value, _ in Refund.SOURCE_CHOICES:
            refund = Refund.objects.create(
                claim=self.claim,
                paypal_refund_id=f'REFUND-SRC-{choice_value}',
                amount=50.00,
                refund_type='FULL',
                external_source=choice_value,
                reason='Test'
            )
            self.assertEqual(refund.external_source, choice_value)
    
    def test_latest_refund_property(self):
        """Test latest_refund property returns most recent refund."""
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-123',
            amount=50.00,
            refund_type='FULL',
            reason='First'
        )
        # Wait a tiny bit to ensure different created_at
        import time
        time.sleep(0.01)
        
        Refund.objects.create(
            claim=self.claim,
            paypal_refund_id='REFUND-124',
            amount=25.00,
            refund_type='PARTIAL',
            reason='Second'
        )
        
        latest = self.claim.latest_refund
        self.assertEqual(latest.paypal_refund_id, 'REFUND-124')
        self.assertEqual(latest.status, 'REQUESTED')
    
    def test_refund_without_claim(self):
        """Test creating refund without claim (null allowed)."""
        refund = Refund.objects.create(
            paypal_refund_id='REFUND-NOCLAIM',
            amount=50.00,
            refund_type='FULL',
            reason='Manual entry without claim',
            external_source='MANUAL'
        )
        
        self.assertIsNone(refund.claim)
        self.assertEqual(refund.external_source, 'MANUAL')
