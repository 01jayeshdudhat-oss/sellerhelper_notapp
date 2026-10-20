import math
import re
import secrets
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from .private_storage import PrivateOrderAudioStorage

User = get_user_model()

SHOP_STOREFRONT_RESERVED_SLUGS = frozenset({
    "www", "api", "admin", "accounts", "shop", "market", "static",
    "media", "mail", "support", "help", "app", "blog", "cdn",
})

INDIAN_GST_STATE_CHOICES = [
    ("01", "Jammu and Kashmir"),
    ("02", "Himachal Pradesh"),
    ("03", "Punjab"),
    ("04", "Chandigarh"),
    ("05", "Uttarakhand"),
    ("06", "Haryana"),
    ("07", "Delhi"),
    ("08", "Rajasthan"),
    ("09", "Uttar Pradesh"),
    ("10", "Bihar"),
    ("11", "Sikkim"),
    ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"),
    ("14", "Manipur"),
    ("15", "Mizoram"),
    ("16", "Tripura"),
    ("17", "Meghalaya"),
    ("18", "Assam"),
    ("19", "West Bengal"),
    ("20", "Jharkhand"),
    ("21", "Odisha"),
    ("22", "Chhattisgarh"),
    ("23", "Madhya Pradesh"),
    ("24", "Gujarat"),
    ("25", "Daman and Diu"),
    ("26", "Dadra and Nagar Haveli and Daman and Diu"),
    ("27", "Maharashtra"),
    ("28", "Andhra Pradesh"),
    ("29", "Karnataka"),
    ("30", "Goa"),
    ("31", "Lakshadweep"),
    ("32", "Kerala"),
    ("33", "Tamil Nadu"),
    ("34", "Puducherry"),
    ("35", "Andaman and Nicobar Islands"),
    ("36", "Telangana"),
    ("37", "Andhra Pradesh"),
    ("38", "Ladakh"),
]

PRODUCT_CATEGORY_CHOICES = [
    ("SABJI", "SABJI"),
    ("FRUITS", "FRUITS"),
    ("KIRANA", "KIRANA"),
    ("DAIRY", "DAIRY"),
    ("BAKERY", "BAKERY"),
    ("KHANA KHAJANA", "KHANA KHAJANA"),
    ("STATIONARY AND BOOKS", "STATIONARY AND BOOKS"),
    ("BIKE & CAR NEW", "BIKE & CAR NEW"),
    ("BIKE & CAR SECOND HAND", "BIKE & CAR SECOND HAND"),
    ("MOBILE & LAPTOP", "MOBILE & LAPTOP"),
    ("ELECTRONIC", "ELECTRONIC"),
    ("ELECTRIC", "ELECTRIC"),
    ("UTENSIL", "UTENSIL"),
    ("CLENING", "CLENING"),
    ("ELECTRICAL", "ELECTRICAL"),
    ("CLENING SERVICE", "CLENING SERVICE"),
    ("BEAUTY MAKEUP SERVICE", "BEAUTY MAKEUP SERVICE"),
    ("PHSYO THERAPY", "PHSYO THERAPY"),
    ("MEDICAL LABORATORY SERVICES", "MEDICAL LABORATORY SERVICES"),
    ("HELTH CHECKUP SERVICES", "HELTH CHECKUP SERVICES"),
    ("AUTO GARAGE SERVICES", "AUTO GARAGE SERVICES"),
    ("TRANSPORT SERVICES", "TRANSPORT SERVICES"),
    ("OTHER", "OTHER"),
]

class ProductCategory(models.Model):
    name = models.CharField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "product categories"

    def __str__(self):
        return self.name


def get_product_category_choices(include_inactive=False):
    categories = list(
        ProductCategory.objects.order_by("sort_order", "name").values_list(
            "name",
            "is_active",
        )
    )
    database_names = {category[0] for category in categories}
    choices = [
        (name, name)
        for name, is_active in categories
        if is_active or include_inactive
    ]
    choices.extend(
        (name, label)
        for name, label in PRODUCT_CATEGORY_CHOICES
        if name not in database_names
    )
    return choices


def generate_otp_code():
    used_codes = set()
    for model in (ClientProfile, ShopStaff):
        used_codes.update(
            model.objects.exclude(verification_otp__in=["", "0000"])
            .values_list("verification_otp", flat=True)
        )
    available_codes = [
        f"{code:04d}"
        for code in range(1000, 10000)
        if f"{code:04d}" not in used_codes
    ]
    if not available_codes:
        raise RuntimeError("No unique four-digit verification codes remain.")
    return secrets.choice(available_codes)


