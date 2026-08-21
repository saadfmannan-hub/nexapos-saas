from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0009_saleitem_stock_warehouse"),
    ]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="customer_supplied_fabric",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Immutable indication that the customer supplied the material "
                    "and no business-owned fabric inventory was consumed."
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="saleitem",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(customer_supplied_fabric=False)
                    | models.Q(fabric_meter_used__isnull=True)
                ),
                name="saleitem_customer_fabric_no_meter",
            ),
        ),
    ]
