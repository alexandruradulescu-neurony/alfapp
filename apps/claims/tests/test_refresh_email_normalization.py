"""Red-phase test: refresh_claim_from_zendesk must lowercase client_email.

client_email is a case-sensitive match key, so the refresh path (which merges
re-extracted Zendesk facts into the claim) must normalize it to lowercase on
write, matching the API path.
"""

import pytest

from apps.claims.models import Claim
from apps.claims.services import refresh_claim_from_zendesk


@pytest.mark.django_db
def test_refresh_lowercases_client_email():
    claim = Claim.objects.create(
        alf_claim_id='ALF-EMAIL-NORM', client_email='old@example.com')

    refresh_claim_from_zendesk(claim, {'client_email': 'Mixed.Case@Example.COM'})

    claim.refresh_from_db()
    assert claim.client_email == 'mixed.case@example.com'
