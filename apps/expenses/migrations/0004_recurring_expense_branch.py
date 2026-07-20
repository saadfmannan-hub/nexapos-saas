"""Add conservative branch ownership to fixed-expense templates."""
import django.db.models.deletion
from django.db import migrations, models


def backfill_reliable_branches(apps, schema_editor):
    Branch = apps.get_model("branches", "Branch")
    Expense = apps.get_model("expenses", "Expense")
    Template = apps.get_model("expenses", "RecurringExpenseTemplate")
    database = schema_editor.connection.alias

    for template in Template.objects.using(database).filter(branch__isnull=True):
        generated_branch_ids = list(
            Expense.objects.using(database)
            .filter(
                recurring_template_id=template.pk,
                branch__business_id=template.business_id,
            )
            .values_list("branch_id", flat=True)
            .distinct()[:2]
        )
        branch_id = (
            generated_branch_ids[0]
            if len(generated_branch_ids) == 1
            else None
        )
        if branch_id is None:
            business_branches = list(
                Branch.objects.using(database)
                .filter(business_id=template.business_id, is_active=True)
                .values_list("id", flat=True)[:2]
            )
            if len(business_branches) == 1:
                branch_id = business_branches[0]
        if branch_id is not None:
            template.branch_id = branch_id
            template.save(update_fields=["branch"])


class Migration(migrations.Migration):
    dependencies = [
        ("branches", "0001_initial"),
        ("expenses", "0003_expense_generated_for_month_recurringexpensetemplate_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringexpensetemplate",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="recurring_expense_templates",
                to="branches.branch",
            ),
        ),
        migrations.RunPython(backfill_reliable_branches, migrations.RunPython.noop),
    ]
