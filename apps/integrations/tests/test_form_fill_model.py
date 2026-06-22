import pytest
from apps.claims.models import Claim
from apps.integrations.models import FormFill


@pytest.mark.django_db
class TestFormFillModel:
    def _claim(self):
        return Claim.objects.create(client_email='c@e.com', alf_claim_id='ALF1', zd_ticket_id='100')

    def test_create_defaults(self):
        ff = FormFill.objects.create(claim=self._claim(), form_url='https://lf.example/report')
        assert ff.status == FormFill.STATUS_STARTED
        assert ff.image_source == FormFill.IMAGE_SOURCE_NONE
        assert ff.posted_to_ticket is False
        assert ff.created_at is not None

    def test_related_name_and_ordering(self):
        claim = self._claim()
        FormFill.objects.create(claim=claim, form_url='https://a')
        FormFill.objects.create(claim=claim, form_url='https://b')
        fills = list(claim.form_fills.all())
        assert len(fills) == 2
        assert fills[0].created_at >= fills[1].created_at  # newest first

    def test_status_choices_cover_lifecycle(self):
        vals = {c[0] for c in FormFill.STATUS_CHOICES}
        assert vals == {'STARTED', 'FILLED', 'SUBMITTED', 'CANCELLED', 'FAILED'}
