from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_product_is_tailoring_item"),
        ("sales", "0004_saleitem_tailoring_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="priority",
            field=models.CharField(
                choices=[
                    ("normal", "Normal"),
                    ("high", "High"),
                    ("urgent", "Urgent"),
                ],
                db_index=True,
                default="normal",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="saleitem",
            name="garment_classification",
            field=models.CharField(
                blank=True,
                choices=[("adult", "Adult"), ("child", "Child")],
                default="",
                max_length=5,
            ),
        ),
        migrations.AddConstraint(
            model_name="sale",
            constraint=models.CheckConstraint(
                condition=models.Q(priority__in=["normal", "high", "urgent"]),
                name="sale_priority_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="saleitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    garment_classification__in=["", "adult", "child"]
                ),
                name="saleitem_classification_valid",
            ),
        ),
    ]
