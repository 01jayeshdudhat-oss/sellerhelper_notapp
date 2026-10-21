from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0086_codpaymentapprovalrequest_deferred_returns"),
    ]

    operations = [
        migrations.AddField(
            model_name="adminproductcatalog",
            name="image_url_2",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="adminproductcatalog",
            name="image_url_3",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="adminproductcatalog",
            name="image_url_4",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="adminproductcatalog",
            name="image_url_5",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
    ]
