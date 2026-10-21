from datetime import datetime, timezone as datetime_timezone
from pathlib import Path

from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.http import FileResponse, Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .push_notifications import queue_expo_push_notifications
from .offline_keyword_dictionary import dictionary_is_ready
from .models import (
    AdminProductCatalog,
    DeliverySharingSettings,
    Order,
    PublicRiderApplication,
    RoadNetworkGeoJSON,
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
    SQLiteBackupSettings,
)
from .sqlite_backups import (
    create_sqlite_backup,
    list_sqlite_backups,
    restore_sqlite_backup,
)
from .public_riders import sync_public_rider_shop_memberships


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
        "public_rider_network_enabled",
        "rider_delivery_preference",
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

    def save_model(self, request, obj, form, change):
        was_public_rider_enabled = (
            Shop.objects.filter(pk=obj.pk)
            .values_list("public_rider_network_enabled", flat=True)
            .first()
            if change
            else False
        )
        super().save_model(request, obj, form, change)
        if was_public_rider_enabled != obj.public_rider_network_enabled:
            for application in PublicRiderApplication.objects.filter(
                status=PublicRiderApplication.STATUS_APPROVED,
            ).iterator():
                sync_public_rider_shop_memberships(application)


@admin.register(PublicRiderApplication)
class PublicRiderApplicationAdmin(admin.ModelAdmin):
    list_display = ("full_name", "user", "phone", "vehicle_type", "status", "created_at")
    list_filter = ("status", "vehicle_type", "created_at")
    search_fields = ("full_name", "phone", "driving_license_number", "user__username")
    readonly_fields = (
        "user",
        "created_at",
        "updated_at",
        "reviewed_by",
        "reviewed_at",
        "license_document_download",
        "identity_document_download",
    )
    fields = (
        "user",
        "full_name",
        "phone",
        "address",
        "vehicle_type",
        "driving_license_number",
        "license_document_download",
        "identity_document_download",
        "status",
        "review_notes",
        "reviewed_by",
        "reviewed_at",
        "created_at",
        "updated_at",
    )
    actions = ("approve_applications", "reject_applications")

    @admin.display(description="Driving licence document")
    def license_document_download(self, obj):
        return self._document_link(obj, "license_document")

    @admin.display(description="Identity document")
    def identity_document_download(self, obj):
        return self._document_link(obj, "identity_document")

    def _document_link(self, obj, field_name):
        if not obj or not getattr(obj, field_name):
            return "Not uploaded"
        url = reverse(
            "admin:seller_publicriderapplication_document",
            args=(obj.pk, field_name),
        )
        return format_html('<a href="{}">Download privately</a>', url)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:application_id>/document/<str:field_name>/",
                self.admin_site.admin_view(self.download_document),
                name="seller_publicriderapplication_document",
            ),
        ]
        return custom_urls + urls

    def download_document(self, request, application_id, field_name):
        application = self.get_object(request, application_id)
        if application is None or not self.has_view_or_change_permission(request, application):
            raise PermissionDenied
        if field_name not in {"license_document", "identity_document"}:
            raise PermissionDenied
        document = getattr(application, field_name)
        if not document:
            raise Http404("Document not found.")
        return FileResponse(
            document.open("rb"),
            as_attachment=True,
            filename=Path(document.name).name,
            content_type="application/octet-stream",
        )

    @admin.action(description="Approve selected public rider applications")
    def approve_applications(self, request, queryset):
        for application in queryset:
            application.status = PublicRiderApplication.STATUS_APPROVED
            application.reviewed_by = request.user
            application.reviewed_at = timezone.now()
            application.save(
                update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"],
            )
            sync_public_rider_shop_memberships(application)
        self.message_user(request, "Selected public rider applications were approved.")

    @admin.action(description="Reject selected public rider applications")
    def reject_applications(self, request, queryset):
        for application in queryset:
            application.status = PublicRiderApplication.STATUS_REJECTED
            application.reviewed_by = request.user
            application.reviewed_at = timezone.now()
            application.save(
                update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"],
            )
            sync_public_rider_shop_memberships(application)
        self.message_user(request, "Selected public rider applications were rejected.")

    def save_model(self, request, obj, form, change):
        if obj.status in {
            PublicRiderApplication.STATUS_APPROVED,
            PublicRiderApplication.STATUS_REJECTED,
        }:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)
        sync_public_rider_shop_memberships(obj)


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


