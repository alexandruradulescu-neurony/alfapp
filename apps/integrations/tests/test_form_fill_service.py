import pytest
from urllib.parse import urlparse
from apps.claims.models import Claim
from apps.integrations.form_fill_service import (
    build_form_secrets, build_fill_task, SUBMIT_TASK, form_host)


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


def test_submit_task_is_explicit():
    assert 'submit' in SUBMIT_TASK.lower()


def test_form_host_extracts_domain():
    assert form_host('https://app.nettracer.aero/lf/report?x=1') == 'app.nettracer.aero'


@pytest.mark.django_db
def test_secrets_split_full_name_into_first_and_last():
    claim = Claim.objects.create(client_email='c@e.com', client_name='Bronach Mary Smith',
                                 alf_claim_id='ALF1')
    placeholders = build_form_secrets(claim, 'h')['h']
    assert placeholders['x_client_first_name'] == 'Bronach'
    assert placeholders['x_client_last_name'] == 'Mary Smith'
    assert placeholders['x_client_name'] == 'Bronach Mary Smith'   # full name kept for single-box forms


@pytest.mark.django_db
def test_fill_task_forbids_masked_tokens_and_directs_secret_keys():
    claim = Claim.objects.create(client_email='c@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9',
                                 email_alias='a@mailapptoday.com', phone='123',
                                 object_description='green suitcase')
    task = build_fill_task('https://lf.example/r', build_form_secrets(claim, 'lf.example'))
    low = task.lower()
    # must explicitly forbid typing the masking tokens into form fields
    assert 'never type a masked placeholder' in low
    assert '<name_' in low
    # the verbatim-repeat rule (inherited from the summary business-context) is overridden for forms
    assert 'not for filling forms' in low
    # separate first/last name boxes are supported
    assert 'x_client_first_name' in task and 'x_client_last_name' in task
    # no fabricating / inferring dropdown values (e.g. guessing a terminal)
    assert 'do not invent or infer' in low
    assert 'outside knowledge' in low
    assert 'leave it blank' in low


def test_fill_task_includes_facts_and_playbook_and_keeps_safety_rules():
    secrets = {'lf.example': {'x_client_first_name': 'ZZNAME', 'x_item_description': 'ZZDESC',
                              'x_baggage_tag': 'ZZTAG'}}
    facts = {'Item type': 'Suitcase', 'Airport': 'EWR'}
    task = build_fill_task('https://lf.example/r', secrets, facts=facts,
                           playbook='Item type is a pop-up picker; type to select.')
    low = task.lower()
    assert 'item type: suitcase' in low and 'airport: ewr' in low      # visible facts shown
    assert 'x_baggage_tag' in task                                     # new secret label present
    assert 'pop-up picker' in low                                      # site playbook injected
    assert 'never type a masked placeholder' in low                    # safety rules kept
    assert 'do not submit' in low
    # the real secret VALUES are never written into the brief (only the x_* keys are)
    assert 'ZZNAME' not in task and 'ZZDESC' not in task and 'ZZTAG' not in task
