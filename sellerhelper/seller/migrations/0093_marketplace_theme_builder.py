from django.core.validators import MaxValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0092_sitethemesettings_homepage_feature_1_text_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_hero_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_nearby_shops_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_category_nav_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_products_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_hero_title",
            field=models.CharField(default="Marketplace", max_length=120),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_hero_text",
            field=models.CharField(
                default="Find products from nearby shops",
                max_length=240,
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_section_order",
            field=models.CharField(
                default="hero,search,nearby,category_nav,products",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_category_order",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_featured_product_ids",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_primary_color",
            field=models.CharField(default="#146c5b", max_length=7),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_accent_color",
            field=models.CharField(default="#16835f", max_length=7),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_header_start_color",
            field=models.CharField(default="#123b65", max_length=7),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_header_end_color",
            field=models.CharField(default="#208b72", max_length=7),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_card_radius",
            field=models.PositiveSmallIntegerField(
                default=18,
                validators=[MaxValueValidator(32)],
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_section_gap",
            field=models.PositiveSmallIntegerField(
                default=22,
                validators=[MaxValueValidator(64)],
            ),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="marketplace_product_gap",
            field=models.PositiveSmallIntegerField(
                default=14,
                validators=[MaxValueValidator(32)],
            ),
        ),
    ]