class Shop(models.Model):
    MARKET_CATEGORY_CHOICES = [
        ("DAILY MARKET", "DAILY MARKET"),
        ("OCCATIONAL MARKET", "OCCATIONAL MARKET"),
        ("SERVICE MARKET", "SERVICE MARKET"),
    ]

    SHOP_CATEGORY_CHOICES = [
        ("SABJI", "SABJI"),
        ("FRUITS", "FRUITS"),
        ("KIRANA", "KIRANA"),
        ("DAIRY", "DAIRY"),
        ("BAKERY", "BAKERY"),
        ("KHANA KHAJANA", "KHANA KHAJANA"),
        ("STATIONARY AND BOOKS", "STATIONARY AND BOOKS"),
        ("BIKE & CAR NEW", "BIKE & CAR NEW"),
        ("BIKE & CAR SECOND HAND", "BIKE & CAR SECOND HAND"),
        ("MOBILE & LAPTOP", "MOBILE & LAPTOP"),
        ("ELECTRONIC", "ELECTRONIC"),
        ("ELECTRIC", "ELECTRIC"),
        ("UTENSIL", "UTENSIL"),
        ("CLENING", "CLENING"),
        ("ELECTRICAL", "ELECTRICAL"),
        ("CLENING SERVICE", "CLENING SERVICE"),
        ("BEAUTY MAKEUP SERVICE", "BEAUTY MAKEUP SERVICE"),
        ("PHSYO THERAPY", "PHSYO THERAPY"),
        ("MEDICAL LABORATORY SERVICES", "MEDICAL LABORATORY SERVICES"),
        ("HELTH CHECKUP SERVICES", "HELTH CHECKUP SERVICES"),
        ("AUTO GARAGE SERVICES", "AUTO GARAGE SERVICES"),
        ("TRANSPORT SERVICES", "TRANSPORT SERVICES"),
        ("OTHER", "OTHER"),
    ]

    name = models.CharField(max_length=200)
    daily_order_counter_date = models.DateField(null=True, blank=True, editable=False)
    daily_order_counter = models.PositiveBigIntegerField(default=0, editable=False)
    storefront_slug = models.SlugField(
        max_length=63,
        unique=True,
        blank=True,
        help_text="Unique shop subdomain selected by the shop owner.",
    )
    storefront_subscription_active = models.BooleanField(
        default=False,
        help_text="Enable after storefront subscription payment has been confirmed.",
    )
    storefront_subscription_expires_at = models.DateField(
        null=True,
        blank=True,
        help_text="Optional subscription end date. Empty means no scheduled expiry.",
    )

    market_category = models.CharField(
        max_length=50,
        choices=MARKET_CATEGORY_CHOICES,
        default="DAILY MARKET",
        blank=True,
    )

    shop_category = models.CharField(
        max_length=80,
        choices=SHOP_CATEGORY_CHOICES,
        default="OTHER",
        blank=True,
    )

    alias_name = models.CharField(
        max_length=200,
        blank=True,
        default=""
    )

    logo_url = models.URLField(max_length=1000, blank=True, default="")
    cover_image_url = models.URLField(max_length=1000, blank=True, default="")
    allow_product_image_overrides = models.BooleanField(
        default=False,
        help_text="Allow this shop's owner and authorized product managers to override catalog images.",
    )
    allow_owner_create_shared_rider_groups = models.BooleanField(
        default=False,
        help_text="Allow this shop's owner to create and manage shared-rider groups.",
    )

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="shops",
        null=True,
        blank=True
    )

    # Example:
    # {
    #     "lat": 23.0225,
    #     "lng": 72.5714
    # }
    location = models.JSONField(
        blank=True,
        null=True
    )

    # Multiple polygons
    #
    # Example:
    # [
    #   [
    #       {"lat": 23.02, "lng": 72.57},
    #       {"lat": 23.03, "lng": 72.58},
    #       ...
    #   ],
    #   [
    #       {"lat": 23.01, "lng": 72.56},
    #       ...
    #   ]
    # ]
    delivery_area = models.JSONField(
        blank=True,
        null=True
    )

    # Example:
    # ["380001", "380002", "380005"]
    delivery_pincode = models.JSONField(
        blank=True,
        null=True
    )

    contact_no = models.CharField(
        max_length=40,
        blank=True,
        default=""
    )

    email_no = models.EmailField(
        blank=True,
        default=""
    )
    upi_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        validators=[
            RegexValidator(
                regex=r"^[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}$",
                message="Enter a valid UPI ID, such as shop@upi.",
            ),
        ],
    )

    billing_address = models.TextField(blank=True, default="")
    gstin = models.CharField(max_length=15, blank=True, default="")
    delivery_fee = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Per-order delivery fee. Leave at 0 for free delivery.",
    )
    delivery_hsn_code = models.CharField(
        max_length=8,
        blank=True,
        default="",
        validators=[
            RegexValidator(
                regex=r"^\d{4}(?:\d{2}){0,2}$",
                message="Enter a 4, 6, or 8 digit HSN/SAC code.",
            )
        ],
    )
    delivery_gst_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        default=None,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
        help_text="Leave blank to use the platform delivery GST rate.",
    )
    gst_state_code = models.CharField(
        max_length=2,
        choices=INDIAN_GST_STATE_CHOICES,
        blank=True,
        default="",
    )

    is_shop_open = models.BooleanField(
        default=True
    )

    allow_scheduled_delivery = models.BooleanField(
        default=False,
        help_text=(
            "Keep this shop's in-stock products visible in the marketplace while "
            "the shop is closed and tell customers delivery will be scheduled."
        ),
    )

    share_riders_with_nearby_shops = models.BooleanField(
        default=False,
        help_text=(
            "Allow active riders from nearby shops to accept this shop's "
            "ready orders. The platform administrator controls the sharing radius."
        ),
    )

    require_customer_otp_verification = models.BooleanField(
        default=False
    )

    delivery_eta_reminder_minutes = models.PositiveSmallIntegerField(
        default=3,
        help_text="Notify customers this many minutes before the estimated delivery time.",
    )

    rider_nearby_notification_meters = models.PositiveSmallIntegerField(
        default=100,
        help_text="Notify customers when their rider is within this many meters.",
    )

    razorpay_contact_id = models.CharField(max_length=80, blank=True, default="")
    razorpay_fund_account_id = models.CharField(max_length=80, blank=True, default="")

    shop_open_time = models.TimeField(
        blank=True,
        null=True
    )

    shop_close_time = models.TimeField(
        blank=True,
        null=True
    )

    # Example:
    # [
    #     {
    #         "start": "13:00",
    #         "end": "14:00"
    #     },
    #     {
    #         "start": "17:00",
    #         "end": "17:30"
    #     }
    # ]
    shop_break_time_list = models.JSONField(
        blank=True,
        null=True
    )

    shop_data = models.TextField(
        blank=True,
        default=""
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    @property
    def has_valid_gstin(self):
        gstin = (self.gstin or "").strip().upper()
        return bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin))

    def save(self, *args, **kwargs):
        if not self.storefront_slug:
            self.storefront_slug = f"shop-{uuid.uuid4().hex[:12]}"
        else:
            self.storefront_slug = self.storefront_slug.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ShopStorefrontSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    monthly_subscription_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Monthly storefront subscription price. Billing is managed outside the app.",
    )
    android_app_download_url = models.URLField(blank=True, default="")
    ios_app_download_url = models.URLField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "shop storefront settings"
        verbose_name_plural = "shop storefront settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "Shop storefront settings"


class MarketplaceLayoutSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    products_per_row = models.PositiveSmallIntegerField(
        default=4,
        validators=[MinValueValidator(1), MaxValueValidator(8)],
        help_text="Number of products visible across each mobile category row.",
    )
    rows_per_category = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(4)],
        help_text="Number of independently scrollable rows per mobile category.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "marketplace layout settings"
        verbose_name_plural = "marketplace layout settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "Marketplace layout settings"


class SiteBrandingSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    site_name = models.CharField(max_length=120, default="Seller Helper")
    tagline = models.CharField(max_length=250, blank=True, default="")
    support_email = models.EmailField(blank=True, default="")
    support_phone = models.CharField(max_length=40, blank=True, default="")
    support_address = models.TextField(blank=True, default="")
    logo = models.FileField(upload_to="site-branding/", blank=True)
    app_icon = models.FileField(upload_to="site-branding/", blank=True)
    favicon = models.FileField(upload_to="site-branding/", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "site branding settings"
        verbose_name_plural = "site branding settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "Site branding settings"


class DeliverySharingSettings(models.Model):
    id = models.PositiveSmallIntegerField(
        primary_key=True,
        default=1,
        editable=False,
    )
    shared_rider_radius_km = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.35"),
        validators=[
            MinValueValidator(Decimal("0.05")),
            MaxValueValidator(Decimal("10.00")),
        ],
        help_text=(
            "Maximum distance from the rider's latest accepted pickup for "
            "neighboring shared-shop coverage."
        ),
    )
    customer_direction_radius_km = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.65"),
        validators=[
            MinValueValidator(Decimal("0.05")),
            MaxValueValidator(Decimal("10.00")),
        ],
        help_text=(
            "Additional forward distance toward the customer for grouping "
            "same-order shop pickups."
        ),
    )
    shared_rider_rate_per_km = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("10.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    shared_rider_base_pay = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("10.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    packer_monthly_base_salary = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    packer_completed_order_bonus = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    rider_monthly_base_salary = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    rider_completed_order_bonus = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1),
                name="delivery_sharing_settings_singleton",
            ),
        ]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Shared rider radius: {self.shared_rider_radius_km} km"


