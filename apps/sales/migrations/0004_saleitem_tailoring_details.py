from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0003_remove_invoicesequence_uniq_invoice_sequence_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="tailoring_details",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
