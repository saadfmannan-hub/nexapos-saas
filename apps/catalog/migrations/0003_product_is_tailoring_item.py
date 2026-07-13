from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="is_tailoring_item",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Require garment classification and a delivery date when this "
                    "product is sold through POS."
                ),
            ),
        ),
    ]
