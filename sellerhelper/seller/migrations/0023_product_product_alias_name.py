from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0022_product_product_category_shop_market_category_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="product_alias_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
