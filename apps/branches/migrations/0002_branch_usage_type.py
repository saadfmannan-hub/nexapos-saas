from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("branches", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="usage_type",
            field=models.CharField(
                choices=[
                    ("sales_branch", "Sales Branch"),
                    ("workshop_stock", "Workshop / Stock Location"),
                ],
                default="sales_branch",
                max_length=20,
            ),
        ),
    ]
