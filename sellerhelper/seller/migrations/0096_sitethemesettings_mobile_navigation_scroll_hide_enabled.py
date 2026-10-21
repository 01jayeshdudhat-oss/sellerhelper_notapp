from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0095_sitethemesettings_layout_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitethemesettings",
            name="mobile_navigation_scroll_hide_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
