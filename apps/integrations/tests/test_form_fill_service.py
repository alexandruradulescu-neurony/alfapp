import pytest
from urllib.parse import urlparse
from apps.claims.models import Claim
from apps.integrations.form_fill_service import (
    build_form_secrets, build_fill_task, build_agent_context, SUBMIT_TASK, form_host)


@pytest.mark.django_db
def test_build_secrets_includes_only_present_fields():
    claim = Claim.objects.create(
        client_email='real@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9',
        email_alias='alias-55@mailapptoday.com',
        object_description='black Sony headphones', lost_location='JFK Terminal 4',
        flight_details='AA100 2026-06-01', zd_ticket_id='55')
    host = 'lf.example'
    secrets = build_form_secrets(claim, host)
    placeholders = secrets[host]
    assert placeholders['x_client_name'] == 'Jo Bloggs'
    # email field must use the alias, not the real client email
    assert placeholders['x_client_email'] == 'alias-55@mailapptoday.com'
    assert 'real@e.com' not in placeholders.values()
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
    assert 'x_client_name' in task
    assert 'do not submit' in task.lower()
    assert 'https://lf.example/report' in task
    # don't let the agent grind on a fiddly control; tell it to skip and report
    assert 'two attempts' in task.lower()
    assert 'list any fields you could not fill' in task.lower()


@pytest.mark.django_db
def test_fill_task_includes_context_when_given():
    claim = Claim.objects.create(client_email='jo@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9')
    secrets = build_form_secrets(claim, 'x')
    task = build_fill_task('https://x/r', secrets, context='BIZ CONTEXT HERE')
    assert 'BIZ CONTEXT HERE' in task


@pytest.mark.django_db
def test_build_agent_context_masks_name_and_includes_business_context():
    from apps.config.models import SystemSettings
    ss = SystemSettings.get_instance()
    ss.pii_tokenization_salt = 'salt-long-enough'
    ss.save()
    claim = Claim.objects.create(client_email='jo@e.com', client_name='Jo Bloggs',
                                 alf_claim_id='ALF9', email_alias='a@mailapptoday.com')
    ticket_data = {
        'subject': 'Lost headphones',
        'description': 'black Sony WH-1000XM5',
        'comments': [{'author': 'Jo Bloggs', 'public': True,
                      'text': 'Jo Bloggs here, serial SN12345 on the headphones'}],
    }
    ctx = build_agent_context(claim, ticket_data)
    assert 'Airport Lost Found' in ctx              # business context present
    assert 'SN12345' in ctx                          # descriptive detail preserved
    assert 'Jo Bloggs' not in ctx                    # client name masked
    assert 'jo@e.com' not in ctx                     # email masked


def test_submit_task_is_explicit():
    assert 'submit' in SUBMIT_TASK.lower()


def test_form_host_extracts_domain():
    assert form_host('https://app.nettracer.aero/lf/report?x=1') == 'app.nettracer.aero'
