from django.db import migrations, models


RIDER_IDENTITY_FIELDS = (
    (
        "rider_identity_otp_expires_at",
        models.DateTimeField(blank=True, null=True),
    ),
    (
        "rider_identity_otp_attempts",
        models.PositiveSmallIntegerField(default=0),
    ),
    (
        "rider_order_acknowledged",
        models.BooleanField(default=False),
    ),
)


def add_missing_rider_identity_fields(apps, schema_editor):
    shop_order = apps.get_model("seller", "ShopOrder")

    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor,
                shop_order._meta.db_table,
            )
        }

    for name, field in RIDER_IDENTITY_FIELDS:
        if name not in columns:
            field = field.clone()
            field.contribute_to_class(shop_order, name)
            schema_editor.add_field(shop_order, field)


def remove_rider_identity_fields(apps, schema_editor):
    shop_order = apps.get_model("seller", "ShopOrder")

    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor,
                shop_order._meta.db_table,
            )
        }

    for name, _field in reversed(RIDER_IDENTITY_FIELDS):
        field = shop_order._meta.get_field(name)
        if field.column in columns:
            schema_editor.remove_field(shop_order, field)


class Migration(migrations.Migration):
    dependencies = [
        ("seller", "0058_shoporder_rider_identity_otp"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_missing_rider_identity_fields,
                    remove_rider_identity_fields,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="shoporder",
                    name=name,
                    field=field,
                )
                for name, field in RIDER_IDENTITY_FIELDS
            ],
        ),
    ]
