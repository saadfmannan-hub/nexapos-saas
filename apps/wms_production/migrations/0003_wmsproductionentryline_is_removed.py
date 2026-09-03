from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "wms_production",
            "0002_remove_wmsproductionentryline_uniq_wms_prod_entry_assignment_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="wmsproductionentryline",
            name="is_removed",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