class SharedRiderGroup(models.Model):
    name = models.CharField(max_length=120)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shared_rider_groups",
    )
    created_from_shop = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="created_shared_rider_groups",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class SharedRiderGroupShop(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending owner approval"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    group = models.ForeignKey(
        SharedRiderGroup,
        on_delete=models.CASCADE,
        related_name="shop_memberships",
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="shared_rider_group_memberships",
    )
    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shared_rider_group_shop_requests",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_shared_rider_group_shop_requests",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["group__name", "shop__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "shop"],
                name="unique_shop_per_shared_rider_group",
            ),
        ]

    def __str__(self):
        return f"{self.shop.name} · {self.group.name} · {self.status}"


# class ShopStaff(models.Model):

#     ROLE_CHOICES = [
#         ("PACKER", "Packer"),
#         ("ORDER_MANAGER", "Order Manager"),
#         ("SHOP_ADMIN", "Shop Admin"),
#     ]

#     shop = models.ForeignKey(
#         Shop,
#         on_delete=models.CASCADE,
#         related_name="staff_members"
#     )

#     user = models.ForeignKey(
#         User,
#         on_delete=models.CASCADE,
#         related_name="shop_staff_members"
#     )

#     role = models.CharField(
#         max_length=30,
#         choices=ROLE_CHOICES
#     )

#     is_active = models.BooleanField(
#         default=True
#     )

#     can_view_orders = models.BooleanField(
#         default=True
#     )

#     can_confirm_orders = models.BooleanField(
#         default=False
#     )

#     can_pack_orders = models.BooleanField(
#         default=False
#     )

#     can_manage_products = models.BooleanField(
#         default=False
#     )

#     can_manage_stock = models.BooleanField(
#         default=False
#     )

#     can_cancel_orders = models.BooleanField(
#         default=False
#     )

#     can_assign_staff = models.BooleanField(
#         default=False
#     )

#     can_view_reports = models.BooleanField(
#         default=False
#     )

#     created_at = models.DateTimeField(
#         auto_now_add=True
#     )

#     updated_at = models.DateTimeField(
#         auto_now=True
#     )

#     class Meta:
#         constraints = [
#             models.UniqueConstraint(
#                 fields=["shop", "user"],
#                 name="unique_shop_staff"
#             )
#         ]

#     def __str__(self):
#         return f"{self.shop.name} - {self.user.username} - {self.role}"    

class ShopStaff(models.Model):

    ROLE_CHOICES = [
        ("PACKER", "Packer"),
        ("MANAGER", "Manager"),
        ("ORDER", "Order Manager"),
        ("DELIVERY", "Delivery Staff"),
        ("RIDER", "Rider"),
    ]

    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="staff_members"
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="shop_staff"
    )

    role = models.CharField(
        max_length=30,
        choices=ROLE_CHOICES,
        default="PACKER"
    )

    verification_otp = models.CharField(max_length=4, default="0000", blank=True)
    mobile_no = models.CharField(max_length=40, blank=True, default="")
    upi_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        validators=[
            RegexValidator(
                regex=r"^[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}$",
                message="Enter a valid UPI ID, such as name@upi.",
            ),
        ],
    )
    max_load_weight_kg = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Maximum rider load weight in kilograms. Leave blank to disable capacity checks.",
    )
    max_load_volume_cubic_ft = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Maximum rider load volume in cubic feet. Leave blank to disable capacity checks.",
    )
    last_location = models.JSONField(default=dict, blank=True, null=True)

    is_active = models.BooleanField(
        default=True
    )
    can_manage_products = models.BooleanField(
        default=False
    )

    can_assign_staff = models.BooleanField(
        default=False
    )

    can_view_reports = models.BooleanField(
        default=False
    )
    can_view_orders = models.BooleanField(default=True)
    can_confirm_orders = models.BooleanField(default=False)
    can_pack_orders = models.BooleanField(default=False)
    can_change_order_status = models.BooleanField(default=False)
    can_manage_stock = models.BooleanField(default=False)
    can_manage_staff = models.BooleanField(default=False)
    can_cancel_orders = models.BooleanField(default=False)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return f"{self.user.username} - {self.shop.name}"

    def save(self, *args, **kwargs):
        if (
            self.verification_otp == "0000"
            or not self.verification_otp
            or not self.verification_otp.isdigit()
            or len(self.verification_otp) != 4
        ):
            self.verification_otp = generate_otp_code()
        super().save(*args, **kwargs)

class AdminProductCatalog(models.Model):
    product_category = models.CharField(
        max_length=80,
        default="OTHER",
    )
    product_name = models.CharField(max_length=200)
    image_url = models.URLField(max_length=1000, blank=True, default="")
    hsn_code = models.CharField(max_length=8, blank=True, default="")
    gst_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product_category", "product_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["product_category", "product_name"],
                name="unique_admin_catalog_product_per_category",
            ),
        ]

    def __str__(self):
        return f"{self.product_name} ({self.product_category})"


class Product(models.Model):
    PRODUCT_CATEGORY_CHOICES = PRODUCT_CATEGORY_CHOICES
    SEARCH_KEYWORD_SOURCE_FIELDS = (
        "product_name",
        "product_alias_name",
        "product_title",
        "product_description",
        "product_category",
        "product_color",
        "product_size",
        "sku",
        "unit",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="products",
    )

    sku = models.CharField(
        max_length=120,
        blank=True,
    )

    product_category = models.CharField(
        max_length=80,
        default="OTHER",
        blank=True,
    )

    product_name = models.CharField(
        max_length=200,
    )

    product_alias_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    product_title = models.CharField(
        max_length=250,
        blank=True,
    )

    product_color = models.CharField(
        max_length=100,
        blank=True,
    )

    product_size = models.CharField(
        max_length=100,
        blank=True,
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    unit = models.CharField(
        max_length=50,
        blank=True,
    )

    image_url = models.URLField(
        max_length=1000,
        blank=True,
    )

    product_description = models.TextField(
        blank=True,
    )

    search_keywords = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Private source-language and translated search terms. "
            "Superusers may edit this field; listing changes regenerate it."
        ),
    )
    search_keywords_translated = models.BooleanField(default=False, editable=False)

    hsn_code = models.CharField(max_length=8, blank=True, default="")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    expiry_price_drop_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    expiry_management_enabled = models.BooleanField(default=False)
    is_approximate_weight = models.BooleanField(default=False)

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    def __str__(self):
        return f"{self.product_name} ({self.sku})"

    def save(self, *args, regenerate_search_keywords=None, **kwargs):
        if regenerate_search_keywords is None:
            regenerate_search_keywords = self._state.adding
            if not regenerate_search_keywords:
                old_values = (
                    type(self).objects.filter(pk=self.pk)
                    .values(*self.SEARCH_KEYWORD_SOURCE_FIELDS)
                    .first()
                )
                regenerate_search_keywords = old_values is None or any(
                    old_values[field] != getattr(self, field)
                    for field in self.SEARCH_KEYWORD_SOURCE_FIELDS
                )

        if regenerate_search_keywords:
            source_keywords = list(dict.fromkeys(
                value.strip()
                for value in (
                    getattr(self, field)
                    for field in self.SEARCH_KEYWORD_SOURCE_FIELDS
                )
                if value and value.strip()
            ))
            from .offline_keyword_dictionary import lookup_product_keywords

            keyword_matches = lookup_product_keywords(" ".join(source_keywords))
            self.search_keywords = " ".join(
                dict.fromkeys(source_keywords + keyword_matches.keywords)
            )
            self.search_keywords_translated = keyword_matches.dictionary_matched
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                    "search_keywords",
                    "search_keywords_translated",
                }
        super().save(*args, **kwargs)


