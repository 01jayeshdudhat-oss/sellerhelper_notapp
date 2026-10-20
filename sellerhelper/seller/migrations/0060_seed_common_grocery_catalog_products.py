from django.db import migrations


PRODUCTS = {
    "FRUITS": (
        "Mango",
        "Orange",
        "Papaya",
        "Pomegranate",
        "Watermelon",
        "Muskmelon",
        "Grapes",
        "Pineapple",
        "Lemon",
        "Sweet Lime",
        "Coconut",
        "Pear",
        "Chikoo",
        "Strawberry",
        "Kiwi",
    ),
    "SABJI": (
        "Potato",
        "Onion",
        "Tomato",
        "Carrot",
        "Cauliflower",
        "Cabbage",
        "Brinjal",
        "Cucumber",
        "Spinach",
        "Coriander Leaves",
        "Green Chili",
        "Ginger",
        "Garlic",
        "Okra",
        "Bitter Gourd",
        "Ridge Gourd",
        "Pumpkin",
        "Capsicum",
        "Green Peas",
        "Beetroot",
        "Radish",
    ),
    "KIRANA": (
        "Rice",
        "Wheat Flour",
        "Toor Dal",
        "Moong Dal",
        "Chana Dal",
        "Sugar",
        "Salt",
        "Cooking Oil",
    ),
    "DAIRY": (
        "Milk",
        "Curd",
        "Paneer",
        "Butter",
    ),
    "BAKERY": (
        "Bread",
        "Eggs",
    ),
}


def seed_common_products(apps, schema_editor):
    ProductCategory = apps.get_model("seller", "ProductCategory")
    AdminProductCatalog = apps.get_model("seller", "AdminProductCatalog")
    database = schema_editor.connection.alias

    for category_name, product_names in PRODUCTS.items():
        ProductCategory.objects.using(database).get_or_create(name=category_name)
        existing_names = {
            name.casefold()
            for name in AdminProductCatalog.objects.using(database)
            .filter(product_category=category_name)
            .values_list("product_name", flat=True)
        }
        for product_name in product_names:
            if product_name.casefold() in existing_names:
                continue
            AdminProductCatalog.objects.using(database).create(
                product_category=category_name,
                product_name=product_name,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("seller", "0059_shoporder_rider_identity_state"),
    ]

    operations = [
        migrations.RunPython(
            seed_common_products,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
