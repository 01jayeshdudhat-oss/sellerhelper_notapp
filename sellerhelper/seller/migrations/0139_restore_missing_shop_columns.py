from django.db import migrations


def restore_missing_shop_columns(apps, schema_editor):
    shop_model = apps.get_model("seller", "Shop")
    table_name = shop_model._meta.db_table
    existing_columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            table_name,
        )
    }

    for field_name in (
        "public_rider_network_enabled",
        "rider_delivery_preference",
        "pickup_locations",
    ):
        field = shop_model._meta.get_field(field_name)
        if field.column in existing_columns:
            continue

        if schema_editor.connection.vendor == "sqlite":
            definition, params = schema_editor.column_sql(
                shop_model,
                field,
                include_default=True,
            )
            check = field.db_parameters(schema_editor.connection).get("check")
            if check:
                definition = f"{definition} CHECK {check}"
            schema_editor.execute(
                "ALTER TABLE {} ADD COLUMN {} {}".format(
                    schema_editor.quote_name(table_name),
                    schema_editor.quote_name(field.column),
                    definition,
                ),
                params,
            )
        else:
            schema_editor.add_field(shop_model, field)
        existing_columns.add(field.column)


class Migration(migrations.Migration):
    dependencies = [
        ("seller", "0138_shop_panorama_image"),
    ]

    operations = [
        migrations.RunPython(
            restore_missing_shop_columns,
            migrations.RunPython.noop,
        ),
    ]