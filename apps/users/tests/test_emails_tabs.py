"""The redesigned emails screen segments inbound institution replies by tab/lens
(Needs reply · Object found · Not found · Resubmit · Handled · All) with live
counts, shows the AI gist inline, and ties each email to its claim/client."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim
from apps.communications.models import EmailLog

User = get_user_model()


class EmailsTabsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='em_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='c@e.com', client_name='Eric Wilson',
            alf_claim_id='ALFE', zd_ticket_id='9001')

        def mk(cat, action=False, auto=False, claim=None, subject='s'):
            return EmailLog.objects.create(
                subject=subject, body='b', category=cat, from_email='x@inst.com',
                action_required=action, auto_resolved=auto, claim=claim)

        self.needs = mk('GENERAL_CORRESPONDENCE', action=True)
        self.found = mk('OBJECT_FOUND', claim=self.claim, subject='Item located')
        self.notfound = mk('OBJECT_NOT_FOUND')
        self.resubmit = mk('RESUBMISSION_REQUIRED')
        self.auto = mk('SUBMISSION_CONFIRMATION', auto=True)

    def _ids(self, tab):
        resp = self.web.get(reverse('agent_emails') + f'?tab={tab}')
        self.assertEqual(resp.status_code, 200)
        return {e.id for e in resp.context['emails']}

    def test_needs_reply_is_default_and_excludes_handled(self):
        resp = self.web.get(reverse('agent_emails'))
        self.assertEqual(resp.context['tab'], 'needs_reply')
        ids = {e.id for e in resp.context['emails']}
        self.assertIn(self.needs.id, ids)
        self.assertNotIn(self.found.id, ids)
        self.assertNotIn(self.auto.id, ids)

    def test_category_tabs(self):
        self.assertIn(self.found.id, self._ids('object_found'))
        self.assertIn(self.notfound.id, self._ids('not_found'))
        self.assertIn(self.resubmit.id, self._ids('resubmit'))
        self.assertNotIn(self.needs.id, self._ids('object_found'))

    def test_handled_excludes_needs_reply(self):
        ids = self._ids('handled')
        self.assertIn(self.auto.id, ids)
        self.assertIn(self.found.id, ids)
        self.assertNotIn(self.needs.id, ids)

    def test_all_includes_everything(self):
        ids = self._ids('all')
        for e in (self.needs, self.found, self.notfound, self.resubmit, self.auto):
            self.assertIn(e.id, ids)

    def test_counts_present(self):
        counts = self.web.get(reverse('agent_emails')).context['tab_counts']
        self.assertEqual(counts['needs_reply'], 1)
        self.assertEqual(counts['object_found'], 1)
        self.assertEqual(counts['resubmit'], 1)

    def test_redesign_markers_present_and_old_removed(self):
        html = self.web.get(reverse('agent_emails') + '?tab=all').content.decode()
        self.assertIn('?tab=needs_reply', html)                # tabs
        self.assertIn('Eric Wilson', html)                      # claim/client shown
        self.assertIn(reverse('agent_email_detail', args=[self.found.id]), html)
        self.assertIn('window.location=', html)                 # clickable row
        self.assertNotIn('Show Auto-Resolved', html)            # old filter panel gone
        self.assertNotIn('<th>ID</th>', html)                   # internal ID column gone
