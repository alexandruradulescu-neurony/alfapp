from django.db import migrations


class Migration(migrations.Migration):
    """Generalize the transient accept-claim guard into one outbound-action mutex
    (also used by the manual supporting-info reply). The column is all-False
    (just added in 0014), so the rename is data-safe."""

    dependencies = [
        ('payments', '0014_dispute_accept_in_flight_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='dispute',
            old_name='accept_in_flight',
            new_name='outbound_in_flight',
        ),
    ]
