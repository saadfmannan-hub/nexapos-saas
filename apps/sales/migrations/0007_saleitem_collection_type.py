from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0006_saleitem_actual_fabric_used_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="collection_type",
            field=models.CharField(
                blank=True,
                choices=[("normal", "Normal"), ("premium", "Premium")],
                default="",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="saleitem",
            constraint=models.CheckConstraint(
                condition=models.Q(collection_type__in=["", "normal", "premium"]),
                name="saleitem_collection_type_valid",
            ),
        ),
    ]
