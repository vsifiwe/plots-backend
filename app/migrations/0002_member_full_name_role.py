from django.db import migrations, models


def populate_existing_member_details(apps, schema_editor):
    Member = apps.get_model("app", "Member")

    for member in Member.objects.all():
        if not member.full_name:
            if member.email:
                local_part = member.email.split("@", 1)[0]
                member.full_name = local_part.replace(".", " ").replace("_", " ").title()
            else:
                member.full_name = f"Member {member.pk}"
        if member.is_superuser:
            member.role = "steward"
        member.save(update_fields=("full_name", "role"))


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="member",
            name="full_name",
            field=models.CharField(default="", max_length=160),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="member",
            name="role",
            field=models.CharField(
                choices=[("member", "Member"), ("steward", "Steward")],
                default="member",
                max_length=16,
            ),
        ),
        migrations.RunPython(
            populate_existing_member_details,
            migrations.RunPython.noop,
        ),
    ]
