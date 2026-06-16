"""
Regression tests for ClaimViewSet API contract.

Key invariant: `status` (and `status_category`, `status_changed_at`) are
read-only through the API — the Zendesk webhook owns those fields via the ORM.
DRF silently ignores read-only fields in incoming data, so a PATCH with
{"status": "Hacked"} must return 200 but leave the DB value unchanged.
"""

import pytest
from rest_framework.test import APIClient

from apps.claims.models import Claim
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.fixture
def agent(db):
    return User.objects.create_user(
        username="api_test_agent",
        password="testpass123",
    )


@pytest.fixture
def api_client(agent):
    client = APIClient()
    client.force_authenticate(user=agent)
    return client


@pytest.fixture
def claim(db):
    return Claim.objects.create(
        client_email="status-test@example.com",
        status="Investigation initiated",
        alf_claim_id="ALF9990001",
    )


@pytest.mark.django_db
class TestClaimStatusReadOnly:
    """Status field is read-only through the API."""

    def test_patch_status_is_silently_ignored(self, api_client, claim):
        """
        PATCH with {"status": "Hacked"} must succeed (200) but leave
        claim.status exactly as it was — DRF drops read-only fields silently.
        """
        original_status = claim.status  # "Investigation initiated"

        response = api_client.patch(
            f"/api/claims/claims/{claim.id}/",
            {"status": "Hacked"},
            format="json",
        )

        # DRF returns 200 — read-only fields are silently ignored, not rejected
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        # DB must be unchanged
        claim.refresh_from_db()
        assert claim.status == original_status, (
            f"status was mutated to {claim.status!r}; expected {original_status!r}"
        )

    def test_patch_status_category_is_silently_ignored(self, api_client, claim):
        """PATCH with status_category is silently ignored (field not even in response)."""
        response = api_client.patch(
            f"/api/claims/claims/{claim.id}/",
            {"status_category": "solved"},
            format="json",
        )

        assert response.status_code == 200

        claim.refresh_from_db()
        assert claim.status_category == "open"  # default, unchanged

    def test_patch_other_fields_still_work(self, api_client, claim):
        """Sanity check: writable fields (e.g. flight_details) are still accepted."""
        response = api_client.patch(
            f"/api/claims/claims/{claim.id}/",
            {"flight_details": "AA100 JFK→LAX"},
            format="json",
        )

        assert response.status_code == 200

        claim.refresh_from_db()
        assert claim.flight_details == "AA100 JFK→LAX"
        # status untouched
        assert claim.status == "Investigation initiated"
