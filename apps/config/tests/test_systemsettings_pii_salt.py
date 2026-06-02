import pytest
from apps.config.models import SystemSettings


@pytest.mark.django_db
def test_pii_tokenization_salt_field_exists_and_persists():
    """SystemSettings has a pii_tokenization_salt field that persists across reads."""
    settings = SystemSettings.get_instance()
    settings.pii_tokenization_salt = 'test_salt_value_long_random_string_at_least_32_chars'
    settings.save()

    fresh = SystemSettings.get_instance()
    assert fresh.pii_tokenization_salt == 'test_salt_value_long_random_string_at_least_32_chars'


@pytest.mark.django_db
def test_pii_tokenization_salt_defaults_to_empty():
    """A newly-created SystemSettings has an empty salt by default."""
    SystemSettings.objects.all().delete()
    settings = SystemSettings.get_instance()
    assert settings.pii_tokenization_salt == ''
