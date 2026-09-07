from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0003_contribution"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="contribution",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ("void_reason", ""),
                        ("voided_at__isnull", True),
                        ("voided_by__isnull", True),
                    )
                    | (
                        models.Q(
                            ("voided_at__isnull", False),
                            ("voided_by__isnull", False),
                        )
                        & ~models.Q(("void_reason", ""))
                    )
                ),
                name="contribution_void_requires_audit",
            ),
        ),
    ]
