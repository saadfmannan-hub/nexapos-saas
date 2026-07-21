import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("branches", "0002_branch_usage_type"),
        ("sales", "0008_sale_checkout_token_saleitem_fabric_meter_used"),
    ]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="stock_warehouse",
            field=models.ForeignKey(
                blank=True,
                help_text="Physical warehouse used for this line's stock movement.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sale_items_stocked",
                to="branches.warehouse",
            ),
        ),
    ]
