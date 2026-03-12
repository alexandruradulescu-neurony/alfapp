import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lora_app.settings')

import django
django.setup()

from django.contrib.auth import get_user_model
User = get_user_model()

# Fix admin user
admin = User.objects.filter(username='admin').first()
if admin:
    admin.role = 'MANAGER'
    admin.is_staff = True
    admin.is_superuser = True
    admin.save()
    print(f"✓ Fixed admin user: {admin.username} (role: {admin.role}, is_staff: {admin.is_staff})")
else:
    print("✗ Admin user not found")
