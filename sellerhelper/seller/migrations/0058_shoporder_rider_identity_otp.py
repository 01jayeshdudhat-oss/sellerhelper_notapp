from django.db import migrations, models


def add_rider_identity_otp_column(apps, schema_editor):
    shop_order = apps.get_model("seller", "ShopOrder")
    field = models.CharField(blank=True, default="", max_length=6)
    field.contribute_to_class(shop_order, "rider_identity_otp")

    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor,
                shop_order._meta.db_table,
            )
        }

    if field.column not in columns:
        schema_editor.add_field(shop_order, field)


def remove_rider_identity_otp_column(apps, schema_editor):
    shop_order = apps.get_model("seller", "ShopOrder")
    field = shop_order._meta.get_field("rider_identity_otp")

    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor,
                shop_order._meta.db_table,
            )
        }

    if field.column in columns:
        schema_editor.remove_field(shop_order, field)


class Migration(migrations.Migration):
    dependencies = [
        ("seller", "0057_shoporder_rider_pickup_otp_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_rider_identity_otp_column,
                    remove_rider_identity_otp_column,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="shoporder",
                    name="rider_identity_otp",
                    field=models.CharField(blank=True, default="", max_length=6),
                ),
            ],
        ),
    ]
