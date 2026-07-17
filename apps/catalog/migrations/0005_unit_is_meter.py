from django.db import migrations, models
from django.db.models import Q


def mark_canonical_meter_units(apps, schema_editor):
    Unit = apps.get_model("catalog", "Unit")
    Unit.objects.filter(
        Q(name__iexact="meter")
        | Q(name__iexact="metre")
        | Q(abbreviation__iexact="m")
    ).update(is_meter=True)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_seed_demo_tailoring"),
        ("catalog", "0004_product_estimated_adult_fabric_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="unit",
            name="is_meter",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(mark_canonical_meter_units, migrations.RunPython.noop),
    ]
