# Generated manually for the contribution ledger.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0002_member_full_name_role"),
    ]

    operations = [
        migrations.CreateModel(
            name="Contribution",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("amount", models.PositiveBigIntegerField()),
                ("received_on", models.DateField(db_index=True)),
                ("note", models.TextField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("void_reason", models.TextField(blank=True, max_length=500)),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contributions",
                        to="app.member",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recorded_contributions",
                        to="app.member",
                    ),
                ),
                (
                    "voided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="voided_contributions",
                        to="app.member",
                    ),
                ),
            ],
            options={
                "ordering": ("-received_on", "-created_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="contribution",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)),
                name="contribution_amount_is_positive",
            ),
        ),
    ]
