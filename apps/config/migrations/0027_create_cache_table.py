"""Create the database cache table backing settings.CACHES['default'].

Using a migration (rather than a one-off `createcachetable` deploy step) means
the table exists in dev, test and prod after `migrate`, so per-IP throttle
counters are shared across gunicorn workers everywhere.
"""
from django.db import migrations


def create_cache_table(apps, schema_editor):
    from django.core.management import call_command
    call_command(
        "createcachetable",
        "lora_cache_table",
        database=schema_editor.connection.alias,
        verbosity=0,
    )


def drop_cache_table(apps, schema_editor):
    schema_editor.execute("DROP TABLE IF EXISTS lora_cache_table")


class Migration(migrations.Migration):

    dependencies = [
        ("config", "0026_systemsettings_import_claims_from_email"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]
