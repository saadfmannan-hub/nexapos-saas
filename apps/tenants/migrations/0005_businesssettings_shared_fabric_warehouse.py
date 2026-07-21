import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("branches", "0002_branch_usage_type"),
        ("tenants", "0004_businesssettings_more_option_label_1_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="businesssettings",
            name="shared_fabric_warehouse",
            field=models.ForeignKey(
                blank=True,
                help_text="Workshop warehouse used for shared fabric stock.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="shared_fabric_for_settings",
                to="branches.warehouse",
                verbose_name="Shared Fabric Location",
            ),
        ),
    ]
