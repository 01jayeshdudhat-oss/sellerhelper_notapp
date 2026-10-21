from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0093_marketplace_theme_builder"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_content_width",
            field=models.PositiveSmallIntegerField(
                default=2400,
                validators=[MinValueValidator(800), MaxValueValidator(3000)],
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_product_width",
            field=models.PositiveSmallIntegerField(
                default=190,
                validators=[MinValueValidator(120), MaxValueValidator(320)],
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_desktop_padding",
            field=models.PositiveSmallIntegerField(
                default=16,
                validators=[MaxValueValidator(48)],
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_mobile_padding",
            field=models.PositiveSmallIntegerField(
                default=2,
                validators=[MaxValueValidator(24)],
            ),
        ),
    ]
