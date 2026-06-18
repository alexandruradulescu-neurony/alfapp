from django.test import TestCase
from apps.ai.prompt_fence import build_messages, STYLE_RULE


class StyleRuleTests(TestCase):
    def test_style_rule_is_in_every_system_message(self):
        msgs = build_messages(system_prompt='Do the thing.', trusted_text=None, untrusted={})
        system = next(m for m in msgs if m['role'] == 'system')['content']
        self.assertIn('never in an AI-generated voice', system)
        self.assertIn('Do not use em-dashes', system)

    def test_style_rule_constant_has_no_em_or_en_dash(self):
        self.assertNotIn('—', STYLE_RULE)   # em dash
        self.assertNotIn('–', STYLE_RULE)   # en dash

    def test_original_system_prompt_preserved(self):
        msgs = build_messages(system_prompt='UNIQUE_MARKER_123', trusted_text=None, untrusted={})
        system = next(m for m in msgs if m['role'] == 'system')['content']
        self.assertIn('UNIQUE_MARKER_123', system)
