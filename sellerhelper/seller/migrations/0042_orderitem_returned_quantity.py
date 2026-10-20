from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0041_adminproductcatalog"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="returned_quantity",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
