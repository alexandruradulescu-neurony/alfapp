import pytest
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
