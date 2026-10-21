from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0132_checkoutattempt_resolution_remark_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="checkoutattempt",
            old_name="resolution_remark",
            new_name="shop_remark",
        ),
        migrations.RenameField(
            model_name="checkoutattempt",
            old_name="resolved_at",
            new_name="remark_updated_at",
        ),
        migrations.RenameField(
            model_name="checkoutattempt",
            old_name="resolved_by",
            new_name="remark_updated_by",
        ),
        migrations.AlterField(
            model_name="checkoutattempt",
            name="remark_updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="updated_checkout_remarks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
