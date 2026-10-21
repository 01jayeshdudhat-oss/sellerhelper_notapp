from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0094_marketplace_layout_sizing"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitethemesettings",
            name="component_layouts",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]