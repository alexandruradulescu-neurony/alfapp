"""The abandoned-cart/checkout origin is the universal starting point of EVERY
claim, so it carries no information — the shared AI business context must tell
the model to ignore it (otherwise summaries narrate "the ticket was abandoned",
which is noise). One fix in ALF_BUSINESS_CONTEXT covers every prompt that
prepends it (summary, sidebar briefing/next-steps/chat, flight check, drafts)."""

from django.test import TestCase
from apps.integrations.briefing import ALF_BUSINESS_CONTEXT, SUMMARY_PROMPT


class AbandonedOriginIgnoredTests(TestCase):
    def test_business_context_addresses_and_suppresses_abandoned_origin(self):
        ctx = ALF_BUSINESS_CONTEXT.lower()
        self.assertIn('abandoned', ctx)        # the origin is acknowledged
        self.assertIn('never mention', ctx)    # ...and the model is told to suppress it

    def test_summary_prompt_inherits_the_instruction(self):
        # SUMMARY_PROMPT = ALF_BUSINESS_CONTEXT + (...), so it carries the rule too
        self.assertIn('never mention', SUMMARY_PROMPT.lower())
        self.assertIn('abandoned', SUMMARY_PROMPT.lower())
