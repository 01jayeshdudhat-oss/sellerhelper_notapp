from django.db import migrations


CATALOG = {
    "SABJI": {
        "image": "photo-1540420773420-3366772f4999",
        "products": ("Broccoli", "Drumstick", "Sweet Corn", "Sweet Potato", "Raw Banana"),
    },
    "FRUITS": {
        "image": "photo-1619566636858-adf3ef46400b",
        "products": ("Avocado", "Blueberry", "Custard Apple", "Plum", "Peach"),
    },
    "KIRANA": {
        "image": "photo-1542838132-92c53300491e",
        "products": ("Poha", "Besan", "Maida", "Jaggery", "Tea", "Coffee", "Mustard Seeds"),
    },
    "DAIRY": {
        "image": "photo-1550583724-b2692b85b150",
        "products": ("Cheese", "Ghee", "Buttermilk", "Cream", "Flavoured Yogurt"),
    },
    "BAKERY": {
        "image": "photo-1509440159596-0249088772ff",
        "products": ("Bun", "Pav", "Rusk", "Cookies", "Cake"),
    },
    "KHANA KHAJANA": {
        "image": "photo-1504674900247-0877df9cc836",
        "products": ("Veg Thali", "Paneer Curry", "Dal Rice", "Samosa", "Masala Dosa"),
    },
    "STATIONARY AND BOOKS": {
        "image": "photo-1455390582262-044cdead277a",
        "products": ("Notebook", "Ball Pen", "Pencil Set", "School Textbook", "Drawing Book"),
    },
    "BIKE & CAR NEW": {
        "image": "photo-1558981806-ec527fa84c39",
        "products": ("New Bicycle", "New Scooter", "New Motorcycle", "New Car", "New Electric Scooter"),
    },
    "BIKE & CAR SECOND HAND": {
        "image": "photo-1503376780353-7e6692767b70",
        "products": ("Used Bicycle", "Used Scooter", "Used Motorcycle", "Used Hatchback Car"),
    },
    "MOBILE & LAPTOP": {
        "image": "photo-1511707171634-5f897ff02aa9",
        "products": ("Android Smartphone", "iPhone", "Laptop", "Tablet", "Wireless Earbuds"),
    },
    "ELECTRONIC": {
        "image": "photo-1518770660439-4636190af475",
        "products": ("Bluetooth Speaker", "Television", "Power Bank", "Digital Camera", "Wi-Fi Router"),
    },
    "ELECTRIC": {
        "image": "photo-1473341304170-971dccb5ac1e",
        "products": ("Electric Kettle", "Ceiling Fan", "LED Bulb", "Mixer Grinder", "Extension Board"),
    },
    "UTENSIL": {
        "image": "photo-1556911220-bff31c812dba",
        "products": ("Stainless Steel Plate", "Cooking Pot", "Frying Pan", "Pressure Cooker", "Water Bottle"),
    },
    "CLENING": {
        "image": "photo-1585421514738-01798e348b17",
        "products": ("Floor Cleaner", "Dishwashing Liquid", "Laundry Detergent", "Toilet Cleaner", "Cleaning Sponge"),
    },
    "ELECTRICAL": {
        "image": "photo-1621905251189-08b45d6a269e",
        "products": ("Electrical Wire", "Power Socket", "Circuit Breaker", "Switch Board", "LED Tube Light"),
    },
    "CLENING SERVICE": {
        "image": "photo-1581578731548-c64695cc6952",
        "products": ("Home Deep Cleaning", "Kitchen Cleaning", "Bathroom Cleaning", "Sofa Cleaning"),
    },
    "BEAUTY MAKEUP SERVICE": {
        "image": "photo-1522335789203-aabd1fc54bc9",
        "products": ("Haircut and Styling", "Bridal Makeup", "Facial", "Manicure and Pedicure"),
    },
    "PHSYO THERAPY": {
        "image": "photo-1576091160399-112ba8d25d1d",
        "products": ("Physiotherapy Consultation", "Back Pain Therapy", "Sports Injury Therapy", "Post-Surgery Rehabilitation"),
    },
    "MEDICAL LABORATORY SERVICES": {
        "image": "photo-1579684385127-1ef15d508118",
        "products": ("Complete Blood Count Test", "Blood Sugar Test", "Thyroid Profile Test", "Lipid Profile Test"),
    },
    "HELTH CHECKUP SERVICES": {
        "image": "photo-1505751172876-fa1923c5c528",
        "products": ("Basic Health Checkup", "Full Body Checkup", "Heart Health Checkup", "Senior Citizen Checkup"),
    },
    "AUTO GARAGE SERVICES": {
        "image": "photo-1486262715619-67b85e0b08d3",
        "products": ("Car General Service", "Two-Wheeler Service", "Puncture Repair", "Car Battery Check"),
    },
    "TRANSPORT SERVICES": {
        "image": "photo-1492144534655-ae79c964c9d7",
        "products": ("Local Goods Transport", "Mini Truck Rental", "Two-Wheeler Towing", "Local Parcel Delivery"),
    },
    "OTHER": {
        "image": "photo-1523275335684-37898b6baf30",
        "products": ("Wrist Watch", "Umbrella", "Travel Bag", "Gift Item", "Reusable Shopping Bag"),
    },
}


def image_url(photo_id):
    return (
        f"https://images.unsplash.com/{photo_id}"
        "?auto=format&fit=crop&w=640&q=80"
    )


def populate_catalog(apps, schema_editor):
    ProductCategory = apps.get_model("seller", "ProductCategory")
    AdminProductCatalog = apps.get_model("seller", "AdminProductCatalog")
    database = schema_editor.connection.alias

    for category_name, category_data in CATALOG.items():
        ProductCategory.objects.using(database).get_or_create(name=category_name)
        category_image = image_url(category_data["image"])

        products = AdminProductCatalog.objects.using(database).filter(
            product_category=category_name,
        )
        for product in products.filter(image_url="").iterator():
            product.image_url = category_image
            product.save(using=database, update_fields=["image_url"])

        existing_names = {
            name.casefold()
            for name in products.values_list("product_name", flat=True)
        }
        for product_name in category_data["products"]:
            if product_name.casefold() in existing_names:
                continue
            AdminProductCatalog.objects.using(database).create(
                product_category=category_name,
                product_name=product_name,
                image_url=category_image,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("seller", "0060_seed_common_grocery_catalog_products"),
    ]

    operations = [
        migrations.RunPython(
            populate_catalog,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
