from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0005_unit_is_meter"),
        ("sales", "0007_saleitem_collection_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="checkout_token",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="saleitem",
            name="fabric_meter_used",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text=(
                    "Immutable meter quantity entered at POS and used for inventory "
                    "deduction. Null for historical and non-tailoring rows."
                ),
                max_digits=14,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0.001")),
                    django.core.validators.MaxValueValidator(
                        Decimal("99999999999.999")
                    ),
                ],
                verbose_name="POS Fabric Meter Used",
            ),
        ),
        migrations.AddConstraint(
            model_name="sale",
            constraint=models.UniqueConstraint(
                condition=models.Q(checkout_token__isnull=False),
                fields=("business", "checkout_token"),
                name="uniq_sale_checkout_token_per_business",
            ),
        ),
        migrations.AddConstraint(
            model_name="saleitem",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(fabric_meter_used__isnull=True)
                    | models.Q(fabric_meter_used__gt=0)
                ),
                name="saleitem_fabric_meter_positive",
            ),
        ),
    ]
