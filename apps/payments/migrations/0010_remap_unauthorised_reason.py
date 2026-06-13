"""Remap the old American reason code to PayPal's exact British value."""
from django.db import migrations


def to_unauthorised(apps, schema_editor):
    Dispute = apps.get_model('payments', 'Dispute')
    Dispute.objects.filter(dispute_reason='UNAUTHORIZED_TRANSACTION').update(
        dispute_reason='UNAUTHORISED')


def to_unauthorized(apps, schema_editor):
    Dispute = apps.get_model('payments', 'Dispute')
    Dispute.objects.filter(dispute_reason='UNAUTHORISED').update(
        dispute_reason='UNAUTHORIZED_TRANSACTION')


class Migration(migrations.Migration):
    dependencies = [
        ('payments', '0009_alter_dispute_dispute_reason'),
    ]
    operations = [
        migrations.RunPython(to_unauthorised, to_unauthorized),
    ]
