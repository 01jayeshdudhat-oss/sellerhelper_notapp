from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models
from django.utils.text import slugify


def populate_storefront_slugs(apps, schema_editor):
    Shop = apps.get_model("seller", "Shop")
    reserved = {
        "www", "api", "admin", "accounts", "shop", "market", "static",
        "media", "mail", "support", "help", "app", "blog", "cdn",
    }
    for shop in Shop.objects.order_by("pk").iterator():
        base_slug = slugify(shop.name)[:50].strip("-") or "shop"
        if base_slug in reserved:
            base_slug = f"{base_slug[:58]}-shop"
        candidate = base_slug
        suffix = 2
        while Shop.objects.filter(storefront_slug=candidate).exclude(pk=shop.pk).exists():
            suffix_text = f"-{suffix}"
            candidate = f"{base_slug[:63 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        Shop.objects.filter(pk=shop.pk).update(storefront_slug=candidate)


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0069_remove_deliverysharingsettings_shared_rider_maximum_pay_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="shop",
            name="storefront_slug",
            field=models.SlugField(
                blank=True,
                max_length=63,
                null=True,
                unique=True,
                help_text="Unique shop subdomain selected by the shop owner.",
            ),
        ),
        migrations.AddField(
            model_name="shop",
            name="storefront_subscription_active",
            field=models.BooleanField(
                default=False,
                help_text="Enable after storefront subscription payment has been confirmed.",
            ),
        ),
        migrations.AddField(
            model_name="shop",
            name="storefront_subscription_expires_at",
            field=models.DateField(
                blank=True,
                null=True,
                help_text="Optional subscription end date. Empty means no scheduled expiry.",
            ),
        ),
        migrations.RunPython(populate_storefront_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="shop",
            name="storefront_slug",
            field=models.SlugField(
                blank=True,
                max_length=63,
                unique=True,
                help_text="Unique shop subdomain selected by the shop owner.",
            ),
        ),
        migrations.CreateModel(
            name="ShopStorefrontSettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "monthly_subscription_price",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0.00"),
                        max_digits=10,
                        validators=[MinValueValidator(Decimal("0.00"))],
                        help_text="Monthly storefront subscription price. Billing is managed outside the app.",
                    ),
                ),
                ("android_app_download_url", models.URLField(blank=True, default="")),
                ("ios_app_download_url", models.URLField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "shop storefront settings",
                "verbose_name_plural": "shop storefront settings",
            },
        ),
    ]
