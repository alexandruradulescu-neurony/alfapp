import pytest
from urllib.parse import urlparse
from apps.claims.models import Claim
from apps.integrations.form_fill_service import (
    build_form_secrets, build_fill_task, SUBMIT_TASK, form_host)


@pytest.mark.django_db
def test_build_secrets_includes_only_present_fields():
    claim = Claim.objects.create(
        client_email='jo@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9',
        object_description='black Sony headphones', lost_location='JFK Terminal 4',
        flight_details='AA100 2026-06-01', zd_ticket_id='55')
    host = 'lf.example'
    secrets = build_form_secrets(claim, host)
    placeholders = secrets[host]
    assert placeholders['x_client_name'] == 'Jo Bloggs'
    assert placeholders['x_client_email'] == 'jo@e.com'
    assert placeholders['x_item_description'] == 'black Sony headphones'
    assert placeholders['x_lost_location'] == 'JFK Terminal 4'
    assert placeholders['x_claim_ref'] == 'ALF9'
    # phone is empty -> omitted
    assert 'x_client_phone' not in placeholders


@pytest.mark.django_db
def test_fill_task_uses_placeholders_not_values_and_says_do_not_submit():
    claim = Claim.objects.create(client_email='jo@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9')
    task = build_fill_task('https://lf.example/report', build_form_secrets(claim, 'lf.example'))
    assert 'Jo Bloggs' not in task and 'jo@e.com' not in task   # real PII never in the prompt
    assert 'x_client_name' in task and 'x_client_email' in task
    assert 'do not submit' in task.lower()
    assert 'https://lf.example/report' in task


def test_submit_task_is_explicit():
    assert 'submit' in SUBMIT_TASK.lower()


def test_form_host_extracts_domain():
    assert form_host('https://app.nettracer.aero/lf/report?x=1') == 'app.nettracer.aero'
