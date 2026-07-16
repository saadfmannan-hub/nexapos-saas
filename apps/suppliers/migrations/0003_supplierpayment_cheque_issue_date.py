from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("suppliers", "0002_supplierpayment_bank_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierpayment",
            name="cheque_issue_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
