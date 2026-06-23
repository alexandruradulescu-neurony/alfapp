import pytest
from unittest.mock import patch
from django.test import Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.integrations.models import FormPlaybook
from apps.integrations.playbooks import playbook_for_domain

User = get_user_model()


def _authed_client(username):
    User.objects.create_user(username=username, password='x')
    c = Client(); c.login(username=username, password='x'); return c


@pytest.mark.django_db
def test_playbook_lookup_by_domain_respects_enabled_and_case():
    FormPlaybook.objects.create(domain='chargerback.com', label='Chargerback',
                                instructions='Type item type into the picker.', enabled=True)
    FormPlaybook.objects.create(domain='off.example', label='Off', instructions='x', enabled=False)
    assert 'picker' in playbook_for_domain('chargerback.com')
    assert playbook_for_domain('CHARGERBACK.COM') != ''     # lookup is case-insensitive
    assert playbook_for_domain('www.chargerback.com') != ''  # subdomain matches the registered domain
    assert playbook_for_domain('off.example') == ''         # disabled -> no instructions
    assert playbook_for_domain('unknown.com') == ''
    assert playbook_for_domain('') == ''


@pytest.mark.django_db
def test_domain_is_lowercased_on_save():
    pb = FormPlaybook.objects.create(domain='  ChargerBack.COM  ', instructions='hi')
    pb.refresh_from_db()
    assert pb.domain == 'chargerback.com'


@pytest.mark.django_db
def test_page_lists_and_creates_playbook():
    c = _authed_client('pb_mgr')
    FormPlaybook.objects.create(domain='chargerback.com', label='Chargerback', instructions='hi')
    resp = c.get(reverse('manager_form_playbooks'))
    assert resp.status_code == 200
    assert b'chargerback.com' in resp.content                 # existing one listed
    resp = c.post(reverse('manager_form_playbooks'),
                  {'action': 'save', 'domain': 'NetTracer.aero', 'label': 'NetTracer',
                   'instructions': 'Use the lookup.', 'enabled': 'on'})
    assert resp.status_code in (200, 302)
    pb = FormPlaybook.objects.get(domain='nettracer.aero')     # lowercased on save
    assert pb.instructions == 'Use the lookup.' and pb.enabled is True


@pytest.mark.django_db
def test_page_edits_and_deletes_playbook():
    c = _authed_client('pb_mgr2')
    pb = FormPlaybook.objects.create(domain='x.com', instructions='old', enabled=True)
    # edit (no 'enabled' in POST => unchecked => disabled)
    c.post(reverse('manager_form_playbooks'),
           {'action': 'save', 'id': pb.id, 'domain': 'x.com', 'instructions': 'new'})
    pb.refresh_from_db()
    assert pb.instructions == 'new' and pb.enabled is False
    # delete
    c.post(reverse('manager_form_playbooks'), {'action': 'delete', 'id': pb.id})
    assert not FormPlaybook.objects.filter(pk=pb.id).exists()


@pytest.mark.django_db
def test_page_requires_login():
    resp = Client().get(reverse('manager_form_playbooks'))
    assert resp.status_code in (302, 403)                      # login_required gate


@pytest.mark.django_db
def test_recent_run_summaries_filters_by_host_newest_first():
    from apps.integrations.playbooks import recent_run_summaries
    from apps.integrations.models import FormFill
    from apps.claims.models import Claim
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    FormFill.objects.create(claim=claim, form_url='https://www.chargerback.com/ewr', result_output='run A')
    FormFill.objects.create(claim=claim, form_url='https://www.chargerback.com/united', result_output='run B')
    FormFill.objects.create(claim=claim, form_url='https://app.nettracer.aero/x', result_output='other host')
    FormFill.objects.create(claim=claim, form_url='https://www.chargerback.com/z', result_output='')  # empty -> skip
    out = recent_run_summaries('chargerback.com')
    assert out == ['run B', 'run A']         # both chargerback runs, newest first
    assert 'other host' not in out           # different host excluded
    assert recent_run_summaries('') == []


def test_suggest_playbook_instructions_calls_ai_and_returns_text():
    from apps.integrations.playbooks import suggest_playbook_instructions, _Suggestion
    with patch('apps.integrations.playbooks.AIClient') as Client_:
        Client_.return_value.complete.return_value = _Suggestion(instructions='Updated tips.')
        out = suggest_playbook_instructions('old tips', ['run said: picker is fiddly'])
    assert out == 'Updated tips.'
    kwargs = Client_.return_value.complete.call_args.kwargs
    assert kwargs['response_schema'].__name__ == '_Suggestion'
    assert not kwargs['call_site'].startswith('dispute')      # => DeepSeek


def test_suggest_returns_empty_without_summaries():
    from apps.integrations.playbooks import suggest_playbook_instructions
    assert suggest_playbook_instructions('x', []) == ''


@pytest.mark.django_db
def test_improve_button_drafts_into_textarea_without_saving():
    c = _authed_client('pb_ai')
    pb = FormPlaybook.objects.create(domain='chargerback.com', instructions='old tips', enabled=True)
    with patch('apps.integrations.playbooks.recent_run_summaries', return_value=['run report']), \
         patch('apps.integrations.playbooks.suggest_playbook_instructions', return_value='FRESH DRAFT'):
        resp = c.post(reverse('manager_form_playbooks'),
                      {'action': 'suggest', 'id': pb.id, 'domain': 'chargerback.com',
                       'instructions': 'old tips', 'enabled': 'on'})
    assert resp.status_code == 200
    assert b'FRESH DRAFT' in resp.content          # draft shown in the page for review
    pb.refresh_from_db()
    assert pb.instructions == 'old tips'           # nothing auto-saved


@pytest.mark.django_db
def test_improve_button_no_runs_leaves_playbook_unchanged():
    c = _authed_client('pb_ai2')
    pb = FormPlaybook.objects.create(domain='chargerback.com', instructions='keep', enabled=True)
    with patch('apps.integrations.playbooks.recent_run_summaries', return_value=[]):
        resp = c.post(reverse('manager_form_playbooks'),
                      {'action': 'suggest', 'id': pb.id, 'domain': 'chargerback.com',
                       'instructions': 'keep', 'enabled': 'on'}, follow=True)
    assert resp.status_code == 200
    pb.refresh_from_db()
    assert pb.instructions == 'keep'
