"""Retroactive re-tag: recompute each ticket's ai_* tags from the UNION of its
EmailLogs and add them (additive). Fixes tickets the global sweep categorized but
never tagged."""
import pytest
from unittest.mock import patch

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.communications.services import retag_tickets_from_email_logs


@pytest.mark.django_db
def test_retag_unions_tags_per_ticket_and_skips_untaggable():
    claim = Claim.objects.create(client_email='c@e.com', alf_claim_id='ALFRT', zd_ticket_id='99000001')
    # Ticket 99000001 has two emails → tags are the UNION across both.
    EmailLog.objects.create(claim=claim, zd_ticket_id='99000001', message_id='<rt1@x>',
                            category=EmailLog.CATEGORY_OBJECT_FOUND, action_required=False)
    EmailLog.objects.create(claim=claim, zd_ticket_id='99000001', message_id='<rt2@x>',
                            category=EmailLog.CATEGORY_SHIPPING_INFORMATION, action_required=True)
    # Ticket 99000002 is general correspondence with no action → no ai tag → untouched.
    EmailLog.objects.create(claim=claim, zd_ticket_id='99000002', message_id='<rt3@x>',
                            category=EmailLog.CATEGORY_GENERAL_CORRESPONDENCE, action_required=False)

    with patch('apps.integrations.services.add_zendesk_ticket_tags', return_value=True) as tag:
        retag_tickets_from_email_logs()

    calls = {call.args[0]: call.args[1] for call in tag.call_args_list}
    assert set(calls['99000001']) == {'ai_object_found', 'ai_shipping_information', 'ai_attention_needed'}
    assert '99000002' not in calls            # general + no action → nothing to tag, never called
