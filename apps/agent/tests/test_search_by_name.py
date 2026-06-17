"""Red-phase test: search_claims_by_name_or_email must find a claim by its
client_name, even when the email is unrelated.

Spec: a name search currently looks at email/alternate_email/object_description
but NOT client_name; this test pins the desired behavior.
"""

import pytest

from apps.agent.services import AgentChatService
from apps.claims.models import Claim


@pytest.mark.django_db
def test_search_finds_claim_by_client_name():
    claim = Claim.objects.create(
        alf_claim_id='ALF-NAME-SRCH',
        client_name='Jane Q Smith',
        client_email='zzz-unrelated@example.com',
    )

    results = AgentChatService().search_claims_by_name_or_email('Jane Smith')

    result_ids = {c.id for c in results}
    assert claim.id in result_ids, (
        f"Expected claim {claim.id} (client_name='Jane Q Smith') in results, "
        f"got ids: {sorted(result_ids)}"
    )
