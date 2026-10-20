from django.contrib import admin, messages
from django.contrib.auth import get_user_model

from .push_notifications import queue_expo_push_notifications
from .offline_keyword_dictionary import dictionary_is_ready
from .models import (
    AdminProductCatalog,
    DeliverySharingSettings,
    Order,
    Product,
    ProductShop,
    Shop,
    Notification,
    ProductCategory,
    ShopStaff,
    UserActivity,
    StockMovement,
    ShopProductForecast,
    ShopWallet,
    ShopWalletTransaction,
    SiteWallet,
    SiteWalletTransaction,
    ShopStorefrontSettings,
)


User = get_user_model()


@admin.register(AdminProductCatalog)
class AdminProductCatalogAdmin(admin.ModelAdmin):
    list_display = (
        "product_name",
        "product_category",
        "hsn_code",
        "gst_rate",
        "is_active",
        "updated_at",
    )
    list_filter = ("product_category", "is_active")
    search_fields = ("product_name", "hsn_code")
    list_editable = ("hsn_code", "gst_rate", "is_active")
    ordering = ("product_category", "product_name")


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name",)
    list_editable = ("sort_order", "is_active")
    ordering = ("sort_order", "name")


@admin.register(SiteWallet)
class SiteWalletAdmin(admin.ModelAdmin):
    list_display = ("key", "balance", "commission_rate_percent", "updated_at")
    readonly_fields = ("key", "balance", "updated_at")
    fields = ("key", "balance", "commission_rate_percent", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteWalletTransaction)
class SiteWalletTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "shop_order",
        "commission_base_amount",
        "commission_rate_percent",
        "amount",
        "balance_after",
        "created_at",
    )
    list_filter = ("commission_rate_percent", "created_at")
    search_fields = ("shop_order__order__order_id", "shop_order__shop__name")
    readonly_fields = tuple(
        field.name for field in SiteWalletTransaction._meta.fields
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================
# SHOP
# ============================================================

@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "name",
        "storefront_slug",
        "storefront_subscription_active",
        "storefront_subscription_expires_at",
        "location",
        "delivery_area",
        "contact_no",
        "logo_url",
        "is_shop_open",
        "share_riders_with_nearby_shops",
        "allow_product_image_overrides",
        "allow_owner_create_shared_rider_groups",
    )

    search_fields = (
        "name",
        "location",
        "delivery_area",
    )
    list_editable = (
        "allow_product_image_overrides",
        "allow_owner_create_shared_rider_groups",
        "storefront_subscription_active",
    )
    readonly_fields = ("storefront_slug",)