class LegalAcceptance(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="legal_acceptances",
    )
    document_slug = models.SlugField(max_length=40)
    document_version = models.CharField(max_length=64)
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "document_slug", "document_version"),
                name="unique_user_legal_acceptance_version",
            ),
        ]
        ordering = ("document_slug",)

    def __str__(self):
        return f"{self.user_id}: {self.document_slug} ({self.document_version})"

class ProductShop(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="shop_assignments"
    )

    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="product_assignments"
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True
    )
    b2b_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional business price before GST. B2B customers use it when set.",
    )
    b2b_unit = models.CharField(max_length=40, blank=True, default="")
    bulk_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional B2C bulk price per bulk unit, before GST.",
    )
    bulk_unit = models.CharField(max_length=40, blank=True, default="")
    b2b_bulk_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional B2B bulk price per B2B bulk unit, before GST.",
    )
    b2b_bulk_unit = models.CharField(max_length=40, blank=True, default="")
    bulk_min_quantity = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Minimum number of bulk units per order, shared by B2C and B2B.",
    )

    stock = models.PositiveIntegerField(default=0)
    b2b_stock = models.PositiveIntegerField(
        default=0,
        help_text="Inventory available to B2B customers, separate from regular stock.",
    )
    stocked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    image_url_override = models.URLField(max_length=1000, blank=True, default="")

    is_active = models.BooleanField(default=True)

    @property
    def display_image_url(self):
        if self.shop.allow_product_image_overrides and self.image_url_override:
            return self.image_url_override
        catalog_image = AdminProductCatalog.objects.filter(
            product_category=self.product.product_category,
            product_name=self.product.product_name,
            is_active=True,
        ).values_list("image_url", flat=True).first()
        if catalog_image is not None:
            return catalog_image

        legacy_catalog_image = Product.objects.filter(
            product_category=self.product.product_category,
            product_name=self.product.product_name,
            created_by__is_staff=True,
            is_active=True,
        ).values_list("image_url", flat=True).first()
        return legacy_catalog_image or ""

    ...

    @property
    def effective_price(self):
        base_price = Decimal(str(
            self.price
            if self.price is not None
            else self.product.price
        ))
        if self.product.expiry_management_enabled and self.expires_at:
            return self._price_after_expiry_markdown(base_price)
        return base_price

    def effective_price_for_customer(self, *, is_b2b=False):
        if not is_b2b or not self.shop.has_valid_gstin or self.b2b_price is None:
            return self.effective_price
        base_price = Decimal(self.b2b_price)
        if self.product.expiry_management_enabled and self.expires_at:
            return self._price_after_expiry_markdown(base_price)
        return base_price

    def stock_for_customer(self, *, is_b2b=False):
        return self.b2b_stock if is_b2b else self.stock

    def effective_bulk_price_for_customer(self, *, is_b2b=False):
        base_price = (
            self.b2b_bulk_price
            if is_b2b and self.shop.has_valid_gstin and self.b2b_bulk_price is not None
            else self.bulk_price
        )
        if base_price is None:
            return None
        base_price = Decimal(base_price)
        if self.product.expiry_management_enabled and self.expires_at:
            return self._price_after_expiry_markdown(base_price)
        return base_price

    def unit_for_customer(self, *, is_b2b=False):
        if is_b2b and self.shop.has_valid_gstin and self.b2b_unit:
            return self.b2b_unit
        return self.product.unit

    def bulk_unit_for_customer(self, *, is_b2b=False):
        if (
            is_b2b
            and self.shop.has_valid_gstin
            and self.b2b_bulk_price is not None
            and self.b2b_bulk_unit
        ):
            return self.b2b_bulk_unit
        return self.bulk_unit

    @property
    def suggested_expiry_price(self):
        base_price = Decimal(str(
            self.price
            if self.price is not None
            else self.product.price
        ))
        return self._price_after_expiry_markdown(base_price)

    def _price_after_expiry_markdown(self, base_price):
        if not self.product.expiry_management_enabled or not self.expires_at:
            return base_price
        from django.utils import timezone
        if timezone.now() >= self.expires_at:
            return Decimal("0.00")
        if not self.product.expiry_price_drop_percent:
            return base_price
        shelf_start = self.stocked_at
        if shelf_start is None and self.product.shelf_life_days:
            shelf_start = self.expires_at - timedelta(days=self.product.shelf_life_days)
        if shelf_start is None:
            return base_price
        shelf_duration = self.expires_at - shelf_start
        if shelf_duration.total_seconds() <= 0:
            return base_price
        markdown_start = shelf_start + timedelta(
            seconds=shelf_duration.total_seconds() * 0.70
        )
        now = timezone.now()
        if now <= markdown_start:
            return base_price
        markdown_duration = self.expires_at - markdown_start
        markdown_progress = Decimal(str((now - markdown_start).total_seconds())) / Decimal(
            str(markdown_duration.total_seconds())
        )
        markdown_percent = min(
            Decimal("100.00"),
            Decimal(str(self.product.expiry_price_drop_percent)) * markdown_progress,
        )
        return (
            base_price * (Decimal("100.00") - markdown_percent) / Decimal("100.00")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

from decimal import Decimal

from django.conf import settings
from django.db import models


class ClientProfile(models.Model):

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="client_profile"
    )

    name = models.CharField(
        max_length=200
    )

    contact_no = models.CharField(
        max_length=40,
        blank=True,
        default=""
    )

    email = models.EmailField(
        blank=True,
        default=""
    )
    gstin = models.CharField(max_length=15, blank=True, default="")

    # --------------------------------------------------
    # DEFAULT DELIVERY LOCATION
    # --------------------------------------------------

    default_delivery_location = models.JSONField(
        blank=True,
        null=True
    )

    verification_otp = models.CharField(
        max_length=4,
        default="0000",
        blank=True,
    )

    # --------------------------------------------------
    # DELIVERY ADDRESS LIST
    # --------------------------------------------------
    #
    # Example:
    #
    # [
    #     {
    #         "label": "Home",
    #         "address": "123 Main Road",
    #         "location": {
    #             "lat": 23.0225,
    #             "lng": 72.5714
    #         }
    #     },
    #     {
    #         "label": "Office",
    #         "address": "ABC Tower",
    #         "location": {
    #             "lat": 23.0300,
    #             "lng": 72.5800
    #         }
    #     }
    # ]
    #
    delivery_address_list = models.JSONField(
        blank=True,
        null=True
    )

    # --------------------------------------------------
    # PREFERRED DELIVERY TIME
    # --------------------------------------------------

    preferred_delivery_time = models.JSONField(
        blank=True,
        null=True
    )

    # --------------------------------------------------
    # MULTIPLE SHOP DELIVERY
    # --------------------------------------------------

    is_single_delivery_for_multiple_shop = models.BooleanField(
        default=False
    )

    # --------------------------------------------------
    # MEMBERSHIP
    # --------------------------------------------------

    is_membership_customer = models.BooleanField(
        default=False
    )

    membership_till_date = models.DateField(
        blank=True,
        null=True
    )

    # --------------------------------------------------
    # WALLET
    # --------------------------------------------------

    wallet_balance = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00")
    )

    # --------------------------------------------------
    # COUPONS
    # --------------------------------------------------
    #
    # Example:
    #
    # [
    #     {
    #         "code": "WELCOME10",
    #         "discount": 10,
    #         "type": "percentage"
    #     }
    # ]
    #

    eligible_coupon_list_data = models.JSONField(
        blank=True,
        null=True
    )

    # --------------------------------------------------
    # OTHER
    # --------------------------------------------------

    date_of_birth = models.DateField(
        blank=True,
        null=True
    )

    gender = models.CharField(
        max_length=20,
        blank=True,
        default=""
    )

    notes = models.TextField(
        blank=True,
        default=""
    )

    is_active = models.BooleanField(
        default=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return self.name or self.user.username

    def save(self, *args, **kwargs):
        if (
            self.verification_otp == "0000"
            or not self.verification_otp
            or not self.verification_otp.isdigit()
            or len(self.verification_otp) != 4
        ):
            self.verification_otp = generate_otp_code()
        super().save(*args, **kwargs)

    @property
    def username(self):
        return self.user.username

    @property
    def membership_active(self):

        if not self.is_membership_customer:
            return False

        if not self.membership_till_date:
            return False

        from django.utils import timezone

        return self.membership_till_date >= timezone.now().date()

class ClientDeliveryAddress(models.Model):

    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="delivery_addresses"
    )

    label = models.CharField(
        max_length=150
    )

    address = models.TextField(
        blank=True
    )

    city = models.CharField(
        max_length=150,
        blank=True
    )

    state_code = models.CharField(
        max_length=2,
        choices=INDIAN_GST_STATE_CHOICES,
        blank=True,
        default="",
    )

    pincode = models.CharField(
        max_length=20,
        blank=True
    )

    gstin = models.CharField(max_length=15, blank=True, default="")

    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True
    )

    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True
    )

    location = models.JSONField(
        default=dict,
        blank=True
    )

    is_active = models.BooleanField(
        default=True
    )

    is_default = models.BooleanField(
        default=False
    )