@admin.register(SQLiteBackupSettings)
class SQLiteBackupSettingsAdmin(admin.ModelAdmin):
    change_form_template = "admin/seller/sqlitebackupsettings/change_form.html"
    list_display = ("interval_minutes", "retention_count", "updated_at")
    fields = ("interval_minutes", "retention_count", "updated_at")
    readonly_fields = ("updated_at",)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "backup/",
                self.admin_site.admin_view(self.backup_view),
                name="seller_sqlitebackupsettings_backup",
            ),
            path(
                "restore/",
                self.admin_site.admin_view(self.restore_view),
                name="seller_sqlitebackupsettings_restore",
            ),
        ]
        return custom_urls + urls

    def backup_view(self, request):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not request.user.is_superuser:
            raise PermissionDenied

        database = connections["default"]
        if database.vendor != "sqlite":
            messages.error(request, "Immediate backups are available only for SQLite.")
            return HttpResponseRedirect(
                reverse("admin:seller_sqlitebackupsettings_change", args=[1])
            )

        database_path = database.settings_dict["NAME"]
        if not isinstance(database_path, (str, Path)) or str(database_path) == ":memory:":
            messages.error(request, "Immediate backups require a file-based SQLite database.")
            return HttpResponseRedirect(
                reverse("admin:seller_sqlitebackupsettings_change", args=[1])
            )

        backup_settings = SQLiteBackupSettings.objects.get(pk=1)
        backup_directory = Path(settings.BASE_DIR) / "sqlite_backups"
        try:
            backup_path = create_sqlite_backup(
                database_path,
                backup_directory,
                backup_settings.retention_count,
            )
        except Exception as error:
            messages.error(request, f"SQLite backup failed: {error}")
        else:
            messages.success(request, f"Created SQLite backup: {backup_path.name}")
        return HttpResponseRedirect(
            reverse("admin:seller_sqlitebackupsettings_change", args=[1])
        )

    def restore_view(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied

        backup_directory = Path(settings.BASE_DIR) / "sqlite_backups"
        backups = list_sqlite_backups(backup_directory)
        if request.method == "POST":
            if request.POST.get("confirmation") != "RESTORE":
                messages.error(request, 'Type "RESTORE" to confirm database replacement.')
            else:
                backup_name = request.POST.get("backup", "")
                backup_path = next(
                    (path for path in backups if path.name == backup_name),
                    None,
                )
                if backup_path is None:
                    messages.error(request, "Choose a valid backup file.")
                else:
                    database = connections["default"]
                    if database.vendor != "sqlite":
                        messages.error(request, "Database restore is available only for SQLite.")
                    else:
                        database_path = Path(database.settings_dict["NAME"])
                        backup_settings = SQLiteBackupSettings.objects.get(pk=1)
                        connections.close_all()
                        try:
                            safety_backup = restore_sqlite_backup(
                                database_path,
                                backup_path,
                                backup_directory,
                                backup_settings.retention_count,
                            )
                        except Exception as error:
                            messages.error(request, f"Database restore failed: {error}")
                        else:
                            messages.success(
                                request,
                                "Database restored successfully. A pre-restore safety "
                                f"backup was saved as {safety_backup.name}.",
                            )
                        return HttpResponseRedirect(
                            reverse("admin:seller_sqlitebackupsettings_restore")
                        )

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Restore SQLite database",
            "backups": [
                {
                    "path": backup,
                    "created_at": timezone.localtime(datetime.fromtimestamp(
                        backup.stat().st_mtime,
                        tz=datetime_timezone.utc,
                    )),
                }
                for backup in backups
            ],
        }
        return TemplateResponse(
            request,
            "admin/seller/sqlitebackupsettings/restore.html",
            context,
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["backup_url"] = reverse(
            "admin:seller_sqlitebackupsettings_backup"
        )
        extra_context["restore_url"] = reverse(
            "admin:seller_sqlitebackupsettings_restore"
        )
        return super().change_view(
            request,
            object_id,
            form_url,
            extra_context=extra_context,
        )

    def has_add_permission(self, request):
        return not SQLiteBackupSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class RoadNetworkGeoJSONInline(admin.TabularInline):
    model = RoadNetworkGeoJSON
    extra = 1


@admin.register(DeliverySharingSettings)
class DeliverySharingSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "public_rider_service_enabled",
        "max_additional_pickup_locations",
        "abandoned_checkout_timeout_minutes",
        "shared_rider_radius_km",
        "customer_direction_radius_km",
        "public_rider_batch_radius_km",
        "public_rider_decline_cooldown_minutes",
        "public_rider_offer_timeout_seconds",
        "public_rider_offer_riders_per_round",
        "public_rider_max_active_offers",
        "shared_rider_rate_per_km",
        "shared_rider_base_pay",
        "updated_at",
    )
    fieldsets = (
        ("Public/shared rider coverage and offer pay", {
            "fields": (
                "public_rider_service_enabled",
                "max_additional_pickup_locations",
                "shared_rider_radius_km",
                "customer_direction_radius_km",
                "public_rider_batch_radius_km",
                "public_rider_decline_cooldown_minutes",
                "public_rider_offer_timeout_seconds",
                "public_rider_offer_riders_per_round",
                "public_rider_max_active_offers",
                "road_network_geojson",
                "shared_rider_base_pay",
                "shared_rider_rate_per_km",
            ),
        }),
        ("Checkout tracking", {
            "fields": ("abandoned_checkout_timeout_minutes",),
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
    inlines = (RoadNetworkGeoJSONInline,)

    def has_add_permission(self, request):
        return not DeliverySharingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        was_enabled = (
            DeliverySharingSettings.objects.filter(pk=1)
            .values_list("public_rider_service_enabled", flat=True)
            .first()
            if change
            else True
        )
        super().save_model(request, obj, form, change)
        if was_enabled != obj.public_rider_service_enabled:
            for application in PublicRiderApplication.objects.filter(
                status=PublicRiderApplication.STATUS_APPROVED,
            ).iterator():
                sync_public_rider_shop_memberships(application)


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
    list_display = ("user", "shop", "role", "is_public_rider", "mobile_no", "is_active")
    list_filter = ("role", "is_public_rider", "is_active", "shop")
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