@admin.register(ShopStorefrontSettings)
class ShopStorefrontSettingsAdmin(admin.ModelAdmin):
    list_display = ("monthly_subscription_price", "updated_at")
    fields = (
        "monthly_subscription_price",
        "android_app_download_url",
        "ios_app_download_url",
        "updated_at",
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not ShopStorefrontSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeliverySharingSettings)
class DeliverySharingSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "shared_rider_radius_km",
        "customer_direction_radius_km",
        "shared_rider_rate_per_km",
        "shared_rider_base_pay",
        "updated_at",
    )
    fieldsets = (
        ("Shared rider coverage and pay", {
            "fields": (
                "shared_rider_radius_km",
                "customer_direction_radius_km",
                "shared_rider_rate_per_km",
                "shared_rider_base_pay",
            ),
        }),
        ("Monthly salary suggestions", {
            "fields": (
                "packer_monthly_base_salary",
                "packer_completed_order_bonus",
                "rider_monthly_base_salary",
                "rider_completed_order_bonus",
            ),
        }),
        ("Audit", {"fields": ("updated_at",)}),
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not DeliverySharingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================
# PRODUCT
# ============================================================

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "sku",
        "product_name",
        "product_title",
        "search_keywords_translated",
        "price",
        "unit",
        "is_active",
        "created_at",
    )

    list_filter = (
        "is_active",
        "product_category",
        "product_color",
        "product_size",
    )

    search_fields = (
        "sku",
        "product_name",
        "product_alias_name",
        "product_title",
        "product_color",
        "product_size",
        "product_description",
        "search_keywords",
    )

    list_editable = (
        "price",
        "is_active",
    )

    readonly_fields = (
        "product_name",
        "search_keywords_translated",
        "created_at",
        "updated_at",
    )
    actions = ("regenerate_search_keywords",)

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            return (*readonly_fields, "search_keywords")
        return readonly_fields

    def save_model(self, request, obj, form, change):
        if request.user.is_superuser and "search_keywords" in form.changed_data:
            obj.save(regenerate_search_keywords=False)
            return
        super().save_model(request, obj, form, change)
        if (
            request.user.is_superuser
            and (
                not change
                or set(form.changed_data).intersection(
                    Product.SEARCH_KEYWORD_SOURCE_FIELDS
                )
            )
            and not obj.search_keywords_translated
        ):
            if dictionary_is_ready():
                warning = (
                    "No dictionary translation matched this product. Original "
                    "keywords and script-search aliases remain available."
                )
            else:
                warning = (
                    "The offline product dictionary is not built. Original keywords "
                    "and script-search aliases were generated; run "
                    "`python manage.py build_product_dictionary` to add word translations."
                )
            self.message_user(
                request,
                f"Product saved. {warning}",
                messages.WARNING,
            )

    @admin.action(description="Regenerate multilingual search keywords")
    def regenerate_search_keywords(self, request, queryset):
        updated_count = 0
        untranslated_count = 0
        for product in queryset.iterator():
            product.save(regenerate_search_keywords=True)
            if product.search_keywords_translated:
                updated_count += 1
            else:
                untranslated_count += 1
        if updated_count:
            self.message_user(
                request,
                f"Generated multilingual search keywords for {updated_count} product(s).",
                messages.SUCCESS,
            )
        if untranslated_count:
            if dictionary_is_ready():
                warning = (
                    f"{untranslated_count} product(s) had no dictionary word "
                    "matches; their original keywords and script aliases remain."
                )
            else:
                warning = (
                    f"{untranslated_count} product(s) were regenerated with original "
                    "keywords and script aliases only because the offline dictionary "
                    "is not built. Run `python manage.py build_product_dictionary`."
                )
            self.message_user(request, warning, messages.WARNING)

    ordering = (
        "-created_at",
    )

    fieldsets = (

        (
            "Product Information",
            {
                "fields": (
                    "product_category",
                    "sku",
                    "product_name",
                    "product_alias_name",
                    "product_title",
                    "product_color",
                    "product_size",
                    "hsn_code",
                    "gst_rate",
                    "shelf_life_days",
                    "expiry_price_drop_percent",
                    "expiry_management_enabled",
                )
            }
        ),

        (
            "Pricing",
            {
                "fields": (
                    "price",
                    "unit",
                )
            }
        ),

        (
            "Product Media & Description",
            {
                "fields": (
                    "image_url",
                    "product_description",
                    "search_keywords",
                )
            }
        ),

        (
            "Status",
            {
                "fields": (
                    "is_active",
                )
            }
        ),

        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            }
        ),

    )


# ============================================================
# PRODUCT SHOP
# ============================================================

@admin.register(ProductShop)
class ProductShopAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "product",
        "shop",
        "price",
        "b2b_price",
        "bulk_price",
        "b2b_bulk_price",
        "stock",
        "is_active",
    )

    list_filter = (
        "is_active",
        "shop",
    )

    search_fields = (
        "product__product_name",
        "product__sku",
        "shop__name",
    )

    ordering = (
        "shop",
        "product",
    )


@admin.register(ShopWallet)
class ShopWalletAdmin(admin.ModelAdmin):
    list_display = ("shop", "balance", "updated_at")
    search_fields = ("shop__name",)
    readonly_fields = ("shop", "balance", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ShopWalletTransaction)
class ShopWalletTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "wallet",
        "transaction_type",
        "amount",
        "balance_after",
        "shop_order",
        "payout",
        "created_at",
    )
    list_filter = ("transaction_type", "created_at")
    search_fields = (
        "wallet__shop__name",
        "shop_order__order__order_id",
        "description",
    )
    readonly_fields = tuple(
        field.name for field in ShopWalletTransaction._meta.fields
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================
# ORDER
# ============================================================

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "order_id",
        "user",
        "shop",
        "status",
        "is_payment_done",
        "payment_amount",
    )

    list_filter = (
        "status",
        "is_payment_done",
        "is_valid_order",
    )

    search_fields = (
        "order_id",
        "user",
        "delivery_address",
        "assigned_rider",
        "assigned_packer",
    )


# ============================================================
# NOTIFICATION
# ============================================================

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "user",
        "title",
        "notification_type",
        "is_read",
        "created_at",
        "updated_at",
    )

    list_filter = (
        "notification_type",
        "is_read",
        "created_at",
    )

    search_fields = (
        "user__username",
        "user__email",
        "title",
        "message",
    )

    ordering = (
        "-created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    list_per_page = 50

    actions = (
        "send_notification_to_all_users",
    )

    @admin.action(
        description="Send selected notification to ALL users"
    )
    def send_notification_to_all_users(
        self,
        request,
        queryset
    ):

        notification = queryset.first()

        if not notification:

            self.message_user(
                request,
                "Please select a notification.",
                level=messages.ERROR,
            )

            return

        users = User.objects.filter(
            is_active=True
        )

        notifications = []

        for user in users:

            notifications.append(
                Notification(
                    user=user,
                    title=notification.title,
                    message=notification.message,
                    notification_type=notification.notification_type,
                    related_url=notification.related_url,
                    is_read=False,
                )
            )

        created_notifications = Notification.objects.bulk_create(
            notifications
        )
        queue_expo_push_notifications(
            notification.pk for notification in created_notifications if notification.pk
        )

        self.message_user(
            request,
            f"Notification sent to {len(notifications)} users.",
            level=messages.SUCCESS,
        )



@admin.register(ShopStaff)
class ShopStaffAdmin(admin.ModelAdmin):
    list_display = ("user", "shop", "role", "mobile_no", "is_active")
    list_filter = ("role", "is_active", "shop")
    search_fields = ("user__username", "mobile_no", "shop__name")


@admin.register(UserActivity)
class UserActivityAdmin(admin.ModelAdmin):
    list_display = ("user", "event_type", "search_term", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("user__username", "search_term", "path")
    readonly_fields = ("created_at",)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("product_shop", "movement_type", "quantity", "created_by", "created_at")
    list_filter = ("movement_type", "created_at")
    search_fields = ("product_shop__product__product_name", "product_shop__shop__name", "reference")
    readonly_fields = ("created_at",)


@admin.register(ShopProductForecast)
class ShopProductForecastAdmin(admin.ModelAdmin):
    list_display = ("product_shop", "daily_average", "recommended_stock", "calculated_at")
    search_fields = ("product_shop__product__product_name", "product_shop__shop__name")
    readonly_fields = ("calculated_at",)


# from django.contrib import admin

# from .models import Order, Product, Shop


# @admin.register(Shop)
# class ShopAdmin(admin.ModelAdmin):
#     list_display = ('id', 'name', 'location', 'delivery_area', 'contact_no')
#     search_fields = ('name', 'location', 'delivery_area')


# # @admin.register(Product)
# # class ProductAdmin(admin.ModelAdmin):
# #     list_display = ('id', 'name', 'shop', 'price', 'stock', 'is_active')
# #     list_filter = ('is_active', 'shop')
# #     search_fields = ('name', 'sku', 'shop__name')


# @admin.register(Order)
# class OrderAdmin(admin.ModelAdmin):
#     list_display = ('id', 'order_id', 'user', 'shop', 'status', 'is_payment_done', 'payment_amount')
#     list_filter = ('status', 'is_payment_done', 'is_valid_order')
#     search_fields = ('order_id', 'user', 'delivery_address', 'assigned_rider', 'assigned_packer')



# from django.contrib import admin

# from .models import Product, ProductShop


# @admin.register(ProductShop)
# class ProductShopAdmin(admin.ModelAdmin):

#     list_display = (
#         "id",
#         "product",
#         "shop",
#         "price",
#         "stock",
#         "is_active",
#     )

#     list_filter = (
#         "is_active",
#         "shop",
#     )

#     search_fields = (
#         "product__product_name",
#         "product__sku",
#         "shop__name",
#     )

#     ordering = (
#         "shop",
#         "product",
#     )


# @admin.register(Product)
# class ProductAdmin(admin.ModelAdmin):

#     list_display = (
#         "id",
#         "sku",
#         "product_name",
#         "product_title",
#         "price",
#         "unit",
#         "is_active",
#         "created_at",
#     )

#     list_filter = (
#         "is_active",
#         "product_color",
#         "product_size",
#     )

#     search_fields = (
#         "sku",
#         "product_name",
#         "product_title",
#         "product_color",
#         "product_size",
#         "product_description",
#     )

#     list_editable = (
#         "price",
#         "is_active",
#     )

#     readonly_fields = (
#         "created_at",
#         "updated_at",
#     )

#     ordering = (
#         "-created_at",
#     )


#     fieldsets = (

#         (
#             "Product Information",
#             {
#                 "fields": (
#                     "sku",
#                     "product_name",
#                     "product_title",
#                     "product_color",
#                     "product_size",
#                 )
#             }
#         ),

#         (
#             "Pricing",
#             {
#                 "fields": (
#                     "price",
#                     "unit",
#                 )
#             }
#         ),

#         (
#             "Product Media & Description",
#             {
#                 "fields": (
#                     "image_url",
#                     "product_description",
#                 )
#             }
#         ),

#         (
#             "Status",
#             {
#                 "fields": (
#                     "is_active",
#                 )
#             }
#         ),

#         (
#             "Timestamps",
#             {
#                 "fields": (
#                     "created_at",
#                     "updated_at",
#                 )
#             }
#         ),

#     )

# from django.contrib import admin
# from .models import Notification


# @admin.register(Notification)
# class NotificationAdmin(admin.ModelAdmin):

#     list_display = (
#         "id",
#         "user",
#         "title",
#         "notification_type",
#         "is_read",
#         "created_at",
#         "updated_at",
#     )

#     list_filter = (
#         "notification_type",
#         "is_read",
#         "created_at",
#     )

#     search_fields = (
#         "user__username",
#         "user__email",
#         "title",
#         "message",
#     )

#     ordering = (
#         "-created_at",
#     )

#     readonly_fields = (
#         "created_at",
#         "updated_at",
#     )

#     list_per_page = 50    

# from django.contrib import admin, messages
# from django.contrib.auth import get_user_model

# from .models import Notification


# User = get_user_model()


# @admin.register(Notification)
# class NotificationAdmin(admin.ModelAdmin):

#     list_display = (
#         "id",
#         "user",
#         "title",
#         "notification_type",
#         "is_read",
#         "created_at",
#     )

#     list_filter = (
#         "notification_type",
#         "is_read",
#         "created_at",
#     )

#     search_fields = (
#         "user__username",
#         "user__email",
#         "title",
#         "message",
#     )

#     ordering = ("-created_at",)

#     readonly_fields = (
#         "created_at",
#         "updated_at",
#     )

#     list_per_page = 50

#     actions = [
#         "send_notification_to_all_users",
#     ]

#     @admin.action(description="Send selected notification to ALL users")
#     def send_notification_to_all_users(self, request, queryset):

#         # Admin selects one notification
#         notification = queryset.first()

#         if not notification:
#             self.message_user(
#                 request,
#                 "Please select a notification.",
#                 level=messages.ERROR,
#             )
#             return

#         users = User.objects.filter(
#             is_active=True
#         )

#         notifications = []

#         for user in users:

#             notifications.append(
#                 Notification(
#                     user=user,
#                     title=notification.title,
#                     message=notification.message,
#                     notification_type=notification.notification_type,
#                     related_url=notification.related_url,
#                     is_read=False,
#                 )
#             )

#         Notification.objects.bulk_create(
#             notifications
#         )

#         self.message_user(
#             request,
#             f"Notification sent to {len(notifications)} users.",
#             level=messages.SUCCESS,
#         )