# class ClientDeliveryAddress(models.Model):


    # client = models.ForeignKey(
    #     ClientProfile,
    #     on_delete=models.CASCADE,
    #     related_name="delivery_addresses"
    # )

    # label = models.CharField(
    #     max_length=100,
    #     default="Home"
    # )

    # address = models.TextField()

    # location = models.JSONField(
    #     blank=True,
    #     null=True
    # )

    # contact_name = models.CharField(
    #     max_length=200,
    #     blank=True,
    #     default=""
    # )

    # contact_no = models.CharField(
    #     max_length=40,
    #     blank=True,
    #     default=""
    # )

    # is_default = models.BooleanField(
    #     default=False
    # )

    # is_active = models.BooleanField(
    #     default=True
    # )

    # created_at = models.DateTimeField(
    #     auto_now_add=True
    # )

    # updated_at = models.DateTimeField(
    #     auto_now=True
    # )

    # def __str__(self):
    #     return f"{self.client.name} - {self.label}"


class ClientWalletTransaction(models.Model):

    TRANSACTION_TYPES = [
        ("CREDIT", "Credit"),
        ("DEBIT", "Debit"),
    ]

    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="wallet_transactions"
    )

    transaction_type = models.CharField(
        max_length=10,
        choices=TRANSACTION_TYPES
    )

    amount = models.DecimalField(
        max_digits=18,
        decimal_places=2
    )

    balance_after = models.DecimalField(
        max_digits=18,
        decimal_places=2
    )

    reference = models.CharField(
        max_length=200,
        blank=True,
        default=""
    )

    description = models.TextField(
        blank=True,
        default=""
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return (
            f"{self.client.name} "
            f"{self.transaction_type} "
            f"{self.amount}"
        )

            
class Order(models.Model):
    order_time = models.DateTimeField(null=True, blank=True)
    order_suggested_delivery_minute = models.PositiveIntegerField(null=True, blank=True)
    order_suggested_delivery_time = models.DateTimeField(null=True, blank=True)
    order_delivered_time = models.DateTimeField(null=True, blank=True)
    order_id = models.CharField(max_length=120, unique=True)
    is_b2b_order = models.BooleanField(default=False)
    tracking_code = models.CharField(
        max_length=6,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )
    payment_id = models.CharField(max_length=120, blank=True)
    payment_type = models.CharField(max_length=80, blank=True)
    payment_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Pending"),
            ("PAID", "Paid"),
            ("COD", "Cash on Delivery"),
            ("PARTIALLY_REFUNDED", "Partially refunded"),
            ("FAILED", "Failed"),
            ("REFUNDED", "Refunded"),
        ],
        default="PENDING",
    )
    is_payment_done = models.BooleanField(default=False)
    payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    finally_received_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    user = models.CharField(max_length=150, blank=True)
    order_data = models.TextField(blank=True)
    delivery_address = models.TextField(blank=True)
    delivery_location = models.CharField(max_length=255, blank=True)
    placed_location = models.JSONField(default=dict, blank=True)
    delivery_contact_no = models.CharField(max_length=40, blank=True)
    is_valid_order = models.BooleanField(default=True)
    status = models.CharField(max_length=100, blank=True)
    cancellation_source = models.CharField(
        max_length=10,
        choices=[("", "Not cancelled"), ("CUSTOMER", "Customer"), ("SHOP", "Shop")],
        blank=True,
        default="",
    )
    shop = models.ForeignKey(Shop, on_delete=models.SET_NULL, null=True, blank=True)
    shop_location = models.CharField(max_length=255, blank=True)
    shop_delivery_area = models.CharField(max_length=255, blank=True)
    shop_delivery_pincode = models.CharField(max_length=20, blank=True)
    assigned_packer = models.CharField(max_length=150, blank=True)
    packer_contact_no = models.CharField(max_length=40, blank=True)
    packer_data = models.TextField(blank=True)
    delivery_partner = models.CharField(max_length=150, blank=True)
    assigned_rider = models.CharField(max_length=150, blank=True)
    rider_contact_no = models.CharField(max_length=40, blank=True)
    rider_pickup_data = models.TextField(blank=True)
    order_pickup_location = models.CharField(max_length=255, blank=True)
    rider_drop_data = models.TextField(blank=True)
    order_drop_location = models.CharField(max_length=255, blank=True)
    order_final_picked_location = models.CharField(max_length=255, blank=True)
    order_final_drop_location = models.CharField(max_length=255, blank=True)
    user_remark = models.TextField(blank=True)
    admin_remark = models.TextField(blank=True)
    admin_remark_for_client = models.TextField(blank=True)
    wallet_amount_used = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    coupon_code = models.CharField(max_length=50, blank=True, default="")
    coupon_discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    coupon_cashback_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    final_billing_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    platform_charge_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    razorpay_fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    final_billing_notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.order_id

