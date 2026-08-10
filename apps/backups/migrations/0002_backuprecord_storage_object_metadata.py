from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("backups", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="backuprecord",
            name="storage_bucket_identifier",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="backuprecord",
            name="storage_object_version_identifier",
            field=models.CharField(blank=True, max_length=1024),
        ),
    ]
