from django.db import migrations


class Migration(migrations.Migration):
    """Remove the manager/agent role split — one trusted user type, gated by
    authentication only. Existing users (all managers in practice) are unaffected
    apart from losing the now-meaningless column."""

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='user',
            name='role',
        ),
    ]