class ShopOrder(models.Model):


    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="shop_orders"
    )

    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="shop_orders"
    )
    shop_order_number = models.CharField(
        max_length=160,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )

    assigned_packer = models.ForeignKey(
        ShopStaff,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_orders"
    )

    assigned_rider = models.ForeignKey(
        ShopStaff,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rider_assigned_orders"
    )
    rider_accepted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    rider_identity_otp = models.CharField(max_length=6, blank=True, default="")
    rider_identity_otp_expires_at = models.DateTimeField(null=True, blank=True)
    rider_identity_otp_attempts = models.PositiveSmallIntegerField(default=0)
    rider_order_acknowledged = models.BooleanField(default=False)
    rider_pickup_otp = models.CharField(max_length=6, blank=True, default="")
    rider_pickup_otp_expires_at = models.DateTimeField(null=True, blank=True)
    rider_pickup_otp_attempts = models.PositiveSmallIntegerField(default=0)

    rider_last_location = models.JSONField(
        default=dict,
        blank=True,
        null=True
    )

    eta_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    rider_nearby_notified_at = models.DateTimeField(null=True, blank=True)

    rider_cancel_reason = models.TextField(
        blank=True,
        default=""
    )

    rider_cancelled_at = models.DateTimeField(
        null=True,
        blank=True
    )

    customer_otp_verified = models.BooleanField(
        default=False
    )

    customer_otp_input = models.CharField(
        max_length=4,
        blank=True,
        default=""
    )

    delivery_completed_at = models.DateTimeField(
        null=True,
        blank=True
    )

    delivery_remark = models.TextField(
        blank=True,
        default=""
    )

    is_scheduled_delivery = models.BooleanField(
        default=False,
        help_text="This shop order was placed while scheduled delivery was required.",
    )

    customer_rating = models.PositiveSmallIntegerField(
        null=True,
        blank=True
    )

    items = models.JSONField(
        default=list,
        blank=True
    )

    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0
    )

    product_count = models.PositiveIntegerField(
        default=0
    )

    total_weight_kg = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0
    )

    packing_minutes = models.PositiveIntegerField(
        default=0
    )

    rider_eta_minutes = models.PositiveIntegerField(
        default=0
    )

    delivery_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    cancellation_source = models.CharField(
        max_length=10,
        choices=[("", "Not cancelled"), ("CUSTOMER", "Customer"), ("SHOP", "Shop")],
        blank=True,
        default="",
    )
    invoice_number = models.CharField(max_length=150, blank=True, default="")
    seller_gstin = models.CharField(max_length=15, blank=True, default="")
    buyer_gstin = models.CharField(max_length=15, blank=True, default="")
    seller_billing_address = models.TextField(blank=True, default="")
    seller_state_code = models.CharField(max_length=2, blank=True, default="")
    buyer_state_code = models.CharField(max_length=2, blank=True, default="")
    delivery_hsn_code = models.CharField(max_length=8, blank=True, default="")
    delivery_gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    razorpay_refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    wallet_refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    wallet_funded_proceeds = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    packing_slip_number = models.CharField(max_length=80, blank=True, default="")
    packing_slip_printed_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=100,
        default="pending"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order.order_id} - {self.shop.name}"

    # # order = models.OneToOneField(
    # #     Order,
    # #     on_delete=models.CASCADE,
    # #     related_name="shop_order"
    # # )

    # order = models.ForeignKey(
    #     Order,
    #     on_delete=models.CASCADE,
    #     related_name="shop_orders"
    # )


    # shop = models.ForeignKey(
    #     Shop,
    #     on_delete=models.CASCADE,
    #     related_name="shop_orders"
    # )

    # assigned_packer = models.ForeignKey(
    #     ShopStaff,
    #     on_delete=models.SET_NULL,
    #     null=True,
    #     blank=True,
    #     related_name="assigned_orders"
    # )

    # assigned_rider = models.ForeignKey(
    #     ShopStaff,
    #     on_delete=models.SET_NULL,
    #     null=True,
    #     blank=True,
    #     related_name="rider_assigned_orders"
    # )

    # rider_last_location = models.JSONField(default=dict, blank=True, null=True)
    # rider_cancel_reason = models.TextField(blank=True, default="")
    # rider_cancelled_at = models.DateTimeField(null=True, blank=True)
    # customer_otp_verified = models.BooleanField(default=False)
    # customer_otp_input = models.CharField(max_length=4, blank=True, default="")
    # delivery_completed_at = models.DateTimeField(null=True, blank=True)
    # delivery_remark = models.TextField(blank=True, default="")
    # customer_rating = models.PositiveSmallIntegerField(null=True, blank=True)

    # #     # existing fields...
    # # class ShopOrder(models.Model):
    # # order = models.ForeignKey(
    # #     Order,
    # #     on_delete=models.CASCADE,
    # #     related_name="shop_orders"
    # # )

    # # shop = models.ForeignKey(
    # #     Shop,
    # #     on_delete=models.CASCADE,
    # #     related_name="shop_orders"
    # # )

    # items = models.JSONField(default=list, blank=True)
    # total_amount = models.DecimalField(
    #     max_digits=12,
    #     decimal_places=2,
    #     default=0
    # )

    # product_count = models.PositiveIntegerField(default=0)
    # total_weight_kg = models.DecimalField(
    #     max_digits=8,
    #     decimal_places=2,
    #     default=0
    # )

    # packing_minutes = models.PositiveIntegerField(default=0)
    # rider_eta_minutes = models.PositiveIntegerField(default=0)

    # delivery_fee = models.DecimalField(
    #     max_digits=10,
    #     decimal_places=2,
    #     default=0
    # )

    # status = models.CharField(
    #     max_length=100,
    #     default="pending"
    # )

    # created_at = models.DateTimeField(auto_now_add=True)
    # updated_at = models.DateTimeField(auto_now=True)

    # class Meta:
    #     ordering = ["-created_at"]

        


# class ShopOrder(models.Model):
#     order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='shop_order')
#     shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='shop_orders')
#     items = models.JSONField(default=list, blank=True)
#     total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
#     product_count = models.PositiveIntegerField(default=0)
#     total_weight_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0)
#     packing_minutes = models.PositiveIntegerField(default=0)
#     rider_eta_minutes = models.PositiveIntegerField(default=0)
#     delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
#     status = models.CharField(max_length=100, default='pending')
#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     class Meta:
#         ordering = ['-created_at']

#     @staticmethod
#     def calculate_packing_time(product_count, total_weight_kg):
#         packing_time = product_count * 1
#         if total_weight_kg > Decimal('7'):
#             packing_time += 5
#         packing_time += 3
#         return packing_time

#     @staticmethod
#     def calculate_rider_eta_minutes(shop_location, delivery_location):
#         if not shop_location or not delivery_location:
#             return 0

