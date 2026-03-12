import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from apps.config.models import SystemSettings

# Create or get the singleton instance
settings = SystemSettings.get_instance()
print(f"SystemSettings instance: {settings}")
print(f"AI Prompt Template length: {len(settings.ai_prompt_template)} chars")
print(f"IMAP Host: {settings.imap_host}")
print("\nSystemSettings is ready. Configure via Django admin.")
