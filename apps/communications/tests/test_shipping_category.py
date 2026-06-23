"""SHIPPING_INFORMATION email category: schema, tag, prompt guide, importance."""
from unittest.mock import patch

from apps.ai.schemas import EmailCategorization
from apps.communications.models import EmailLog
from apps.communications.services import (
    AI_TAG_BY_CATEGORY, AUTO_RESOLVABLE_CATEGORIES, EMAIL_CATEGORY_GUIDE,
    _ai_tags_for, call_qwen_ai)


def test_schema_accepts_shipping_information():
    obj = EmailCategorization(summary='s', category='SHIPPING_INFORMATION',
                              action_required=True, auto_resolvable=False)
    assert obj.category == 'SHIPPING_INFORMATION'


def test_shipping_maps_to_its_tag():
    assert AI_TAG_BY_CATEGORY[EmailLog.CATEGORY_SHIPPING_INFORMATION] == 'ai_shipping_information'
    assert 'ai_shipping_information' in _ai_tags_for(EmailLog.CATEGORY_SHIPPING_INFORMATION, True)


def test_shipping_is_not_auto_resolvable():
    # important like Object Found — never auto-closed
    assert EmailLog.CATEGORY_SHIPPING_INFORMATION not in AUTO_RESOLVABLE_CATEGORIES


def test_call_qwen_ai_appends_shipping_guide_to_prompt():
    fake = EmailCategorization(summary='s', category='SHIPPING_INFORMATION',
                               action_required=True, auto_resolvable=False)
    with patch('apps.ai.client.AIClient.complete', return_value=fake) as m:
        out = call_qwen_ai('BASE PROMPT', 'tracking number 1Z999 via UPS', 'Your item shipped')
    sysp = m.call_args.kwargs['system_prompt']
    assert 'BASE PROMPT' in sysp                 # original prompt preserved
    assert 'SHIPPING_INFORMATION' in sysp        # taxonomy injected from code
    assert 'tracking' in sysp.lower()            # the model is told what shipping looks like
    assert out['category'] == 'SHIPPING_INFORMATION'