#         try:
#             shop_coords = shop_location.get('coordinates') if isinstance(shop_location, dict) else None
#             if isinstance(shop_coords, list) and len(shop_coords) >= 2:
#                 lat1, lng1 = float(shop_coords[0]), float(shop_coords[1])
#             else:
#                 lat1, lng1 = None, None

#             if isinstance(delivery_location, str):
#                 parts = [part.strip() for part in delivery_location.split(',') if part.strip()]
#                 if len(parts) >= 2:
#                     lat2, lng2 = float(parts[0]), float(parts[1])
#                 else:
#                     return 0
#             else:
#                 return 0

#             if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
#                 return 0

#             radius = 6371
#             lat1_rad = math.radians(lat1)
#             lat2_rad = math.radians(lat2)
#             dlat = math.radians(lat2 - lat1)
#             dlon = math.radians(lng2 - lng1)
#             a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
#             c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
#             distance_km = radius * c
#             return max(1, int(math.ceil((distance_km / 20) * 60)))
#         except (TypeError, ValueError):
#             return 0

#     def save(self, *args, **kwargs):
#         if not self.packing_minutes:
#             self.packing_minutes = self.calculate_packing_time(self.product_count, self.total_weight_kg)
#         if not self.rider_eta_minutes:
#             self.rider_eta_minutes = self.calculate_rider_eta_minutes(self.order.shop_location if self.order else None, self.order.delivery_location if self.order else None)
#         super().save(*args, **kwargs)

class OrderVoiceInstruction(models.Model):
    RECIPIENT_SHOP = "SHOP"
    RECIPIENT_RIDER = "RIDER"
    RECIPIENT_CHOICES = [
        (RECIPIENT_SHOP, "Shop team"),
        (RECIPIENT_RIDER, "Assigned rider"),
    ]

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="order_voice_instructions",
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="order_voice_instructions",
    )
    shop_order = models.OneToOneField(
        ShopOrder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="voice_instruction",
    )
    recipient = models.CharField(
        max_length=8,
        choices=RECIPIENT_CHOICES,
        default=RECIPIENT_SHOP,
    )
    instruction_text = models.TextField(blank=True, default="")
    audio_file = models.FileField(
        upload_to="order-instructions/",
        storage=PrivateOrderAudioStorage(),
        blank=True,
    )
    audio_content_type = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order instruction for {self.shop.name}"


class ShopWallet(models.Model):
    shop = models.OneToOneField(Shop, on_delete=models.CASCADE, related_name="wallet")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.shop.name} wallet: {self.balance}"


class SiteWallet(models.Model):
    key = models.CharField(max_length=20, unique=True, default="SITE")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    commission_rate_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("3.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Site owner wallet: {self.balance}"


class SiteWalletTransaction(models.Model):
    wallet = models.ForeignKey(
        SiteWallet,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    shop_order = models.OneToOneField(
        ShopOrder,
        on_delete=models.PROTECT,
        related_name="site_commission",
    )
    commission_base_amount = models.DecimalField(max_digits=14, decimal_places=2)
    commission_rate_percent = models.DecimalField(max_digits=5, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=250)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Commission for {self.shop_order}: {self.amount}"


class ShopPayout(models.Model):
    STATUS_CHOICES = [
        ("PROCESSING", "Processing"),
        ("QUEUED", "Queued"),
        ("PENDING", "Pending"),
        ("PROCESSED", "Processed"),
        ("FAILED", "Failed"),
        ("REVERSED", "Reversed"),
        ("UNKNOWN", "Status unknown"),
    ]

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="payouts")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PROCESSING")
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    razorpay_payout_id = models.CharField(max_length=80, blank=True, default="")
    razorpay_fund_account_id = models.CharField(max_length=80)
    reference_id = models.CharField(max_length=40, unique=True)
    failure_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.shop.name} payout {self.amount} ({self.status})"


class ShopWalletTransaction(models.Model):
    TRANSACTION_CHOICES = [
        ("CREDIT", "Order credit"),
        ("COMMISSION", "Platform commission"),
        ("RIDER_FEE", "Shared rider fee"),
        ("RIDER_INCOME", "Shared rider income"),
        ("DEBIT", "Withdrawal"),
        ("REVERSAL", "Withdrawal reversal"),
    ]

    wallet = models.ForeignKey(ShopWallet, on_delete=models.CASCADE, related_name="transactions")
    transaction_type = models.CharField(max_length=12, choices=TRANSACTION_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    shop_order = models.ForeignKey(
        ShopOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wallet_transactions",
    )
    payout = models.ForeignKey(
        ShopPayout,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wallet_transaction",
    )
    description = models.CharField(max_length=250)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SharedRiderDeliveryPayment(models.Model):
    shop_order = models.OneToOneField(
        ShopOrder,
        on_delete=models.PROTECT,
        related_name="shared_rider_payment",
    )
    paying_shop = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="shared_rider_payments",
    )
    rider_home_shop = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="received_shared_rider_payments",
    )
    rider = models.ForeignKey(
        ShopStaff,
        on_delete=models.PROTECT,
        related_name="shared_rider_payments",
    )
    delivery_distance_km = models.DecimalField(
        max_digits=8,
        decimal_places=3,
    )
    rate_per_km = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    delivered_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-delivered_at", "-id"]

    def __str__(self):
        return f"{self.rider} · {self.shop_order} · ₹{self.amount}"


class RiderCashLedgerEntry(models.Model):
    ENTRY_TYPES = [
        ("COLLECTION", "COD collected from customer"),
        ("HANDOVER", "Cash handed over to shop"),
    ]

    rider = models.ForeignKey(
        ShopStaff,
        on_delete=models.PROTECT,
        related_name="cash_ledger_entries",
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="rider_cash_ledger_entries",
    )
    shop_order = models.ForeignKey(
        ShopOrder,
        on_delete=models.PROTECT,
        related_name="cash_ledger_entries",
    )
    payment_method = models.CharField(
        max_length=16,
        choices=[
            ("CASH", "Cash"),
            ("STAFF_UPI", "Staff personal UPI"),
        ],
        default="CASH",
    )
    entry_type = models.CharField(max_length=12, choices=ENTRY_TYPES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rider_cash_ledger_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["shop_order"],
                condition=models.Q(entry_type="COLLECTION"),
                name="unique_rider_cash_collection_per_order",
            ),
        ]

    def __str__(self):
        return f"{self.get_entry_type_display()} · {self.shop_order} · ₹{self.amount}"


class ShopStaffWallet(models.Model):
    staff = models.OneToOneField(
        ShopStaff,
        on_delete=models.CASCADE,
        related_name="wallet",
    )
    balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.staff} · ₹{self.balance}"


class ShopStaffWalletTransaction(models.Model):
    TRANSACTION_TYPES = [
        ("COD_UPI", "COD received to personal UPI"),
        ("HANDOVER", "COD handed over to shop"),
        ("ADJUSTMENT", "COD collection amount adjusted"),
    ]

    wallet = models.ForeignKey(
        ShopStaffWallet,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    shop_order = models.ForeignKey(
        "ShopOrder",
        on_delete=models.PROTECT,
        related_name="staff_wallet_transactions",
    )
    transaction_type = models.CharField(max_length=12, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=250)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["shop_order"],
                condition=models.Q(transaction_type="COD_UPI"),
                name="unique_staff_wallet_cod_upi_per_order",
            ),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} · {self.shop_order} · ₹{self.amount}"


class CODBalancePayment(models.Model):
    shop_order = models.ForeignKey(
        ShopOrder,
        on_delete=models.PROTECT,
        related_name="cod_balance_payments",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cod_balance_payments_received",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"COD balance payment · {self.shop_order} · ₹{self.amount}"


class OrderItem(models.Model):

    shop_order = models.ForeignKey(
        ShopOrder,
        on_delete=models.CASCADE,
        related_name="order_items"
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    product_shop = models.ForeignKey(
        ProductShop,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    product_name = models.CharField(max_length=250)

    sku = models.CharField(
        max_length=120,
        blank=True
    )

    quantity = models.PositiveIntegerField()
    returned_quantity = models.PositiveIntegerField(default=0)
    returned_received_quantity = models.PositiveIntegerField(default=0)

    is_approximate_weight = models.BooleanField(default=False)
    estimated_weight = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    final_weight = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=40, blank=True, default="")
    is_bulk_order = models.BooleanField(default=False)

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    total = models.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    hsn_code = models.CharField(max_length=8, blank=True, default="")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    @property
    def pending_return_quantity(self):
        return max(0, self.returned_quantity - self.returned_received_quantity)

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class FavoriteShop(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorite_shops")
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "shop"], name="unique_favorite_shop_per_user"),
        ]
        ordering = ["-created_at"]


class FavoriteProduct(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorite_products")
    product_shop = models.ForeignKey(
        ProductShop,
        on_delete=models.CASCADE,
        related_name="favorited_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "product_shop"], name="unique_favorite_product_per_user"),
        ]
        ordering = ["-created_at"]


class SavedBasket(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_baskets",
    )
    name = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"],
                name="unique_saved_basket_name_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.user})"


class SavedBasketItem(models.Model):
    basket = models.ForeignKey(
        SavedBasket,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product_shop = models.ForeignKey(
        ProductShop,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saved_basket_items",
    )
    product_name = models.CharField(max_length=200)
    shop_name = models.CharField(max_length=200, blank=True, default="")
    quantity = models.PositiveIntegerField(default=1)
    is_bulk_order = models.BooleanField(default=False)

    class Meta:
        ordering = ["product_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["basket", "product_shop", "is_bulk_order"],
                name="unique_product_shop_offer_per_saved_basket",
            ),
        ]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class CustomerProductFeedback(models.Model):
    COMPLAINT = "COMPLAINT"
    REJECTED = "REJECTED"
    FEEDBACK_CHOICES = [
        (COMPLAINT, "Complaint"),
        (REJECTED, "Rejected product"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="product_feedback")
    shop_order = models.ForeignKey(ShopOrder, on_delete=models.CASCADE, related_name="customer_feedback")
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="customer_feedback",
    )
    feedback_type = models.CharField(max_length=12, choices=FEEDBACK_CHOICES)
    details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class UserActivity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_events",
    )
    event_type = models.CharField(max_length=80)
    path = models.CharField(max_length=500, blank=True, default="")
    search_term = models.CharField(max_length=200, blank=True, default="")
    location = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class StockMovement(models.Model):
    MOVEMENT_CHOICES = [
        ("IMPORT", "Import"),
        ("SALE", "Sale"),
        ("ADJUSTMENT", "Adjustment"),
        ("EXPIRY", "Expiry"),
    ]

    product_shop = models.ForeignKey(ProductShop, on_delete=models.CASCADE, related_name="stock_movements")
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_CHOICES)
    quantity = models.IntegerField()
    reference = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ShopProductForecast(models.Model):
    product_shop = models.OneToOneField(ProductShop, on_delete=models.CASCADE, related_name="forecast")
    daily_average = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    weekday_average = models.JSONField(default=dict, blank=True)
    festival_average = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    recommended_stock = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    calculated_at = models.DateTimeField(auto_now=True)

class OrderStatusHistory(models.Model):

    STATUS_CHOICES = [
        ("PLACED", "Order Placed"),
        ("CONFIRMED", "Confirmed"),
        ("PACKING", "Packing"),
        ("READY", "Ready for Pickup"),
        ("RIDER_ASSIGNED", "Accepted by Rider"),
        ("PICKED_UP", "Picked Up"),
        ("OUT_FOR_DELIVERY", "Out for Delivery"),
        ("DELIVERED", "Delivered"),
        ("RETURNED", "Returned to Shop"),
        ("CANCELLED", "Cancelled"),
    ]

    shop_order = models.ForeignKey(
        ShopOrder,
        on_delete=models.CASCADE,
        related_name="status_history"
    )

    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES
    )

    remark = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        ordering = ["created_at"]   

class Notification(models.Model):
    TYPE_CHOICES = [
        ("ORDER", "Order"),
        ("SHOP", "Shop"),
        ("JOB", "Job"),
        ("SYSTEM", "System"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications"
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="SYSTEM")
    related_url = models.CharField(max_length=500, blank=True, default="")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.title}"


class NotificationDeviceToken(models.Model):
    PLATFORM_CHOICES = [
        ("android", "Android"),
        ("ios", "iOS"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notification_device_tokens",
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.platform})"


class Coupon(models.Model):

    code = models.CharField(
        max_length=50,
        unique=True
    )

    discount_type = models.CharField(
        max_length=20,
        choices=[
            ("PERCENT", "Percentage"),
            ("FLAT", "Flat"),
        ]
    )

    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    minimum_order_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    maximum_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True
    )

    cashback_type = models.CharField(
        max_length=20,
        choices=[("PERCENT", "Percentage"), ("FLAT", "Flat")],
        blank=True,
        default="",
    )
    cashback_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    valid_from = models.DateTimeField()

    valid_until = models.DateTimeField()

    is_active = models.BooleanField(
        default=True
    )


class JobOpening(models.Model):
    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="job_openings"
    )
    title = models.CharField(max_length=200)
    role = models.CharField(max_length=80, default="Staff")
    location = models.CharField(max_length=200, blank=True, default="")
    employment_type = models.CharField(
        max_length=50,
        default="Full Time",
        blank=True,
    )
    description = models.TextField(blank=True, default="")
    requirements = models.TextField(blank=True, default="")
    openings = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.shop.name} - {self.title}"


class JobApplication(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("REVIEWING", "Reviewing"),
        ("INTERVIEW", "Interview"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_applications"
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_applications"
    )
    job_opening = models.ForeignKey(
        JobOpening,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications"
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=40, blank=True, default="")
    job_title = models.CharField(max_length=200, blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    experience = models.CharField(max_length=200, blank=True, default="")
    cover_letter = models.TextField(blank=True, default="")
    interview_datetime = models.DateTimeField(null=True, blank=True)
    interview_notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} - {self.job_title or 'Job application'}"
