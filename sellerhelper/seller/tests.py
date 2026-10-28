from datetime import date, datetime, time, timedelta
from decimal import Decimal
from contextlib import closing

import json
import hashlib
import hmac
import io
from pathlib import Path
import sqlite3
import tempfile
import zipfile
import xml.etree.ElementTree as ElementTree
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.db.models import Sum
from django.test import Client, TestCase, override_settings
from asgiref.sync import async_to_sync
from django.urls import reverse
from django.utils import timezone

from .forms import (
    AdminProductCatalogForm,
    ProductForm,
    ShopForm,
    StorefrontProductEditForm,
)
from .models import (
    AdminProductCatalog,
    CheckoutAttempt,
    FavoriteShop,
    FavoriteProduct,
    MarketplaceLayoutSettings,
    ProductCloneSettings,
    SiteBrandingSettings,
    SiteThemeRevision,
    SiteThemeSettings,
    LegalAcceptance,
    Shop,
    ShopStaff,
    StaffDutySession,
    StaffLeaveApplication,
    StaffPayrollRecord,
    StaffResignationRequest,
    JobApplication,
    JobOpening,
    ClientProfile,
    ClientDeliveryAddress,
    PublicRiderApplication,
    ClientWalletTransaction,
    Product,
    HSNCodeDescription,
    SACCodeDescription,
    OrderVoiceInstruction,
    ProductCategory,
    ProductShop,
    Order,
    OrderItem,
    OrderStatusHistory,
    ShopOrder,
    Notification,
    NotificationDeviceToken,
    UserPresence,
    StockMovement,
    DeliverySharingSettings,
    RoadNetworkGeoJSON,
    SharedRiderGroup,
    SharedRiderGroupShop,
    ShopProductForecast,
    ShopVendor,
    VendorPurchase,
    VendorPurchaseItem,
    VendorPurchasePayment,
    ShopWallet,
    SiteWallet,
    SiteWalletTransaction,
    ShopPayout,
    ShopWalletTransaction,
    RiderCashLedgerEntry,
    SharedRiderDeliveryPayment,
    PublicRiderWallet,
    PublicRiderWalletTransaction,
    CODBalancePayment,
    CODPaymentApprovalRequest,
    ShopStaffWallet,
    ShopStaffWalletTransaction,
    Coupon,
    SavedBasket,
    SavedBasketItem,
    SQLiteBackupSettings,
)
from . import views as seller_views
from .sqlite_backups import create_sqlite_backup, restore_sqlite_backup
from .utils import distance_km_between_points

User = get_user_model()


def install_test_road_network(test_case, coordinates):
    from .road_routing import _load_road_graph

    road_data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "residential"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": coordinates,
                },
            },
        ],
    }
    settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
    _load_road_graph.cache_clear()
    settings.road_network_geojson.save(
        "test-road-network.geojson",
        SimpleUploadedFile(
            "test-road-network.geojson",
            json.dumps(road_data).encode("utf-8"),
            content_type="application/geo+json",
        ),
        save=True,
    )
    test_case.addCleanup(_load_road_graph.cache_clear)
    test_case.addCleanup(settings.road_network_geojson.delete, save=False)
    return settings


class SellerProjectConfigTests(TestCase):
    def test_admin_can_disable_public_rider_service_for_every_shop(self):
        settings_obj, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        settings_obj.public_rider_service_enabled = False
        settings_obj.save(update_fields=["public_rider_service_enabled"])
        owner = User.objects.create_user(username="global_rider_owner")
        shop = Shop.objects.create(
            name="Global Rider Toggle Shop",
            owner=owner,
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        order = Order.objects.create(
            order_id="GLOBAL-PUBLIC-RIDER-OFF",
            user="customer",
            status="READY",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )

        self.assertFalse(seller_views.public_rider_service_enabled())
        self.assertFalse(seller_views.uses_public_rider_network(shop_order))

    def test_public_rider_batch_radius_admin_setting_is_limited_to_configured_range(self):
        field = DeliverySharingSettings._meta.get_field(
            "public_rider_batch_radius_km",
        )
        self.assertEqual(field.clean(Decimal("0.5"), None), Decimal("0.5"))
        self.assertEqual(field.clean(Decimal("4.2"), None), Decimal("4.2"))
        for invalid_radius in (Decimal("0.4"), Decimal("4.3")):
            with self.subTest(invalid_radius=invalid_radius):
                with self.assertRaises(ValidationError):
                    field.clean(invalid_radius, None)

    def test_public_rider_decline_cooldown_admin_setting_defaults_to_90_seconds(self):
        field = DeliverySharingSettings._meta.get_field(
            "public_rider_decline_cooldown_minutes",
        )
        self.assertEqual(field.default, Decimal("1.5"))
        self.assertEqual(field.clean(Decimal("0.5"), None), Decimal("0.5"))
        self.assertEqual(field.clean(Decimal("60.0"), None), Decimal("60.0"))
        for invalid_minutes in (Decimal("0.4"), Decimal("60.1")):
            with self.subTest(invalid_minutes=invalid_minutes):
                with self.assertRaises(ValidationError):
                    field.clean(invalid_minutes, None)

    def test_public_rider_offer_controls_have_valid_admin_ranges(self):
        timeout = DeliverySharingSettings._meta.get_field(
            "public_rider_offer_timeout_seconds",
        )
        rider_count = DeliverySharingSettings._meta.get_field(
            "public_rider_offer_riders_per_round",
        )
        self.assertEqual(timeout.default, 60)
        self.assertEqual(timeout.clean(15, None), 15)
        self.assertEqual(timeout.clean(600, None), 600)
        self.assertEqual(rider_count.default, 5)
        self.assertEqual(rider_count.clean(1, None), 1)
        self.assertEqual(rider_count.clean(100, None), 100)
        max_active_offers = DeliverySharingSettings._meta.get_field(
            "public_rider_max_active_offers",
        )
        self.assertEqual(max_active_offers.default, 2)
        self.assertEqual(max_active_offers.clean(1, None), 1)
        self.assertEqual(max_active_offers.clean(20, None), 20)
        for field, invalid_value in (
            (timeout, 14),
            (timeout, 601),
            (rider_count, 0),
            (rider_count, 101),
            (max_active_offers, 0),
            (max_active_offers, 21),
        ):
            with self.subTest(field=field.name, value=invalid_value):
                with self.assertRaises(ValidationError):
                    field.clean(invalid_value, None)

    def test_public_rider_can_receive_order_inside_any_saved_work_area(self):
        from .public_rider_dispatch import eligible_public_riders
        from .public_riders import sync_public_rider_shop_memberships

        owner = User.objects.create_user(username="multi_area_rider_shop_owner")
        shop = Shop.objects.create(
            name="Multi-area public rider shop",
            owner=owner,
            location={"lat": 21.17, "lng": 72.83},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        rider_user = User.objects.create_user(username="multi_area_public_rider")
        rider_application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Multi-area Rider",
            phone="9876543201",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-MULTI-AREA-1",
            work_area={
                "type": "circle",
                "center": {"lat": 24.0, "lng": 72.0},
                "radius_km": 2,
            },
            work_areas=[
                {
                    "type": "circle",
                    "center": {"lat": 24.0, "lng": 72.0},
                    "radius_km": 2,
                },
                {
                    "type": "circle",
                    "center": {"lat": 21.17, "lng": 72.83},
                    "radius_km": 2,
                },
            ],
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        sync_public_rider_shop_memberships(rider_application)
        order = Order.objects.create(
            order_id="MULTI-AREA-PUBLIC-RIDER-ORDER",
            user="multi_area_customer",
            status="READY",
            placed_location={"lat": 21.18, "lng": 72.84},
            delivery_location="21.18,72.84",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )

        eligible = eligible_public_riders(shop_order)

        self.assertEqual(
            [rider.user_id for rider in eligible],
            [rider_user.pk],
        )

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        dashboard = self.client.get(reverse("rider_dashboard"))

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context["available_order_count"], 1)
        self.assertEqual(
            [available_order.pk for available_order in dashboard.context["orders"]],
            [shop_order.pk],
        )

    def test_abandoned_checkout_timeout_defaults_to_three_minutes_and_is_bounded(self):
        field = DeliverySharingSettings._meta.get_field(
            "abandoned_checkout_timeout_minutes",
        )
        self.assertEqual(field.default, 3)
        self.assertEqual(field.clean(1, None), 1)
        self.assertEqual(field.clean(1440, None), 1440)
        for invalid_value in (0, 1441):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ValidationError):
                    field.clean(invalid_value, None)

    def test_public_rider_offer_timers_are_randomized_with_a_twelve_second_floor(self):
        from .public_rider_dispatch import _next_public_rider_offer_timeout

        with patch(
            "seller.public_rider_dispatch.randbelow",
            side_effect=[0, 18],
        ):
            self.assertEqual(_next_public_rider_offer_timeout(30), 12)
            self.assertEqual(_next_public_rider_offer_timeout(30), 30)

    def test_delivery_sharing_settings_admin_change_form_renders(self):
        admin_user = User.objects.create_superuser(
            username="delivery_settings_admin",
            email="delivery_settings_admin@example.com",
            password="test-password",
        )
        sharing_settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        self.client.force_login(admin_user)

        response = self.client.get(
            reverse(
                "admin:seller_deliverysharingsettings_change",
                args=[sharing_settings.pk],
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Public/shared rider coverage and offer pay")
        self.assertContains(response, "shared_rider_base_pay")
        self.assertContains(response, "public_rider_offer_timeout_seconds")
        self.assertContains(response, "public_rider_offer_riders_per_round")
        self.assertContains(response, "public_rider_max_active_offers")
        self.assertContains(response, "public_rider_service_enabled")
        self.assertContains(response, "public_rider_pay_include_pickup_distance")
        self.assertContains(
            response,
            "public_rider_pickup_distance_pay_percentage",
        )
        self.assertContains(
            response,
            "public_rider_pickup_threshold_bonus_enabled",
        )
        self.assertContains(response, "abandoned_checkout_timeout_minutes")
        self.assertContains(response, "road_network_geojson")
        self.assertContains(response, "Road network GeoJSON files")
        self.assertContains(response, "geojson_file")
        response = self.client.post(
            reverse(
                "admin:seller_deliverysharingsettings_change",
                args=[sharing_settings.pk],
            ),
            {
                "shared_rider_radius_km": "0.35",
                "customer_direction_radius_km": "0.65",
                "public_rider_batch_radius_km": "1.0",
                "public_rider_decline_cooldown_minutes": "1.5",
                "public_rider_offer_timeout_seconds": "45",
                "public_rider_offer_riders_per_round": "3",
                "public_rider_max_active_offers": "4",
                "max_additional_pickup_locations": "3",
                "public_rider_service_enabled": "on",
                "abandoned_checkout_timeout_minutes": "5",
                "shared_rider_base_pay": "15.00",
                "shared_rider_rate_per_km": "3.50",
                "public_rider_pay_include_pickup_distance": "on",
                "public_rider_pickup_distance_pay_percentage": "75.00",
                "public_rider_pickup_threshold_bonus_enabled": "on",
                "public_rider_pickup_threshold_km": "2.000",
                "public_rider_pickup_threshold_rate_per_km": "3.00",
                "public_rider_pickup_threshold_distance_method": "ROAD",
                "packer_monthly_base_salary": "0.00",
                "packer_completed_order_bonus": "0.00",
                "rider_monthly_base_salary": "0.00",
                "rider_completed_order_bonus": "0.00",
                "road_network_files-TOTAL_FORMS": "1",
                "road_network_files-INITIAL_FORMS": "0",
                "road_network_files-MIN_NUM_FORMS": "0",
                "road_network_files-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 302)
        sharing_settings.refresh_from_db()
        self.assertEqual(sharing_settings.shared_rider_base_pay, Decimal("15.00"))
        self.assertEqual(sharing_settings.shared_rider_rate_per_km, Decimal("3.50"))
        self.assertTrue(sharing_settings.public_rider_pay_include_pickup_distance)
        self.assertEqual(
            sharing_settings.public_rider_pickup_distance_pay_percentage,
            Decimal("75.00"),
        )
        self.assertTrue(
            sharing_settings.public_rider_pickup_threshold_bonus_enabled,
        )
        self.assertEqual(
            sharing_settings.public_rider_pickup_threshold_km,
            Decimal("2.000"),
        )
        self.assertEqual(
            sharing_settings.public_rider_pickup_threshold_rate_per_km,
            Decimal("3.00"),
        )
        self.assertEqual(
            sharing_settings.public_rider_pickup_threshold_distance_method,
            "ROAD",
        )
        self.assertEqual(
            sharing_settings.public_rider_pickup_threshold_distance_method,
            "ROAD",
        )
        self.assertEqual(sharing_settings.public_rider_offer_timeout_seconds, 45)
        self.assertEqual(sharing_settings.public_rider_offer_riders_per_round, 3)
        self.assertEqual(sharing_settings.public_rider_max_active_offers, 4)
        self.assertEqual(sharing_settings.abandoned_checkout_timeout_minutes, 5)
        self.assertTrue(sharing_settings.public_rider_service_enabled)

    def test_local_road_router_uses_network_path_not_straight_line(self):
        from .road_routing import road_graph_from_geojson, road_route_distance_km

        road_data = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"highway": "residential"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [72.0, 23.0],
                            [72.001, 23.0],
                            [72.001, 23.001],
                        ],
                    },
                },
            ],
        }
        graph = road_graph_from_geojson(road_data)
        distance = road_route_distance_km(
            graph,
            {"lat": 23.0, "lng": 72.0},
            {"lat": 23.001, "lng": 72.001},
        )
        straight_line_distance = distance_km_between_points(
            23.0,
            72.0,
            23.001,
            72.001,
        )

        self.assertIsNotNone(distance)
        self.assertGreater(distance, straight_line_distance)

    def test_local_road_router_handles_one_way_road_dead_ends(self):
        from .road_routing import road_graph_from_geojson, road_route_distance_km

        road_data = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "highway": "residential",
                        "oneway": "yes",
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [72.0, 23.0],
                            [72.0001, 23.0],
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {
                        "highway": "residential",
                        "oneway": "yes",
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [72.0, 23.0],
                            [72.001, 23.0],
                        ],
                    },
                },
            ],
        }
        graph = road_graph_from_geojson(road_data)

        distance = road_route_distance_km(
            graph,
            {"lat": 23.0, "lng": 72.0},
            {"lat": 23.0, "lng": 72.001},
        )

        self.assertIsNotNone(distance)
        self.assertGreater(distance, 0)

    def test_local_road_router_combines_multiple_uploaded_geojson_files(self):
        from .road_routing import (
            _load_road_graph,
            local_road_route_distance_km,
        )

        first_network = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"highway": "residential"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[72.0, 23.0], [72.001, 23.0]],
                },
            }],
        }
        second_network = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"highway": "residential"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[72.001, 23.0], [72.002, 23.0]],
                },
            }],
        }
        settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                road_files = []
                for label, road_data in (
                    ("west area", first_network),
                    ("east area", second_network),
                ):
                    road_file = RoadNetworkGeoJSON(
                        delivery_settings=settings,
                        label=label,
                    )
                    road_file.geojson_file.save(
                        f"{label.replace(' ', '-')}.geojson",
                        SimpleUploadedFile(
                            f"{label}.geojson",
                            json.dumps(road_data).encode("utf-8"),
                            content_type="application/geo+json",
                        ),
                        save=True,
                    )
                    road_files.append(road_file)
                self.addCleanup(_load_road_graph.cache_clear)
                distance = local_road_route_distance_km(
                    tuple(
                        road_file.geojson_file.name
                        for road_file in road_files
                    ),
                    {"lat": 23.0, "lng": 72.0},
                    {"lat": 23.0, "lng": 72.002},
                )
                self.assertIsNotNone(distance)
                self.assertGreater(distance, 0)

    def test_public_rider_estimate_uses_local_road_distance_and_order_load(self):
        road_data = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"highway": "residential"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [72.0, 23.0],
                            [72.001, 23.0],
                            [72.001, 23.001],
                        ],
                    },
                },
            ],
        }
        owner = User.objects.create_user(
            username="local_route_shop_owner",
            password="testpass123",
        )
        shop = Shop.objects.create(
            name="Local Route Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
        )
        order = Order.objects.create(
            order_id="LOCAL-ROUTE-ORDER",
            user="local_route_customer",
            status="READY",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
            product_count=3,
            total_weight_kg=Decimal("2.40"),
        )
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                pay_settings, _ = DeliverySharingSettings.objects.get_or_create(
                    pk=1,
                )
                pay_settings.road_network_geojson.save(
                    "test-roads.geojson",
                    SimpleUploadedFile(
                        "test-roads.geojson",
                        json.dumps(road_data).encode("utf-8"),
                        content_type="application/geo+json",
                    ),
                    save=True,
                )
                estimate = seller_views._public_rider_route_estimate(
                    shop_order,
                    {"lat": 23.0, "lng": 72.0},
                    pay_settings,
                )
                self.assertIsNotNone(estimate)
                self.assertFalse(estimate["needs_rider_location"])
                self.assertEqual(
                    estimate["distance_calculation_method"],
                    "road",
                )
                self.assertGreater(
                    float(estimate["pickup_to_delivery_km"]),
                    distance_km_between_points(
                        23.0,
                        72.0,
                        23.001,
                        72.001,
                    ),
                )
                self.assertEqual(estimate["item_count"], 3)
                self.assertEqual(estimate["weight_kg"], Decimal("2.40"))
                self.assertGreater(estimate["volume_cubic_ft"], Decimal("0"))
                estimate_without_rider_location = (
                    seller_views._public_rider_route_estimate(
                        shop_order,
                        None,
                        pay_settings,
                    )
                )
                self.assertTrue(
                    estimate_without_rider_location["needs_rider_location"],
                )
                self.assertIsNotNone(
                    estimate_without_rider_location["pickup_to_delivery_km"],
                )

    def test_public_rider_route_estimate_explains_missing_road_data(self):
        owner = User.objects.create_user(
            username="missing_road_data_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Missing Road Data Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
        )
        order = Order.objects.create(
            order_id="MISSING-ROAD-DATA-ORDER",
            user="missing_road_data_customer",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )
        pay_settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)

        estimate = seller_views._public_rider_route_estimate(
            shop_order,
            {"lat": 23.0, "lng": 72.0},
            pay_settings,
        )

        self.assertIsNone(estimate["pickup_to_delivery_km"])
        self.assertEqual(
            estimate["distance_calculation_method"],
            "straight_line_fallback",
        )
        straight_line_rider_to_pickup = distance_km_between_points(
            23.0,
            72.0,
            23.0,
            72.0,
        )
        straight_line_pickup_to_delivery = distance_km_between_points(
            23.0,
            72.0,
            23.001,
            72.001,
        )
        expected_distance = (
            Decimal(str(
                straight_line_rider_to_pickup
                + straight_line_pickup_to_delivery,
            )) * Decimal("1.7")
        ).quantize(Decimal("0.001"))
        expected_pay = (
            pay_settings.shared_rider_base_pay
            + expected_distance * pay_settings.shared_rider_rate_per_km
        ).quantize(Decimal("0.01"))
        self.assertEqual(
            estimate["estimated_pay_distance_km"],
            expected_distance,
        )
        self.assertEqual(estimate["estimated_pay"], expected_pay)
        self.assertIsNotNone(estimate["straight_line_rider_to_pickup_km"])
        self.assertIsNotNone(estimate["straight_line_pickup_to_delivery_km"])
        pay_settings.public_rider_pickup_distance_pay_percentage = Decimal(
            "50.00",
        )
        partial_estimate = seller_views._public_rider_route_estimate(
            shop_order,
            {"lat": 23.0, "lng": 72.0},
            pay_settings,
        )
        expected_partial_distance = (
            (
                partial_estimate["straight_line_pickup_to_delivery_km"]
                + partial_estimate["straight_line_rider_to_pickup_km"]
                * Decimal("0.50")
            )
            * Decimal("1.7")
        ).quantize(Decimal("0.001"))
        self.assertEqual(
            partial_estimate["estimated_pay_distance_km"],
            expected_partial_distance,
        )
        self.assertEqual(
            partial_estimate["estimated_pay"],
            (
                pay_settings.shared_rider_base_pay
                + expected_partial_distance
                * pay_settings.shared_rider_rate_per_km
            ).quantize(Decimal("0.01")),
        )
        self.assertIn("administrator must upload", estimate["unavailable_reason"])
        estimate_without_rider_location = seller_views._public_rider_route_estimate(
            shop_order,
            None,
            pay_settings,
        )
        self.assertTrue(estimate_without_rider_location["needs_rider_location"])
        self.assertIsNone(estimate_without_rider_location["estimated_pay"])

    def test_public_rider_earnings_dashboard_calculates_period_totals(self):
        rider_user = User.objects.create_user(
            username="public_rider_earnings_summary",
            email="public_rider_earnings_summary@example.com",
            password="secret123",
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Earnings Rider",
            phone="9876543211",
            address="Earnings route",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-EARNINGS-1",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 10,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        wallet = PublicRiderWallet.objects.create(rider=application, balance=Decimal("0.00"))
        shop_owner = User.objects.create_user(
            username="public_rider_earnings_shop_owner",
            email="public_rider_earnings_shop_owner@example.com",
            password="secret123",
        )
        shop = Shop.objects.create(
            name="Earnings Shop",
            owner=shop_owner,
            location={"lat": 23.0, "lng": 72.0},
        )
        today_order_1 = Order.objects.create(
            order_id="EARN-ORDER-1",
            user="customer-one",
            status="DELIVERED",
            payment_type="COD",
            payment_amount=Decimal("120.00"),
            final_billing_amount=Decimal("120.00"),
            placed_location={"lat": 23.01, "lng": 72.01},
            delivery_location="23.01,72.01",
        )
        today_order_2 = Order.objects.create(
            order_id="EARN-ORDER-2",
            user="customer-two",
            status="DELIVERED",
            payment_type="COD",
            payment_amount=Decimal("80.00"),
            final_billing_amount=Decimal("80.00"),
            placed_location={"lat": 23.02, "lng": 72.02},
            delivery_location="23.02,72.02",
        )
        yesterday_order = Order.objects.create(
            order_id="EARN-ORDER-3",
            user="customer-three",
            status="DELIVERED",
            payment_type="COD",
            payment_amount=Decimal("70.00"),
            final_billing_amount=Decimal("70.00"),
            placed_location={"lat": 23.03, "lng": 72.03},
            delivery_location="23.03,72.03",
        )
        today_shop_order_1 = ShopOrder.objects.create(order=today_order_1, shop=shop, status="DELIVERED")
        today_shop_order_2 = ShopOrder.objects.create(order=today_order_2, shop=shop, status="DELIVERED")
        yesterday_shop_order = ShopOrder.objects.create(order=yesterday_order, shop=shop, status="DELIVERED")
        now = timezone.now()
        today_transaction = PublicRiderWalletTransaction.objects.create(
            wallet=wallet,
            shop_order=today_shop_order_1,
            transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
            amount=Decimal("120.00"),
            balance_after=Decimal("120.00"),
            description="Today fare",
        )
        second_today_transaction = PublicRiderWalletTransaction.objects.create(
            wallet=wallet,
            shop_order=today_shop_order_2,
            transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
            amount=Decimal("80.00"),
            balance_after=Decimal("200.00"),
            description="Today fare 2",
        )
        yesterday_transaction = PublicRiderWalletTransaction.objects.create(
            wallet=wallet,
            shop_order=yesterday_shop_order,
            transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
            amount=Decimal("70.00"),
            balance_after=Decimal("270.00"),
            description="Yesterday fare",
        )
        PublicRiderWalletTransaction.objects.filter(pk=today_transaction.pk).update(
            created_at=now.replace(hour=9, minute=0, second=0, microsecond=0),
        )
        PublicRiderWalletTransaction.objects.filter(pk=second_today_transaction.pk).update(
            created_at=now.replace(hour=18, minute=0, second=0, microsecond=0),
        )
        PublicRiderWalletTransaction.objects.filter(pk=yesterday_transaction.pk).update(
            created_at=now - timedelta(days=1),
        )
        self.client.force_login(rider_user)
        self.shop.allow_product_image_overrides = True
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.assignment.image_url_override = "https://example.com/workflow-product.jpg"
        self.assignment.save(update_fields=["image_url_override"])
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "earnings", "period": "today"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["earnings_view"])
        self.assertEqual(
            response.context["public_rider_earnings_summary"]["total"],
            Decimal("200.00"),
        )
        self.assertEqual(
            response.context["public_rider_earnings_summary"]["orders"],
            2,
        )
        self.assertContains(response, "Earnings overview")
        self.assertContains(response, "Today")

    def test_public_rider_dashboard_has_home_and_multi_area_controls(self):
        rider_user = User.objects.create_user(
            username="public_rider_home_settings",
            email="public_rider_home_settings@example.com",
            password="test-password",
        )
        PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Home Settings Rider",
            phone="9876543210",
            address="Home area address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-SETTINGS-1",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 5.0,
            },
            home_area={
                "type": "circle",
                "center": {"lat": 23.01, "lng": 72.01},
                "radius_km": 3.0,
            },
            return_home_enabled=True,
            return_home_after_minutes=120,
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        response = self.client.get(reverse("rider_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="work_areas"')
        self.assertContains(response, 'name="home_area"')
        self.assertContains(response, 'name="return_home_after_minutes"')
        self.assertContains(response, 'name="return_home_start_time"')
        self.assertContains(response, 'name="return_home_enabled"')
        self.assertContains(response, 'id="publicRiderWorkArea" class="card shadow-sm mb-4 public-rider-work-area">')
        self.assertContains(response, 'id="publicRiderPlaceHomePin"')
        self.assertContains(response, 'id="publicRiderClearHomeArea"')
        self.assertContains(response, "Home location cleared. Save work area")
        self.assertContains(response, "Work area &amp; home settings")
        self.assertContains(response, "Click the map where your home is")
        self.assertContains(response, 'color: "#e85d04"')
        self.assertContains(response, "Go to home")
        self.assertContains(response, reverse("notifications"))
        self.assertContains(response, "bi-bell")

    def test_public_rider_homeward_mode_only_receives_orders_toward_home(self):
        owner = User.objects.create_user(
            username="homeward_offer_shop_owner",
            email="homeward_offer_shop_owner@example.com",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Homeward Offer Shop",
            owner=owner,
            location={"lat": 23.1, "lng": 72.0},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        rider_user = User.objects.create_user(
            username="homeward_offer_rider",
            email="homeward_offer_rider@example.com",
            password="test-password",
        )
        now = timezone.make_aware(
            datetime(2026, 10, 14, 13, 30),
            timezone.get_current_timezone(),
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Homeward Offer Rider",
            phone="9876543211",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-HOMEWARD-1",
            work_area={
                "type": "circle",
                "center": {"lat": 23.1, "lng": 72.0},
                "radius_km": 30,
            },
            home_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 2,
            },
            return_home_enabled=True,
            return_home_after_minutes=120,
            return_home_start_time=time(13, 0),
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        from .public_rider_dispatch import eligible_public_riders
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        rider_membership = ShopStaff.objects.get(
            shop=shop,
            user=rider_user,
            is_public_rider=True,
        )
        rider_membership.last_location = {
            "lat": 23.1,
            "lng": 72.0,
            "observed_at": now.timestamp(),
        }
        rider_membership.save(update_fields=["last_location", "updated_at"])

        homeward_order = Order.objects.create(
            order_id="HOMEWARD-OFFER-ORDER",
            user="homeward_customer",
            status="READY",
            placed_location={"lat": 23.05, "lng": 72.0},
            delivery_location="23.05,72.0",
        )
        homeward_shop_order = ShopOrder.objects.create(
            order=homeward_order,
            shop=shop,
            status="READY",
        )
        away_order = Order.objects.create(
            order_id="AWAYWARD-OFFER-ORDER",
            user="awayward_customer",
            status="READY",
            placed_location={"lat": 23.15, "lng": 72.0},
            delivery_location="23.15,72.0",
        )
        away_shop_order = ShopOrder.objects.create(
            order=away_order,
            shop=shop,
            status="READY",
        )

        def offers_at(when, candidate):
            rider_membership.last_location = {
                "lat": 23.1,
                "lng": 72.0,
                "observed_at": when.timestamp(),
            }
            rider_membership.save(update_fields=["last_location", "updated_at"])
            with patch(
                "seller.public_rider_dispatch.timezone.now",
                return_value=when,
            ):
                return eligible_public_riders(candidate)

        self.assertEqual(
            [rider.user_id for rider in offers_at(now, homeward_shop_order)],
            [rider_user.pk],
        )
        self.assertEqual(offers_at(now, away_shop_order), [])
        before_window = now.replace(hour=12, minute=59)
        self.assertEqual(
            [rider.user_id for rider in offers_at(before_window, away_shop_order)],
            [rider_user.pk],
        )
        after_window = now.replace(hour=15, minute=0)
        self.assertEqual(
            [rider.user_id for rider in offers_at(after_window, away_shop_order)],
            [rider_user.pk],
        )

        application.return_home_start_time = time(23, 0)
        application.save(update_fields=["return_home_start_time", "updated_at"])
        during_overnight_window = now.replace(hour=0, minute=30)
        self.assertEqual(offers_at(during_overnight_window, away_shop_order), [])
        after_overnight_window = now.replace(hour=1, minute=0)
        self.assertEqual(
            [
                rider.user_id
                for rider in offers_at(after_overnight_window, away_shop_order)
            ],
            [rider_user.pk],
        )

    def test_public_rider_homeward_schedule_saves_daily_start_time_and_duration(self):
        owner = User.objects.create_user(
            username="homeward_schedule_shop_owner",
            email="homeward_schedule_shop_owner@example.com",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="homeward_schedule_rider",
            email="homeward_schedule_rider@example.com",
            password="test-password",
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Homeward Schedule Rider",
            phone="9876543212",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-HOMEWARD-3",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 5,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        response = self.client.post(
            reverse("save_public_rider_work_area"),
            {
                "work_area": json.dumps({
                    "type": "circle",
                    "center": {"lat": 23.0, "lng": 72.0},
                    "radius_km": 5,
                }),
                "home_area": json.dumps({
                    "type": "circle",
                    "center": {"lat": 23.0, "lng": 72.0},
                    "radius_km": 2,
                }),
                "return_home_enabled": "on",
                "return_home_start_time": "13:00",
                "return_home_after_minutes": "120",
            },
        )

        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        self.assertTrue(application.return_home_enabled)
        self.assertEqual(application.return_home_start_time, time(13, 0))
        self.assertEqual(application.return_home_after_minutes, 120)

    def test_public_rider_location_updates_still_save_after_schedule_change(self):
        owner = User.objects.create_user(
            username="homeward_gps_shop_owner",
            email="homeward_gps_shop_owner@example.com",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Homeward GPS Shop",
            owner=owner,
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        rider_user = User.objects.create_user(
            username="homeward_gps_rider",
            email="homeward_gps_rider@example.com",
            password="test-password",
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Homeward GPS Rider",
            phone="9876543213",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-HOMEWARD-4",
            home_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 2,
            },
            return_home_enabled=True,
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        location_response = self.client.post(
            reverse("update_public_rider_location"),
            {"latitude": "23.1", "longitude": "72.0"},
        )
        self.assertEqual(location_response.status_code, 200)
        rider_membership = ShopStaff.objects.get(
            shop=shop,
            user=rider_user,
        )
        self.assertEqual(rider_membership.last_location["lat"], 23.1)

    def test_secret_key_is_not_default_placeholder(self):
        self.assertNotIn("django-insecure", settings.SECRET_KEY.lower())

    def test_default_auto_field_is_configured(self):
        self.assertEqual(settings.DEFAULT_AUTO_FIELD, "django.db.models.BigAutoField")

    def test_sqlite_backup_settings_start_with_requested_defaults(self):
        backup_settings = SQLiteBackupSettings.objects.get(pk=1)

        self.assertEqual(backup_settings.interval_minutes, 30)
        self.assertEqual(backup_settings.retention_count, 5)

    def test_core_routes_resolve(self):
        self.assertEqual(reverse("home"), "/")
        self.assertEqual(reverse("login"), "/accounts/login/")
        self.assertEqual(reverse("signup"), "/signup/")

    def test_authenticated_websocket_receives_live_order_events(self):
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from .consumers import LiveUpdatesConsumer

        user = User.objects.create_user(
            username="websocket_customer",
            password="test-websocket-password",
        )

        async def connect_and_receive():
            communicator = WebsocketCommunicator(
                LiveUpdatesConsumer.as_asgi(),
                "/ws/live/",
            )
            communicator.scope["user"] = user
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            ready_message = await communicator.receive_json_from()
            self.assertEqual(ready_message["type"], "connection.ready")
            self.assertEqual(
                ready_message["shared_channel_layer"],
                settings.CHANNEL_LAYERS["default"]["BACKEND"]
                == "channels_redis.core.RedisChannelLayer",
            )
            await get_channel_layer().group_send(
                f"live_user_{user.pk}",
                {"type": "live.event", "event_type": "orders.updated"},
            )
            message = await communicator.receive_json_from()
            await communicator.disconnect()
            return message

        self.assertEqual(
            async_to_sync(connect_and_receive)(),
            {"type": "orders.updated"},
        )

    def test_redis_websocket_receives_event_after_idle_period(self):
        if (
            settings.CHANNEL_LAYERS["default"]["BACKEND"]
            != "channels_redis.core.RedisChannelLayer"
        ):
            self.skipTest("Redis-backed channel layer is not configured")

        import asyncio
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from .consumers import LiveUpdatesConsumer

        user = User.objects.create_user(
            username="websocket_idle_customer",
        )

        async def connect_wait_and_receive():
            communicator = WebsocketCommunicator(
                LiveUpdatesConsumer.as_asgi(),
                "/ws/live/",
            )
            communicator.scope["user"] = user
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.receive_json_from()
            await asyncio.sleep(6)
            await get_channel_layer().group_send(
                f"live_user_{user.pk}",
                {"type": "live.event", "event_type": "orders.updated"},
            )
            message = await communicator.receive_json_from(timeout=2)
            await communicator.disconnect()
            return message

        self.assertEqual(
            async_to_sync(connect_wait_and_receive)(),
            {"type": "orders.updated"},
        )

    def test_anonymous_websocket_connection_is_rejected(self):
        from channels.testing import WebsocketCommunicator
        from sellerhelper.asgi import application

        async def connect_anonymously():
            communicator = WebsocketCommunicator(
                application,
                "/ws/live/",
                headers=[(b"origin", b"http://testserver")],
            )
            return await communicator.connect()

        connected, close_code = async_to_sync(connect_anonymously)()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4401)

    def test_order_update_reaches_customer_owner_and_shop_staff(self):
        from .live_updates import order_update_recipients

        owner = User.objects.create_user(
            username="websocket_shop_owner",
            password="test-websocket-password",
        )
        customer = User.objects.create_user(
            username="websocket_order_customer",
            password="test-websocket-password",
        )
        staff_user = User.objects.create_user(
            username="websocket_order_staff",
            password="test-websocket-password",
        )
        rider_user = User.objects.create_user(
            username="websocket_order_rider",
            password="test-websocket-password",
        )
        packer_user = User.objects.create_user(
            username="websocket_order_packer",
            password="test-websocket-password",
        )
        available_rider_user = User.objects.create_user(
            username="websocket_available_rider",
        )
        nearby_rider_user = User.objects.create_user(
            username="websocket_nearby_rider",
        )
        shop = Shop.objects.create(name="Websocket Shop", owner=owner)
        shop.share_riders_with_nearby_shops = True
        shop.save(update_fields=["share_riders_with_nearby_shops"])
        ShopStaff.objects.create(
            shop=shop,
            user=staff_user,
            role="MANAGER",
            is_active=True,
        )
        packer = ShopStaff.objects.create(
            shop=shop,
            user=packer_user,
            role="PACKER",
            is_active=True,
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        ShopStaff.objects.create(
            shop=shop,
            user=available_rider_user,
            role="RIDER",
            is_active=True,
        )
        nearby_shop = Shop.objects.create(name="Websocket Nearby Shop", owner=owner)
        ShopStaff.objects.create(
            shop=nearby_shop,
            user=nearby_rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-WEBSOCKET-RECIPIENTS",
            user=customer.username,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            assigned_packer=packer,
            assigned_rider=rider,
        )

        self.assertEqual(
            order_update_recipients(shop_order),
            {
                owner.pk,
                customer.pk,
                staff_user.pk,
                packer_user.pk,
                rider_user.pk,
                available_rider_user.pk,
                nearby_rider_user.pk,
            },
        )
        with (
            patch("seller.live_updates._send_user_event") as send_event,
            patch(
                "seller.live_updates.submit_background_task",
                side_effect=lambda task, *args, **kwargs: task(*args),
            ),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                shop_order.status = "PACKING"
                shop_order.save(update_fields=["status"])

        self.assertEqual(
            {call.args[0] for call in send_event.call_args_list},
            order_update_recipients(shop_order),
        )
        self.assertEqual(
            {call.args[1] for call in send_event.call_args_list},
            {"orders.updated"},
        )

    def test_order_update_reaches_riders_in_approved_shared_group(self):
        from .live_updates import order_update_recipients

        owner = User.objects.create_user(username="websocket_group_owner")
        customer = User.objects.create_user(username="websocket_group_customer")
        rider_user = User.objects.create_user(username="websocket_group_rider")
        shop = Shop.objects.create(name="Websocket Group Shop", owner=owner)
        rider_shop = Shop.objects.create(
            name="Websocket Group Rider Shop",
            owner=owner,
        )
        rider = ShopStaff.objects.create(
            shop=rider_shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        group = SharedRiderGroup.objects.create(
            name="Websocket Rider Group",
            owner=owner,
            created_from_shop=shop,
        )
        SharedRiderGroupShop.objects.create(
            group=group,
            shop=shop,
            status=SharedRiderGroupShop.STATUS_APPROVED,
            requested_by=owner,
        )
        SharedRiderGroupShop.objects.create(
            group=group,
            shop=rider_shop,
            status=SharedRiderGroupShop.STATUS_APPROVED,
            requested_by=owner,
        )
        order = Order.objects.create(
            order_id="ORD-WEBSOCKET-SHARED-GROUP",
            user=customer.username,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop)

        self.assertIn(rider.user_id, order_update_recipients(shop_order))

    def test_public_information_and_legal_pages_render(self):
        pages = (
            ("about", "About SellerHelper"),
            ("contact", "Contact Us"),
            ("terms", "Terms and Conditions"),
            ("privacy_policy", "Privacy Policy"),
            ("disclaimer", "Disclaimer"),
            ("refund_policy", "Cancellation and Refund Policy"),
            ("delivery_policy", "Delivery Policy"),
            ("grievance_policy", "Grievance Redressal"),
        )

        for route_name, heading in pages:
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, heading)
                self.assertContains(response, 'aria-label="About and legal"')

    @override_settings(
        SUPPORT_EMAIL="help@example.test",
        SUPPORT_PHONE="+911234567890",
        GRIEVANCE_OFFICER_NAME="Support Officer",
        GRIEVANCE_OFFICER_EMAIL="grievance@example.test",
    )
    def test_contact_page_uses_configured_support_and_grievance_details(self):
        response = self.client.get(reverse("contact"))

        self.assertContains(response, "mailto:help@example.test")
        self.assertContains(response, "tel:+911234567890")
        self.assertContains(response, "Support Officer")
        self.assertContains(response, "mailto:grievance@example.test")


    def test_login_page_displays_and_preselects_current_workspace(self):
        session = self.client.session
        session["active_portal"] = "SHOP_OWNER"
        session.save()

        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_portal"], "SHOP_OWNER")
        self.assertContains(response, "Selected workspace: <strong id=\"selectedWorkspaceName\">Shop owner</strong>")
        self.assertContains(
            response,
            'value="SHOP_OWNER" data-workspace-label="Shop owner" checked',
        )

    def test_login_page_remembers_user_id_only_and_enables_password_manager(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'autocomplete="username"')
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertContains(response, "Remember this user ID and workspace on this device")
        self.assertContains(response, 'id="rememberedLoginSelect"')
        self.assertContains(response, 'id="forgetRememberedLogin"')
        self.assertContains(response, "sellerhelper.rememberedLogins")
        self.assertContains(response, "account.portal")
        self.assertContains(response, "Passwords are never stored here.")
        self.assertContains(response, "sellerhelper.rememberedLoginUsername")
        self.assertContains(response, "usernameInput.value.trim()")
        self.assertNotContains(response, "rememberedLoginPassword")

    def test_login_page_offers_public_rider_workspace(self):
        response = self.client.get(reverse("login"))

        self.assertContains(response, 'value="RIDER" data-workspace-label="Public rider"')
        self.assertContains(response, ">Public rider</span>")
        self.assertContains(response, "bi-bicycle")
        self.assertContains(response, reverse("public_rider_apply"))

    def test_public_rider_application_is_a_home_card_not_a_bottom_nav_item(self):
        user = User.objects.create_user(
            username="public_rider_home_customer",
            password="test-password",
        )
        ClientProfile.objects.create(user=user, name="Public Rider Applicant")
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-component="home_public_rider_card"')
        self.assertContains(response, "Become a public rider")
        self.assertContains(response, "Apply now")
        self.assertRegex(
            response.content.decode(),
            r'(?s)<section class="card portal-card p-4 mt-3" '
            r'data-theme-component="home_public_rider_card".*?'
            r'href="{}"'.format(reverse("public_rider_apply")),
        )
        self.assertNotRegex(
            response.content.decode(),
            r'(?s)<div class="mobile-bottom-nav mobile-only".*?'
            r'Become a public rider.*?</div>',
        )


    def test_staff_user_dashboard_redirects_to_staff_dashboard(self):
        owner = User.objects.create_user(username="owner", password="pass1234")
        staff_user = User.objects.create_user(username="staff", password="pass1234")
        shop = Shop.objects.create(name="Test Shop", owner=owner)
        ShopStaff.objects.create(shop=shop, user=staff_user, role="MANAGER", is_active=True)

        self.client.force_login(staff_user)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("staff_dashboard"))

    def test_login_as_staff_opens_their_assigned_workflow(self):
        owner = User.objects.create_user(
            username="portal_staff_owner",
            password="test-password",
        )
        staff_user = User.objects.create_user(
            username="portal_staff_user",
            password="test-password",
        )
        shop = Shop.objects.create(name="Portal Staff Shop", owner=owner)
        ShopStaff.objects.create(
            shop=shop,
            user=staff_user,
            role="RIDER",
            is_active=True,
        )

        response = self.client.post(
            reverse("login"),
            {
                "username": staff_user.username,
                "password": "test-password",
                "login_as": "SHOP_STAFF",
            },
        )

        self.assertRedirects(response, reverse("rider_dashboard"))
        self.assertEqual(self.client.session["active_portal"], "SHOP_STAFF")

        home_response = self.client.get(reverse("home"))
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "Rider · Portal Staff Shop")
        self.assertContains(home_response, "Rider deliveries")

    def test_shop_rider_must_use_shop_staff_workspace(self):
        owner = User.objects.create_user(
            username="portal_rider_owner",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="portal_rider_user",
            password="test-password",
        )
        shop = Shop.objects.create(name="Portal Rider Shop", owner=owner)
        ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )

        response = self.client.post(
            reverse("login"),
            {
                "username": rider_user.username,
                "password": "test-password",
                "login_as": "RIDER",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shop riders must sign in using the Shop staff workspace.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_public_rider_login_requires_admin_approval(self):
        user = User.objects.create_user(
            username="portal_not_rider_user",
            password="test-password",
        )
        owner = User.objects.create_user(
            username="portal_not_rider_owner",
            password="test-password",
        )
        shop = Shop.objects.create(name="Portal Not Rider Shop", owner=owner)
        ShopStaff.objects.create(
            shop=shop,
            user=user,
            role="MANAGER",
            is_active=True,
        )

        response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "test-password",
                "login_as": "RIDER",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your public rider application must be approved")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_customer_can_apply_with_private_documents_and_login_after_approval(self):
        user = User.objects.create_user(
            username="public_rider_applicant",
            password="test-password",
        )
        ClientProfile.objects.create(user=user, name="Applicant", contact_no="9876543210")
        shop = Shop.objects.create(
            name="Public Network Shop",
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )

        self.client.force_login(user)
        response = self.client.post(
            reverse("public_rider_apply"),
            {
                "full_name": "Applicant Rider",
                "phone": "+919876543210",
                "address": "12 Main Road, Ahmedabad",
                "vehicle_type": "MOTORCYCLE",
                "driving_license_number": "DL-123456",
                "consent_to_verification": "on",
                "license_document": SimpleUploadedFile(
                    "license.pdf",
                    b"%PDF-1.4 license",
                    content_type="application/pdf",
                ),
                "identity_document": SimpleUploadedFile(
                    "identity.pdf",
                    b"%PDF-1.4 identity",
                    content_type="application/pdf",
                ),
            },
        )

        application = PublicRiderApplication.objects.get(user=user)
        self.assertRedirects(response, reverse("public_rider_apply"))
        self.assertEqual(application.status, PublicRiderApplication.STATUS_PENDING)
        self.addCleanup(application.license_document.delete, save=False)
        self.addCleanup(application.identity_document.delete, save=False)
        self.assertEqual(
            Path(application.license_document.storage.location).name,
            "private_rider_documents",
        )
        with self.assertRaises(ValueError):
            application.license_document.url
        self.assertFalse(ShopStaff.objects.filter(user=user, shop=shop).exists())

        reviewer = User.objects.create_superuser(
            username="public_rider_reviewer",
            email="reviewer@example.test",
            password="test-password",
        )
        self.client.force_login(reviewer)
        document_response = self.client.get(
            reverse(
                "admin:seller_publicriderapplication_document",
                args=(application.pk, "license_document"),
            ),
        )
        self.assertEqual(document_response.status_code, 200)
        self.assertIn(b"%PDF-1.4 license", b"".join(document_response.streaming_content))

        self.client.logout()
        login_response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "test-password",
                "login_as": "RIDER",
            },
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, "must be approved")
        self.assertNotIn("_auth_user_id", self.client.session)

        application.status = PublicRiderApplication.STATUS_APPROVED
        application.save(update_fields=["status", "updated_at"])
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        approved_response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "test-password",
                "login_as": "RIDER",
            },
        )

        self.assertRedirects(approved_response, reverse("rider_dashboard"))
        self.assertEqual(self.client.session["active_portal"], "RIDER")
        self.assertTrue(
            ShopStaff.objects.filter(
                user=user,
                shop=shop,
                role="RIDER",
                is_public_rider=True,
                is_active=True,
            ).exists()
        )

    def test_public_rider_prepaid_delivery_fare_is_credited_once_to_wallet(self):
        owner = User.objects.create_user(
            username="public_prepaid_wallet_owner",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="public_prepaid_wallet_rider",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Public Prepaid Wallet Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
            public_rider_network_enabled=True,
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Prepaid Public Rider",
            phone="9876543211",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-PREPAID-WALLET",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        rider_membership = ShopStaff.objects.get(
            shop=shop,
            user=rider_user,
            is_public_rider=True,
        )
        order = Order.objects.create(
            order_id="PUBLIC-PREPAID-WALLET-ORDER",
            user="public_prepaid_wallet_customer",
            status="OUT_FOR_DELIVERY",
            payment_type="RAZORPAY",
            payment_status="PAID",
            is_payment_done=True,
            final_billing_amount=Decimal("100.00"),
            payment_amount=Decimal("100.00"),
            delivery_location="23.001,72.001",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("100.00"),
            status="OUT_FOR_DELIVERY",
            assigned_rider=rider_membership,
            public_rider_route_estimate={
                "estimated_pay": "48.50",
                "total_distance_km": "12.000",
                "base_fare": "12.50",
                "rate_per_km": "3.00",
            },
        )

        seller_views._finish_rider_delivery(
            shop_order,
            order,
            rider_membership,
        )
        rider_wallet = PublicRiderWallet.objects.get(rider=application)
        self.assertEqual(rider_wallet.balance, Decimal("48.50"))
        fare_entries = PublicRiderWalletTransaction.objects.filter(
            shop_order=shop_order,
            transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
        )
        self.assertEqual(fare_entries.count(), 1)
        self.assertEqual(fare_entries.get().amount, Decimal("48.50"))
        self.assertFalse(
            RiderCashLedgerEntry.objects.filter(shop_order=shop_order).exists(),
        )

        seller_views._credit_shop_wallet_for_delivery(shop_order)
        rider_wallet.refresh_from_db()
        self.assertEqual(rider_wallet.balance, Decimal("48.50"))
        self.assertEqual(fare_entries.count(), 1)
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="RIDER_FEE",
            ).count(),
            1,
        )

    def test_public_rider_can_claim_orders_only_from_opted_in_shops(self):
        install_test_road_network(self, [
            [72.0, 23.0],
            [72.001, 23.0],
            [72.001, 23.001],
        ])
        user = User.objects.create_user(
            username="network_rider_user",
            password="test-password",
        )
        ClientProfile.objects.create(user=user, name="Network Rider")
        application = PublicRiderApplication.objects.create(
            user=user,
            full_name="Network Rider",
            phone="9876543210",
            address="22 Rider Street",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-98765",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        owner = User.objects.create_user(
            username="public_network_shop_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Network Delivery Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
            require_customer_otp_verification=False,
        )
        order = Order.objects.create(
            order_id="PUBLIC-RIDER-ORDER",
            user=user.username,
            status="READY",
            payment_type="COD",
            payment_amount=Decimal("25.00"),
            final_billing_amount=Decimal("25.00"),
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )

        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        self.client.force_login(user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        offline_response = self.client.post(
            reverse("set_public_rider_availability"),
            {"is_online": "false", "view": "available"},
        )
        self.assertEqual(
            offline_response.url,
            f"{reverse('rider_dashboard')}?view=available",
        )
        application.refresh_from_db()
        self.assertFalse(application.is_online)
        offline_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(offline_dashboard.context["available_order_count"], 0)
        self.assertContains(offline_dashboard, "Offline")
        self.assertContains(offline_dashboard, "Go online")
        self.assertNotRegex(
            offline_dashboard.content.decode(),
            r'<input id="publicRiderAvailabilityToggle"[^>]*aria-label="Go online"[^>]*checked',
        )
        shop_order.refresh_from_db()
        self.assertIsNone(shop_order.public_rider_offer_user_id)

        online_response = self.client.post(
            reverse("set_public_rider_availability"),
            {"is_online": "true", "view": "available"},
        )
        self.assertEqual(
            online_response.url,
            f"{reverse('rider_dashboard')}?view=available",
        )
        application.refresh_from_db()
        self.assertTrue(application.is_online)

        dashboard_response = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertTrue(dashboard_response.context["is_public_rider_portal"])
        self.assertFalse(dashboard_response.context["is_customer_portal"])
        self.assertContains(dashboard_response, "Deliveries")
        self.assertContains(dashboard_response, "Work area &amp; home settings")
        self.assertContains(dashboard_response, "UPI payment settings")
        self.assertContains(dashboard_response, "Page zoom")
        self.assertContains(dashboard_response, 'data-rider-zoom-adjust="-0.1"')
        self.assertContains(dashboard_response, 'data-rider-zoom-adjust="0.1"')
        self.assertContains(dashboard_response, "Lock zoom")
        self.assertContains(
            dashboard_response,
            f'<a href="{reverse("rider_dashboard")}">',
        )
        rendered_dashboard = dashboard_response.content.decode()
        availability_control_index = rendered_dashboard.index(
            'aria-label="Public rider availability"',
        )
        collapsed_menu_index = rendered_dashboard.index(
            'id="mainNavbar"',
        )
        self.assertLess(availability_control_index, collapsed_menu_index)
        self.assertLess(
            rendered_dashboard.index('id="mainNavbar"'),
            rendered_dashboard.index('class="rider-content-zoom-panel"'),
        )
        self.assertLess(
            rendered_dashboard.index('class="rider-content-zoom-panel"'),
            rendered_dashboard.index('class="dropdown-menu"'),
        )
        self.assertContains(
            dashboard_response,
            'data-sellerhelper-scan-order="rider-pickup-order"',
        )
        self.assertContains(dashboard_response, "Deliveries")
        self.assertContains(dashboard_response, "data-public-rider-offer-count")
        self.assertContains(
            dashboard_response,
            'aria-label="1 new delivery offer">1</span>',
        )
        self.assertContains(dashboard_response, "Go offline")
        self.assertContains(
            dashboard_response,
            'class="nav nav-pills gap-2 mb-4 rider-dashboard-tabs"',
        )
        self.assertContains(
            dashboard_response,
            'aria-label="More rider dashboard tabs"',
        )
        self.assertContains(
            dashboard_response,
            '<details class="rider-dashboard-more-tabs">',
        )
        self.assertContains(
            dashboard_response,
            '<summary class="nav-link rider-dashboard-more-toggle',
        )
        self.assertContains(
            dashboard_response,
            '<i class="bi bi-chevron-down" aria-hidden="true"></i>',
        )
        self.assertNotContains(dashboard_response, ">More</span>")
        self.assertContains(dashboard_response, ">Available</span>")
        self.assertContains(dashboard_response, ">Active</span>")
        self.assertContains(dashboard_response, ">Recently verified</span>")
        self.assertContains(dashboard_response, ">Done</span>")
        self.assertContains(dashboard_response, "flex-wrap: nowrap")
        self.assertContains(dashboard_response, "overflow-x: auto")
        self.assertRegex(
            rendered_dashboard,
            r'<input id="publicRiderAvailabilityToggle"[^>]*aria-label="Go offline"[^>]*checked',
        )
        self.assertLess(
            rendered_dashboard.index("Handover"),
            rendered_dashboard.index("Stats"),
        )
        self.assertContains(dashboard_response, "Public rider earnings wallet")
        self.assertLess(
            rendered_dashboard.index("rider-order-track"),
            rendered_dashboard.index("Public rider earnings wallet"),
        )
        self.assertLess(
            rendered_dashboard.index('id="showNearbyRiderOrders"'),
            rendered_dashboard.index('class="row g-3 rider-order-track"'),
        )
        self.assertLess(
            rendered_dashboard.index('class="row g-3 rider-order-track"'),
            rendered_dashboard.index("Public rider network"),
        )
        self.assertLess(
            rendered_dashboard.index("Public rider network"),
            rendered_dashboard.index('id="publicRiderDispatch"'),
        )
        self.assertLess(
            rendered_dashboard.index('id="publicRiderDispatch"'),
            rendered_dashboard.index('id="publicRiderWorkArea"'),
        )
        self.assertEqual(dashboard_response.context["available_order_count"], 1)
        self.assertEqual(
            [row.pk for row in dashboard_response.context["orders"]],
            [shop_order.pk],
        )
        self.assertContains(dashboard_response, "Estimated delivery route &amp; rider pay")
        self.assertContains(
            dashboard_response,
            "Straight-line distances (display only; pay uses road distance):",
        )
        self.assertContains(dashboard_response, "Current location to pickup:")
        self.assertContains(dashboard_response, "Pickup to delivery:")
        offer_estimate_index = rendered_dashboard.index(
            f'id="rider-next-action-{shop_order.pk}"',
        )
        offer_timer_index = rendered_dashboard.index(
            "data-public-rider-offer",
            offer_estimate_index,
        )
        order_heading_index = rendered_dashboard.index(
            '<h5 class="mb-1">#',
            offer_estimate_index,
        )
        self.assertLess(offer_estimate_index, offer_timer_index)
        self.assertLess(offer_timer_index, order_heading_index)
        offer_timer_badge_index = rendered_dashboard.rindex(
            'class="badge text-bg-info text-nowrap"',
            offer_estimate_index,
            offer_timer_index,
        )
        self.assertLess(offer_estimate_index, offer_timer_badge_index)
        pay_settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        pay_settings.shared_rider_base_pay = Decimal("12.50")
        pay_settings.shared_rider_rate_per_km = Decimal("2.75")
        pay_settings.save(update_fields=[
            "shared_rider_base_pay",
            "shared_rider_rate_per_km",
        ])
        offer_notification_response = self.client.get(
            reverse("notifications_api"),
        )
        self.assertEqual(
            offer_notification_response.json()["available_delivery_count"],
            1,
        )
        self.assertEqual(
            offer_notification_response.json()["active_delivery_count"],
            0,
        )
        self.assertEqual(
            offer_notification_response.json()["public_offer_count"],
            1,
        )

        accept_response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
            {
                "rider_latitude": "23.0",
                "rider_longitude": "72.0",
                "rider_location_observed_at": str(timezone.now().timestamp()),
            },
        )

        self.assertEqual(accept_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")
        self.assertEqual(shop_order.assigned_rider.user_id, user.id)
        self.assertTrue(shop_order.assigned_rider.is_public_rider)
        self.assertEqual(
            shop_order.public_rider_route_estimate["rider_location"]["lat"],
            23.0,
        )
        self.assertEqual(
            shop_order.public_rider_route_estimate["rider_location"]["lng"],
            72.0,
        )
        route_estimate = shop_order.public_rider_route_estimate
        self.assertEqual(route_estimate["rider_to_pickup_km"], "0.000")
        expected_pay = (
            Decimal(route_estimate["base_fare"])
            + Decimal(route_estimate["total_distance_km"])
            * Decimal(route_estimate["rate_per_km"])
        ).quantize(Decimal("0.01"))
        self.assertEqual(Decimal(route_estimate["estimated_pay"]), expected_pay)
        self.assertEqual(
            self.client.get(reverse("notifications_api")).json()["public_offer_count"],
            0,
        )
        active_notification_counts = self.client.get(
            reverse("notifications_api"),
        ).json()
        self.assertEqual(active_notification_counts["available_delivery_count"], 0)
        self.assertEqual(active_notification_counts["active_delivery_count"], 1)

        active_response = self.client.get(
            f"{reverse('rider_dashboard')}?view=active",
        )
        self.assertContains(active_response, "Recorded route &amp; rider pay")
        self.assertContains(active_response, "Saved using your location when you accepted")
        self.assertContains(active_response, 'class="rider-next-step ')
        self.assertContains(active_response, "Go to the shop and collect the parcel")
        self.assertContains(
            active_response,
            f'href="#rider-next-action-{shop_order.pk}"',
        )
        self.assertContains(active_response, "Navigate to pickup")
        self.assertContains(active_response, "Show your pickup QR or OTP to shop staff")
        active_dashboard_html = active_response.content.decode()
        self.assertNotContains(
            active_response,
            "Start pickup / stop new offers",
        )
        self.assertLess(
            active_dashboard_html.index('id="showNearbyRiderOrders"'),
            active_dashboard_html.index('class="row g-3 rider-order-track"'),
        )
        self.assertLess(
            active_dashboard_html.index('class="rider-active-actions mb-3"'),
            active_dashboard_html.index('class="row g-3 rider-order-track"'),
        )
        pickup_otp = active_response.context["orders"][0].pickup_otp
        self.client.force_login(owner)
        pickup_response = self.client.post(
            reverse("verify_rider_pickup_otp"),
            {"pickup_otp": pickup_otp},
        )
        self.assertEqual(pickup_response.status_code, 302)
        application.refresh_from_db()
        self.assertFalse(application.public_rider_batch_accepting)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")

        self.client.force_login(user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        qr_confirmation = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {
                "order_qr": (
                    f"http://testserver"
                    f"{reverse('shop_order_detail', kwargs={'shop_order_id': shop_order.pk})}"
                ),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(qr_confirmation.status_code, 200)
        self.assertTrue(qr_confirmation.json()["delivery_started"])
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

        verify_response = self.client.post(
            reverse("verify_customer_otp", kwargs={"shop_order_id": shop_order.pk}),
            {"otp": ""},
        )
        self.assertEqual(verify_response.status_code, 302)
        complete_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": "25.00", "cod_payment_method": "CASH"},
        )
        self.assertEqual(complete_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "DELIVERED")
        cod_collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        self.assertEqual(cod_collection.amount, Decimal("25.00"))
        self.assertEqual(cod_collection.payment_method, "CASH")
        fare_transaction = PublicRiderWalletTransaction.objects.get(
            shop_order=shop_order,
            transaction_type=PublicRiderWalletTransaction.DELIVERY_FARE,
        )
        self.assertEqual(
            fare_transaction.amount,
            Decimal(route_estimate["estimated_pay"]),
        )
        rider_wallet = PublicRiderWallet.objects.get(rider=application)
        self.assertEqual(rider_wallet.balance, fare_transaction.amount)
        self.assertTrue(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="RIDER_FEE",
                amount=fare_transaction.amount,
            ).exists()
        )
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        handover_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "handover"},
        )
        self.assertContains(handover_dashboard, "Public rider earnings wallet")
        self.assertContains(handover_dashboard, "₹25.00")
        self.assertContains(handover_dashboard, "Total cash COD awaiting shop handover")
        self.client.force_login(owner)
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": "25.00"},
        )
        self.assertEqual(handover_response.status_code, 302)
        self.assertTrue(
            RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="HANDOVER",
                amount=Decimal("25.00"),
            ).exists()
        )
        rider_wallet.refresh_from_db()
        self.assertEqual(rider_wallet.balance, fare_transaction.amount)

    def test_public_offer_retries_and_shop_rider_assignment_is_manual(self):
        owner = User.objects.create_user(
            username="public_fallback_shop_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Public Fallback Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        shop_rider_user = User.objects.create_user(
            username="public_fallback_shop_rider",
            password="test-password",
        )
        shop_rider = ShopStaff.objects.create(
            shop=shop,
            user=shop_rider_user,
            role="RIDER",
            is_active=True,
        )
        public_rider_user = User.objects.create_user(
            username="public_fallback_public_rider",
            password="test-password",
        )
        application = PublicRiderApplication.objects.create(
            user=public_rider_user,
            full_name="Public Fallback Rider",
            phone="9876543210",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-PUBLIC-FALLBACK",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        customer = User.objects.create_user(
            username="public_fallback_customer",
            password="test-password",
        )
        order = Order.objects.create(
            order_id="PUBLIC-FALLBACK-ORDER",
            user=customer.username,
            status="READY",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )

        self.client.force_login(owner)
        assign_response = self.client.post(
            reverse("assign_rider", kwargs={"shop_order_id": shop_order.pk}),
            {"rider": "PUBLIC"},
        )
        self.assertEqual(assign_response.status_code, 302)

        self.client.force_login(public_rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        public_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(public_dashboard.context["available_order_count"], 1)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.public_rider_offer_user_id, public_rider_user.pk)

        shop_order.public_rider_offer_expires_at = timezone.now() - timedelta(seconds=1)
        shop_order.save(update_fields=[
            "public_rider_offer_expires_at",
            "updated_at",
        ])
        expired_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(expired_dashboard.context["available_order_count"], 0)
        shop_order.refresh_from_db()
        self.assertFalse(shop_order.public_rider_fallback_to_staff)
        self.assertTrue(shop_order.public_rider_network_requested)
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertIsNone(shop_order.public_rider_offer_user_id)
        self.assertIsNotNone(shop_order.public_rider_offer_retry_at)
        self.assertFalse(
            Notification.objects.filter(
                user=shop_rider_user,
                title="Delivery available",
            ).exists()
        )

        self.client.force_login(shop_rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        staff_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(staff_dashboard.context["available_order_count"], 0)
        self.client.force_login(owner)
        self.assertContains(
            self.client.get(
                reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
            ),
            "This order remains in the public rider retry queue",
        )

        manual_assign_response = self.client.post(
            reverse("assign_rider", kwargs={"shop_order_id": shop_order.pk}),
            {"rider": str(shop_rider.pk)},
        )
        self.assertEqual(manual_assign_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, shop_rider.pk)
        self.assertFalse(shop_order.public_rider_fallback_to_staff)
        self.assertFalse(shop_order.public_rider_network_requested)

    def test_public_order_uses_admin_offer_window_and_nearest_rider_round_size(self):
        install_test_road_network(self, [
            [72.0, 23.0],
            [72.0, 23.05],
        ])
        owner = User.objects.create_user(
            username="public_offer_shop_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Public Offer Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        riders = []
        from .public_riders import sync_public_rider_shop_memberships

        observed_at = timezone.now().timestamp()
        for index in range(6):
            rider = User.objects.create_user(
                username=f"public_offer_rider_{index}",
                password="test-password",
            )
            application = PublicRiderApplication.objects.create(
                user=rider,
                full_name=f"Public Offer Rider {index}",
                phone=f"98765432{index:02d}",
                address="Rider address",
                vehicle_type="MOTORCYCLE",
                driving_license_number=f"DL-OFFER-{index}",
                work_area={
                    "type": "circle",
                    "center": {"lat": 23.0, "lng": 72.0},
                    "radius_km": 20,
                },
                status=PublicRiderApplication.STATUS_APPROVED,
            )
            sync_public_rider_shop_memberships(application)
            membership = ShopStaff.objects.get(
                shop=shop,
                user=rider,
                is_public_rider=True,
            )
            membership.last_location = {
                "lat": 23.0 + index * 0.01,
                "lng": 72.0,
                "observed_at": observed_at,
            }
            membership.save(update_fields=["last_location", "updated_at"])
            riders.append(rider)

        customer = User.objects.create_user(
            username="public_offer_customer",
            password="test-password",
        )
        order = Order.objects.create(
            order_id="PUBLIC-OFFER-ORDER",
            user=customer.username,
            status="READY",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )
        pay_settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        pay_settings.shared_rider_base_pay = Decimal("12.50")
        pay_settings.shared_rider_rate_per_km = Decimal("2.75")
        pay_settings.public_rider_offer_timeout_seconds = 30
        pay_settings.public_rider_offer_riders_per_round = 3
        pay_settings.save(update_fields=[
            "shared_rider_base_pay",
            "shared_rider_rate_per_km",
            "public_rider_offer_timeout_seconds",
            "public_rider_offer_riders_per_round",
        ])

        self.client.force_login(riders[5])
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        far_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(far_dashboard.status_code, 200)
        self.assertEqual(far_dashboard.context["available_order_count"], 0)
        self.assertContains(
            far_dashboard,
            "No public offers are assigned to you right now",
        )
        self.assertContains(
            far_dashboard,
            "Each ready public-network order is offered to one eligible online rider at a time",
        )

        shop_order.refresh_from_db()
        self.assertEqual(shop_order.public_rider_offer_user_id, riders[0].id)
        self.assertGreater(
            (shop_order.public_rider_offer_expires_at - timezone.now()).total_seconds(),
            11,
        )
        self.assertLessEqual(
            (shop_order.public_rider_offer_expires_at - timezone.now()).total_seconds(),
            30,
        )
        rejected_rider_accept = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(rejected_rider_accept.status_code, 302)
        shop_order.refresh_from_db()
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertEqual(shop_order.public_rider_offer_user_id, riders[0].id)

        for index in range(3):
            self.client.force_login(riders[index])
            session = self.client.session
            session["active_portal"] = "RIDER"
            session.save()
            dashboard = self.client.get(reverse("rider_dashboard"))
            self.assertTrue(dashboard.context["is_public_rider"], index)
            self.assertTrue(dashboard.context["available_view"], index)
            self.assertEqual(dashboard.context["available_order_count"], 1)
            self.assertEqual(len(dashboard.context["orders"]), 1, index)
            self.assertContains(dashboard, "data-public-offer-route")
            self.assertContains(dashboard, "data-public-rider-pickup-distance")
            self.assertContains(dashboard, "data-public-pickup-delivery-distance")
            self.assertContains(dashboard, "data-public-total-distance")
            self.assertContains(dashboard, "data-public-rider-fare")
            self.assertContains(dashboard, "locally uploaded road network")
            offered_order = dashboard.context["orders"][0]
            self.assertEqual(offered_order.status, "READY")
            self.assertIsNone(offered_order.assigned_rider_id)
            self.assertFalse(offered_order.is_assigned_to_current_rider)
            self.assertContains(dashboard, "Decline offer")
            response = self.client.post(
                reverse(
                    "accept_rider_order",
                    kwargs={"shop_order_id": shop_order.pk},
                ),
                {"action": "decline"},
            )
            self.assertEqual(response.status_code, 302)
            shop_order.refresh_from_db()
            expected_next_rider = riders[index + 1] if index < 2 else None
            self.assertEqual(
                shop_order.public_rider_offer_user_id,
                riders[index].id,
            )
            self.assertLessEqual(
                shop_order.public_rider_offer_expires_at,
                timezone.now(),
            )
            if expected_next_rider:
                next_offer_at = shop_order.public_rider_offer_retry_at
                self.assertIsNotNone(next_offer_at)
                self.assertGreaterEqual(
                    (next_offer_at - timezone.now()).total_seconds(),
                    11,
                )
                self.client.force_login(expected_next_rider)
                session = self.client.session
                session["active_portal"] = "RIDER"
                session.save()
                with patch(
                    "seller.public_rider_dispatch.timezone.now",
                    return_value=next_offer_at + timedelta(seconds=1),
                ):
                    next_dashboard = self.client.get(
                        reverse("rider_dashboard"),
                    )
                self.assertEqual(next_dashboard.context["available_order_count"], 1)
                shop_order.refresh_from_db()
                self.assertEqual(
                    shop_order.public_rider_offer_user_id,
                    expected_next_rider.id,
                )

        shop_order.refresh_from_db()
        self.assertIsNotNone(shop_order.public_rider_offer_retry_at)
        retry_delay = (
            shop_order.public_rider_offer_retry_at - timezone.now()
        ).total_seconds()
        self.assertGreater(retry_delay, 11)
        self.assertLessEqual(retry_delay, 12)
        self.assertEqual(
            set(shop_order.public_rider_offer_declined_until),
            {str(rider.id) for rider in riders[:3]},
        )
        self.client.force_login(riders[5])
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        with patch(
            "seller.public_rider_dispatch.timezone.now",
            return_value=shop_order.public_rider_offer_retry_at + timedelta(seconds=1),
        ):
            expired_offer_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(expired_offer_dashboard.context["available_order_count"], 0)
        shop_order.refresh_from_db()
        self.assertIsNone(shop_order.public_rider_offer_user_id)
        self.assertIsNotNone(shop_order.public_rider_offer_retry_at)

        self.client.force_login(riders[0])
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        with patch(
            "seller.public_rider_dispatch.timezone.now",
            return_value=shop_order.public_rider_offer_retry_at + timedelta(seconds=1),
        ):
            next_rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(next_rider_dashboard.context["available_order_count"], 1)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.public_rider_offer_user_id, riders[0].id)
        with patch(
            "seller.public_rider_dispatch.timezone.now",
            return_value=shop_order.public_rider_offer_expires_at - timedelta(seconds=1),
        ):
            accept_response = self.client.post(
                reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
                {
                    "rider_latitude": "23.0",
                    "rider_longitude": "72.0",
                    "rider_location_observed_at": str(timezone.now().timestamp()),
                },
            )
        self.assertEqual(accept_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")
        self.assertEqual(shop_order.assigned_rider.user_id, riders[0].id)
        self.assertIsNone(shop_order.public_rider_offer_user_id)
        OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="PICKED_UP",
            remark=(
                "Shop staff verified rider public_offer_rider_0 "
                "and confirmed parcel pickup by OTP."
            ),
        )
        verified_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "verified"},
        )
        self.assertEqual(verified_dashboard.status_code, 200)
        self.assertContains(verified_dashboard, "PUBLIC-OFFER-ORDER")
        self.assertEqual(verified_dashboard.context["verified_order_count"], 1)

        location_response = self.client.post(
            reverse("update_public_rider_location"),
            {"latitude": "23.1", "longitude": "72.1"},
        )
        self.assertEqual(location_response.status_code, 200)
        rider_membership = ShopStaff.objects.get(shop=shop, user=riders[0])
        self.assertEqual(rider_membership.last_location["lat"], 23.1)
        self.assertIn("observed_at", rider_membership.last_location)

    def test_login_as_shop_owner_opens_owner_dashboard(self):
        owner = User.objects.create_user(
            username="portal_selected_owner",
            password="test-password",
        )
        Shop.objects.create(name="Portal Selected Shop", owner=owner)

        response = self.client.post(
            reverse("login"),
            {
                "username": owner.username,
                "password": "test-password",
                "login_as": "SHOP_OWNER",
            },
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(
            response.cookies[settings.SESSION_COOKIE_NAME]["domain"],
            "",
        )
        self.assertEqual(self.client.session["active_portal"], "SHOP_OWNER")
        dashboard_response = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, "Manage all shops")

    def test_login_rejects_staff_portal_without_active_staff_assignment(self):
        user = User.objects.create_user(
            username="portal_unassigned_user",
            password="test-password",
        )

        response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "test-password",
                "login_as": "SHOP_STAFF",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no active shop staff assignment")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_owner_portal_does_not_switch_dual_role_account_to_staff_dashboard(self):
        user = User.objects.create_user(
            username="portal_dual_role_owner",
            password="test-password",
        )
        Shop.objects.create(name="Portal Owner Shop", owner=user)
        staff_shop_owner = User.objects.create_user(
            username="portal_dual_role_staff_owner",
            password="test-password",
        )
        staff_shop = Shop.objects.create(
            name="Portal Staff Shop",
            owner=staff_shop_owner,
        )
        ShopStaff.objects.create(
            shop=staff_shop,
            user=user,
            role="MANAGER",
            is_active=True,
        )
        self.client.force_login(user)
        session = self.client.session
        session["active_portal"] = "SHOP_OWNER"
        session.save()

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manage all shops")
        self.assertContains(response, "Add shop")

        home_response = self.client.get(reverse("home"))
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "All shops")
        self.assertContains(home_response, reverse("shop_list"))
        self.assertContains(home_response, reverse("shop_add"))

    def test_customer_and_staff_otp_codes_are_unique_stable_and_not_default(self):
        customer = User.objects.create_user(
            username="otp_customer_unique",
            password="test-password",
        )
        staff_user = User.objects.create_user(
            username="otp_staff_unique",
            password="test-password",
        )
        client_profile = ClientProfile.objects.create(user=customer)
        shop = Shop.objects.create(
            name="OTP Unique Shop",
            owner=User.objects.create_user(
                username="otp_shop_owner",
                password="test-password",
            ),
        )
        staff_profile = ShopStaff.objects.create(
            shop=shop,
            user=staff_user,
            role="RIDER",
            is_active=True,
        )

        self.assertRegex(client_profile.verification_otp, r"^[1-9]\d{3}$")
        self.assertRegex(staff_profile.verification_otp, r"^[1-9]\d{3}$")
        self.assertNotEqual(
            client_profile.verification_otp,
            staff_profile.verification_otp,
        )

        customer_code = client_profile.verification_otp
        staff_code = staff_profile.verification_otp
        client_profile.name = "Changed customer name"
        client_profile.save()
        staff_profile.mobile_no = "9876543210"
        staff_profile.save()
        self.assertEqual(client_profile.verification_otp, customer_code)
        self.assertEqual(staff_profile.verification_otp, staff_code)

    def test_product_form_filters_product_names_by_category_and_catalog_image(self):
        owner = User.objects.create_user(username="owner_catalog", password="pass1234", is_staff=True)

        Product.objects.create(
            created_by=owner,
            product_category="SABJI",
            product_name="Potato",
            price="25.00",
            unit="kg",
            image_url="https://example.com/potato.jpg",
        )
        Product.objects.create(
            created_by=owner,
            product_category="SABJI",
            product_name="Tomato",
            price="35.00",
            unit="kg",
            image_url="https://example.com/tomato.jpg",
        )
        Product.objects.create(
            created_by=owner,
            product_category="KIRANA",
            product_name="Rice",
            price="50.00",
            unit="kg",
            image_url="https://example.com/rice.jpg",
        )

        form = ProductForm(user=owner, initial={"product_category": "SABJI"})

        self.assertEqual(
            form.fields["product_category"].__class__.__name__,
            "ChoiceField",
        )
        self.assertEqual(
            form["product_category"].field.widget.attrs.get(
                "data-product-category-select",
            ),
            "true",
        )
        self.assertIn(
            ("SABJI", "SABJI"),
            form.fields["product_category"].choices,
        )
        self.assertEqual(
            form["product_name"].field.widget.attrs.get("data-product-name-select"),
            "true",
        )
        self.assertEqual(
            [choice[0] for choice in form.fields["product_name"].choices],
            ["", "Potato", "Tomato"],
        )

        unselected_form = ProductForm(user=owner)
        self.assertEqual(
            [choice[0] for choice in unselected_form.fields["product_name"].choices],
            [""],
        )
        kirana_form = ProductForm(
            user=owner,
            initial={"product_category": "KIRANA"},
        )
        self.assertEqual(
            [choice[0] for choice in kirana_form.fields["product_name"].choices],
            ["", "Rice"],
        )

        mismatched_form = ProductForm(
            data={
                "product_category": "SABJI",
                "product_name": "Rice",
            },
            user=owner,
        )
        self.assertFalse(mismatched_form.is_valid())
        self.assertIn("product_name", mismatched_form.errors)

        self.assertEqual(
            form.get_catalog_image_url_for_name("Tomato"),
            "https://example.com/tomato.jpg",
        )

    def test_admin_product_catalog_supplies_owner_product_defaults_by_category(self):
        owner = User.objects.create_user(username="owner_admin_catalog", password="test-password")
        AdminProductCatalog.objects.create(
            product_category="SABJI",
            product_name="Catalog Potato",
            image_url="https://example.com/catalog-potato.png",
            hsn_code="070190",
            gst_rate="5.00",
        )
        AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Catalog Potato",
            image_url="https://example.com/catalog-fruit.png",
            hsn_code="080810",
            gst_rate="12.00",
        )

        form = ProductForm(
            user=owner,
            initial={"product_category": "SABJI"},
        )
        self.assertIn(
            ("Catalog Potato", "Catalog Potato"),
            form.fields["product_name"].choices,
        )
        self.assertEqual(
            form.get_catalog_defaults()["SABJI"]["Catalog Potato"],
            {
                "image_url": "https://example.com/catalog-potato.png",
                "hsn_code": "070190",
                "gst_rate": "5.00",
            },
        )
        self.assertNotIn("Catalog Potato", form.get_catalog_defaults().get("OTHER", {}))

        selected_form = ProductForm(
            data={
                "sku": "",
                "product_category": "SABJI",
                "product_name": "Catalog Potato",
                "product_alias_name": "",
                "product_title": "",
                "product_color": "",
                "product_size": "",
                "price": "40.00",
                "hsn_code": "",
                "gst_rate": "",
                "unit_value": "500",
                "unit_text": "gm",
                "image_url": "",
                "shelf_life_days": "",
                "expiry_price_drop_percent": "0.00",
                "product_description": "",
            },
            user=owner,
        )
        self.assertTrue(selected_form.is_valid(), selected_form.errors)
        product = selected_form.save(commit=False)
        self.assertEqual(product.image_url, "https://example.com/catalog-potato.png")
        self.assertEqual(product.hsn_code, "070190")
        self.assertEqual(product.gst_rate, Decimal("5.00"))
        self.assertEqual(product.unit, "500gm")

    def test_product_form_uses_admin_catalog_image_and_blocks_owner_override(self):
        owner = User.objects.create_user(
            username="owner_custom_catalog_values",
            password="testpass123",
        )
        AdminProductCatalog.objects.create(
            product_category="SABJI",
            product_name="Customizable Potato",
            image_url="https://example.com/catalog-potato.png",
            hsn_code="070190",
            gst_rate="5.00",
        )
        form = ProductForm(
            data={
                "sku": "",
                "product_category": "SABJI",
                "product_name": "Customizable Potato",
                "product_alias_name": "",
                "product_title": "",
                "product_color": "",
                "product_size": "",
                "price": "40.00",
                "hsn_code": "071290",
                "gst_rate": "12.00",
                "unit_value": "1",
                "unit_text": "kg",
                "image_url": "https://example.com/shop-potato.png",
                "shelf_life_days": "",
                "expiry_price_drop_percent": "0.00",
                "product_description": "",
            },
            user=owner,
        )

        self.assertFalse(form.fields["hsn_code"].widget.attrs.get("readonly"))
        self.assertFalse(form.fields["gst_rate"].widget.attrs.get("readonly"))
        self.assertTrue(form.fields["image_url"].disabled)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save(commit=False)
        self.assertEqual(product.hsn_code, "071290")
        self.assertEqual(product.gst_rate, Decimal("12.00"))
        self.assertEqual(product.image_url, "https://example.com/catalog-potato.png")

    def test_new_product_form_includes_shared_catalog_fallback_choices(self):
        owner = User.objects.create_user(
            username="owner_fruit_catalog_fallback",
            password="test-password",
        )
        AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Apple",
            image_url="https://example.com/apple.jpg",
            hsn_code="080810",
            gst_rate="0.00",
        )

        form = ProductForm(user=owner)

        self.assertEqual(
            list(form.fields["product_name"].choices),
            [("", "---------")],
        )
        self.assertIn("Apple", form.catalog_data["FRUITS"])

        fruits_form = ProductForm(
            user=owner,
            initial={"product_category": "FRUITS"},
        )
        self.assertEqual(
            list(fruits_form.fields["product_name"].choices),
            [("", "---------"), ("Apple", "Apple")],
        )

    def test_staff_product_is_available_as_shared_product_choice(self):
        owner = User.objects.create_user(
            username="owner_shared_catalog",
            password="test-password",
        )
        administrator = User.objects.create_user(
            username="admin_shared_catalog",
            password="test-password",
            is_staff=True,
        )
        Product.objects.create(
            created_by=administrator,
            product_category="SABJI",
            product_name="Admin Potato",
            price="25.00",
            unit="kg",
            image_url="https://example.com/admin-potato.jpg",
            hsn_code="070190",
            gst_rate="5.00",
        )

        form = ProductForm(
            user=owner,
            initial={"product_category": "SABJI"},
        )

        self.assertIn(
            ("Admin Potato", "Admin Potato"),
            form.fields["product_name"].choices,
        )
        self.assertEqual(
            form.catalog_defaults["SABJI"]["Admin Potato"],
            {
                "image_url": "https://example.com/admin-potato.jpg",
                "hsn_code": "070190",
                "gst_rate": "5.00",
            },
        )

    def test_marketplace_searches_shops_and_sorts_nearby_shops(self):
        user = User.objects.create_user(username="nearby_customer", password="pass1234")
        client_profile = ClientProfile.objects.create(user=user, name="Nearby Customer")
        ClientDeliveryAddress.objects.create(
            client=client_profile,
            label="Home",
            address="Home Road",
            location={"lat": 23.0225, "lng": 72.5714},
            is_default=True,
        )
        owner = User.objects.create_user(username="nearby_owner", password="pass1234")
        near_shop = Shop.objects.create(name="Near Fresh", owner=owner, location={"lat": 23.023, "lng": 72.572})
        far_shop = Shop.objects.create(name="Far Fresh", owner=owner, location={"lat": 23.10, "lng": 72.65})
        for shop, name in ((near_shop, "Near Potato"), (far_shop, "Far Potato")):
            product = Product.objects.create(created_by=owner, product_name=name, price="20.00", unit="kg", product_category="SABJI")
            ProductShop.objects.create(shop=shop, product=product, price="20.00", stock=5, is_active=True)

        self.client.force_login(user)
        response = self.client.get(reverse("marketplace"), {"q": "Near Fresh"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Near Fresh")
        self.assertContains(response, "Near Potato")
        self.assertContains(response, "Add Near Potato to favourites")
        self.assertContains(response, "favorite-heart")
        self.assertNotContains(response, ">Favorite</button>")
        self.assertNotContains(response, "Far Potato")
        self.assertEqual(response.context["nearby_shops"][0]["shop"], near_shop)

        product_shop = ProductShop.objects.get(shop=near_shop)
        favorite_response = self.client.post(
            reverse(
                "toggle_favorite_product",
                kwargs={"product_shop_id": product_shop.pk},
            ),
            {"return_to": reverse("marketplace")},
        )
        self.assertRedirects(favorite_response, reverse("marketplace"))
        response = self.client.get(reverse("marketplace"), {"q": "Near Fresh"})
        self.assertContains(response, "Remove Near Potato from favourites")
        self.assertContains(response, "bi-heart-fill")

    def test_marketplace_prioritizes_nearest_shops_and_varies_products_within_category(self):
        user = User.objects.create_user(
            username="varied_market_customer",
            password="testpass123",
        )
        profile = ClientProfile.objects.create(user=user, name="Varied Market Customer")
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Market Road",
            location={"lat": 23.0225, "lng": 72.5714},
            is_default=True,
        )
        owner = User.objects.create_user(
            username="varied_market_owner",
            password="testpass123",
        )
        shops = [
            Shop.objects.create(
                name=name,
                owner=owner,
                location={"lat": latitude, "lng": 72.5714},
            )
            for name, latitude in (
                ("Nearest Vegetables", 23.0226),
                ("Middle Vegetables", 23.025),
                ("Far Vegetables", 23.04),
            )
        ]
        for shop, product_names in (
            (shops[0], ("Potato", "Tomato")),
            (shops[1], ("Potato", "Onion")),
            (shops[2], ("Potato", "Cucumber")),
        ):
            for product_name in product_names:
                Product.objects.create(
                    created_by=owner,
                    product_name=product_name,
                    price="20.00",
                    unit="kg",
                    product_category="SABJI",
                )
                ProductShop.objects.create(
                    shop=shop,
                    product=product,
                    price="20.00",
                    stock=5,
                    is_active=True,
                )

        self.client.force_login(user)
        response = self.client.get(reverse("marketplace"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry["shop"] for entry in response.context["nearby_shops"]],
            shops,
        )
        daily_market = next(
            group for group in response.context["product_groups"]
            if group["market_name"] == "DAILY MARKET"
        )
        sabji_category = next(
            category for category in daily_market["subcategories"]
            if category["name"] == "SABJI"
        )
        ordered_items = sabji_category["items"]
        first_round_names = [
            item.product.product_name for item in ordered_items[:4]
        ]
        self.assertEqual(set(first_round_names), {
            "Potato",
            "Tomato",
            "Onion",
            "Cucumber",
        })
        self.assertEqual(len(first_round_names), len(set(first_round_names)))
        self.assertEqual(
            [
                item.shop
                for item in ordered_items
                if item.product.product_name == "Potato"
            ],
            shops,
        )

    def test_marketplace_theme_orders_categories_and_features_selected_products(self):
        user = User.objects.create_user(
            username="market_theme_customer",
            password="test-password",
        )
        profile = ClientProfile.objects.create(user=user, name="Market Theme Customer")
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Market Road",
            location={"lat": 23.0225, "lng": 72.5714},
            is_default=True,
        )
        owner = User.objects.create_user(
            username="market_theme_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Theme Market Shop",
            owner=owner,
            location={"lat": 23.023, "lng": 72.572},
        )
        tomato = Product.objects.create(
            created_by=owner,
            product_name="Featured Tomato",
            price="20.00",
            unit="kg",
            product_category="SABJI",
        )
        potato = Product.objects.create(
            created_by=owner,
            product_name="Regular Potato",
            price="18.00",
            unit="kg",
            product_category="SABJI",
        )
        apple = Product.objects.create(
            created_by=owner,
            product_name="Fresh Apple",
            price="30.00",
            unit="kg",
            product_category="FRUITS",
        )
        for product in (tomato, potato, apple):
            ProductShop.objects.create(
                shop=shop,
                product=product,
                price=product.price,
                stock=5,
                is_active=True,
            )
        featured_catalog_product = AdminProductCatalog.objects.create(
            product_category="SABJI",
            product_name="Featured Tomato",
            image_url="https://example.test/tomato.jpg",
        )
        theme = SiteThemeSettings.objects.create(
            marketplace_hero_title="Fresh picks",
            marketplace_category_order="FRUITS,SABJI",
            marketplace_featured_product_ids=[str(featured_catalog_product.pk)],
            marketplace_section_order="search,hero,nearby,products,category_nav",
        )

        self.client.force_login(user)
        response = self.client.get(reverse("marketplace"))

        self.assertEqual(response.status_code, 200)
        daily_market = next(
            group for group in response.context["product_groups"]
            if group["market_name"] == "DAILY MARKET"
        )
        self.assertEqual(
            [category["name"] for category in daily_market["subcategories"]],
            ["FRUITS", "SABJI"],
        )
        sabji = daily_market["subcategories"][1]
        self.assertEqual(sabji["items"][0].product, tomato)
        self.assertTrue(sabji["items"][0].is_marketplace_featured)
        self.assertFalse(sabji["items"][1].is_marketplace_featured)
        self.assertContains(response, "Fresh picks")
        self.assertContains(response, f'order: {response.context["marketplace_theme_order"]["hero"]}')
        self.assertContains(response, "Featured")

    def test_marketplace_shop_search_filters_by_delivery_pincode(self):
        user = User.objects.create_user(username="pincode_customer", password="test-password")
        profile = ClientProfile.objects.create(user=user, name="Pincode Customer")
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Market Road",
            pincode="380001",
            is_default=True,
        )
        owner = User.objects.create_user(username="pincode_owner", password="test-password")
        delivering_shop = Shop.objects.create(
            name="Fresh Market Store",
            owner=owner,
            delivery_pincode=["380001"],
        )
        excluded_shop = Shop.objects.create(
            name="Fresh Market Express",
            owner=owner,
            delivery_pincode=["380099"],
        )
        for shop, product_name in (
            (delivering_shop, "Deliverable Potato"),
            (excluded_shop, "Undeliverable Potato"),
        ):
            product = Product.objects.create(
                created_by=owner,
                product_name=product_name,
                price="20.00",
                unit="kg",
                product_category="SABJI",
            )
            ProductShop.objects.create(
                shop=shop,
                product=product,
                price="20.00",
                stock=5,
                is_active=True,
            )

        self.client.force_login(user)
        response = self.client.get(reverse("marketplace"), {"q": "Fresh Market"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deliverable Potato")
        self.assertNotContains(response, "Undeliverable Potato")
        self.assertEqual(
            [entry["shop"] for entry in response.context["nearby_shops"]],
            [delivering_shop],
        )
        self.assertContains(response, "<details", html=False)

    def test_marketplace_search_includes_product_catalog_metadata(self):
        user = User.objects.create_user(username="catalog_search_user", password="test-password")
        ClientProfile.objects.create(user=user, name="Catalog Search User")
        owner = User.objects.create_user(username="catalog_search_owner", password="test-password")
        shop = Shop.objects.create(
            name="Everyday Store",
            alias_name="Green Basket",
            owner=owner,
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Fresh Produce",
            product_alias_name="Palak garbanzo chickpea",
            product_title="Local leafy greens",
            product_description="Fresh spinach bunch harvested today",
            product_category="SABJI",
            product_color="Dark Green",
            product_size="250 g",
            sku="SPINACH-250",
            price="30.00",
            unit="bunch",
        )
        ProductShop.objects.create(
            shop=shop,
            product=product,
            price="30.00",
            stock=4,
            is_active=True,
        )
        product.search_keywords = (
            f"{product.search_keywords}\n\nHindi\n\nचुकंदर"
            "\n\nBengali\n\nচাংদ"
        )
        product.save(update_fields=["search_keywords"])

        self.client.force_login(user)
        for query in (
            "spinach",
            "SPINACH-250",
            "250 g",
            "SABJI",
            "Green Basket",
            "garbanzo",
            "चुकँदर्",
            "চাঁদ",
        ):
            with self.subTest(query=query):
                response = self.client.get(reverse("marketplace"), {"q": query})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-voice-search')
                self.assertContains(response, 'aria-label="Search by voice"')
                self.assertContains(response, 'data-voice-search-status')
                self.assertContains(response, 'webkitSpeechRecognition')
                self.assertContains(response, "Fresh Produce")
                self.assertEqual(
                    [item.product_id for item in response.context["products"]],
                    [product.id],
                )
                if query == "garbanzo":
                    marketplace_detail = response.context["products"][0].marketplace_detail
                    self.assertNotIn(
                        "search_keywords",
                        marketplace_detail,
                    )
                    self.assertNotIn(
                        "garbanzo",
                        " ".join(str(value) for value in marketplace_detail.values()).lower(),
                    )
                if query == "चुकँदर्":
                    self.assertContains(response, "normalizeSearchText")

    def test_marketplace_product_search_matches_any_query_word(self):
        user = User.objects.create_user(
            username="partial_word_search_user",
            password="pass12345",
        )
        ClientProfile.objects.create(user=user, name="Partial Word Search User")
        owner = User.objects.create_user(
            username="partial_word_search_owner",
            password="pass12345",
        )
        shop = Shop.objects.create(name="Neighborhood Grocer", owner=owner)
        matching_product = Product.objects.create(
            created_by=owner,
            product_name="Spinach",
            product_category="SABJI",
            price="30.00",
            unit="bunch",
        )
        unrelated_product = Product.objects.create(
            created_by=owner,
            product_name="Cucumber",
            product_category="SABJI",
            price="20.00",
            unit="kg",
        )
        for product in (matching_product, unrelated_product):
            ProductShop.objects.create(
                shop=shop,
                product=product,
                price=product.price,
                stock=5,
                is_active=True,
            )

        self.client.force_login(user)
        response = self.client.get(
            reverse("marketplace"),
            {"q": "spinach unmatchedword"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item.product_id for item in response.context["products"]],
            [matching_product.pk],
        )

    def test_marketplace_product_search_matches_spelling_mistake(self):
        user = User.objects.create_user(
            username="typo_search_user",
            password="pass12345",
        )
        ClientProfile.objects.create(user=user, name="Typo Search User")
        owner = User.objects.create_user(
            username="typo_search_owner",
            password="pass12345",
        )
        shop = Shop.objects.create(name="Typo Search Shop", owner=owner)
        matching_product = Product.objects.create(
            created_by=owner,
            product_name="Spinach",
            product_category="SABJI",
            price="30.00",
            unit="bunch",
        )
        unrelated_product = Product.objects.create(
            created_by=owner,
            product_name="Cucumber",
            product_category="SABJI",
            price="20.00",
            unit="kg",
        )
        for product in (matching_product, unrelated_product):
            ProductShop.objects.create(
                shop=shop,
                product=product,
                price=product.price,
                stock=5,
                is_active=True,
            )

        self.client.force_login(user)
        response = self.client.get(
            reverse("marketplace"),
            {"q": "spinich no-match"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item.product_id for item in response.context["products"]],
            [matching_product.pk],
        )

    def test_search_text_normalizes_devanagari_nasal_and_terminal_virama(self):
        from .offline_keyword_dictionary import normalize_search_text

        self.assertEqual(
            normalize_search_text("चुकँदर्"),
            normalize_search_text("चुकंदर"),
        )
        self.assertEqual(
            normalize_search_text("ચુકઁદર્"),
            normalize_search_text("ચુકંદર"),
        )
        self.assertEqual(
            normalize_search_text("চাঁদ"),
            normalize_search_text("চাংদ"),
        )
        self.assertEqual(
            normalize_search_text("café"),
            normalize_search_text("cafe"),
        )
        self.assertEqual(
            normalize_search_text("سُرْخ"),
            normalize_search_text("سرخ"),
        )

    def test_checkout_uses_wallet_and_notifies_shop_staff(self):
        user = User.objects.create_user(username="wallet_customer", password="pass1234")
        client_profile = ClientProfile.objects.create(user=user, name="Wallet Customer", wallet_balance="100.00")
        address = ClientDeliveryAddress.objects.create(client=client_profile, label="Home", address="Wallet Road", location={"lat": 1, "lng": 2}, is_default=True)
        owner = User.objects.create_user(username="wallet_owner", password="pass1234")
        staff_user = User.objects.create_user(username="wallet_staff", password="pass1234")
        shop = Shop.objects.create(
            name="Wallet Shop",
            owner=owner,
            location={"lat": 1, "lng": 2},
            allow_product_image_overrides=True,
        )
        ShopStaff.objects.create(shop=shop, user=staff_user, role="PACKER", is_active=True)
        product = Product.objects.create(created_by=owner, product_name="Wallet Item", price="40.00", unit="piece", product_category="OTHER")
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="40.00",
            stock=5,
            is_active=True,
            image_url_override="https://example.com/wallet-item.png",
        )

        self.client.force_login(user)
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        checkout_response = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_response.status_code, 200)
        self.assertContains(checkout_response, "Confirm your purchase")
        self.assertContains(checkout_response, 'id="useWalletBalance"')
        self.assertRegex(
            checkout_response.content.decode(),
            r'id="useWalletBalance"[^>]*\bchecked\b',
        )
        self.assertTrue(checkout_response.context["use_wallet_balance"])
        self.assertEqual(
            checkout_response.context["checkout_shop_summary"][0]["shop_name"],
            shop.name,
        )
        response = self.client.post(reverse("checkout"), {"address": address.id})

        self.assertEqual(response.status_code, 302)
        client_profile.refresh_from_db()
        order = Order.objects.get(user=user.username)
        checkout_attempt = CheckoutAttempt.objects.get(
            customer=user,
            shop=shop,
        )
        self.assertEqual(checkout_attempt.order, order)
        self.assertEqual(order.wallet_amount_used, checkout_response.context["wallet_amount_used"])
        self.assertGreater(order.wallet_amount_used, Decimal("40.00"))
        self.assertEqual(
            client_profile.wallet_balance,
            Decimal("100.00") - order.wallet_amount_used,
        )
        self.assertEqual(order.placed_location, {"lat": 1, "lng": 2})
        shop_order = ShopOrder.objects.get(order=order)
        self.assertEqual(shop_order.status, "PLACED")
        self.assertEqual(shop_order.rider_identity_otp, "")
        self.assertEqual(shop_order.packing_minutes, 5)
        self.assertGreaterEqual(shop_order.rider_eta_minutes, 5)
        self.assertTrue(Notification.objects.filter(user=staff_user, notification_type="ORDER").exists())
        staff_notification = Notification.objects.get(user=staff_user, notification_type="ORDER")
        self.assertEqual(
            staff_notification.related_url,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
        )
        self.assertTrue(Notification.objects.filter(user=owner, related_url=staff_notification.related_url).exists())

        self.client.force_login(staff_user)
        packer_dashboard_response = self.client.get(reverse("packer_dashboard"))
        self.assertEqual(packer_dashboard_response.status_code, 200)
        self.assertContains(packer_dashboard_response, order.order_id)
        self.assertContains(packer_dashboard_response, "Available to pack")
        self.assertContains(packer_dashboard_response, "Accept order for packing")
        self.assertContains(
            packer_dashboard_response,
            'src="https://example.com/wallet-item.png"',
        )
        self.assertRegex(
            packer_dashboard_response.content.decode(),
            r'<details class="border rounded p-3 mb-3 packer-order-items">\s*<summary',
        )
        self.assertRegex(
            packer_dashboard_response.content.decode(),
            r"Items to pack\s*\(1\)",
        )

    def test_checkout_can_disable_wallet_balance(self):
        user = User.objects.create(username="wallet_optout_customer")
        profile = ClientProfile.objects.create(
            user=user,
            name="Wallet Opt-out Customer",
            wallet_balance="100.00",
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Wallet Opt-out Road",
            location={"lat": 1, "lng": 2},
            is_default=True,
        )
        owner = User.objects.create(username="wallet_optout_owner")
        shop = Shop.objects.create(
            name="Wallet Opt-out Shop",
            owner=owner,
            location={"lat": 1, "lng": 2},
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Wallet Opt-out Item",
            price="40.00",
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="40.00",
            stock=5,
            is_active=True,
        )

        self.client.force_login(user)
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        response = self.client.post(
            reverse("checkout"),
            {"address": address.id, "use_wallet_balance": "0"},
        )

        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        order = Order.objects.get(user=user.username)
        self.assertEqual(order.wallet_amount_used, Decimal("0.00"))
        self.assertEqual(profile.wallet_balance, Decimal("100.00"))

    def test_wallet_can_cover_entire_online_checkout_without_gateway_payment(self):
        user = User.objects.create(username="wallet_full_online_customer")
        profile = ClientProfile.objects.create(
            user=user,
            name="Wallet Online Customer",
            wallet_balance="100.00",
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Wallet Online Road",
            is_default=True,
        )
        owner = User.objects.create(username="wallet_full_online_owner")
        shop = Shop.objects.create(name="Wallet Online Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Wallet Online Item",
            price="40.00",
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="40.00",
            stock=5,
            is_active=True,
        )

        self.client.force_login(user)
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        with patch("seller.views.requests.post") as gateway_order:
            response = self.client.post(
                reverse("checkout"),
                {"address": address.id, "payment_method": "RAZORPAY"},
            )

        self.assertEqual(response.status_code, 302)
        gateway_order.assert_not_called()
        order = Order.objects.get(user=user.username)
        profile.refresh_from_db()
        self.assertEqual(order.payment_type, "WALLET")
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertEqual(order.wallet_amount_used, order.final_billing_amount)
        self.assertGreater(order.wallet_amount_used, Decimal("40.00"))
        self.assertEqual(profile.wallet_balance, Decimal("100.00") - order.wallet_amount_used)

    def test_checkout_confirmation_summarizes_each_shop_and_amount(self):
        customer = User.objects.create_user(
            username="checkout_multi_shop_customer",
            password="test-password",
        )
        profile = ClientProfile.objects.create(user=customer, name="Multi Shop Customer")
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Multi Shop Road",
            is_default=True,
        )
        shops = []
        assignments = []
        for index, price in enumerate(("25.00", "40.00"), start=1):
            owner = User.objects.create_user(
                username=f"checkout_multi_shop_owner_{index}",
                password="test-password",
            )
            shop = Shop.objects.create(
                name=f"Checkout Shop {index}",
                owner=owner,
                delivery_fee=Decimal("12.50") if index == 1 else Decimal("0.00"),
            )
            product = Product.objects.create(
                created_by=owner,
                product_name=f"Checkout Product {index}",
                price=price,
                unit="piece",
                product_category="OTHER",
            )
            assignments.append(ProductShop.objects.create(
                shop=shop,
                product=product,
                price=price,
                stock=5,
                is_active=True,
            ))
            shops.append(shop)

        self.client.force_login(customer)
        session = self.client.session
        session["cart"] = {str(assignment.pk): 1 for assignment in assignments}
        session.save()

        response = self.client.get(reverse("checkout"))

        self.assertEqual(response.status_code, 200)
        summaries = response.context["checkout_shop_summary"]
        self.assertEqual(
            [row["shop_name"] for row in summaries],
            [shop.name for shop in shops],
        )
        self.assertEqual(
            [Decimal(row["product_total"]) for row in summaries],
            [Decimal("25.00"), Decimal("40.00")],
        )
        self.assertEqual(
            [Decimal(row["delivery_fee"]) for row in summaries],
            [Decimal("12.50"), Decimal("0.00")],
        )
        self.assertContains(response, "Delivery fee by shop")
        self.assertContains(response, "Checkout Shop 1")
        self.assertContains(response, "₹12.50")
        self.assertContains(response, "Free")
        self.assertContains(response, "Total amount due")

        response = self.client.post(
            reverse("checkout"),
            {"address": address.id, "payment_method": "COD"},
        )
        self.assertEqual(response.status_code, 302)
        placed_order = Order.objects.get(user=customer.username)
        self.assertEqual(
            list(
                placed_order.shop_orders.order_by("shop__name").values_list(
                    "delivery_fee",
                    flat=True,
                )
            ),
            [Decimal("12.50"), Decimal("0.00")],
        )
        tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": placed_order.order_id})
        )
        self.assertEqual(tracking_response.status_code, 200)
        self.assertContains(tracking_response, "Delivery fee · Checkout Shop 1")
        self.assertContains(tracking_response, "₹12.50")
        self.assertContains(tracking_response, "Delivery fee · Checkout Shop 2")

    def test_staff_dashboard_returns_200_for_assigned_staff(self):
        owner = User.objects.create_user(username="owner2", password="pass1234")
        staff_user = User.objects.create_user(username="staff2", password="pass1234")
        shop = Shop.objects.create(name="Assigned Shop", owner=owner)
        ShopStaff.objects.create(shop=shop, user=staff_user, role="MANAGER", is_active=True)

        product = Product.objects.create(
            product_name="Low stock rice",
            price=Decimal("30.00"),
            unit="500 g",
            created_by=owner,
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            stock=Decimal("2.0000"),
        )
        ShopProductForecast.objects.create(
            product_shop=assignment,
            recommended_stock=Decimal("6.0000"),
        )
        vendor = ShopVendor.objects.create(shop=shop, name="Recent Rice Vendor")
        purchase = VendorPurchase.objects.create(
            shop=shop,
            vendor=vendor,
            subtotal_amount=Decimal("80.00"),
            total_amount=Decimal("80.00"),
        )
        VendorPurchaseItem.objects.create(
            purchase=purchase,
            product_shop=assignment,
            quantity=Decimal("4.0000"),
            purchased_quantity=4,
            unit_cost=Decimal("20.00"),
            line_total=Decimal("80.00"),
        )
        Notification.objects.create(
            user=staff_user,
            title="Staff dashboard test alert",
            message="Unread staff notification",
        )

        self.client.force_login(staff_user)
        response = self.client.get(reverse("staff_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned Shop")
        self.assertContains(response, "data-sellerhelper-scan-order")
        self.assertContains(response, "Shortage")
        self.assertContains(response, "2000 g")
        self.assertContains(response, "Recent Rice Vendor")
        self.assertContains(response, "₹20.00")
        self.assertContains(response, "table-danger")
        self.assertContains(response, 'data-unread-notifications-count>1<')

    def test_shop_staff_page_is_accessible_to_owner_and_staff_with_manage_permission(self):
        owner = User.objects.create_user(username="owner_staff_access", password="pass1234")
        staff_user = User.objects.create_user(username="staff_manager", password="pass1234")
        shop = Shop.objects.create(name="Access Shop", owner=owner)
        ShopStaff.objects.create(
            shop=shop,
            user=staff_user,
            role="MANAGER",
            is_active=True,
            can_manage_staff=True,
        )

        self.client.force_login(owner)
        owner_response = self.client.get(reverse("shop_staff_list_shop", kwargs={"shop_id": shop.id}))
        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, "Remove from this shop")

        self.client.force_login(staff_user)
        staff_response = self.client.get(reverse("shop_staff_list_shop", kwargs={"shop_id": shop.id}))
        self.assertEqual(staff_response.status_code, 200)

    def test_owner_can_create_shop_staff_from_shop_form(self):
        owner = User.objects.create_user(username="owner_creator", password="pass1234")
        shop = Shop.objects.create(name="Create Staff Shop", owner=owner)

        self.client.force_login(owner)
        response = self.client.post(
            reverse("shop_staff_create_shop", kwargs={"shop_id": shop.id}),
            {
                "username": "new_rider",
                "password": "pass1234",
                "role": "RIDER",
            },
        )

        self.assertEqual(response.status_code, 302)
        new_staff = ShopStaff.objects.get(
            shop=shop,
            user__username="new_rider",
        )
        self.assertTrue(new_staff.is_active)

    def test_owner_can_share_existing_staff_as_packer_with_another_shop(self):
        owner = User.objects.create_user(username="owner_share_packer")
        source_shop = Shop.objects.create(name="Packer Source Shop", owner=owner)
        target_shop = Shop.objects.create(name="Packer Target Shop", owner=owner)
        existing_staff_user = User.objects.create_user(username="shared_packer")
        ShopStaff.objects.create(
            shop=source_shop,
            user=existing_staff_user,
            role="MANAGER",
            is_active=True,
        )

        self.client.force_login(owner)
        response = self.client.post(
            reverse(
                "shop_staff_create_shop",
                kwargs={"shop_id": target_shop.pk},
            ),
            {
                "existing_staff_user": str(existing_staff_user.pk),
                "role": "PACKER",
            },
        )

        self.assertEqual(response.status_code, 302)
        shared_packer_membership = ShopStaff.objects.get(
            shop=target_shop,
            user=existing_staff_user,
        )
        self.assertEqual(shared_packer_membership.role, "PACKER")
        self.assertTrue(shared_packer_membership.is_active)

    def test_payment_manager_can_be_assigned_to_multiple_shops_as_same_staff_user(self):
        owner = User.objects.create_user(username="multi_shop_payment_owner")
        first_shop = Shop.objects.create(
            name="First Payment Shop",
            owner=owner,
        )
        second_shop = Shop.objects.create(
            name="Second Payment Shop",
            owner=owner,
        )
        third_shop = Shop.objects.create(
            name="Unapproved Payment Shop",
            owner=owner,
        )
        self.client.force_login(owner)

        first_membership_response = self.client.post(
            reverse(
                "shop_staff_create_shop",
                kwargs={"shop_id": first_shop.pk},
            ),
            {
                "username": "multi_shop_payment_manager",
                "password": "preserved-account-password",
                "role": "PAYMENT_MANAGER",
                "can_confirm_rider_payments": "on",
                "is_active": "on",
            },
        )
        self.assertEqual(first_membership_response.status_code, 302)
        payment_user = User.objects.get(username="multi_shop_payment_manager")
        inactive_user = User.objects.create_user(username="inactive_shop_employee")
        ShopStaff.objects.create(
            shop=first_shop,
            user=inactive_user,
            role="PACKER",
            is_active=False,
        )
        public_rider_user = User.objects.create_user(username="public_shop_employee")
        ShopStaff.objects.create(
            shop=first_shop,
            user=public_rider_user,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
        )
        existing_target_shop_user = User.objects.create_user(
            username="already_in_target_shop",
        )
        ShopStaff.objects.create(
            shop=second_shop,
            user=existing_target_shop_user,
            role="PACKER",
            is_active=True,
        )

        second_shop_form = self.client.get(
            reverse(
                "shop_staff_create_shop",
                kwargs={"shop_id": second_shop.pk},
            ),
        )
        self.assertEqual(second_shop_form.status_code, 200)
        self.assertIn(
            payment_user,
            second_shop_form.context["form"].fields[
                "existing_staff_user"
            ].queryset,
        )
        available_staff = second_shop_form.context["form"].fields[
            "existing_staff_user"
        ].queryset
        self.assertNotIn(inactive_user, available_staff)
        self.assertNotIn(public_rider_user, available_staff)
        self.assertNotIn(existing_target_shop_user, available_staff)

        second_membership_response = self.client.post(
            reverse(
                "shop_staff_create_shop",
                kwargs={"shop_id": second_shop.pk},
            ),
            {
                "existing_staff_user": str(payment_user.pk),
                "role": "PAYMENT_MANAGER",
                "can_confirm_rider_payments": "on",
            },
        )
        self.assertEqual(second_membership_response.status_code, 302)
        self.assertTrue(payment_user.check_password("preserved-account-password"))
        second_membership = ShopStaff.objects.get(
            user=payment_user,
            shop=second_shop,
        )
        self.assertEqual(second_membership.role, "PAYMENT_MANAGER")
        self.assertTrue(second_membership.can_confirm_rider_payments)
        self.assertTrue(second_membership.is_active)

        self.client.post(
            reverse(
                "shop_staff_create_shop",
                kwargs={"shop_id": third_shop.pk},
            ),
            {
                "username": "multi_shop_payment_manager",
                "role": "PAYMENT_MANAGER",
                "is_active": "on",
            },
        )
        self.client.force_login(payment_user)
        dashboard = self.client.get(
            reverse("staff_payment_manager_dashboard"),
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(
            {
                membership.shop_id
                for membership in dashboard.context["authorized_shops"]
            },
            {first_shop.pk, second_shop.pk},
        )

    def test_owner_can_remove_staff_from_one_shop_without_deleting_account_or_other_membership(self):
        owner = User.objects.create_user(username="remove_staff_owner")
        first_shop = Shop.objects.create(name="Remove Staff First Shop", owner=owner)
        second_shop = Shop.objects.create(name="Remove Staff Second Shop", owner=owner)
        employee = User.objects.create_user(username="employee_in_multiple_shops")
        first_membership = ShopStaff.objects.create(
            shop=first_shop,
            user=employee,
            role="PAYMENT_MANAGER",
        )
        second_membership = ShopStaff.objects.create(
            shop=second_shop,
            user=employee,
            role="PAYMENT_MANAGER",
        )

        self.client.force_login(owner)
        response = self.client.post(
            reverse(
                "shop_staff_remove",
                kwargs={
                    "shop_id": first_shop.pk,
                    "staff_id": first_membership.pk,
                },
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ShopStaff.objects.filter(pk=first_membership.pk).exists())
        self.assertTrue(
            ShopStaff.objects.filter(pk=second_membership.pk, user=employee).exists(),
        )
        self.assertTrue(User.objects.filter(pk=employee.pk).exists())

    def test_staff_removal_rejects_non_owner(self):
        owner = User.objects.create_user(username="protected_staff_owner")
        other_user = User.objects.create_user(username="protected_staff_other")
        shop = Shop.objects.create(name="Protected Staff Shop", owner=owner)
        employee = User.objects.create_user(username="protected_staff_employee")
        membership = ShopStaff.objects.create(
            shop=shop,
            user=employee,
            role="PACKER",
        )

        self.client.force_login(other_user)
        response = self.client.post(
            reverse(
                "shop_staff_remove",
                kwargs={"shop_id": shop.pk, "staff_id": membership.pk},
            ),
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ShopStaff.objects.filter(pk=membership.pk).exists())

    def test_shop_edit_form_applies_bootstrap_widget_styles(self):
        shop = Shop.objects.create(name="Styled Shop")
        form = ShopForm(instance=shop)

        self.assertEqual(form.fields["name"].widget.attrs["class"], "form-control")
        self.assertEqual(
            form.fields["market_category"].widget.attrs["class"],
            "form-select",
        )
        self.assertEqual(
            form.fields["delivery_fee"].widget.attrs["class"],
            "form-control",
        )
        self.assertEqual(form.fields["delivery_fee"].widget.input_type, "number")
        self.assertEqual(form.fields["shop_open_time"].widget.input_type, "time")

    def test_shop_owner_can_edit_gst_registration_details(self):
        owner = User.objects.create_user(username="shop_gst_settings_owner", password="test-password")
        shop = Shop.objects.create(name="Non GST Shop", owner=owner)
        self.client.force_login(owner)

        edit_response = self.client.get(
            reverse("shop_edit", kwargs={"pk": shop.pk})
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertEqual(
            edit_response.context["form"].fields["name"].widget.attrs["class"],
            "form-control",
        )
        self.assertEqual(
            edit_response.context["form"].fields["market_category"].widget.attrs["class"],
            "form-select",
        )
        self.assertContains(edit_response, "GST registration (optional)")
        self.assertContains(edit_response, 'name="gstin"')
        self.assertContains(edit_response, 'name="gst_state_code"')
        self.assertContains(edit_response, 'name="billing_address"')
        self.assertContains(
            edit_response,
            'name="share_riders_with_nearby_shops"',
        )
        self.assertContains(edit_response, 'id="useCurrentShopLocation"')
        self.assertContains(edit_response, "Satellite")
        self.assertContains(edit_response, '"🗺️ Normal": normalMap')
        self.assertContains(edit_response, '"Satellite": deliverySatelliteLayer')
        self.assertContains(edit_response, "satelliteMap.addTo(locationMap)")
        self.assertContains(edit_response, "deliverySatelliteLayer.addTo(deliveryMap)")
        self.assertContains(edit_response, "basemaps.cartocdn.com")
        self.assertNotContains(edit_response, "locationTileLayer.addTo(locationMap)")

        save_response = self.client.post(
            reverse("shop_edit", kwargs={"pk": shop.pk}),
            {
                "name": shop.name,
                "storefront_slug": "non-gst-shop",
                "delivery_fee": "0.00",
                "gstin": "24ABCDE1234F1Z5",
                "gst_state_code": "24",
                "billing_address": "Registered address, Ahmedabad",
                "delivery_hsn_code": "996511",
                "delivery_gst_rate": "18.00",
                "share_riders_with_nearby_shops": "on",
            },
        )
        self.assertEqual(save_response.status_code, 302)
        shop.refresh_from_db()
        self.assertEqual(shop.gstin, "24ABCDE1234F1Z5")
        self.assertEqual(shop.gst_state_code, "24")
        self.assertEqual(shop.billing_address, "Registered address, Ahmedabad")
        self.assertEqual(shop.delivery_hsn_code, "996511")
        self.assertEqual(shop.delivery_gst_rate, Decimal("18.00"))
        self.assertTrue(shop.share_riders_with_nearby_shops)

    def test_owner_can_view_shop_order_detail(self):
        owner = User.objects.create_user(username="owner_order_view", password="pass1234")
        shop = Shop.objects.create(name="Order Shop", owner=owner, location={"lat": 21.17, "lng": 72.83})
        order = Order.objects.create(
            order_id="ORD-1001",
            user="buyer",
            delivery_address="Main Road, Surat",
            delivery_location="21.1702,72.8311",
            delivery_contact_no="9876543210",
            status="PLACED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="250.00",
            status="PLACED",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Order detail image product",
            price="25.00",
            unit="piece",
            image_url="https://example.com/order-detail-product.jpg",
            sku="OD-IMAGE-1",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=product,
            product_name=product.product_name,
            sku=product.sku,
            quantity=2,
            price="25.00",
            total="50.00",
        )

        self.client.force_login(owner)
        response = self.client.get(reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ORD-1001")
        self.assertContains(response, 'src="https://example.com/order-detail-product.jpg"')
        self.assertContains(response, 'alt="Order detail image product"')
        self.assertContains(response, "SKU: OD-IMAGE-1")
        self.assertContains(response, "Open shop location in Maps")
        self.assertNotContains(response, "{&#x27;lat&#x27;")

    def test_shop_order_detail_displays_quantity_times_unit_value(self):
        owner = User.objects.create_user(username="owner_unit_measure_order")
        shop = Shop.objects.create(name="Unit Measure Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-UNIT-MEASURE",
            user="unit_measure_buyer",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop)
        product = Product.objects.create(
            created_by=owner,
            product_name="Unit Measure Product",
            price="10.00",
            unit="2pcs",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=product,
            product_name=product.product_name,
            quantity=3,
            price="10.00",
            total="30.00",
        )
        self.client.force_login(owner)

        response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 × 2pcs = 6pcs")

    def test_shop_order_detail_handles_approximate_item_without_product(self):
        owner = User.objects.create_user(
            username="owner_approximate_item_without_product",
        )
        shop = Shop.objects.create(name="Legacy Order Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-LEGACY-APPROXIMATE",
            user="legacy_buyer",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop)
        OrderItem.objects.create(
            shop_order=shop_order,
            product_name="Legacy weight item",
            quantity=1,
            is_approximate_weight=True,
            estimated_weight=Decimal("1.000"),
            price=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        self.client.force_login(owner)

        response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "price ₹10.00/unit")

    def test_owner_can_assign_specific_rider_to_order(self):
        owner = User.objects.create_user(username="owner_rider_assign", password="pass1234")
        rider_user = User.objects.create_user(username="rider_assignee", password="pass1234")
        shop = Shop.objects.create(name="Rider Shop", owner=owner)
        rider_staff = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            mobile_no="9123456789",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-2001",
            user="buyer",
            delivery_address="Road 7",
            delivery_location="22.0,73.0",
            delivery_contact_no="9898989898",
            status="READY",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="120.00",
            status="READY",
        )

        self.client.force_login(owner)
        response = self.client.post(
            reverse("assign_rider", kwargs={"shop_order_id": shop_order.id}),
            {"rider": rider_staff.id},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, rider_staff.id)
        self.assertFalse(shop_order.public_rider_network_requested)
        order.refresh_from_db()
        self.assertEqual(order.rider_contact_no, "9123456789")
        order_list_response = self.client.get(reverse("shop_order_list"))
        self.assertContains(order_list_response, "rider_assignee")
        self.assertContains(order_list_response, "Rider Contact")
        self.assertContains(order_list_response, "9123456789")
        self.assertContains(order_list_response, 'href="tel:9123456789"')

    def test_shop_can_route_one_order_to_public_rider_network(self):
        owner = User.objects.create_user(
            username="owner_public_network_assignment",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="public_network_assignment_rider",
            password="test-password",
        )
        PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Public Network Assignment Rider",
            phone="9876543210",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-PUBLIC-ASSIGN",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        shop = Shop.objects.create(
            name="Per Order Public Network Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
            public_rider_network_enabled=True,
            rider_delivery_preference="STAFF",
        )
        order = Order.objects.create(
            order_id="ORD-PUBLIC-NETWORK-ASSIGN",
            user="public_network_customer",
            status="READY",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )
        other_order = Order.objects.create(
            order_id="ORD-PUBLIC-NETWORK-UNSELECTED",
            user="another_public_network_customer",
            status="READY",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
            shop=shop,
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_order,
            shop=shop,
            status="READY",
        )
        distant_delivery_order = Order.objects.create(
            order_id="ORD-PUBLIC-NETWORK-DISTANT",
            user="distant_public_network_customer",
            status="READY",
            placed_location={"lat": 23.05, "lng": 72.05},
            delivery_location="23.05,72.05",
            shop=shop,
        )
        distant_delivery_shop_order = ShopOrder.objects.create(
            order=distant_delivery_order,
            shop=shop,
            status="READY",
        )
        outside_area_order = Order.objects.create(
            order_id="ORD-PUBLIC-NETWORK-OUTSIDE-AREA",
            user="outside_public_network_customer",
            status="READY",
            placed_location={"lat": 23.5, "lng": 72.5},
            delivery_location="23.5,72.5",
            shop=shop,
        )
        outside_area_shop_order = ShopOrder.objects.create(
            order=outside_area_order,
            shop=shop,
            status="READY",
        )
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(
            rider_user.public_rider_application,
        )

        self.client.force_login(owner)
        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertContains(detail_response, "Public rider network")
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        application = rider_user.public_rider_application
        application.work_area = {}
        application.save(update_fields=["work_area", "updated_at"])
        no_area_response = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(no_area_response.status_code, 200)
        self.assertEqual(no_area_response.context["available_order_count"], 0)
        self.assertContains(no_area_response, "Live delivery offers are paused.")
        self.assertContains(
            no_area_response,
            "Set and save a work area to start receiving offers.",
        )
        self.assertContains(
            no_area_response,
            "data-offer-poll-interval=\"5000\"",
        )
        self.assertContains(no_area_response, 'data-work-area-ready="false"')
        self.assertContains(
            no_area_response,
            'dispatch.dataset.workAreaReady === "true"',
        )
        polygon_area = {
            "type": "polygon",
            "points": [
                {"lat": 22.9, "lng": 71.9},
                {"lat": 23.1, "lng": 71.9},
                {"lat": 23.1, "lng": 72.1},
                {"lat": 22.9, "lng": 72.1},
            ],
        }
        area_response = self.client.post(
            reverse("save_public_rider_work_area"),
            {"work_area": json.dumps(polygon_area)},
        )
        self.assertEqual(area_response.status_code, 302)
        rider_user.public_rider_application.refresh_from_db()
        self.assertEqual(rider_user.public_rider_application.work_area, polygon_area)
        circle_area = {
            "type": "circle",
            "center": {"lat": 23.0, "lng": 72.0},
            "radius_km": 5,
        }
        circle_response = self.client.post(
            reverse("save_public_rider_work_area"),
            {"work_area": json.dumps(circle_area)},
        )
        self.assertEqual(circle_response.status_code, 302)
        rider_user.public_rider_application.refresh_from_db()
        self.assertEqual(
            rider_user.public_rider_application.work_area,
            {
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 5.0,
            },
        )
        restore_polygon_response = self.client.post(
            reverse("save_public_rider_work_area"),
            {"work_area": json.dumps(polygon_area)},
        )
        self.assertEqual(restore_polygon_response.status_code, 302)
        public_membership = ShopStaff.objects.get(
            user=rider_user,
            shop=shop,
            is_public_rider=True,
        )
        public_membership.last_location = {
            "lat": 23.0123,
            "lng": 72.0456,
            "observed_at": timezone.now().timestamp(),
        }
        public_membership.save(update_fields=["last_location", "updated_at"])

        self.client.force_login(owner)
        assign_response = self.client.post(
            reverse("assign_rider", kwargs={"shop_order_id": shop_order.pk}),
            {"rider": "PUBLIC"},
        )
        self.assertEqual(assign_response.status_code, 302)

        shop_order.refresh_from_db()
        self.assertTrue(shop_order.public_rider_network_requested)
        self.assertIsNone(shop_order.assigned_rider_id)

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        dashboard_response = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_response.context["available_order_count"], 1)
        self.assertEqual(
            dashboard_response.context["orders"][0].pk,
            shop_order.pk,
        )
        self.assertContains(dashboard_response, 'id="publicRiderWorkAreaMap"')
        self.assertContains(
            dashboard_response,
            'aria-label="Map for drawing public rider work area"',
        )
        self.assertContains(
            dashboard_response,
            "function isPublicRiderWorkAreaBeingEdited()",
        )
        self.assertContains(
            dashboard_response,
            'details.open',
        )
        self.assertContains(dashboard_response, "markWorkAreaDirty();")
        self.assertContains(
            dashboard_response,
            '&& !document.querySelector(".public-rider-work-area[open]")',
        )
        self.assertContains(
            dashboard_response,
            "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        )
        self.assertContains(
            dashboard_response,
            'id="publicRiderFallbackDrawControls" hidden',
        )
        self.assertContains(
            dashboard_response,
            'drawPolygonButton?.addEventListener("click"',
        )
        self.assertContains(
            dashboard_response,
            'map.on("click", (event) => {',
        )
        self.assertContains(
            dashboard_response,
            "Browser location requires HTTPS (or localhost)",
        )
        self.assertContains(
            dashboard_response,
            "Showing your last saved location while live location updates.",
        )
        self.assertContains(
            dashboard_response,
            '"lat": 23.0123, "lng": 72.0456',
        )
        self.assertContains(dashboard_response, "Focus map on my location")
        self.assertContains(dashboard_response, 'bindTooltip("Your current location"')
        self.assertContains(
            dashboard_response,
            "if (followCurrentLocation) centerOnCurrentLocation();",
        )
        self.assertContains(
            dashboard_response,
            '<option value="circle" selected>Circle</option>',
        )
        self.assertContains(
            dashboard_response,
            'id="publicRiderCircleRadius"',
        )
        self.assertContains(dashboard_response, 'min="0.05" max="1000" step="any" value="5"')
        self.assertContains(dashboard_response, "setCircleCenter(location)")
        self.assertContains(dashboard_response, "workAreaType === \"circle\"")
        self.assertContains(dashboard_response, 'id="publicRiderDrawPolygon"')
        self.assertContains(dashboard_response, 'id="publicRiderFinishPolygon"')
        self.assertContains(dashboard_response, "satelliteLayer.addTo(map)")
        self.assertContains(
            dashboard_response,
            "centerOnCurrentLocation();",
            count=4,
        )
        self.assertContains(dashboard_response, 'workAreaForm?.addEventListener("submit"')
        self.assertContains(
            dashboard_response,
            "Draw and finish a work area before saving.",
        )
        self.assertContains(dashboard_response, "Saving your work area…")
        self.assertContains(dashboard_response, "navigator.geolocation.watchPosition(")
        self.assertContains(dashboard_response, "Click the map to add at least three polygon points")
        self.assertContains(dashboard_response, "workAreaMapHandlesLocation")
        self.assertContains(dashboard_response, "currentLocationMarker.setLatLng(location)")
        self.assertContains(dashboard_response, "now - lastLocationSentAt < 15000")
        self.assertContains(dashboard_response, "map.setView(location, Math.max(map.getZoom(), 13)")
        self.assertContains(dashboard_response, "currentLocationCentered")
        self.assertContains(dashboard_response, '"Satellite": satelliteLayer')
        self.assertContains(
            dashboard_response,
            "requestAnimationFrame(invalidateMap)",
        )
        self.assertNotIn(
            other_shop_order.pk,
            [row.pk for row in dashboard_response.context["orders"]],
        )
        shop_order.refresh_from_db()
        self.assertEqual(
            shop_order.public_rider_offer_user_id,
            rider_user.pk,
        )

        accept_response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
            {
                "rider_latitude": "23.0",
                "rider_longitude": "72.0",
                "rider_location_observed_at": str(timezone.now().timestamp()),
            },
        )
        self.assertEqual(accept_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider.user_id, rider_user.pk)
        rider_application = rider_user.public_rider_application
        rider_application.refresh_from_db()
        self.assertTrue(rider_application.public_rider_batch_accepting)

        for candidate in (
            other_shop_order,
            distant_delivery_shop_order,
            outside_area_shop_order,
        ):
            candidate.public_rider_network_requested = True
            candidate.save(update_fields=[
                "public_rider_network_requested",
                "updated_at",
            ])
        other_shop_order.public_rider_offer_expires_at = (
            timezone.now() + timedelta(seconds=20)
        )
        other_shop_order.public_rider_offer_user = rider_user
        other_shop_order.save(update_fields=[
            "public_rider_offer_expires_at",
            "public_rider_offer_user",
            "updated_at",
        ])
        rider_application.public_rider_batch_started_at = (
            timezone.now() - timedelta(minutes=6)
        )
        rider_application.save(update_fields=[
            "public_rider_batch_started_at",
            "updated_at",
        ])
        expired_batch_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertEqual(expired_batch_response.context["available_order_count"], 0)
        self.assertEqual(expired_batch_response.context["selected_view"], "active")
        self.assertNotIn(
            other_shop_order.pk,
            [row.pk for row in expired_batch_response.context["orders"]],
        )
        self.assertFalse(
            any(
                row.is_collection_offer
                for row in expired_batch_response.context["orders"]
            )
        )
        self.assertTrue(
            expired_batch_response.context[
                "public_rider_collection_window_expired"
            ]
        )
        self.assertTrue(
            expired_batch_response.context[
                "public_rider_collection_window_closed"
            ]
        )
        self.assertContains(expired_batch_response, "Offer window closed")
        self.assertNotContains(expired_batch_response, "<span>Available</span>")
        self.assertTrue(
            Notification.objects.filter(
                user=rider_user,
                title="Collection window closed",
            ).exists()
        )
        rider_application.public_rider_batch_started_at = timezone.now()
        rider_application.save(update_fields=[
            "public_rider_batch_started_at",
            "updated_at",
        ])
        other_shop_order.public_rider_offer_expires_at = (
            timezone.now() + timedelta(seconds=20)
        )
        other_shop_order.public_rider_offer_user = rider_user
        other_shop_order.save(update_fields=[
            "public_rider_offer_expires_at",
            "public_rider_offer_user",
            "updated_at",
        ])

        batch_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertEqual(batch_response.status_code, 200)
        self.assertContains(batch_response, "Order collection window")
        self.assertEqual(batch_response.context["available_order_count"], 1)
        self.assertEqual(
            batch_response.context["orders"][0].pk,
            other_shop_order.pk,
        )
        self.assertNotIn(
            distant_delivery_shop_order.pk,
            [row.pk for row in batch_response.context["orders"]],
        )
        self.assertNotIn(
            outside_area_shop_order.pk,
            [row.pk for row in batch_response.context["orders"]],
        )
        other_shop_order.refresh_from_db()
        self.assertEqual(other_shop_order.public_rider_offer_user_id, rider_user.pk)

        active_batch_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertEqual(active_batch_response.status_code, 200)
        active_batch_orders = {
            row.pk: row for row in active_batch_response.context["orders"]
        }
        self.assertIn(shop_order.pk, active_batch_orders)
        self.assertIn(other_shop_order.pk, active_batch_orders)
        self.assertTrue(active_batch_orders[other_shop_order.pk].is_collection_offer)
        self.assertContains(active_batch_response, "Accept delivery")
        self.assertContains(active_batch_response, "Stop offers &amp; start pickup")

        close_batch_response = self.client.post(
            reverse("stop_public_rider_batch_offers"),
        )
        self.assertEqual(close_batch_response.status_code, 302)
        other_shop_order.refresh_from_db()
        self.assertNotEqual(
            other_shop_order.public_rider_offer_user_id,
            rider_user.pk,
        )
        post_stop_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertEqual(post_stop_dashboard.status_code, 200)
        rider_application.refresh_from_db()
        self.assertFalse(rider_application.public_rider_batch_accepting)
        self.assertTrue(
            post_stop_dashboard.context[
                "public_rider_collection_window_closed"
            ]
        )
        self.assertContains(post_stop_dashboard, "You stopped new offers")
        other_shop_order.refresh_from_db()
        self.assertIsNone(other_shop_order.public_rider_offer_user_id)

    def test_rider_can_accept_unassigned_order_when_no_order_accepted_yet(self):
        owner = User.objects.create_user(username="owner_accept_order", password="pass1234")
        rider_user = User.objects.create_user(username="rider_acceptor", password="pass1234")
        shop = Shop.objects.create(
            name="Accept Shop",
            owner=owner,
            rider_delivery_preference="STAFF",
        )
        rider_staff = ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-2002",
            user="buyer2",
            delivery_address="Road 8",
            delivery_location="22.1,73.1",
            delivery_contact_no="9898989897",
            status="READY",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="150.00",
            status="READY",
        )

        self.client.force_login(rider_user)
        response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.id}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.id}",
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, rider_staff.id)

    def test_rider_dashboard_tabs_render_above_dashboard_content(self):
        owner = User.objects.create_user(
            username="owner_sticky_tabs",
            password="test-pass",
        )
        rider_user = User.objects.create_user(
            username="rider_sticky_tabs",
            password="test-pass",
        )
        shop = Shop.objects.create(name="Sticky Tabs Shop", owner=owner)
        ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        self.client.force_login(rider_user)

        response = self.client.get(reverse("rider_dashboard"))

        self.assertEqual(response.status_code, 200)
        response_html = response.content.decode()
        tabs_position = response_html.index(
            'aria-label="Rider dashboard tabs"',
        )
        shop_identity_position = response_html.index(
            f"{shop.name} · {rider_user.username}",
        )
        self.assertLess(tabs_position, shop_identity_position)
        self.assertNotContains(response, "Rider dashboard</h2>")
        self.assertNotContains(response, "Shared-shop offers appear")
        self.assertContains(response, ">Available</span>")
        self.assertContains(response, ">Active</span>")
        self.assertContains(response, ">Recently verified</span>")
        self.assertContains(response, ">Done</span>")
        self.assertContains(response, ">Stats</span>")
        self.assertContains(response, ">Handover</span>")
        self.assertContains(response, "?view=handover")
        self.assertContains(response, "navbar.getBoundingClientRect().height / contentZoom")
        self.assertContains(response, "rider-dashboard-tabs")
        self.assertContains(response, 'class="rider-dashboard-more-menu"')
        self.assertContains(response, 'className = "rider-dashboard-tabs-spacer"')
        self.assertContains(response, "@media (min-width: 992px)")
        self.assertContains(response, ".rider-order-slide > .card:hover")

        handover_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "handover"},
        )
        self.assertEqual(handover_response.status_code, 200)
        self.assertTrue(handover_response.context["handover_view"])
        self.assertEqual(handover_response.context["handover_count"], 0)

    def test_active_route_cards_are_inside_collapsed_load_panel(self):
        owner = User.objects.create_user(
            username="owner_collapsed_route_cards",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="rider_collapsed_route_cards",
            password="test-password",
        )
        shop = Shop.objects.create(name="Collapsed Route Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-COLLAPSED-ROUTE",
            user="collapsed_route_customer",
            delivery_address="Route destination",
            delivery_location="22.1,73.1",
            status="OUT_FOR_DELIVERY",
            shop=shop,
        )
        ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("100.00"),
            status="OUT_FOR_DELIVERY",
            assigned_rider=rider,
        )
        self.client.force_login(rider_user)

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(response.status_code, 200)
        response_html = response.content.decode()
        panel_start = response_html.index(
            '<details class="card border-0 shadow-sm mb-4 rider-load-panel"',
        )
        panel_end = response_html.index("</details>", panel_start)
        self.assertNotIn(
            '<details class="card border-0 shadow-sm mb-4 rider-load-panel" open',
            response_html,
        )
        for panel_content in (
            "Current estimated load &amp; route details",
            "Multi-stop delivery route",
            "Complete active-order route",
            "Active route stops",
            "No parcels are currently picked up",
        ):
            content_position = response_html.index(panel_content)
            self.assertGreater(content_position, panel_start)
            self.assertLess(content_position, panel_end)
        tabs_wrap_start = response_html.index('class="rider-dashboard-tabs-wrap"')
        actions_start = response_html.index('class="rider-active-actions"')
        actions_end = response_html.index("</section>", actions_start)
        wrapper_end = response_html.index("</div>", actions_end)
        self.assertGreater(actions_start, tabs_wrap_start)
        self.assertLess(actions_end, wrapper_end)

    def test_rider_dashboard_shows_calculated_item_measure(self):
        owner = User.objects.create_user(
            username="owner_item_measure",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="rider_item_measure",
            password="test-password",
        )
        shop = Shop.objects.create(name="Item Measure Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-RIDER-ITEM-MEASURE",
            delivery_address="Measure Street",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="180.00",
            status="READY",
            assigned_rider=rider,
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product_name="Apples",
            quantity=3,
            unit="2pcs",
            price="60.00",
            total="180.00",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product_name="Tomatoes",
            quantity=2,
            unit="kg",
            price="30.00",
            total="60.00",
            is_approximate_weight=True,
            estimated_weight=Decimal("2.000"),
            final_weight=Decimal("1.750"),
        )
        self.client.force_login(rider_user)

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 × 2pcs = 6pcs")
        self.assertContains(response, "Final weight: 1.750 kg")
        self.assertContains(response, ">Final</span>")
        self.assertContains(response, ">Done</span>")
        self.assertContains(response, ">Stats</span>")

    def test_rider_dashboard_displays_latest_status_change_time(self):
        owner = User.objects.create_user(
            username="owner_status_timestamp",
        )
        rider_user = User.objects.create_user(
            username="rider_status_timestamp",
        )
        shop = Shop.objects.create(
            name="Status Timestamp Shop",
            owner=owner,
            location={"lat": 21.2, "lng": 72.8},
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-RIDER-STATUS-TIMESTAMP",
            delivery_address="Timestamp Street",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="25.00",
            status="READY",
            assigned_rider=rider,
        )
        placed_at = timezone.now() - timedelta(days=30)
        changed_at = timezone.now() - timedelta(days=2)
        ShopOrder.objects.filter(pk=shop_order.pk).update(
            created_at=placed_at,
            updated_at=placed_at,
        )
        history = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="READY",
            remark="Order became ready.",
        )
        OrderStatusHistory.objects.filter(pk=history.pk).update(
            created_at=changed_at,
        )
        self.client.force_login(rider_user)

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(response.status_code, 200)
        changed_display = timezone.localtime(changed_at).strftime(
            "%d %b %Y %I:%M %p",
        )
        placed_display = timezone.localtime(placed_at).strftime(
            "%d %b %Y %I:%M %p",
        )
        self.assertContains(response, f"Status updated: {changed_display}")
        self.assertNotContains(response, placed_display)

    def test_rider_dashboard_can_call_or_copy_customer_phone(self):
        owner = User.objects.create_user(
            username="owner_customer_phone",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="rider_customer_phone",
            password="test-password",
        )
        shop = Shop.objects.create(name="Customer Phone Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-RIDER-CUSTOMER-PHONE",
            delivery_address="Phone Street",
            delivery_contact_no="+919988776655",
            shop=shop,
        )
        ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="25.00",
            status="READY",
            assigned_rider=rider,
        )
        self.client.force_login(rider_user)

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Call customer")
        self.assertContains(response, 'href="tel:+919988776655"')
        self.assertContains(response, 'class="btn btn-sm btn-outline-secondary ms-1 rider-copy-phone"')
        self.assertContains(response, 'data-phone="+919988776655"')

    def test_rider_capacity_allows_order_at_exact_limit(self):
        owner = User.objects.create_user(username="owner_capacity_exact", password="test-pass")
        rider_user = User.objects.create_user(username="rider_capacity_exact", password="test-pass")
        shop = Shop.objects.create(name="Capacity Exact Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            max_load_weight_kg=Decimal("0.50"),
            max_load_volume_cubic_ft=Decimal("0.25"),
        )
        order = Order.objects.create(
            order_id="ORD-CAPACITY-EXACT",
            user="capacity_exact_customer",
            status="READY",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("500.00"),
            product_count=1,
            status="READY",
        )

        self.client.force_login(rider_user)
        response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, rider.pk)

    def test_rider_capacity_blocks_overflow_from_available_and_acceptance(self):
        owner = User.objects.create_user(username="owner_capacity_full", password="test-pass")
        rider_user = User.objects.create_user(username="rider_capacity_full", password="test-pass")
        shop = Shop.objects.create(name="Capacity Full Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            max_load_weight_kg=Decimal("0.25"),
            max_load_volume_cubic_ft=Decimal("1.00"),
        )
        active_order = Order.objects.create(
            order_id="ORD-CAPACITY-ACTIVE",
            user="capacity_active_customer",
            status="RIDER_ASSIGNED",
            shop=shop,
        )
        ShopOrder.objects.create(
            order=active_order,
            shop=shop,
            total_amount=Decimal("400.00"),
            product_count=1,
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
        )
        available_order = Order.objects.create(
            order_id="ORD-CAPACITY-NEXT",
            user="capacity_next_customer",
            status="READY",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=available_order,
            shop=shop,
            total_amount=Decimal("400.00"),
            product_count=1,
            status="READY",
        )

        self.client.force_login(rider_user)
        dashboard_response = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertNotContains(dashboard_response, "ORD-CAPACITY-NEXT")
        self.assertNotContains(
            dashboard_response,
            "aria-label=\"Rider load capacity\"",
        )
        self.assertContains(
            dashboard_response,
            "Load capacity exceeded.",
        )

        active_dashboard_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(active_dashboard_response, "Current estimated load")
        self.assertContains(
            active_dashboard_response,
            '<details class="card border-0 shadow-sm mb-4 rider-load-panel"',
        )
        self.assertNotContains(
            active_dashboard_response,
            '<details class="card border-0 shadow-sm mb-4 rider-load-panel" open',
        )

        handover_dashboard_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "handover"},
        )
        self.assertContains(
            handover_dashboard_response,
            "Load capacity exceeded.",
        )
        self.assertNotContains(
            handover_dashboard_response,
            "aria-label=\"Rider load capacity\"",
        )

        response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertEqual(shop_order.status, "READY")

    def test_rider_can_save_load_capacity_in_payment_settings(self):
        owner = User.objects.create_user(username="owner_capacity_settings", password="test-pass")
        rider_user = User.objects.create_user(username="rider_capacity_settings", password="test-pass")
        shop = Shop.objects.create(name="Capacity Settings Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        self.client.force_login(rider_user)

        settings_response = self.client.get(reverse("staff_payment_settings"))
        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, 'name="max_load_weight_kg"')
        self.assertContains(settings_response, 'name="max_load_volume_cubic_ft"')

        save_response = self.client.post(
            reverse("staff_payment_settings"),
            {
                "max_load_weight_kg": "25.00",
                "max_load_volume_cubic_ft": "4.50",
            },
        )

        self.assertEqual(save_response.status_code, 302)
        rider.refresh_from_db()
        self.assertEqual(rider.max_load_weight_kg, Decimal("25.00"))
        self.assertEqual(rider.max_load_volume_cubic_ft, Decimal("4.50"))

    def test_shop_order_otp_verifies_exact_rider_shop_order_and_is_one_time(self):
        owner = User.objects.create_user(
            username="pickup_qr_shop_owner",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="pickup_qr_rider",
            password="test-password",
        )
        outsider = User.objects.create_user(
            username="pickup_qr_outsider",
            password="test-password",
        )
        shop = Shop.objects.create(name="QR Pickup Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-PICKUP-QR-1",
            user="pickup_qr_customer",
            status="READY",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )
        self.client.force_login(rider_user)
        self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")

        rider_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(rider_dashboard, "verify this parcel is in your active orders")
        shop_order.refresh_from_db()
        pickup_otp = shop_order.rider_pickup_otp
        self.assertRegex(pickup_otp, r"^\d{6}$")
        otp_seconds_remaining = (
            shop_order.rider_pickup_otp_expires_at - timezone.now()
        ).total_seconds()
        self.assertGreater(otp_seconds_remaining, 295)
        self.assertLessEqual(otp_seconds_remaining, 300)
        self.assertContains(rider_dashboard, pickup_otp)
        self.assertContains(rider_dashboard, "Scan at shop")
        self.assertContains(
            rider_dashboard,
            "Task in this QR: verify rider identity and confirm parcel pickup.",
        )
        self.assertContains(rider_dashboard, "data:image/png;base64,")
        with patch.object(
            seller_views,
            "_qr_code_data_uri",
            wraps=seller_views._qr_code_data_uri,
        ) as qr_generator:
            self.client.get(
                reverse("rider_dashboard"),
                {"view": "active"},
            )
        self.assertIn(
            f"{seller_views.RIDER_PICKUP_QR_PREFIX}{pickup_otp}",
            [call.args[0] for call in qr_generator.call_args_list],
        )

        self.client.force_login(owner)
        shop_detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(shop_detail.status_code, 200)
        self.assertContains(shop_detail, "six-digit pickup OTP")
        self.assertContains(shop_detail, "Scan rider OTP QR")
        self.assertContains(shop_detail, "data-pickup-otp-input")
        self.assertContains(shop_detail, "ORD-PICKUP-QR-1")
        self.assertContains(shop_detail, rider_user.username)
        self.assertNotContains(shop_detail, pickup_otp)
        self.assertTrue(shop_detail.context["can_confirm_rider_pickup"])

        other_owner = User.objects.create_user(
            username="pickup_other_shop_owner",
            password="test-password",
        )
        other_shop = Shop.objects.create(name="Other OTP Shop", owner=other_owner)
        other_order = Order.objects.create(
            order_id="ORD-PICKUP-OTP-OTHER",
            user="other_otp_customer",
            status="READY",
            shop=other_shop,
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_order,
            shop=other_shop,
            assigned_rider=rider,
            status="RIDER_ASSIGNED",
        )
        other_otp = seller_views._ensure_rider_pickup_otp(other_shop_order)
        self.assertNotEqual(other_otp, pickup_otp)
        self.client.force_login(other_owner)
        cross_shop_attempt = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": other_shop_order.pk}),
            {"pickup_otp": pickup_otp},
        )
        self.assertRedirects(
            cross_shop_attempt,
            reverse("shop_order_detail", kwargs={"shop_order_id": other_shop_order.pk}),
            fetch_redirect_response=False,
        )
        other_shop_order.refresh_from_db()
        self.assertEqual(other_shop_order.status, "RIDER_ASSIGNED")
        other_shop_order.status = "CANCELLED"
        other_shop_order.save(update_fields=["status"])

        wrong_otp = "000000" if pickup_otp != "000000" else "000001"
        self.client.force_login(owner)
        mismatch_response = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": shop_order.pk}),
            {"pickup_otp": wrong_otp},
        )
        self.assertEqual(mismatch_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")

        self.client.force_login(outsider)
        self.assertEqual(
            self.client.post(
                reverse("rider_pickup_order", kwargs={"shop_order_id": shop_order.pk}),
                {"pickup_otp": pickup_otp},
            ).status_code,
            403,
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")

        shop_order.rider_pickup_otp_expires_at = timezone.now() - timedelta(seconds=1)
        shop_order.save(update_fields=["rider_pickup_otp_expires_at"])
        self.client.force_login(owner)
        expired = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": shop_order.pk}),
            {"pickup_otp": pickup_otp},
        )
        self.assertRedirects(
            expired,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
            fetch_redirect_response=False,
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")

        self.client.force_login(rider_user)
        self.client.get(reverse("rider_dashboard"), {"view": "active"})
        shop_order.refresh_from_db()
        renewed_otp = shop_order.rider_pickup_otp
        self.assertNotEqual(renewed_otp, pickup_otp)

        self.client.force_login(owner)
        all_shop_orders = self.client.get(reverse("shop_order_list"))
        self.assertNotContains(
            all_shop_orders,
            "Verify rider and confirm pickup",
        )
        self.assertNotContains(all_shop_orders, "Rider COD handovers")
        shop_order_list = self.client.get(
            reverse("shop_order_list"),
            {"view": "handover"},
        )
        self.assertContains(shop_order_list, "Verify rider and confirm pickup")
        self.assertContains(shop_order_list, "shopPickupOtp")
        self.assertContains(shop_order_list, "Scan rider OTP QR")
        self.assertContains(shop_order_list, 'id="riderPickupTaskQrForm"')
        self.assertContains(shop_order_list, "SH-TASK:VERIFY_RIDER_PICKUP:")
        invalid_scan = self.client.post(
            reverse("verify_rider_pickup_otp"),
            {"pickup_otp": "SH-PICKUP:000000"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(invalid_scan.status_code, 400)
        self.assertFalse(invalid_scan.json()["success"])
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")
        confirmed = self.client.post(
            reverse("verify_rider_pickup_otp"),
            {
                "pickup_otp": (
                    f"{seller_views.RIDER_PICKUP_QR_PREFIX}{renewed_otp}"
                ),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.json()["success"])
        self.assertEqual(
            confirmed.json()["redirect_url"],
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")
        self.assertTrue(
            shop_order.status_history.filter(
                status="PICKED_UP",
                remark__contains=owner.username,
            ).exists()
        )
        pickup_detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertContains(pickup_detail, "Picked Up")
        replayed = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": shop_order.pk}),
            {"pickup_otp": renewed_otp},
        )
        self.assertRedirects(
            replayed,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
            fetch_redirect_response=False,
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")
        self.client.force_login(rider_user)
        active_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(active_dashboard, "Start delivery")
        start_delivery = self.client.post(
            reverse("rider_start_active_deliveries"),
        )
        self.assertRedirects(
            start_delivery,
            f"{reverse('rider_dashboard')}?view=active",
            fetch_redirect_response=False,
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

    def test_assigned_rider_can_check_shared_order_qr_without_changing_status(self):
        owner = User.objects.create_user(
            username="printed_pickup_shop_owner",
        )
        rider_user = User.objects.create_user(
            username="printed_pickup_rider",
        )
        other_rider_user = User.objects.create_user(
            username="printed_pickup_other_rider",
        )
        shop = Shop.objects.create(name="Printed Pickup Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        other_rider = ShopStaff.objects.create(
            shop=shop,
            user=other_rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-PRINTED-PICKUP",
            user="printed_pickup_customer",
            status="RIDER_ASSIGNED",
            assigned_rider=rider_user.username,
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            assigned_rider=rider,
            status="RIDER_ASSIGNED",
            rider_accepted_at=timezone.now(),
        )
        other_order = Order.objects.create(
            order_id="ORD-PRINTED-PICKUP-OTHER",
            user="printed_pickup_other_customer",
            status="RIDER_ASSIGNED",
            shop=shop,
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_order,
            shop=shop,
            assigned_rider=other_rider,
            status="RIDER_ASSIGNED",
            rider_accepted_at=timezone.now(),
        )
        accepted_only_order = Order.objects.create(
            order_id="ORD-ACCEPTED-WITHOUT-FK",
            user="accepted_without_fk_customer",
            status="RIDER_ASSIGNED",
            assigned_rider=rider_user.username,
            shop=shop,
        )
        accepted_only_shop_order = ShopOrder.objects.create(
            order=accepted_only_order,
            shop=shop,
            status="RIDER_ASSIGNED",
            rider_accepted_at=timezone.now(),
        )

        self.client.force_login(owner)
        with patch.object(
            seller_views,
            "_qr_code_data_uri",
            wraps=seller_views._qr_code_data_uri,
        ) as qr_generator:
            slip_response = self.client.get(
                reverse("packing_slip", kwargs={"shop_order_id": shop_order.pk}),
            )
        self.assertEqual(slip_response.status_code, 200)
        self.assertContains(slip_response, "A rider can scan it in the SellerHelper app")
        self.assertTrue(
            slip_response.context["qr_code_data_uri"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertEqual(qr_generator.call_count, 1)
        printed_order_qr_value = qr_generator.call_args_list[0].args[0]
        self.assertEqual(
            slip_response.content.count(b'<img class="order-qr"'),
            1,
        )
        self.assertNotContains(slip_response, "pickup-qr-block")

        order_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(order_response.status_code, 200)
        self.assertContains(
            order_response,
            "Rider: scan this QR to verify the parcel is in your active orders",
        )
        self.assertContains(order_response, "Print slip with order QR")
        self.assertTrue(
            order_response.context["order_qr_data_uri"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertEqual(
            order_response.context["order_qr_data_uri"],
            slip_response.context["qr_code_data_uri"],
        )
        ready_order = Order.objects.create(
            order_id="ORD-READY-ASSIGNED-QR",
            user="ready_assigned_qr_customer",
            status="READY",
            shop=shop,
        )
        ready_shop_order = ShopOrder.objects.create(
            order=ready_order,
            shop=shop,
            assigned_rider=rider,
            status="READY",
        )
        self.client.force_login(rider_user)
        ready_rider_detail_response = self.client.get(
            reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": ready_shop_order.pk},
            ),
        )
        self.assertEqual(ready_rider_detail_response.status_code, 200)
        self.assertTrue(ready_rider_detail_response.context["show_rider_order_qr"])
        self.assertContains(
            ready_rider_detail_response,
            "Rider: scan this QR to verify the parcel is in your active orders",
        )
        accepted_only_detail_response = self.client.get(
            reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": accepted_only_shop_order.pk},
            ),
        )
        self.assertTrue(accepted_only_detail_response.context["show_rider_order_qr"])
        self.assertContains(
            accepted_only_detail_response,
            "Rider: scan this QR to verify the parcel is in your active orders",
        )
        self.assertContains(
            accepted_only_detail_response,
            rider_user.username,
        )
        with patch.object(
            seller_views,
            "_qr_code_data_uri",
            wraps=seller_views._qr_code_data_uri,
        ) as accepted_only_qr_generator:
            accepted_only_slip_response = self.client.get(
                reverse(
                    "packing_slip",
                    kwargs={"shop_order_id": accepted_only_shop_order.pk},
                ),
            )
        self.assertEqual(
            accepted_only_slip_response.content.count(b'<img class="order-qr"'),
            1,
        )
        accepted_only_order_qr = accepted_only_qr_generator.call_args_list[0].args[0]
        with patch.object(
            seller_views,
            "_qr_code_data_uri",
            wraps=seller_views._qr_code_data_uri,
        ) as qr_generator:
            self.client.get(
                reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
            )
        self.assertEqual(len(qr_generator.call_args_list), 1)
        screen_order_qr_value = qr_generator.call_args_list[0].args[0]
        self.assertEqual(screen_order_qr_value, printed_order_qr_value)

        self.client.force_login(other_rider_user)
        wrong_rider_response = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {"order_qr": screen_order_qr_value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(
            wrong_rider_response.status_code,
            403,
        )
        self.assertFalse(wrong_rider_response.json()["success"])
        self.assertIn(
            "neither assigned to nor accepted by your rider account",
            wrong_rider_response.json()["message"],
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")

        self.client.force_login(rider_user)
        rider_session = self.client.session
        rider_session["active_portal"] = "SHOP_STAFF"
        rider_session.save()
        dashboard_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertTrue(dashboard_response.context["is_staff_portal"])
        self.assertEqual(
            dashboard_response.context["staff_permissions"].role,
            "RIDER",
        )
        self.assertContains(
            dashboard_response,
            "Check this order is in your active orders",
        )
        self.assertContains(
            dashboard_response,
            'data-sellerhelper-scan-order="rider-pickup-order"',
        )
        self.assertRegex(
            dashboard_response.content.decode(),
            r'(?s)<div class="mobile-bottom-nav mobile-only">.*?'
            r'data-sellerhelper-scan-order="rider-pickup-order".*?'
            r"Check order.*?</button>",
        )
        self.assertContains(dashboard_response, "riderOrderQrScanForm")

        self.client.force_login(owner)
        pickup_otp = seller_views._ensure_rider_pickup_otp(shop_order)
        staff_pickup_response = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": shop_order.pk}),
            {"pickup_otp": pickup_otp},
        )
        self.assertEqual(staff_pickup_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")

        self.client.force_login(rider_user)
        picked_up_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(
            picked_up_dashboard,
            "Check this parcel is in your active orders",
        )
        active_order_ids = {
            active_order.pk
            for active_order in picked_up_dashboard.context["orders"]
        }
        self.assertIn(shop_order.pk, active_order_ids)

        verification_response = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {"order_qr": screen_order_qr_value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(verification_response.status_code, 200)
        self.assertTrue(verification_response.json()["success"])
        self.assertEqual(
            verification_response.json()["order_url"],
            reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": shop_order.pk},
            ),
        )
        self.assertTrue(verification_response.json()["assigned_to_you"])
        self.assertTrue(verification_response.json()["accepted_by_you"])
        self.assertTrue(verification_response.json()["is_active_order"])
        self.assertContains(
            dashboard_response,
            "window.location.assign(result.order_url);",
        )
        self.assertContains(
            dashboard_response,
            "new URL(shopOrderPath, window.location.origin).toString()",
        )
        self.assertNotContains(
            dashboard_response,
            "scannedUrl.origin === window.location.origin",
        )
        self.assertEqual(
            verification_response.json()["order_status"],
            "PICKED_UP",
        )
        self.assertIn("in your active orders", verification_response.json()["message"])
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")
        self.assertEqual(order.status, "PICKED_UP")
        self.assertEqual(
            shop_order.status_history.filter(
                status="PICKED_UP",
            ).count(),
            1,
        )

        replay_response = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {"order_qr": screen_order_qr_value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(replay_response.status_code, 200)
        self.assertTrue(replay_response.json()["success"])
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")

        self.assertEqual(
            accepted_only_detail_response.context["order_qr_data_uri"],
            accepted_only_slip_response.context["qr_code_data_uri"],
        )
        accepted_only_scan = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {"order_qr": accepted_only_order_qr},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(accepted_only_scan.status_code, 200)
        self.assertFalse(accepted_only_scan.json()["assigned_to_you"])
        self.assertTrue(accepted_only_scan.json()["accepted_by_you"])
        self.assertTrue(accepted_only_scan.json()["is_active_order"])
        self.assertIn("in your active orders", accepted_only_scan.json()["message"])
        accepted_only_shop_order.refresh_from_db()
        self.assertEqual(accepted_only_shop_order.status, "RIDER_ASSIGNED")
        accepted_only_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertIn(
            accepted_only_shop_order.pk,
            {
                active_order.pk
                for active_order in accepted_only_dashboard.context["orders"]
            },
        )

        self.client.force_login(other_rider_user)
        self.assertEqual(
            self.client.post(
                reverse("rider_scan_pickup_qr"),
                {"order_qr": "https://example.invalid/shop/orders/123/"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            ).status_code,
            400,
        )
        other_shop_order.refresh_from_db()
        self.assertEqual(other_shop_order.status, "RIDER_ASSIGNED")

    def test_public_rider_can_start_delivery_after_both_pickup_checks(self):
        owner = User.objects.create_user(
            username="owner_public_start_delivery",
            email="owner_public_start_delivery@example.com",
            password="secret123",
        )
        rider_user = User.objects.create_user(
            username="rider_public_start_delivery",
            email="rider_public_start_delivery@example.com",
            password="secret123",
        )
        PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Public Start Delivery Rider",
            phone="9898989898",
            address="Rider route",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-PUBLIC-START",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 10,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        shop = Shop.objects.create(name="Public Start Delivery Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
        )
        order = Order.objects.create(
            order_id="ORD-PUBLIC-START-DELIVERY",
            user="public_start_delivery_customer",
            status="PICKED_UP",
            assigned_rider=rider_user.username,
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            assigned_rider=rider,
            status="PICKED_UP",
            rider_accepted_at=timezone.now(),
            rider_order_acknowledged=True,
        )
        fallback_order = Order.objects.create(
            order_id="ORD-PUBLIC-START-DELIVERY-FALLBACK",
            user="public_start_delivery_customer",
            status="PICKED_UP",
            assigned_rider=rider_user.username,
            shop=shop,
        )
        fallback_shop_order = ShopOrder.objects.create(
            order=fallback_order,
            shop=shop,
            status="PICKED_UP",
            rider_accepted_at=timezone.now(),
            rider_order_acknowledged=True,
        )
        ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="READY",
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertTrue(dashboard.context["show_start_active_deliveries"])
        self.assertContains(dashboard, 'id="rider-start-active-deliveries"')
        response = self.client.post(
            reverse("rider_start_active_deliveries"),
        )

        self.assertRedirects(
            response,
            f"{reverse('rider_dashboard')}?view=active",
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        fallback_shop_order.refresh_from_db()
        self.assertEqual(fallback_shop_order.status, "OUT_FOR_DELIVERY")

    def test_public_rider_can_finish_accepted_delivery_when_new_offers_are_disabled(self):
        rider_user = User.objects.create_user(
            username="public_rider_service_disabled_active",
            email="public_rider_service_disabled_active@example.com",
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Active Delivery Public Rider",
            phone="9898989897",
            address="Rider route",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-PUBLIC-ACTIVE-DISABLED",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 10,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        sharing_settings, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        sharing_settings.public_rider_service_enabled = False
        sharing_settings.save(update_fields=["public_rider_service_enabled"])
        owner = User.objects.create_user(
            username="owner_public_rider_service_disabled_active",
        )
        shop = Shop.objects.create(
            name="Public Rider Existing Delivery Shop",
            owner=owner,
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
        )
        order = Order.objects.create(
            order_id="ORD-PUBLIC-RIDER-SERVICE-DISABLED-ACTIVE",
            user="public_rider_existing_delivery_customer",
            assigned_rider=rider_user.username,
            status="PICKED_UP",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
            rider_order_acknowledged=True,
            status="PICKED_UP",
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertTrue(dashboard.context["show_start_active_deliveries"])
        self.assertNotContains(
            dashboard,
            "No shops are currently accepting deliveries",
        )
        self.assertContains(dashboard, "ORD-PUBLIC-RIDER-SERVICE-DISABLED-ACTIVE")
        rider.refresh_from_db()
        self.assertTrue(rider.is_active)
        response = self.client.post(reverse("rider_start_active_deliveries"))
        self.assertRedirects(
            response,
            f"{reverse('rider_dashboard')}?view=active",
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

    def test_public_rider_active_dashboard_hides_invalid_deleted_orders(self):
        rider_user = User.objects.create_user(
            username="public_rider_invalid_active_order",
            email="public_rider_invalid_active_order@example.com",
        )
        rider_application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Invalid Active Order Rider",
            phone="9898989896",
            address="Rider route",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-PUBLIC-INVALID-ACTIVE",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 10,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        rider_application.public_rider_batch_accepting = True
        rider_application.public_rider_batch_started_at = (
            timezone.now() - timedelta(minutes=6)
        )
        rider_application.save(update_fields=[
            "public_rider_batch_accepting",
            "public_rider_batch_started_at",
            "updated_at",
        ])
        owner = User.objects.create_user(
            username="owner_public_rider_invalid_active_order",
        )
        shop = Shop.objects.create(
            name="Invalid Active Order Shop",
            owner=owner,
            location={"lat": 21.17, "lng": 72.83},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
        )
        order = Order.objects.create(
            order_id="ORD-PUBLIC-RIDER-DELETED-ACTIVE",
            user="public_rider_invalid_order_customer",
            assigned_rider=rider_user.username,
            status="OUT_FOR_DELIVERY",
            is_valid_order=False,
            shop=shop,
        )
        ShopOrder.objects.create(
            order=order,
            shop=shop,
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
            status="OUT_FOR_DELIVERY",
        )
        available_order = Order.objects.create(
            order_id="ORD-PUBLIC-RIDER-AVAILABLE-AFTER-INVALID-ACTIVE",
            user="public_rider_available_order_customer",
            status="READY",
            placed_location={"lat": 21.18, "lng": 72.84},
            delivery_location="21.18,72.84",
        )
        available_shop_order = ShopOrder.objects.create(
            order=available_order,
            shop=shop,
            status="READY",
            public_rider_network_requested=True,
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["orders"].count(), 0)
        self.assertNotContains(response, "ORD-PUBLIC-RIDER-DELETED-ACTIVE")
        available_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertEqual(available_response.status_code, 200)
        self.assertEqual(available_response.context["available_order_count"], 1)
        self.assertEqual(
            list(
                available_response.context["orders"].values_list(
                    "pk",
                    flat=True,
                ),
            ),
            [available_shop_order.pk],
        )
        rider_application.refresh_from_db()
        self.assertFalse(rider_application.public_rider_batch_accepting)
        self.assertIsNone(rider_application.public_rider_batch_started_at)

    def test_rider_can_start_delivery_only_after_all_assigned_parcels_are_collected(self):
        owner = User.objects.create_user(
            username="owner_order_pickup_scope",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="rider_order_pickup_scope",
            password="test-password",
        )
        shop = Shop.objects.create(name="Order Pickup Scope Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        customer_order = Order.objects.create(
            order_id="ORD-PICKUP-SCOPE",
            user="pickup_scope_customer",
            status="RIDER_ASSIGNED",
            shop=shop,
        )
        same_order_parcels = [
            ShopOrder.objects.create(
                order=customer_order,
                shop=shop,
                assigned_rider=rider,
                status="RIDER_ASSIGNED",
                total_amount="20.00",
            )
            for _ in range(2)
        ]
        unrelated_order = Order.objects.create(
            order_id="ORD-PICKUP-UNRELATED",
            user="pickup_scope_customer",
            status="RIDER_ASSIGNED",
            shop=shop,
        )
        unrelated_parcel = ShopOrder.objects.create(
            order=unrelated_order,
            shop=shop,
            assigned_rider=rider,
            status="RIDER_ASSIGNED",
            total_amount="15.00",
        )
        self.client.force_login(owner)

        first_pickup = self.client.post(
            reverse(
                "rider_pickup_order",
                kwargs={"shop_order_id": same_order_parcels[0].pk},
            ),
            {
                "pickup_otp": seller_views._ensure_rider_pickup_otp(
                    same_order_parcels[0],
                ),
            },
        )
        self.assertEqual(first_pickup.status_code, 302)
        same_order_parcels[0].refresh_from_db()
        same_order_parcels[1].refresh_from_db()
        customer_order.refresh_from_db()
        self.assertEqual(same_order_parcels[0].status, "PICKED_UP")
        self.assertEqual(same_order_parcels[1].status, "RIDER_ASSIGNED")
        self.assertEqual(customer_order.status, "PICKED_UP")
        self.client.force_login(rider_user)
        partial_pickup_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertTrue(
            partial_pickup_dashboard.context["show_start_active_deliveries"],
        )
        self.assertContains(
            partial_pickup_dashboard,
            '<button type="submit" class="btn btn-success">',
        )
        partial_pickup_start = self.client.post(
            reverse("rider_start_active_deliveries"),
        )
        self.assertRedirects(
            partial_pickup_start,
            f"{reverse('rider_dashboard')}?view=active",
        )
        same_order_parcels[0].refresh_from_db()
        same_order_parcels[1].refresh_from_db()
        customer_order.refresh_from_db()
        self.assertEqual(same_order_parcels[0].status, "OUT_FOR_DELIVERY")
        self.assertEqual(same_order_parcels[1].status, "RIDER_ASSIGNED")
        self.assertEqual(customer_order.status, "PICKED_UP")

        self.client.force_login(owner)
        final_pickup = self.client.post(
            reverse(
                "rider_pickup_order",
                kwargs={"shop_order_id": same_order_parcels[1].pk},
            ),
            {
                "pickup_otp": seller_views._ensure_rider_pickup_otp(
                    same_order_parcels[1],
                ),
            },
        )
        self.assertEqual(final_pickup.status_code, 302)
        same_order_parcels[0].refresh_from_db()
        same_order_parcels[1].refresh_from_db()
        self.assertEqual(same_order_parcels[0].status, "OUT_FOR_DELIVERY")
        self.assertEqual(same_order_parcels[1].status, "PICKED_UP")
        customer_order.refresh_from_db()
        unrelated_parcel.refresh_from_db()
        unrelated_order.refresh_from_db()
        self.assertEqual(customer_order.status, "PICKED_UP")
        self.assertEqual(unrelated_parcel.status, "RIDER_ASSIGNED")
        self.client.force_login(rider_user)
        partial_start = self.client.post(
            reverse("rider_start_active_deliveries"),
        )
        self.assertRedirects(
            partial_start,
            f"{reverse('rider_dashboard')}?view=active",
            fetch_redirect_response=False,
        )
        blocked_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(
            blocked_dashboard,
            'id="rider-start-active-deliveries"',
        )
        self.assertContains(
            blocked_dashboard,
            'disabled aria-describedby="rider-start-delivery-help"',
        )
        for parcel in same_order_parcels:
            parcel.refresh_from_db()
            self.assertEqual(parcel.status, "OUT_FOR_DELIVERY")
        unrelated_parcel.refresh_from_db()
        self.assertEqual(unrelated_parcel.status, "RIDER_ASSIGNED")
        customer_order.refresh_from_db()
        self.assertEqual(customer_order.status, "OUT_FOR_DELIVERY")

        self.client.force_login(owner)
        last_rider_pickup = self.client.post(
            reverse(
                "rider_pickup_order",
                kwargs={"shop_order_id": unrelated_parcel.pk},
            ),
            {
                "pickup_otp": seller_views._ensure_rider_pickup_otp(
                    unrelated_parcel,
                ),
            },
        )
        self.assertEqual(last_rider_pickup.status_code, 302)
        self.client.force_login(rider_user)
        ready_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(ready_dashboard, "Ready to start delivery")
        self.assertContains(ready_dashboard, "Start delivery")
        self.assertContains(
            ready_dashboard,
            '<button type="submit" class="btn btn-success">',
        )
        available_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertTrue(
            available_dashboard.context["show_start_active_deliveries"],
        )
        self.assertContains(
            available_dashboard,
            'id="rider-start-active-deliveries"',
        )
        self.assertContains(available_dashboard, "Start delivery")
        start_delivery = self.client.post(
            reverse("rider_start_active_deliveries"),
        )
        self.assertRedirects(
            start_delivery,
            f"{reverse('rider_dashboard')}?view=active",
            fetch_redirect_response=False,
        )
        for parcel in [*same_order_parcels, unrelated_parcel]:
            parcel.refresh_from_db()
            self.assertEqual(parcel.status, "OUT_FOR_DELIVERY")
            self.assertTrue(
                parcel.status_history.filter(
                    status="OUT_FOR_DELIVERY",
                    remark__contains="Rider",
                ).exists()
            )
        customer_order.refresh_from_db()
        unrelated_order.refresh_from_db()
        self.assertEqual(customer_order.status, "OUT_FOR_DELIVERY")
        self.assertEqual(unrelated_order.status, "OUT_FOR_DELIVERY")

    def test_rider_can_accept_multiple_orders_and_route_only_includes_their_valid_stops(self):
        owner = User.objects.create_user(username="owner_multi_route", password="route-test-password")
        rider_user = User.objects.create_user(username="rider_multi_route", password="route-test-password")
        shop = Shop.objects.create(
            name="Multi Route Shop",
            owner=owner,
            location={"lat": 22.0, "lng": 73.0},
        )
        rider_staff = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        customer_user = User.objects.create_user(
            username="multi_route_customer",
            password="test-password",
        )
        ClientProfile.objects.create(
            user=customer_user,
            name="Route Customer Name",
        )
        shop_orders = []
        for order_id, location in (
            ("ORD-MULTI-1", "22.1,73.1"),
            ("ORD-MULTI-2", "22.2,73.2"),
            ("ORD-MULTI-INVALID", "not-a-coordinate"),
            ("ORD-MULTI-UNASSIGNED", "22.4,73.4"),
        ):
            order = Order.objects.create(
                order_id=order_id,
                user="multi_route_customer",
                delivery_address=f"Address for {order_id}",
                delivery_location=location,
                delivery_contact_no="9898989897",
                status="READY",
                shop=shop,
            )
            shop_orders.append(ShopOrder.objects.create(
                order=order,
                shop=shop,
                total_amount="100.00",
                status="READY",
            ))

        self.client.force_login(rider_user)
        for shop_order in shop_orders[:3]:
            response = self.client.post(
                reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.id}),
            )
            self.assertEqual(response.status_code, 302)
            shop_order.refresh_from_db()
            self.assertEqual(shop_order.assigned_rider_id, rider_staff.id)
            self.assertEqual(shop_order.status, "RIDER_ASSIGNED")

        available_response = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(available_response.status_code, 200)
        self.assertEqual(available_response.context["available_order_count"], 1)
        self.assertContains(available_response, "ORD-MULTI-UNASSIGNED")
        self.assertNotContains(available_response, "ORD-MULTI-1")

        response = self.client.get(reverse("rider_dashboard"), {"view": "active"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["navigation_stops"], [])
        self.assertEqual(response.context["pickup_pending_count"], 3)
        self.assertEqual(len(response.context["pickup_navigation_stops"]), 1)
        pickup_order_numbers = [
            shop_order.shop_order_number or shop_order.order.order_id
            for shop_order in shop_orders[:3]
        ]
        self.assertEqual(
            response.context["pickup_navigation_stops"][0]["order_numbers"],
            pickup_order_numbers,
        )
        for order_number in pickup_order_numbers:
            self.assertContains(response, order_number)
        self.assertEqual(
            [len(group) for group in response.context["combined_route_groups"]],
            [1, 0],
        )
        self.assertContains(response, "stop.order_numbers.join")
        self.assertContains(response, "const shortLabel =")
        self.assertContains(response, "const stackedLabels = locationStops")
        self.assertContains(response, "marker.bindPopup(")
        self.assertContains(response, "const stopsByLocation = new Map()")
        self.assertContains(response, "const locationKey = `${Number(stop.lat).toFixed(6)},${Number(stop.lng).toFixed(6)}`")
        self.assertContains(response, ".map((stop) => `<div>${escapeHtml(shortLabel(stop))}</div>`)")
        self.assertContains(response, "marker.bindTooltip(stackedLabels")
        self.assertContains(response, 'aria-label="Navigate all active orders"')
        self.assertContains(response, "Full route")
        self.assertContains(response, 'aria-label="Navigate shop-to-shop for pending pickups"')
        self.assertContains(response, "Pickups")
        self.assertContains(response, "Start delivery")
        self.assertNotContains(response, "disabled aria-describedby=")
        empty_start = self.client.post(
            reverse("rider_start_active_deliveries"),
        )
        self.assertRedirects(
            empty_start,
            f"{reverse('rider_dashboard')}?view=active",
            fetch_redirect_response=False,
        )
        for shop_order in shop_orders[:3]:
            shop_order.refresh_from_db()
            self.assertEqual(shop_order.status, "RIDER_ASSIGNED")
        premature_completion = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_orders[0].pk}),
            {},
        )
        self.assertEqual(premature_completion.status_code, 302)
        shop_orders[0].refresh_from_db()
        self.assertEqual(shop_orders[0].status, "RIDER_ASSIGNED")

        for shop_order in shop_orders[:2]:
            self.client.force_login(owner)
            pickup_response = self.client.post(
                reverse("rider_pickup_order", kwargs={"shop_order_id": shop_order.pk}),
                {
                    "pickup_otp": seller_views._ensure_rider_pickup_otp(shop_order),
                },
            )
            self.assertEqual(pickup_response.status_code, 302)
            shop_order.refresh_from_db()
            self.assertEqual(shop_order.status, "PICKED_UP")
        self.client.force_login(rider_user)
        waiting = self.client.get(reverse("rider_dashboard"), {"view": "active"})
        self.assertEqual(waiting.context["pickup_pending_count"], 1)
        self.assertEqual(waiting.context["navigation_stops"], [])
        self.assertContains(waiting, 'id="pickupRouteNavigation"')
        self.assertContains(waiting, "Parcels awaiting pickup stay unchanged.")
        self.client.post(reverse("rider_start_active_deliveries"))
        for shop_order in shop_orders[:2]:
            shop_order.refresh_from_db()
            self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

        self.client.force_login(owner)
        final_pickup_response = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": shop_orders[2].pk}),
            {
                "pickup_otp": seller_views._ensure_rider_pickup_otp(shop_orders[2]),
            },
        )
        self.assertEqual(final_pickup_response.status_code, 302)
        self.client.force_login(rider_user)
        ready_to_start = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(ready_to_start, "Ready to start delivery")
        self.client.post(reverse("rider_start_active_deliveries"))
        for shop_order in shop_orders[:3]:
            shop_order.refresh_from_db()
            self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

        response = self.client.get(reverse("rider_dashboard"), {"view": "active"})
        navigation_stops = response.context["navigation_stops"]
        self.assertEqual(
            [stop["order_id"] for stop in navigation_stops],
            ["ORD-MULTI-1", "ORD-MULTI-2"],
        )
        self.assertEqual(
            [len(group) for group in response.context["combined_route_groups"]],
            [0, 2],
        )
        self.assertEqual(
            [stop["customer_name"] for stop in navigation_stops],
            ["Route Customer Name", "Route Customer Name"],
        )
        self.assertContains(response, 'id="rider-combined-route-groups"')
        self.assertContains(response, 'id="rider-combined-map-stops"')
        self.assertContains(response, 'id="toggleRiderRouteMap"')
        self.assertContains(response, 'aria-expanded="false"')
        self.assertContains(response, 'aria-controls="riderRouteMapContent"')
        self.assertContains(response, 'id="riderRouteMapContent" hidden')
        self.assertContains(response, 'aria-label="Route and delivery actions"')
        self.assertContains(response, 'id="multiStopNavigation"')
        self.assertContains(response, 'id="completeActiveRouteNavigation"')
        self.assertContains(response, "tabsWrap.offsetHeight")
        self.assertContains(response, 'className = "rider-dashboard-tabs-spacer"')
        response_html = response.content.decode()
        tabs_wrap_start = response_html.index('class="rider-dashboard-tabs-wrap"')
        actions_start = response_html.index('class="rider-active-actions mb-3"')
        order_cards_start = response_html.index('class="row g-3 rider-order-track"')
        self.assertGreater(actions_start, tabs_wrap_start)
        self.assertLess(actions_start, order_cards_start)
        self.assertContains(response, "Stops at the same location appear together in one stacked label; select the marker for full names and order numbers.")
        self.assertContains(response, "Your current location appears as a blue marker.")
        self.assertContains(response, 'id="riderCurrentLocationStatus"')
        self.assertNotContains(response, "Pickup distance from current location:")
        self.assertContains(response, "Pickup to delivery destination:")
        self.assertContains(response, "data-distance-latitude=")
        self.assertContains(response, "data-pickup-latitude=")
        self.assertContains(response, "L.circleMarker")
        self.assertContains(response, "Tiles &copy; Esri")
        self.assertContains(response, '"Satellite": satelliteLayer')
        self.assertContains(response, '"Normal": normalLayer')
        self.assertContains(response, "L.polyline(routePoints")
        self.assertContains(response, "rider-route-direction-arrow")
        self.assertContains(response, "actual roads may differ.")
        self.assertContains(response, "Route Customer Name")
        self.assertContains(response, "rider-route-customer-label")
        self.assertContains(response, 'aria-label="Navigate all accepted deliveries"')
        self.assertContains(response, "Deliveries")
        self.assertContains(response, 'id="multiStopNavigationStatus"')
        self.assertNotContains(response, "Opening Google Maps")

    def test_shop_detail_page_shows_staff_jobs_products_and_orders(self):
        owner = User.objects.create_user(username="owner_shop_detail", password="pass1234")
        staff_user = User.objects.create_user(username="shop_staff_member", password="pass1234")
        shop = Shop.objects.create(
            name="Shop Detail Test",
            owner=owner,
            location={"lat": 23.0225, "lng": 72.5714},
            delivery_area=[[{"lat": 23.022, "lng": 72.570}, {"lat": 23.023, "lng": 72.572}, {"lat": 23.024, "lng": 72.571}]],
            delivery_pincode=["380001"],
        )
        ShopStaff.objects.create(shop=shop, user=staff_user, role="MANAGER", is_active=True, can_manage_staff=True)
        product = Product.objects.create(
            created_by=owner,
            product_name="Tomato",
            product_alias_name="Tamatar",
            price="30.00",
            unit="kg",
            product_category="SABJI",
        )
        ProductShop.objects.create(shop=shop, product=product, price="30.00", stock=10, is_active=True)
        order = Order.objects.create(
            order_id="ORD-3001",
            user="buyer_detail",
            delivery_address="Shop Detail Road",
            delivery_location="23.0225,72.5714",
            delivery_contact_no="9123456789",
            status="PLACED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop, total_amount="300.00", status="PLACED")
        opening = JobOpening.objects.create(shop=shop, title="Delivery Boy", role="Rider")
        application = JobApplication.objects.create(
            shop=shop,
            job_opening=opening,
            full_name="Ava Candidate",
            email="ava@example.com",
            job_title="Delivery Boy",
            status="PENDING",
        )

        self.client.force_login(owner)
        response = self.client.get(reverse("shop_detail", kwargs={"pk": shop.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Staff Management")
        self.assertContains(response, "Tomato")
        self.assertContains(response, "Recent Orders")
        self.assertContains(response, "Delivery Boy")
        self.assertContains(response, "Ava Candidate")
        self.assertContains(response, reverse("stock_import", kwargs={"shop_id": shop.id}))
        self.assertContains(response, reverse("shop_job_opening_create", kwargs={"shop_id": shop.id}))
        self.assertContains(response, "shopDeliveryMap")
        self.assertContains(response, "Set delivery coverage")
        self.assertContains(
            response,
            reverse("shop_edit", kwargs={"pk": shop.pk}),
        )
        self.assertContains(response, '"Normal": normalLayer')
        self.assertContains(response, "satelliteLayer.addTo(map)")
        self.assertContains(response, "Center map on my current location")

        edit_response = self.client.get(
            reverse("shop_edit", kwargs={"pk": shop.pk}),
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Use the polygon tool on the map")
        self.assertContains(edit_response, "delivery_area_json")

    def test_owner_statement_aggregates_only_the_current_owners_shop_data(self):
        owner = User.objects.create_user(username="statement_owner", password="test-pass")
        other_owner = User.objects.create_user(username="statement_other_owner", password="test-pass")
        first_shop = Shop.objects.create(name="Statement Shop One", owner=owner)
        second_shop = Shop.objects.create(name="Statement Shop Two", owner=owner)
        other_shop = Shop.objects.create(name="Private Other Shop", owner=other_owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Statement product",
            price="12.00",
            unit="each",
            product_category="OTHER",
        )
        ProductShop.objects.create(
            shop=first_shop,
            product=product,
            price="12.00",
            stock=7,
            is_active=True,
        )
        staff_user = User.objects.create_user(username="statement_staff", password="test-pass")
        ShopStaff.objects.create(
            shop=second_shop,
            user=staff_user,
            role="PACKER",
            is_active=True,
        )
        wallet = ShopWallet.objects.create(
            shop=first_shop,
            balance=Decimal("45.00"),
        )
        ShopWalletTransaction.objects.create(
            wallet=wallet,
            transaction_type="RIDER_FEE",
            amount=Decimal("5.00"),
            balance_after=Decimal("45.00"),
            description="Statement shared rider charge",
        )
        ProductShop.objects.create(
            shop=other_shop,
            product=product,
            price="12.00",
            stock=99,
            is_active=True,
        )

        self.client.force_login(owner)
        dashboard = self.client.get(reverse("dashboard"))
        response = self.client.get(reverse("owner_statement"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(dashboard, reverse("owner_statement"))
        self.assertEqual(response.context["total_products"], 1)
        self.assertEqual(response.context["total_staff"], 1)
        self.assertEqual(response.context["wallet_balance"], Decimal("45.00"))
        self.assertContains(response, "Statement Shop One")
        self.assertContains(response, "Statement Shop Two")
        self.assertContains(response, "Statement product")
        self.assertContains(response, "Statement shared rider charge")
        self.assertNotContains(response, "Private Other Shop")

    def test_job_openings_are_public_but_applying_requires_login(self):
        owner = User.objects.create_user(username="public_job_owner", password="test-pass")
        shop = Shop.objects.create(name="Public job shop", owner=owner)
        opening = JobOpening.objects.create(
            shop=shop,
            title="Public job listing",
            role="Packer",
        )

        public_response = self.client.get(reverse("job_openings"))
        apply_response = self.client.get(
            reverse("job_apply", kwargs={"job_id": opening.pk}),
        )

        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "Public job listing")
        self.assertContains(public_response, "Log in to apply")
        self.assertRedirects(
            apply_response,
            f"{reverse('login')}?next={reverse('job_apply', kwargs={'job_id': opening.pk})}",
        )
        applicant = User.objects.create_user(username="job_applicant", password="test-pass")
        self.client.force_login(applicant)
        self.assertEqual(
            self.client.get(
                reverse("job_apply", kwargs={"job_id": opening.pk}),
            ).status_code,
            200,
        )

    def test_job_map_defaults_to_satellite_and_has_location_and_map_type_controls(self):
        response = self.client.get(reverse("job_openings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="use-current-location"')
        self.assertContains(response, "Use my current location")
        self.assertContains(response, 'L.control.layers({')
        self.assertContains(response, '"Street map": streetLayer')
        self.assertContains(response, '"Satellite": satelliteLayer')
        self.assertContains(response, 'collapsed: true')
        self.assertContains(response, "satelliteLayer.addTo(map)")
        self.assertNotContains(response, 'class="jobs-map-control"')
        self.assertContains(response, "Find jobs from map")
        self.assertContains(response, 'data-bs-target="#jobs-map-panel"')
        self.assertContains(response, 'aria-expanded="false"')
        self.assertContains(response, 'class="collapse" id="jobs-map-panel"')
        self.assertContains(response, 'map.invalidateSize({ pan: false })')
        content = response.content.decode()
        self.assertContains(response, 'role="group" aria-label="Job listing filters"')
        self.assertContains(response, 'class="btn btn-primary active"')
        self.assertLess(
            content.index('aria-label="Job listing filters"'),
            content.index('id="job-map-heading"'),
        )
        self.assertLess(
            content.index('id="job-results-status"'),
            content.index('class="collapse" id="jobs-map-panel"'),
        )

    def test_job_openings_text_search_matches_all_public_listing_fields(self):
        owner = User.objects.create_user(username="job_search_owner")
        field_values = {
            "title": {"title": "Needle picker"},
            "role": {"role": "Needle packer"},
            "employment_type": {"employment_type": "Needle shifts"},
            "location": {"location": "Needle district"},
            "shop": {},
            "description": {"description": "Looking for a Needle specialist"},
        }
        expected_ids = set()
        for index, (field, values) in enumerate(field_values.items()):
            shop_name = "Needle Market" if field == "shop" else f"Search Shop {index}"
            shop = Shop.objects.create(name=shop_name, owner=owner)
            opening = JobOpening.objects.create(
                shop=shop,
                **{"title": f"Opening {index}", **values},
            )
            expected_ids.add(opening.pk)
        unrelated_shop = Shop.objects.create(name="Other Market", owner=owner)
        JobOpening.objects.create(
            shop=unrelated_shop,
            title="Ordinary opening",
            role="Cashier",
            description="No matching terms",
        )

        response = self.client.get(reverse("job_openings"), {"q": "needle"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="job-search"')
        self.assertContains(response, 'data-voice-search')
        self.assertContains(response, 'aria-label="Search by voice"')
        self.assertContains(response, 'data-voice-search-status')
        self.assertEqual(
            {opening.pk for opening in response.context["openings"]},
            expected_ids,
        )
        self.assertEqual(response.context["search_query"], "needle")

    def test_job_openings_filter_by_selected_radius_using_shop_coordinates(self):
        owner = User.objects.create_user(username="job_radius_owner")
        nearby_shop = Shop.objects.create(
            name="Nearby Job Shop",
            owner=owner,
            location={"lat": 0, "lng": 0.01},
        )
        distant_shop = Shop.objects.create(
            name="Distant Job Shop",
            owner=owner,
            location={"lat": 0, "lng": 0.02},
        )
        missing_location_shop = Shop.objects.create(
            name="Unlocated Job Shop",
            owner=owner,
            location={},
        )
        nearby_opening = JobOpening.objects.create(
            shop=nearby_shop,
            title="Nearby Packer",
        )
        distant_opening = JobOpening.objects.create(
            shop=distant_shop,
            title="Distant Packer",
        )
        JobOpening.objects.create(
            shop=missing_location_shop,
            title="Unlocated Packer",
        )

        response = self.client.get(
            reverse("job_openings"),
            {"lat": "0", "lng": "0", "radius_km": "1.25"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [opening.pk for opening in response.context["openings"]],
            [nearby_opening.pk],
        )
        self.assertNotIn(
            distant_opening.pk,
            {opening.pk for opening in response.context["openings"]},
        )
        self.assertEqual(response.context["radius_km"], 1.25)
        self.assertContains(response, 'name="lat" value="0.0"')
        self.assertContains(response, 'name="lng" value="0.0"')
        self.assertContains(response, 'name="radius_km" value="1.25"')
        self.assertContains(
            response,
            'type="number" min="0.1" max="100"',
        )
        self.assertContains(response, 'value="1.25" inputmode="decimal"')
        self.assertContains(response, 'id="decrease-nearby-radius"')
        self.assertContains(response, 'id="increase-nearby-radius"')
        self.assertContains(response, "adjustRadius(-0.1)")
        self.assertContains(response, "adjustRadius(0.1)")
        response_content = response.content.decode()
        self.assertContains(response, 'class="badge rounded-pill text-bg-primary fs-6"')
        self.assertLess(
            response_content.index('id="job-results-status"'),
            response_content.index('id="jobs-map"'),
        )

        all_jobs_response = self.client.get(
            reverse("job_openings"),
            {"lat": "0", "lng": "0", "radius_km": "1.25", "tab": "all"},
        )
        self.assertEqual(all_jobs_response.context["job_tab"], "all")
        self.assertEqual(len(all_jobs_response.context["openings"]), 3)
        self.assertContains(all_jobs_response, "All jobs")
        self.assertContains(all_jobs_response, "Nearby jobs")

        nearby_jobs_response = self.client.get(
            reverse("job_openings"),
            {"lat": "0", "lng": "0", "radius_km": "1.25", "tab": "nearby"},
        )
        self.assertEqual(nearby_jobs_response.context["job_tab"], "nearby")
        self.assertEqual(
            [opening.pk for opening in nearby_jobs_response.context["openings"]],
            [nearby_opening.pk],
        )

        custom_radius_response = self.client.get(
            reverse("job_openings"),
            {"lat": "0", "lng": "0", "radius_km": "2.5"},
        )
        self.assertEqual(custom_radius_response.context["radius_km"], 2.5)
        self.assertEqual(
            {opening.pk for opening in custom_radius_response.context["openings"]},
            {nearby_opening.pk, distant_opening.pk},
        )

        invalid_response = self.client.get(
            reverse("job_openings"),
            {"lat": "invalid", "lng": "0", "radius_km": "invalid"},
        )
        self.assertContains(
            invalid_response,
            "The selected location is invalid. Choose a point on the map and try again.",
        )
        self.assertContains(
            invalid_response,
            "The selected radius is invalid. Using the default 1.25 km radius.",
        )
        self.assertEqual(len(invalid_response.context["openings"]), 3)

    def test_shop_owner_can_open_stock_import_page(self):
        owner = User.objects.create_user(username="stock_import_owner", password="test-password")
        shop = Shop.objects.create(name="Stock Import Shop", owner=owner)

        self.client.force_login(owner)
        response = self.client.get(reverse("stock_import", kwargs={"shop_id": shop.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload and update stock")
        self.assertContains(response, "Download sample CSV")
        self.assertContains(response, f"{reverse('stock_import', kwargs={'shop_id': shop.id})}?format=sample")

        sample_response = self.client.get(
            reverse("stock_import", kwargs={"shop_id": shop.id}),
            {"format": "sample"},
        )
        self.assertEqual(sample_response.status_code, 200)
        self.assertIn("stock-import-sample.csv", sample_response["Content-Disposition"])
        self.assertEqual(
            sample_response.content.decode().splitlines(),
            [
                "product_shop_id,sku,product_name,stock",
                "17,SKU-001,Potato,50",
            ],
        )

        product = Product.objects.create(
            created_by=owner,
            product_name="Current Potato",
            sku="POT-01",
            price="12.00",
            product_category="SABJI",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            stock=8,
        )
        response = self.client.get(
            reverse("stock_import", kwargs={"shop_id": shop.id}),
            {"format": "update"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("shop-{}-current-stock.csv".format(shop.id), response["Content-Disposition"])
        self.assertEqual(
            response.content.decode().splitlines(),
            [
                "product_shop_id,sku,product_name,stock",
                f"{assignment.id},POT-01,Current Potato,8",
            ],
        )
        update_response = self.client.post(
            reverse("stock_import", kwargs={"shop_id": shop.id}),
            {
                "file": SimpleUploadedFile(
                    "updated-stock.csv",
                    (
                        "product_shop_id,sku,product_name,stock\n"
                        f"{assignment.id},POT-01,Current Potato,23\n"
                    ).encode(),
                    content_type="text/csv",
                ),
            },
        )
        self.assertEqual(update_response.status_code, 200)
        assignment.refresh_from_db()
        self.assertEqual(assignment.stock, 23)

    def test_owner_can_schedule_interview_and_update_status(self):
        owner = User.objects.create_user(username="owner3", password="pass1234")
        shop = Shop.objects.create(name="Owner Shop", owner=owner)
        opening = JobOpening.objects.create(shop=shop, title="Cashier")
        application = JobApplication.objects.create(
            shop=shop,
            job_opening=opening,
            full_name="Ava",
            email="ava@example.com",
            status="PENDING",
        )

        self.client.force_login(owner)

        response = self.client.post(
            reverse("shop_job_application_status", kwargs={"shop_id": shop.id, "application_id": application.id}),
            {"status": "INTERVIEW", "interview_time": "2026-09-20T10:30"},
        )

        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        self.assertEqual(application.status, "INTERVIEW")
        self.assertIsNotNone(application.interview_datetime)

        response = self.client.post(
            reverse("shop_job_application_status", kwargs={"shop_id": shop.id, "application_id": application.id}),
            {"status": "APPROVED"},
        )

        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        self.assertEqual(application.status, "APPROVED")

    def test_user_can_view_their_job_applications(self):
        user = User.objects.create_user(username="candidate", password="pass1234")
        shop = Shop.objects.create(name="Candidate Shop", owner=User.objects.create_user(username="shopowner", password="pass1234"))
        opening = JobOpening.objects.create(shop=shop, title="Delivery Boy")
        JobApplication.objects.create(
            user=user,
            shop=shop,
            job_opening=opening,
            full_name="Candidate User",
            email="candidate@example.com",
            status="PENDING",
        )

        self.client.force_login(user)
        response = self.client.get(reverse("my_job_applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delivery Boy")

    def test_marketplace_displays_market_and_product_categories(self):
        user = User.objects.create_user(username="buyer", password="pass1234")
        ClientProfile.objects.create(user=user, name="Buyer User")
        shop = Shop.objects.create(
            name="Fresh Basket",
            owner=user,
            market_category="DAILY MARKET",
            shop_category="SABJI",
        )
        product = Product.objects.create(
            created_by=user,
            product_name="Potato",
            product_alias_name="Aalu",
            price="20.00",
            unit="kg",
            product_category="SABJI",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="20.00",
            bulk_price="15.00",
            bulk_unit="box",
            stock=5,
            is_active=True,
        )
        catalog_product, _ = AdminProductCatalog.objects.update_or_create(
            product_category="SABJI",
            product_name="Potato",
            defaults={
                "image_url": "https://example.com/potato-primary.jpg",
                "image_url_2": "https://example.com/potato-2.jpg",
                "image_url_3": "https://example.com/potato-3.jpg",
                "image_url_4": "https://example.com/potato-4.jpg",
                "image_url_5": "https://example.com/potato-5.jpg",
            },
        )
        shop.allow_product_image_overrides = True
        shop.save(update_fields=["allow_product_image_overrides"])
        assignment.image_url_override = "https://example.com/shop-potato.jpg"
        assignment.save(update_fields=["image_url_override"])
        self.assertEqual(
            assignment.display_image_urls,
            [
                "https://example.com/shop-potato.jpg",
                *catalog_product.image_urls,
            ],
        )
        ClientDeliveryAddress.objects.create(
            client=user.client_profile,
            label="Home",
            address="Main Street",
            location={"lat": 23.0225, "lng": 72.5714},
            is_active=True,
            is_default=True,
        )

        self.client.force_login(user)
        response = self.client.get(reverse("marketplace"), {"current_lat": 23.0225, "current_lng": 72.5714})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["marketplace_products_per_row"], 4)
        self.assertEqual(response.context["marketplace_rows_per_category"], 2)
        self.assertContains(response, "DAILY MARKET")
        self.assertContains(response, "SABJI")
        self.assertContains(response, "Aalu")
        self.assertContains(
            response,
            'class="card market-category-card border-0 shadow-sm" data-theme-component="marketplace_category"',
        )
        self.assertContains(
            response,
            'data-image-candidates="https://example.com/shop-potato.jpg|'
            'https://example.com/potato-primary.jpg|https://example.com/potato-2.jpg|'
            'https://example.com/potato-3.jpg|https://example.com/potato-4.jpg|'
            'https://example.com/potato-5.jpg"',
        )
        self.assertContains(response, "sellerHelperTryNextProductImage(this)")
        self.assertContains(response, 'class="form-control category-product-search-input"')
        self.assertContains(response, 'id="modalProductImageThumbnails"')
        self.assertContains(response, 'class="modal-product-thumbnails"')
        self.assertContains(response, "Show product image ${index + 1}")
        self.assertContains(response, 'thumbnail.addEventListener("click"')
        self.assertContains(response, "Search SABJI products within this category")
        self.assertContains(response, "item.dataset.searchText.toLocaleLowerCase().includes(query)")
        self.assertContains(response, "}, 10000)")
        self.assertContains(response, "const resetCategorySearchTimer = (input) =>")
        self.assertContains(response, '["pointerdown", "pointermove", "touchstart", "touchmove", "wheel", "keydown"]')
        self.assertContains(response, "resetActiveCategorySearchTimers")
        self.assertContains(response, "input.closest(\".market-category-card\")")
        self.assertContains(response, "No matching products")
        self.assertContains(response, 'input.addEventListener("focus"')
        self.assertContains(response, "categorySearchScrollPositions.set(input, savedScrollY)")
        self.assertContains(response, "const marketTabsHeight = marketTabsWrap")
        self.assertContains(response, "inputTop - navbarBottom - marketTabsHeight - 8")
        self.assertContains(response, "window.scrollTo({ top: savedScrollY, behavior: \"auto\" })")
        self.assertContains(
            response,
            '.marketplace-shell [data-marketplace-section][hidden] { display: none !important; }',
        )
        self.assertContains(response, 'productModalElement.addEventListener("show.bs.modal"')
        self.assertContains(response, 'productModalElement.addEventListener("hidden.bs.modal"')
        self.assertContains(response, "market-product-modal-open .market-category-nav-wrap")
        self.assertContains(response, 'document.body.classList.add("market-product-modal-open")')
        self.assertContains(response, 'document.body.classList.remove("market-product-modal-open")')
        self.assertContains(response, 'marketplaceSections.forEach((section) => section.setAttribute("hidden", ""))')
        self.assertContains(response, "section.removeAttribute(\"hidden\")")
        self.assertContains(response, f'id="market-product-data-{response.context["products"][0].id}"')
        self.assertContains(response, '"bulk_available": true')
        page_content = response.content.decode()
        modal_start = page_content.index('id="productModal"')
        self.assertEqual(page_content.count('id="modalBulkOffer"'), 1)
        self.assertGreater(page_content.index('id="modalBulkOffer"'), modal_start)
        self.assertNotContains(response, "`/market/product/")

        MarketplaceLayoutSettings.objects.update_or_create(
            pk=1,
            defaults={"products_per_row": 5, "rows_per_category": 3},
        )
        configured_response = self.client.get(
            reverse("marketplace"),
            {"current_lat": 23.0225, "current_lng": 72.5714},
        )
        self.assertEqual(configured_response.context["marketplace_products_per_row"], 5)
        self.assertEqual(configured_response.context["marketplace_rows_per_category"], 3)
        self.assertContains(configured_response, 'const productsPerRow = Number("5")')
        self.assertContains(configured_response, 'const configuredRows = Number("3")')
        self.assertContains(
            configured_response,
            "const blockSize = productsPerRow * configuredRows;",
        )
        self.assertContains(
            configured_response,
            "const rowIndex = blockIndex === 0",
        )
        self.assertContains(
            configured_response,
            "? Math.floor(indexInBlock / productsPerRow)",
        )
        self.assertContains(
            configured_response,
            ": indexInBlock % configuredRows;",
        )
        self.assertContains(
            configured_response,
            "category-product-row > .category-product-item",
        )
        self.assertContains(
            configured_response,
            "grid-template-columns: repeat(4, minmax(0, 1fr));",
        )
        self.assertContains(
            configured_response,
            "-webkit-overflow-scrolling: touch;",
        )
        self.assertContains(
            configured_response,
            "container.classList.add(\"product-auto-scroll-ready\")",
        )
        self.assertContains(
            configured_response,
            "container.scrollLeft + state.direction * elapsed * 0.08",
        )
        self.assertContains(
            configured_response,
            ".product-auto-scroll-ready { scroll-behavior: auto !important; scroll-snap-type: none !important; }",
        )

    def test_stock_report_calculates_demand_and_removes_expired_stock(self):
        owner = User.objects.create_user(username="forecast_owner", password="forecast-pass")
        shop = Shop.objects.create(name="Forecast Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Forecast Potato",
            price="20.00",
            unit="kg",
            product_category="SABJI",
            expiry_management_enabled=True,
            shelf_life_days=5,
        )
        expiring_assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="20.00",
            stock=6,
            is_active=True,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        fresh_assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="20.00",
            stock=4,
            is_active=True,
        )
        for _ in range(30):
            StockMovement.objects.create(
                product_shop=fresh_assignment,
                movement_type="SALE",
                quantity=-2,
                reference="historical sale",
                created_by=owner,
            )

        self.client.force_login(owner)
        response = self.client.get(reverse("stock_report", kwargs={"shop_id": shop.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Peak / festival avg.")
        self.assertContains(response, "Short by")
        forecast = ShopProductForecast.objects.get(product_shop=fresh_assignment)
        self.assertGreater(forecast.daily_average, 0)
        self.assertIn("Mon", forecast.weekday_average)
        self.assertGreater(forecast.festival_average, 0)
        expiring_assignment.refresh_from_db()
        self.assertEqual(expiring_assignment.stock, 0)
        self.assertTrue(
            StockMovement.objects.filter(
                product_shop=expiring_assignment,
                movement_type="EXPIRY",
                quantity=-6,
            ).exists()
        )

        excel_response = self.client.get(
            reverse("stock_report", kwargs={"shop_id": shop.id}),
            {"format": "csv"},
        )
        self.assertIn(b"Mon avg.", excel_response.content)

    def test_shelf_life_markdown_applies_progressively_then_expires(self):
        owner = User.objects.create_user(username="expiry_price_owner", password="expiry-pass")
        shop = Shop.objects.create(name="Expiry Price Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Expiry Tomato",
            price="100.00",
            unit="kg",
            product_category="SABJI",
            expiry_management_enabled=True,
            shelf_life_days=10,
            expiry_price_drop_percent="30.00",
        )
        now = timezone.now()
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="100.00",
            stock=5,
            stocked_at=now - timedelta(days=9),
            expires_at=now + timedelta(days=1),
        )
        self.assertGreater(assignment.effective_price, Decimal("70.00"))
        self.assertLess(assignment.effective_price, Decimal("100.00"))

        assignment.expires_at = now - timedelta(seconds=1)
        self.assertEqual(assignment.suggested_expiry_price, Decimal("0.00"))

    def test_approximate_weight_checkout_finalization_updates_bill_and_notifies_customer(self):
        buyer = User.objects.create_user(username="weight_buyer", password="weight-pass")
        profile = ClientProfile.objects.create(user=buyer, name="Weight Buyer", contact_no="9000000000")
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Weight Road",
            location={"lat": 23.02, "lng": 72.57},
            is_default=True,
            is_active=True,
        )
        owner = User.objects.create_user(username="weight_owner", password="weight-pass")
        shop = Shop.objects.create(name="Weight Shop", owner=owner, location={"lat": 23.02, "lng": 72.57})
        product = Product.objects.create(
            created_by=owner,
            product_name="Loose Rice",
            price="50.00",
            unit="kg",
            product_category="KIRANA",
            is_approximate_weight=True,
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="50.00",
            stock=8,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 2}
        session.save()
        self.client.force_login(buyer)
        checkout_response = self.client.post(reverse("checkout"), {"address": address.id})
        self.assertEqual(checkout_response.status_code, 302)

        order = Order.objects.get(user=buyer.username)
        shop_order = ShopOrder.objects.get(order=order, shop=shop)
        order_item = shop_order.order_items.get()
        self.assertTrue(order_item.is_approximate_weight)
        self.assertEqual(order_item.estimated_weight, Decimal("2.000"))

        self.client.force_login(owner)
        response = self.client.post(
            reverse("finalize_approximate_weights", kwargs={"shop_order_id": shop_order.id}),
            {f"final_weight_{order_item.id}": "1.500"},
        )
        self.assertRedirects(
            response,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
            fetch_redirect_response=False,
        )
        order_item.refresh_from_db()
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order_item.final_weight, Decimal("1.500"))
        self.assertEqual(order_item.total, Decimal("75.00"))
        self.assertEqual(shop_order.total_amount, Decimal("75.00"))
        self.assertEqual(order.final_billing_amount, Decimal("85.08"))
        self.assertEqual(order.platform_charge_amount, Decimal("10.08"))
        self.assertEqual(order.payment_amount, Decimal("85.08"))
        self.assertTrue(Notification.objects.filter(user=buyer, title="Final order weight and bill updated").exists())
        invalid_weight_response = self.client.post(
            reverse("finalize_approximate_weights", kwargs={"shop_order_id": shop_order.id}),
            {f"final_weight_{order_item.id}": "not-a-weight"},
        )
        self.assertRedirects(
            invalid_weight_response,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
            fetch_redirect_response=False,
        )
        order_item.refresh_from_db()
        self.assertEqual(order_item.final_weight, Decimal("1.500"))

    def test_approximate_weight_numeric_unit_uses_quantity_estimate_and_prorated_bill(self):
        buyer = User.objects.create_user(username="numeric_weight_buyer")
        profile = ClientProfile.objects.create(user=buyer, name="Numeric Weight Buyer")
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Numeric Weight Road",
            location={"lat": 23.02, "lng": 72.57},
            is_default=True,
            is_active=True,
        )
        owner = User.objects.create_user(username="numeric_weight_owner")
        shop = Shop.objects.create(name="Numeric Weight Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Loose Produce",
            price="50.00",
            unit="500gm",
            product_category="KIRANA",
            is_approximate_weight=True,
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="50.00",
            stock=8,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 2}
        session.save()
        self.client.force_login(buyer)
        checkout_response = self.client.post(reverse("checkout"), {"address": address.id})
        self.assertEqual(checkout_response.status_code, 302)

        order = Order.objects.get(user=buyer.username)
        shop_order = ShopOrder.objects.get(order=order, shop=shop)
        order_item = shop_order.order_items.get()
        self.assertEqual(order_item.estimated_weight, Decimal("1000.000"))
        self.assertEqual(shop_order.total_weight_kg, Decimal("1.00"))

        self.client.force_login(owner)
        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "2 × 500gm = 1000gm")
        self.assertContains(detail_response, "final packed weight (gm)")

        finalize_response = self.client.post(
            reverse("finalize_approximate_weights", kwargs={"shop_order_id": shop_order.id}),
            {f"final_weight_{order_item.id}": "950"},
        )
        self.assertRedirects(
            finalize_response,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
            fetch_redirect_response=False,
        )
        order_item.refresh_from_db()
        shop_order.refresh_from_db()
        self.assertEqual(order_item.final_weight, Decimal("950.000"))
        self.assertEqual(order_item.total, Decimal("95.00"))
        self.assertEqual(shop_order.total_weight_kg, Decimal("0.95"))

    def test_rider_change_tracks_only_settled_amount_for_later_billing(self):
        from .views import _settle_order_wallet

        buyer = User.objects.create_user(username="change_buyer", password="test-password")
        profile = ClientProfile.objects.create(user=buyer, name="Change Buyer")
        order = Order.objects.create(
            order_id="ORD-CHANGE-TEST",
            user=buyer.username,
            final_billing_amount=Decimal("50.00"),
            payment_amount=Decimal("50.00"),
        )

        _settle_order_wallet(order, Decimal("100.00"))

        order.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(order.finally_received_amount, Decimal("50.00"))
        self.assertEqual(profile.wallet_balance, Decimal("50.00"))

    def test_marketplace_market_filter_limits_products_to_selected_market(self):
        user = User.objects.create_user(username="market_filter_buyer", password="market-filter-pass")
        ClientProfile.objects.create(user=user, name="Market Filter Buyer")
        owner = User.objects.create_user(username="market_filter_owner", password="market-filter-pass")
        markets = [value for value, _label in Shop.MARKET_CATEGORY_CHOICES if value]
        self.assertGreaterEqual(len(markets), 2)
        first_shop = Shop.objects.create(name="First Market Shop", owner=owner, market_category=markets[0])
        second_shop = Shop.objects.create(name="Second Market Shop", owner=owner, market_category=markets[1])
        for shop, product_name in (
            (first_shop, "First Market Product"),
            (second_shop, "Second Market Product"),
        ):
            product = Product.objects.create(
                created_by=owner,
                product_name=product_name,
                price="10.00",
                unit="piece",
                product_category="OTHER",
            )
            ProductShop.objects.create(shop=shop, product=product, price="10.00", stock=2, is_active=True)

        self.client.force_login(user)
        response = self.client.get(reverse("marketplace"), {"market": markets[0]})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First Market Product")
        self.assertNotContains(response, "Second Market Product")
        self.assertEqual(response.context["selected_market"], markets[0])

    def test_default_delivery_address_is_visible_and_changeable_from_navbar(self):
        user = User.objects.create_user(username="navbar_address_buyer", password="navbar-address-pass")
        profile = ClientProfile.objects.create(user=user, name="Navbar Address Buyer")
        default_address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="12 Home Street",
            latitude=Decimal("23.0225000"),
            longitude=Decimal("72.5714000"),
            is_default=True,
            is_active=True,
        )
        alternate_address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Office",
            address="34 Office Road",
            is_active=True,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("marketplace"))
        self.assertContains(response, "Delivering to")
        self.assertContains(response, "Home")
        self.assertContains(response, "Office")
        self.assertNotContains(response, "data-sellerhelper-app-refresh")
        self.assertContains(response, reverse("delivery_address_default", kwargs={"pk": alternate_address.id}))

        addresses_response = self.client.get(reverse("delivery_address_list"))
        self.assertContains(addresses_response, "★ Default address")
        self.assertContains(
            addresses_response,
            "This address is selected automatically at checkout.",
        )
        self.assertContains(addresses_response, "border-success border-2 bg-success-subtle")
        self.assertNotContains(addresses_response, "23.022500")
        self.assertNotContains(addresses_response, "72.571400")
        self.assertNotContains(addresses_response, "google.com/maps")

        change_response = self.client.post(
            reverse("delivery_address_default", kwargs={"pk": alternate_address.id})
        )
        self.assertRedirects(change_response, reverse("delivery_address_list"))
        default_address.refresh_from_db()
        alternate_address.refresh_from_db()
        self.assertFalse(default_address.is_default)
        self.assertTrue(alternate_address.is_default)

    def test_delivery_address_form_autofills_state_from_location_and_allows_override(self):
        user = User.objects.create_user(username="pin_state_customer", password="test-password")
        ClientProfile.objects.create(user=user, name="Pin State Customer")
        self.client.force_login(user)

        response = self.client.get(reverse("delivery_address_add"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_state_code"')
        self.assertContains(response, "Detected from the pin location unless you choose a state here.")
        self.assertContains(response, "stateField.value = detectedStateCode")
        self.assertContains(response, "!stateWasExplicitlySelected")
        self.assertContains(response, "Using the state you selected.")
        self.assertContains(response, 'andhrapradesh: "37"')
        self.assertContains(response, '"Normal": normalLayer')
        self.assertContains(response, '"Satellite": satelliteLayer')
        self.assertContains(response, "satelliteLayer.addTo(map)")
        self.assertContains(response, "Current Location")
        self.assertContains(response, "mapAccessStatus")
        self.assertContains(response, "function initializeDeliveryAddressMap()")
        self.assertContains(response, 'document.readyState === "loading"')
        self.assertContains(response, "initializeDeliveryAddressMap();")
        self.assertContains(response, "map.invalidateSize({pan: false})")
        self.assertNotContains(response, "Coordinates")
        self.assertNotContains(response, "Latitude")
        self.assertNotContains(response, "Longitude")
        self.assertTrue(response.context["form"].fields["latitude"].widget.is_hidden)
        self.assertTrue(response.context["form"].fields["longitude"].widget.is_hidden)

    def test_user_can_view_notifications_and_mark_read(self):
        user = User.objects.create_user(username="notifyuser", password="pass1234")
        notification = Notification.objects.create(
            user=user,
            title="Order is ready",
            message="Your order is packed and ready for pickup.",
            notification_type="ORDER",
            is_read=False,
        )
        already_read = Notification.objects.create(
            user=user,
            title="Earlier update",
            message="This notification was already read.",
            notification_type="SYSTEM",
            is_read=True,
        )

        self.client.force_login(user)

        list_response = self.client.get(reverse("notifications"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Order is ready")
        self.assertNotContains(list_response, "Earlier update")
        self.assertContains(list_response, "data-notification-toast-stack")
        self.assertContains(list_response, "data-unread-notifications-count")
        self.assertContains(list_response, 'class="nav-item desktop-only me-lg-2"')
        self.assertContains(
            list_response,
            f'href="{reverse("notifications")}"',
        )
        self.assertContains(list_response, 'class="bi bi-bell fs-5"')

        all_response = self.client.get(reverse("notifications"), {"view": "all"})
        self.assertContains(all_response, "Earlier update")

        api_response = self.client.get(reverse("notifications_api"))
        self.assertEqual(api_response.status_code, 200)
        payload = json.loads(api_response.content)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertGreaterEqual(len(payload["items"]), 1)
        self.assertEqual(
            payload["items"][0]["read_url"],
            reverse(
                "mark_notification_read",
                kwargs={"notification_id": notification.id},
            ),
        )
        notifications_only_response = self.client.get(
            reverse("notifications_api"),
            {"notifications_only": "1"},
        )
        self.assertEqual(notifications_only_response.status_code, 200)
        notifications_only_payload = notifications_only_response.json()
        self.assertEqual(notifications_only_payload["unread_count"], 1)
        self.assertEqual(notifications_only_payload["available_delivery_count"], 0)

        notification.related_url = reverse("marketplace")
        notification.save(update_fields=["related_url"])
        open_response = self.client.get(
            reverse("mark_notification_read", kwargs={"notification_id": notification.id})
        )
        self.assertEqual(open_response.status_code, 302)
        self.assertEqual(open_response["Location"], reverse("marketplace"))
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

        mark_response = self.client.post(reverse("mark_notification_read", kwargs={"notification_id": notification.id}))
        self.assertEqual(mark_response.status_code, 200)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        read_all_notification = Notification.objects.create(
            user=user,
            title="Another unread update",
            message="This should be marked read in bulk.",
            notification_type="SYSTEM",
            is_read=False,
        )
        read_all_response = self.client.post(
            reverse("mark_all_notifications_read"),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(read_all_response.status_code, 200)
        self.assertEqual(json.loads(read_all_response.content)["updated_count"], 1)
        read_all_notification.refresh_from_db()
        self.assertTrue(read_all_notification.is_read)
        other_user = User.objects.create_user(
            username="notify_other_user",
            password="test-password",
        )
        other_notification = Notification.objects.create(
            user=other_user,
            title="Private unread",
            message="Must stay unread.",
            notification_type="SYSTEM",
        )
        other_notification.refresh_from_db()
        self.assertFalse(other_notification.is_read)

    def test_notification_push_token_registration_and_delivery(self):
        user = User.objects.create_user(username="push_notify_user")
        self.client.force_login(user)
        registration_url = reverse("register_notification_device_token")

        csrf_response = self.client.get(registration_url)
        self.assertEqual(csrf_response.status_code, 200)
        self.assertTrue(csrf_response.json()["csrf_token"])

        invalid_response = self.client.post(
            registration_url,
            {"token": "not-a-push-token", "platform": "android"},
        )
        self.assertEqual(invalid_response.status_code, 400)

        token = "ExpoPushToken[test-device-token]"
        registered = self.client.post(
            registration_url,
            {"token": token, "platform": "android"},
        )
        self.assertEqual(registered.status_code, 200)
        self.assertTrue(registered.json()["success"])
        self.assertTrue(
            NotificationDeviceToken.objects.filter(
                user=user,
                token=token,
                platform="android",
                is_active=True,
            ).exists()
        )

        push_response = Mock()
        push_response.raise_for_status.return_value = None
        push_response.json.return_value = {"data": [{"status": "ok"}]}
        queued_pushes = []

        def capture_background_task(task, *args, **kwargs):
            queued_pushes.append((task, args))

        with (
            patch(
                "seller.push_notifications.requests.post",
                return_value=push_response,
            ) as send_push,
            patch(
                "seller.push_notifications.submit_background_task",
                side_effect=capture_background_task,
            ),
            patch("seller.live_updates.submit_background_task"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                Notification.objects.create(
                    user=user,
                    title="New shop order",
                    message="An order needs your attention.",
                    notification_type="ORDER",
                    related_url="/shop/orders/42/",
                )

            send_push.assert_not_called()
            self.assertEqual(len(queued_pushes), 1)
            task, args = queued_pushes[0]
            task(*args)
            send_push.assert_called_once()
            push_payload = send_push.call_args.kwargs["json"][0]
            self.assertEqual(push_payload["to"], token)
            self.assertEqual(push_payload["sound"], "default")
            self.assertEqual(push_payload["channelId"], "sellerhelper-alerts-v2")
            self.assertEqual(push_payload["data"]["url"], "/shop/orders/42/")

        unregistered = self.client.post(
            registration_url,
            {"token": token, "action": "unregister"},
        )
        self.assertEqual(unregistered.status_code, 200)
        self.assertTrue(unregistered.json()["success"])
        self.assertFalse(
            NotificationDeviceToken.objects.get(token=token).is_active
        )

    def test_customer_order_cancellation_supports_immediate_ajax_response(self):
        customer = User.objects.create_user(
            username="fast_cancel_customer",
            password="test-password",
        )
        owner = User.objects.create_user(
            username="fast_cancel_owner",
            password="test-password",
        )
        shop = Shop.objects.create(name="Fast Cancel Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-FAST-CANCEL",
            user=customer.username,
            payment_type="COD",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("20.00"),
            taxable_amount=Decimal("20.00"),
            status="PLACED",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Cancel Restock Item",
            price=Decimal("10.00"),
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("10.00"),
            stock=3,
            is_active=True,
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=product,
            product_shop=assignment,
            product_name=product.product_name,
            quantity=2,
            price=Decimal("10.00"),
            total=Decimal("20.00"),
        )
        self.client.force_login(customer)
        order_list_response = self.client.get(reverse("order_list"))
        self.assertContains(order_list_response, "orders-hero")
        self.assertContains(order_list_response, "orders-summary")
        self.assertContains(order_list_response, "customer-order-card")
        self.assertContains(order_list_response, "customer-order-status")
        self.assertContains(order_list_response, "Shop progress")
        self.assertContains(order_list_response, "data-customer-order-action=\"cancel\"")
        self.assertContains(order_list_response, "data-order-count=\"active\"")
        self.assertContains(order_list_response, "data-customer-order-action=\"address\"")
        self.assertContains(order_list_response, 'name="csrfmiddlewaretoken"')

        response = self.client.post(
            reverse("customer_cancel_order", kwargs={"order_id": order.order_id}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(order.__class__.objects.get(pk=order.pk).status, "CANCELLED")
        self.assertEqual(
            ShopOrder.objects.get(pk=shop_order.pk).status,
            "CANCELLED",
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.stock, 5)

        repeated_response = self.client.post(
            reverse("customer_cancel_order", kwargs={"order_id": order.order_id}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertFalse(repeated_response.json()["success"])
        assignment.refresh_from_db()
        self.assertEqual(assignment.stock, 5)

    def test_customer_can_cancel_one_shop_order_from_tracking(self):
        customer = User.objects.create_user(
            username="single_shop_cancel_customer",
        )
        ClientProfile.objects.create(user=customer, name="Single Shop Customer")
        first_owner = User.objects.create_user(username="single_shop_cancel_owner")
        second_owner = User.objects.create_user(username="other_shop_cancel_owner")
        first_shop = Shop.objects.create(name="Cancel One Shop", owner=first_owner)
        second_shop = Shop.objects.create(name="Keep Active Shop", owner=second_owner)
        order = Order.objects.create(
            order_id="ORD-SINGLE-SHOP-CANCEL",
            tracking_code="SC1234",
            user=customer.username,
            status="PLACED",
            payment_type="COD",
            payment_amount=Decimal("40.00"),
            final_billing_amount=Decimal("40.00"),
        )
        cancelled_shop_order = ShopOrder.objects.create(
            order=order,
            shop=first_shop,
            total_amount=Decimal("20.00"),
            taxable_amount=Decimal("20.00"),
            status="PLACED",
        )
        active_shop_order = ShopOrder.objects.create(
            order=order,
            shop=second_shop,
            total_amount=Decimal("20.00"),
            taxable_amount=Decimal("20.00"),
            status="PLACED",
        )
        product = Product.objects.create(
            created_by=first_owner,
            product_name="Single Shop Restock Item",
            price=Decimal("10.00"),
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=first_shop,
            product=product,
            price=Decimal("10.00"),
            stock=3,
            is_active=True,
        )
        OrderItem.objects.create(
            shop_order=cancelled_shop_order,
            product=product,
            product_shop=assignment,
            product_name=product.product_name,
            quantity=2,
            price=Decimal("10.00"),
            total=Decimal("20.00"),
        )

        self.client.force_login(customer)
        tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.tracking_code}),
        )
        self.assertEqual(tracking_response.status_code, 200)
        self.assertContains(tracking_response, "Cancel this shop order")
        self.assertContains(
            tracking_response,
            reverse(
                "customer_cancel_shop_order",
                kwargs={
                    "order_id": order.tracking_code,
                    "shop_order_id": cancelled_shop_order.pk,
                },
            ),
        )

        response = self.client.post(
            reverse(
                "customer_cancel_shop_order",
                kwargs={
                    "order_id": order.tracking_code,
                    "shop_order_id": cancelled_shop_order.pk,
                },
            ),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["shop_order_id"], cancelled_shop_order.pk)
        self.assertEqual(response.json()["status"], "CANCELLED")
        cancelled_shop_order.refresh_from_db()
        active_shop_order.refresh_from_db()
        order.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(cancelled_shop_order.status, "CANCELLED")
        self.assertEqual(cancelled_shop_order.cancellation_source, "CUSTOMER")
        self.assertEqual(active_shop_order.status, "PLACED")
        self.assertEqual(order.status, "PLACED")
        self.assertEqual(order.final_billing_amount, Decimal("20.00"))
        self.assertEqual(order.payment_amount, Decimal("20.00"))
        self.assertEqual(assignment.stock, 5)
        self.assertTrue(
            OrderStatusHistory.objects.filter(
                shop_order=cancelled_shop_order,
                status="CANCELLED",
                remark="Cancelled by customer before shop confirmation.",
            ).exists()
        )

        repeated_response = self.client.post(
            reverse(
                "customer_cancel_shop_order",
                kwargs={
                    "order_id": order.tracking_code,
                    "shop_order_id": cancelled_shop_order.pk,
                },
            ),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertFalse(repeated_response.json()["success"])
        assignment.refresh_from_db()
        self.assertEqual(assignment.stock, 5)

        final_shop_response = self.client.post(
            reverse(
                "customer_cancel_shop_order",
                kwargs={
                    "order_id": order.tracking_code,
                    "shop_order_id": active_shop_order.pk,
                },
            ),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(final_shop_response.json()["success"])
        order.refresh_from_db()
        self.assertEqual(order.status, "CANCELLED")
        self.assertEqual(order.cancellation_source, "CUSTOMER")

    def test_customer_cannot_cancel_shop_order_after_confirmation(self):
        customer = User.objects.create_user(
            username="confirmed_shop_cancel_customer",
        )
        owner = User.objects.create_user(username="confirmed_shop_cancel_owner")
        shop = Shop.objects.create(name="Confirmed Shop Cancel", owner=owner)
        order = Order.objects.create(
            order_id="ORD-CONFIRMED-SHOP-CANCEL",
            user=customer.username,
            status="CONFIRMED",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="CONFIRMED",
            total_amount=Decimal("20.00"),
            taxable_amount=Decimal("20.00"),
        )
        self.client.force_login(customer)

        response = self.client.post(
            reverse(
                "customer_cancel_shop_order",
                kwargs={"order_id": order.order_id, "shop_order_id": shop_order.pk},
            ),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertEqual(
            response.json()["message"],
            "This shop order can only be cancelled before the shop confirms it.",
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "CONFIRMED")

    def test_checkout_works_for_selected_delivery_address(self):
        user = User.objects.create_user(username="checkoutuser", password="pass1234")
        client_profile = ClientProfile.objects.create(user=user, name="Checkout User", contact_no="9876543210")
        shop = Shop.objects.create(
            name="Fresh Cart",
            owner=user,
            location={"lat": 23.0225, "lng": 72.5714},
            delivery_area=[[{"lat": 23.022, "lng": 72.570}, {"lat": 23.023, "lng": 72.572}, {"lat": 23.024, "lng": 72.571}]],
            delivery_pincode=["380001"],
        )
        product = Product.objects.create(
            created_by=user,
            product_name="Banana",
            price="40.00",
            unit="dozen",
            product_category="SABJI",
        )
        product_shop = ProductShop.objects.create(shop=shop, product=product, price="40.00", stock=8, is_active=True)
        zero_quantity_product = Product.objects.create(
            created_by=user,
            product_name="Zero Quantity Item",
            price="15.00",
            unit="piece",
            product_category="OTHER",
        )
        zero_quantity_assignment = ProductShop.objects.create(
            shop=shop,
            product=zero_quantity_product,
            price="15.00",
            stock=5,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=client_profile,
            label="Home",
            address="Main Road, Ahmedabad",
            location={"lat": 23.023, "lng": 72.571},
            is_default=True,
            is_active=True,
        )
        alternate_address = ClientDeliveryAddress.objects.create(
            client=client_profile,
            label="Work",
            address="Office Road",
            is_active=True,
        )

        session = self.client.session
        session["cart_delivery_address_id"] = str(address.id)
        session["carts_by_delivery_address"] = {
            str(address.id): {
                str(product_shop.id): 2,
                str(zero_quantity_assignment.id): 0,
            },
            str(alternate_address.id): {
                str(product_shop.id): 1,
            },
        }
        session["cart"] = session["carts_by_delivery_address"][str(address.id)]
        session.save()

        self.client.force_login(user)
        checkout_page = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(checkout_page, 'name="csrfmiddlewaretoken"')
        self.assertContains(checkout_page, 'document.querySelectorAll("[data-cart-count]")')
        self.assertContains(checkout_page, "sellerhelper:pending-checkout:")
        self.assertContains(checkout_page, "showCheckoutPending")
        self.assertNotContains(checkout_page, "<iframe")
        self.assertContains(checkout_page, "restoreTemporaryCart")
        self.assertContains(checkout_page, "submitNativeCheckout")
        self.assertContains(checkout_page, 'credentials: "same-origin"')
        self.assertContains(checkout_page, "Placing your order")
        checkout_key = self.client.session["checkout_attempt_key"]
        self.assertContains(
            checkout_page,
            f'name="checkout_attempt_key" value="{checkout_key}"',
        )
        self.assertContains(checkout_page, "Banana")
        self.assertNotContains(checkout_page, "Zero Quantity Item")
        self.assertNotIn(
            str(zero_quantity_assignment.id),
            self.client.session["cart"],
        )
        response = self.client.post(
            reverse("checkout"),
            {
                "address": address.id,
                "payment_method": "COD",
                "checkout_attempt_key": checkout_key,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        order = Order.objects.get(user=user.username)
        self.assertEqual(response.json()["order_id"], order.order_id)
        self.assertEqual(
            response.json()["redirect_url"],
            reverse("order_list"),
        )
        order_notification = Notification.objects.get(
            user=user,
            title="Order placed successfully",
        )
        self.assertEqual(
            order_notification.related_url,
            reverse("order_tracking", kwargs={"order_id": order.tracking_code}),
        )
        self.assertEqual(Order.objects.filter(user=user.username).count(), 1)
        self.assertEqual(self.client.session["cart"], {})
        self.assertEqual(
            self.client.session["carts_by_delivery_address"],
            {str(address.id): {}},
        )
        retry_response = self.client.post(
            reverse("checkout"),
            {
                "address": address.id,
                "payment_method": "COD",
                "checkout_attempt_key": checkout_key,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(retry_response.status_code, 200)
        self.assertTrue(retry_response.json()["success"])
        self.assertEqual(Order.objects.filter(user=user.username).count(), 1)
        self.assertEqual(self.client.session["cart"], {})
        replayed_session = self.client.session
        replayed_session["cart"] = {str(product_shop.id): 2}
        replayed_session["checkout_attempt_key"] = checkout_key
        replayed_session.save()
        replayed_ajax_response = self.client.post(
            reverse("checkout"),
            {
                "address": address.id,
                "payment_method": "COD",
                "checkout_attempt_key": checkout_key,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(replayed_ajax_response.status_code, 200)
        self.assertTrue(replayed_ajax_response.json()["success"])
        self.assertEqual(
            replayed_ajax_response.json()["redirect_url"],
            reverse("order_list"),
        )
        self.assertEqual(Order.objects.filter(user=user.username).count(), 1)
        self.assertEqual(self.client.session["cart"], {})
        cart_response = self.client.get(reverse("cart"))
        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(cart_response.context["items"], [])
        self.assertEqual(cart_response.context["active_item_count"], 0)
        order_list_response = self.client.get(reverse("order_list"))
        self.assertEqual(order_list_response.status_code, 200)
        self.assertEqual(order_list_response.context["active_tab"], "active")
        self.assertIn(
            order,
            order_list_response.context["order_groups"]["active"],
        )
        self.assertContains(order_list_response, f"Order {order.tracking_code}")
        self.assertNotContains(order_list_response, "Share live location")

        replayed_session = self.client.session
        replayed_session["cart"] = {str(product_shop.id): 2}
        replayed_session["checkout_attempt_key"] = checkout_key
        replayed_session.save()
        replay_response = self.client.post(
            reverse("checkout"),
            {
                "address": address.id,
                "payment_method": "COD",
                "checkout_attempt_key": checkout_key,
            },
            HTTP_USER_AGENT=(
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
        )
        self.assertEqual(
            replay_response.url,
            reverse("order_list"),
        )
        self.assertEqual(Order.objects.filter(user=user.username).count(), 1)
        self.assertEqual(self.client.session["cart"], {})
        self.assertEqual(
            self.client.session["carts_by_delivery_address"],
            {str(address.id): {}},
        )

    def test_checkout_cash_on_delivery_keeps_due_as_unpaid(self):
        user = User.objects.create_user(username="cod_checkout_user", password="cod-test-password")
        profile = ClientProfile.objects.create(user=user, name="COD Customer", wallet_balance="10.00")
        shop = Shop.objects.create(name="COD Shop", owner=user)
        product = Product.objects.create(
            created_by=user,
            product_name="COD Item",
            price="40.00",
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="40.00",
            stock=5,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="COD Road",
            is_default=True,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()

        self.client.force_login(user)
        response = self.client.post(
            reverse("checkout"),
            {"address": address.id, "payment_method": "COD"},
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(user=user.username)
        self.assertEqual(order.payment_type, "COD")
        self.assertEqual(order.payment_status, "COD")
        self.assertFalse(order.is_payment_done)
        self.assertEqual(order.platform_charge_amount, Decimal("10.04"))
        self.assertEqual(order.razorpay_fee_amount, Decimal("0.00"))
        self.assertEqual(order.payment_amount, Decimal("40.04"))
        profile.refresh_from_db()
        self.assertEqual(profile.wallet_balance, Decimal("0.00"))

    def test_shop_with_gstin_gets_tax_invoice_when_product_gst_rate_is_zero(self):
        buyer = User.objects.create_user(
            username="zero_rate_invoice_buyer",
            password="test-password",
        )
        profile = ClientProfile.objects.create(user=buyer, name="Zero Rate Buyer")
        owner = User.objects.create_user(
            username="zero_rate_invoice_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Zero Rate GST Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
            gst_state_code="24",
            billing_address="Registered GST address",
            delivery_fee=Decimal("50.00"),
            delivery_hsn_code="996511",
            delivery_gst_rate=Decimal("18.00"),
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Zero GST Rate Item",
            price="100.00",
            unit="piece",
            product_category="OTHER",
            hsn_code="123456",
            gst_rate="0.00",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="100.00",
            stock=2,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="GST Road",
            state_code="24",
            is_default=True,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.pk): 1}
        session.save()
        self.client.force_login(buyer)

        checkout_response = self.client.post(
            reverse("checkout"),
            {"address": address.pk, "payment_method": "COD"},
        )

        self.assertEqual(checkout_response.status_code, 302)
        shop_order = ShopOrder.objects.get(order__user=buyer.username)
        self.assertEqual(shop_order.seller_gstin, shop.gstin)
        self.assertEqual(shop_order.invoice_number, f"GST-{shop_order.order.order_id}-{shop.pk}")
        self.assertEqual(shop_order.order_items.get().gst_rate, Decimal("0.00"))
        self.assertEqual(shop_order.delivery_hsn_code, "996511")
        self.assertEqual(shop_order.delivery_gst_rate, Decimal("18.00"))
        self.assertEqual(shop_order.delivery_gst_amount, Decimal("9.00"))

        self.client.force_login(owner)
        invoice_response = self.client.get(
            reverse("packing_slip", kwargs={"shop_order_id": shop_order.pk})
        )

        self.assertEqual(invoice_response.status_code, 200)
        self.assertContains(invoice_response, "Tax Invoice")
        self.assertContains(invoice_response, shop.gstin)
        self.assertContains(invoice_response, "0.00%")
        self.assertContains(invoice_response, "996511")
        self.assertContains(invoice_response, "18.00%")

    def test_gst_checkout_snapshots_tax_and_exports_invoice_data(self):
        buyer = User.objects.create_user(username="gst_checkout_buyer", password="test-password")
        profile = ClientProfile.objects.create(user=buyer, name="GST Buyer")
        owner = User.objects.create_user(username="gst_shop_owner", password="test-password")
        shop = Shop.objects.create(
            name="GST Fresh Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
            gst_state_code="24",
            billing_address="1 Market Road, Ahmedabad",
            delivery_fee=Decimal("50.00"),
            delivery_gst_rate=Decimal("18.00"),
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="GST Item",
            price="100.00",
            unit="piece",
            product_category="OTHER",
            hsn_code="123456",
            gst_rate="18.00",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="100.00",
            stock=3,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="GST Road",
            state_code="24",
            gstin="24ABCDE1234F1Z5",
            is_default=True,
            is_active=True,
        )
        alternate_address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Office",
            address="Other State Road",
            state_code="27",
            gstin="27ABCDE1234F1Z5",
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        self.client.force_login(buyer)

        checkout_response = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_response.status_code, 200)
        self.assertContains(checkout_response, "Delivery charge")
        self.assertContains(checkout_response, "₹50.00")
        self.assertContains(checkout_response, "GST on items")
        self.assertContains(checkout_response, "₹18.00")
        self.assertContains(checkout_response, "GST on delivery")
        self.assertContains(checkout_response, "₹9.00")

        response = self.client.post(
            reverse("checkout"),
            {"address": address.id, "payment_method": "COD"},
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(user=buyer.username)
        shop_order = order.shop_orders.get()
        order_item = shop_order.order_items.get()
        self.assertEqual(order.gst_amount, Decimal("27.00"))
        self.assertEqual(order.final_billing_amount, Decimal("181.00"))
        self.assertEqual(shop_order.taxable_amount, Decimal("150.00"))
        self.assertEqual(shop_order.cgst_amount, Decimal("13.50"))
        self.assertEqual(shop_order.sgst_amount, Decimal("13.50"))
        self.assertEqual(order_item.hsn_code, "123456")
        self.assertEqual(order_item.gst_rate, Decimal("18.00"))

        address_response = self.client.post(
            reverse(
                "customer_change_order_address",
                kwargs={"order_id": order.order_id},
            ),
            {"address_id": alternate_address.id},
        )
        self.assertEqual(address_response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.buyer_state_code, "27")
        self.assertEqual(shop_order.buyer_gstin, "27ABCDE1234F1Z5")
        self.assertEqual(shop_order.cgst_amount, Decimal("0.00"))
        self.assertEqual(shop_order.igst_amount, Decimal("27.00"))
        self.assertEqual(order.gst_amount, Decimal("27.00"))

        self.client.force_login(owner)
        invoice_response = self.client.get(
            reverse("packing_slip", kwargs={"shop_order_id": shop_order.id})
        )
        self.assertEqual(invoice_response.status_code, 200)
        self.assertContains(invoice_response, "Tax Invoice")
        self.assertContains(invoice_response, "24ABCDE1234F1Z5")
        self.assertContains(invoice_response, "₹177.00")
        self.assertContains(invoice_response, "Scan to open this order")
        self.assertContains(invoice_response, "data:image/png;base64,")

        report_response = self.client.get(
            reverse("shop_order_report", kwargs={"shop_id": shop.id}),
            {
                "date_from": timezone.localdate().isoformat(),
                "date_to": timezone.localdate().isoformat(),
                "format": "gstr1",
            },
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertIn("123456", report_response.content.decode())
        self.assertIn("18.00", report_response.content.decode())

    @override_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
    def test_razorpay_option_is_selectable_but_reports_missing_server_credentials(self):
        user = User.objects.create_user(username="razorpay_unconfigured", password="checkout-test-password")
        profile = ClientProfile.objects.create(user=user, name="Online Customer")
        shop = Shop.objects.create(name="Razorpay Setup Shop", owner=user)
        product = Product.objects.create(
            created_by=user,
            product_name="Setup Item",
            price="20.00",
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="20.00",
            stock=3,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Setup Road",
            latitude=Decimal("21.2350000"),
            longitude=Decimal("72.8350000"),
            is_default=True,
            is_active=True,
        )
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Office",
            address="Second saved address",
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        self.client.force_login(user)

        checkout_page = self.client.get(reverse("checkout"))

        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(
            checkout_page,
            '<aside class="col-lg-4 checkout-sidebar" aria-label="Order summary and checkout actions">',
        )
        self.assertContains(checkout_page, '<details class="card border-0 checkout-card checkout-collapsible')
        self.assertContains(checkout_page, "Your Items · 1 item")
        self.assertContains(checkout_page, "Savings &amp; wallet")
        self.assertContains(checkout_page, 'value="RAZORPAY"')
        self.assertNotContains(checkout_page, 'value="RAZORPAY" disabled')
        self.assertContains(checkout_page, "Choose another saved address")
        self.assertContains(checkout_page, "Second saved address")
        self.assertNotContains(checkout_page, "21.235000")
        self.assertNotContains(checkout_page, "72.835000")
        self.assertNotContains(checkout_page, "google.com/maps")
        self.assertRegex(
            checkout_page.content.decode(),
            rf'name="address" value="{address.id}"[^>]*checked',
        )
        self.assertContains(checkout_page, "checkout-address-label is-selected")
        self.assertContains(checkout_page, "const updateSelectedAddressStyle = () =>")
        self.assertRegex(
            checkout_page.content.decode(),
            r'id="razorpayCredentialsWarning"[^>]*\bhidden\b',
        )
        self.assertContains(
            checkout_page,
            "if (razorpayCredentialsWarning) razorpayCredentialsWarning.hidden = !online;",
        )
        self.assertRegex(
            checkout_page.content.decode(),
            r'id="razorpayFeeRow"[^>]*\bhidden\b',
        )
        self.assertContains(checkout_page, "#razorpayFeeRow[hidden]")
        self.assertContains(checkout_page, 'id="razorpayFeeNotice"')
        self.assertContains(checkout_page, "if (feeNotice) feeNotice.hidden = !online;")
        self.assertContains(checkout_page, "Online payment needs Razorpay credentials")

        response = self.client.post(
            reverse("checkout"),
            {"address": address.id, "payment_method": "RAZORPAY"},
            follow=True,
        )
        self.assertContains(response, "Online payment is not configured")
        self.assertFalse(Order.objects.filter(user=user.username).exists())

    @override_settings(RAZORPAY_KEY_ID="rzp_test_public", RAZORPAY_KEY_SECRET="razorpay-test-secret")
    def test_razorpay_checkout_verifies_capture_before_placing_paid_order(self):
        user = User.objects.create_user(username="razorpay_checkout_user", password="razorpay-test-password")
        profile = ClientProfile.objects.create(
            user=user,
            name="Online Customer",
            contact_no="9000000000",
            wallet_balance="100.00",
        )
        owner = User.objects.create_user(username="razorpay_shop_owner", password="razorpay-test-password")
        shop = Shop.objects.create(name="Razorpay Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Online Item",
            price="42.50",
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price="42.50",
            stock=4,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Online Road",
            is_default=True,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        self.client.force_login(user)

        razorpay_create_response = Mock()
        razorpay_create_response.json.return_value = {
            "id": "order_gateway_123",
            "amount": 5359,
        }
        razorpay_create_response.raise_for_status.return_value = None
        with patch("seller.views.requests.post", return_value=razorpay_create_response) as create_payment_order:
            checkout_response = self.client.post(
                reverse("checkout"),
                {
                    "address": address.id,
                    "payment_method": "RAZORPAY",
                    "use_wallet_balance": "0",
                },
            )

        self.assertEqual(checkout_response.status_code, 200)
        self.assertContains(checkout_response, "checkout.razorpay.com/v1/checkout.js")
        self.assertContains(checkout_response, "onlineBase * feePercent / 100")
        self.assertEqual(create_payment_order.call_args.kwargs["json"]["amount"], 5359)
        self.assertFalse(Order.objects.filter(user=user.username).exists())
        payment_intent = self.client.session["razorpay_checkout_intent"]
        self.assertEqual(Decimal(payment_intent["wallet_amount_used"]), Decimal("0.00"))

        payment_id = "pay_gateway123"
        signature = hmac.new(
            b"razorpay-test-secret",
            f"order_gateway_123|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        invalid_signature_response = self.client.post(
            reverse("verify_razorpay_payment"),
            {
                "razorpay_order_id": "order_gateway_123",
                "razorpay_payment_id": payment_id,
                "razorpay_signature": "invalid-signature",
            },
        )
        self.assertEqual(invalid_signature_response.status_code, 302)
        self.assertFalse(Order.objects.filter(user=user.username).exists())

        razorpay_payment_response = Mock()
        razorpay_payment_response.json.return_value = {
            "order_id": "order_gateway_123",
            "amount": payment_intent["amount_paise"],
            "currency": "INR",
            "status": "captured",
        }
        razorpay_payment_response.raise_for_status.return_value = None
        with patch("seller.views.requests.get", return_value=razorpay_payment_response):
            verify_response = self.client.post(
                reverse("verify_razorpay_payment"),
                {
                    "razorpay_order_id": "order_gateway_123",
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature,
                },
            )

        self.assertEqual(verify_response.status_code, 302)
        order = Order.objects.get(user=user.username)
        self.assertEqual(order.payment_type, "RAZORPAY")
        self.assertEqual(order.payment_status, "PAID")
        self.assertTrue(order.is_payment_done)
        self.assertEqual(order.payment_id, payment_id)
        self.assertEqual(order.finally_received_amount, Decimal("53.59"))
        self.assertEqual(order.platform_charge_amount, Decimal("10.04"))
        self.assertEqual(order.razorpay_fee_amount, Decimal("1.05"))
        profile.refresh_from_db()
        self.assertEqual(profile.wallet_balance, Decimal("100.00"))
        self.assertEqual(order.wallet_amount_used, Decimal("0.00"))
        self.assertNotIn("razorpay_checkout_intent", self.client.session)

    def test_shop_wallet_credits_paid_order_only_once_after_delivery(self):
        owner = User.objects.create_user(username="wallet_credit_owner", password="test-pass")
        shop = Shop.objects.create(name="Wallet Credit Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-WALLET-CREDIT",
            user="wallet_credit_customer",
            is_payment_done=True,
            payment_status="PAID",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("35.50"),
        )

        wallet = ShopWallet.objects.create(shop=shop)
        seller_views._credit_shop_wallet_for_delivery(shop_order)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertFalse(ShopWalletTransaction.objects.filter(shop_order=shop_order).exists())

        shop_order.status = "DELIVERED"
        shop_order.delivery_completed_at = timezone.now()
        shop_order.save(update_fields=["status", "delivery_completed_at"])
        seller_views._credit_shop_wallet_for_delivery(shop_order)
        seller_views._credit_shop_wallet_for_delivery(shop_order)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("34.43"))
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="CREDIT",
            ).count(),
            1,
        )
        commission_debit = ShopWalletTransaction.objects.get(
            shop_order=shop_order,
            transaction_type="COMMISSION",
        )
        self.assertEqual(commission_debit.amount, Decimal("1.07"))
        self.assertEqual(commission_debit.balance_after, Decimal("34.43"))
        site_wallet = SiteWallet.objects.get(key="SITE")
        self.assertEqual(site_wallet.balance, Decimal("1.07"))
        site_commission = SiteWalletTransaction.objects.get(shop_order=shop_order)
        self.assertEqual(site_commission.commission_base_amount, Decimal("35.50"))
        self.assertEqual(site_commission.commission_rate_percent, Decimal("3.00"))
        self.assertEqual(site_commission.amount, Decimal("1.07"))
        seller_views._credit_shop_wallet_for_delivery(shop_order)
        site_wallet.refresh_from_db()
        self.assertEqual(site_wallet.balance, Decimal("1.07"))

    def test_wallet_funded_shop_proceeds_are_allocated_without_rounding_overage(self):
        owner = User.objects.create_user(
            username="wallet_split_owner",
            password="test-password",
        )
        shops = [
            Shop.objects.create(name=f"Wallet Split Shop {index}", owner=owner)
            for index in range(2)
        ]
        order = Order.objects.create(
            order_id="ORD-WALLET-SPLIT",
            user="wallet_split_customer",
            payment_type="COD",
            wallet_amount_used=Decimal("1.01"),
            payment_amount=Decimal("1.01"),
        )
        shop_orders = [
            ShopOrder.objects.create(
                order=order,
                shop=shop,
                total_amount=Decimal("1.01"),
                taxable_amount=Decimal("1.01"),
                status="DELIVERED",
                delivery_completed_at=timezone.now(),
            )
            for shop in shops
        ]

        for shop_order in shop_orders:
            seller_views._credit_shop_wallet_for_delivery(shop_order)

        credited_amounts = []
        for shop_order in shop_orders:
            shop_order.refresh_from_db()
            wallet = ShopWallet.objects.get(shop=shop_order.shop)
            credited_amounts.append(shop_order.wallet_funded_proceeds)
            self.assertEqual(wallet.balance, shop_order.wallet_funded_proceeds)
        self.assertEqual(credited_amounts, [Decimal("0.51"), Decimal("0.50")])
        self.assertEqual(sum(credited_amounts), order.wallet_amount_used)
        self.assertFalse(SiteWalletTransaction.objects.filter(
            shop_order__order=order,
        ).exists())

    def test_site_commission_excludes_gst_and_delivery_and_uses_configured_rate(self):
        owner = User.objects.create_user(
            username="commission_base_owner",
            password="testpass123",
        )
        shop = Shop.objects.create(name="Commission Base Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-COMMISSION-BASE",
            user="commission_base_customer",
            is_payment_done=True,
            payment_status="PAID",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("100.00"),
            taxable_amount=Decimal("100.00"),
            delivery_fee=Decimal("25.00"),
            cgst_amount=Decimal("9.00"),
            sgst_amount=Decimal("9.00"),
            delivery_gst_amount=Decimal("4.50"),
            seller_gstin="27ABCDE1234F1Z5",
            status="DELIVERED",
            delivery_completed_at=timezone.now(),
        )
        site_wallet = SiteWallet.objects.get(key="SITE")
        shop_wallet = ShopWallet.objects.create(shop=shop)

        seller_views._credit_shop_wallet_for_delivery(shop_order)

        site_wallet.refresh_from_db()
        shop_wallet.refresh_from_db()
        commission = SiteWalletTransaction.objects.get(shop_order=shop_order)
        self.assertEqual(commission.commission_base_amount, Decimal("100.00"))
        self.assertEqual(commission.amount, Decimal("3.00"))
        self.assertEqual(site_wallet.balance, Decimal("3.00"))
        self.assertEqual(shop_wallet.balance, Decimal("110.50"))

    @override_settings(
        RAZORPAYX_KEY_ID="rzpx_test_key",
        RAZORPAYX_KEY_SECRET="razorpayx-test-secret",
        RAZORPAYX_ACCOUNT_NUMBER="1234567890123456",
    )
    def test_shop_wallet_withdrawal_reserves_balance_and_uses_idempotency_key(self):
        owner = User.objects.create_user(username="wallet_payout_owner", password="test-pass")
        shop = Shop.objects.create(
            name="Wallet Payout Shop",
            owner=owner,
            razorpay_fund_account_id="fa_test123",
        )
        wallet = ShopWallet.objects.create(shop=shop, balance=Decimal("80.00"))
        self.client.force_login(owner)
        payout_response = Mock()
        payout_response.json.return_value = {"id": "pout_test123", "status": "processing"}
        payout_response.raise_for_status.return_value = None

        with patch("seller.views.requests.request", return_value=payout_response) as payout_request:
            response = self.client.post(
                reverse("shop_wallet", args=[shop.pk]),
                {"amount": "25.00", "mode": "IMPS"},
            )

        self.assertEqual(response.status_code, 302)
        wallet.refresh_from_db()
        payout = ShopPayout.objects.get(shop=shop)
        self.assertEqual(wallet.balance, Decimal("55.00"))
        self.assertEqual(payout.status, "PROCESSING")
        self.assertEqual(payout_request.call_args.kwargs["json"]["amount"], 2500)
        self.assertEqual(
            payout_request.call_args.kwargs["headers"]["X-Payout-Idempotency"],
            str(payout.idempotency_key),
        )

    @override_settings(RAZORPAYX_WEBHOOK_SECRET="payout-webhook-test-secret")
    def test_payout_webhook_failure_releases_funds_once_and_processed_is_terminal(self):
        owner = User.objects.create_user(username="wallet_webhook_owner", password="test-pass")
        shop = Shop.objects.create(name="Webhook Wallet Shop", owner=owner)
        wallet = ShopWallet.objects.create(shop=shop, balance=Decimal("75.00"))
        payout = ShopPayout.objects.create(
            shop=shop,
            amount=Decimal("25.00"),
            status="PROCESSING",
            razorpay_payout_id="pout_webhook123",
            razorpay_fund_account_id="fa_webhook123",
            reference_id="S1WEBHOOKREF",
        )
        ShopWalletTransaction.objects.create(
            wallet=wallet,
            payout=payout,
            transaction_type="DEBIT",
            amount=Decimal("25.00"),
            balance_after=wallet.balance,
            description="Withdrawal initiated.",
        )
        def post_payout_event(payout_id, status):
            body = json.dumps({
                "payload": {
                    "payout": {
                        "entity": {"id": payout_id, "status": status}
                    }
                }
            }).encode()
            signature = hmac.new(
                b"payout-webhook-test-secret",
                body,
                hashlib.sha256,
            ).hexdigest()
            return self.client.post(
                reverse("razorpayx_payout_webhook"),
                data=body,
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE=signature,
            )

        first = post_payout_event(payout.razorpay_payout_id, "failed")
        second = post_payout_event(payout.razorpay_payout_id, "failed")
        wallet.refresh_from_db()
        payout.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(wallet.balance, Decimal("100.00"))
        self.assertEqual(payout.status, "FAILED")
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                payout=payout,
                transaction_type="REVERSAL",
            ).count(),
            1,
        )

        processed_payout = ShopPayout.objects.create(
            shop=shop,
            amount=Decimal("25.00"),
            status="PROCESSED",
            razorpay_payout_id="pout_processed123",
            razorpay_fund_account_id="fa_webhook123",
            reference_id="S1PROCESSEDREF",
        )
        wallet.balance -= Decimal("25.00")
        wallet.save(update_fields=["balance"])
        ShopWalletTransaction.objects.create(
            wallet=wallet,
            payout=processed_payout,
            transaction_type="DEBIT",
            amount=Decimal("25.00"),
            balance_after=wallet.balance,
            description="Processed withdrawal.",
        )
        post_payout_event(processed_payout.razorpay_payout_id, "failed")
        wallet.refresh_from_db()
        processed_payout.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("75.00"))
        self.assertEqual(processed_payout.status, "PROCESSED")

        post_payout_event(processed_payout.razorpay_payout_id, "reversed")
        post_payout_event(processed_payout.razorpay_payout_id, "reversed")
        wallet.refresh_from_db()
        processed_payout.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("100.00"))
        self.assertEqual(processed_payout.status, "REVERSED")
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                payout=processed_payout,
                transaction_type="REVERSAL",
            ).count(),
            1,
        )

    def test_order_tracking_page_renders_after_checkout(self):
        user = User.objects.create_user(username="trackinguser", password="pass1234")
        client_profile = ClientProfile.objects.create(user=user, name="Tracking User", contact_no="9876543210")
        shop = Shop.objects.create(
            name="Track Shop",
            owner=user,
            is_shop_open=False,
            allow_scheduled_delivery=True,
            require_customer_otp_verification=True,
            location={"lat": 22.0, "lng": 72.0, "address": "Main shop"},
            pickup_locations=[{
                "name": "Office Branch",
                "address": "MG Road branch",
                "lat": 23.01,
                "lng": 72.57,
            }],
        )
        product = Product.objects.create(created_by=user, product_name="Orange", price="35.00", unit="kg", product_category="SABJI")
        product_shop = ProductShop.objects.create(shop=shop, product=product, price="35.00", stock=10, is_active=True)
        second_shop = Shop.objects.create(name="Second Track Shop", owner=user)
        second_product = Product.objects.create(created_by=user, product_name="Apple", price="40.00", unit="kg", product_category="FRUITS")
        second_product_shop = ProductShop.objects.create(
            shop=second_shop,
            product=second_product,
            price="40.00",
            stock=10,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=client_profile,
            label="Office",
            address="MG Road",
            location={"lat": 23.01, "lng": 72.57},
            is_default=True,
            is_active=True,
        )

        session = self.client.session
        session["cart"] = {
            str(product_shop.id): 1,
            str(second_product_shop.id): 1,
        }
        session.save()

        self.client.force_login(user)
        checkout_page = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(checkout_page, "Scheduled delivery:")
        self.assertContains(checkout_page, "Delivery timing will be scheduled with this shop.")
        checkout_response = self.client.post(reverse("checkout"), {"address": address.id})
        order = Order.objects.filter(user=user.username).latest("created_at")
        shop_order = ShopOrder.objects.get(order=order, shop=shop)
        second_shop_order = ShopOrder.objects.get(order=order, shop=second_shop)

        self.assertEqual(checkout_response.status_code, 302)
        self.assertRegex(order.tracking_code, r"^[0-9A-F]{6}$")
        self.assertEqual(order.order_id, f"{order.tracking_code}-{shop_order.shop_order_number}")
        self.assertRegex(
            shop_order.shop_order_number,
            r"^TRACK-SHOP-ORD-\d{8}-\d{6}-1$",
        )
        self.assertRegex(
            second_shop_order.shop_order_number,
            r"^SECOND-TRACK-SHOP-ORD-\d{8}-\d{6}-1$",
        )
        self.assertTrue(shop_order.is_scheduled_delivery)
        self.assertFalse(second_shop_order.is_scheduled_delivery)
        self.assertEqual(
            shop_order.pickup_location["name"],
            "Office Branch",
        )
        self.assertTrue(
            Notification.objects.filter(
                user=user,
                title="Delivery timing needs to be scheduled",
                related_url=f"/orders/{order.tracking_code}/tracking/",
            ).exists()
        )
        tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.tracking_code})
        )
        self.assertEqual(tracking_response.status_code, 200)
        client_profile.verification_otp = "2468"
        client_profile.save(update_fields=["verification_otp"])
        shop_order.status = "OUT_FOR_DELIVERY"
        shop_order.save(update_fields=["status"])
        tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.tracking_code})
        )
        self.assertContains(tracking_response, f"Tracking #{order.tracking_code}")
        self.assertContains(tracking_response, "Scheduled delivery")
        self.assertContains(
            tracking_response,
            "The shop will confirm the delivery timing.",
        )
        self.assertContains(tracking_response, "Pickup: Office Branch")
        self.assertContains(tracking_response, "MG Road branch")
        self.assertContains(tracking_response, "Delivery verification code", count=1)
        self.assertContains(
            tracking_response,
            "Share your live location with the rider assigned to this active order?",
        )
        updates_response = self.client.post(
            reverse("order_tracking_updates", kwargs={"order_id": order.tracking_code})
        )
        self.assertEqual(updates_response.status_code, 200)
        tracking_shop_orders = {
            row["id"]: row
            for row in updates_response.json()["shop_orders"]
        }
        self.assertTrue(tracking_shop_orders[shop_order.pk]["is_scheduled_delivery"])
        self.assertFalse(
            tracking_shop_orders[second_shop_order.pk]["is_scheduled_delivery"]
        )
        self.assertContains(
            tracking_response,
            f'data-updates-url="/orders/{order.tracking_code}/tracking/updates/"',
        )
        legacy_tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.order_id})
        )
        self.assertEqual(legacy_tracking_response.status_code, 200)
        shop_order.customer_otp_verified = True
        shop_order.save(update_fields=["customer_otp_verified"])
        verified_tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.tracking_code})
        )
        self.assertNotContains(
            verified_tracking_response,
            "Delivery verification code",
        )

    def test_shop_order_numbers_count_per_shop_and_reset_daily(self):
        from .views import generate_shop_order_number

        first_shop = Shop.objects.create(name="Counter Shop")
        second_shop = Shop.objects.create(name="Another Shop")
        first_day = timezone.make_aware(datetime(2026, 6, 17, 12, 0, 0))
        next_day = timezone.make_aware(datetime(2026, 6, 18, 9, 30, 0))

        self.assertEqual(
            generate_shop_order_number(first_shop, first_day),
            "COUNTER-SHOP-ORD-17062026-120000-1",
        )
        self.assertEqual(
            generate_shop_order_number(first_shop, first_day),
            "COUNTER-SHOP-ORD-17062026-120000-2",
        )
        self.assertEqual(
            generate_shop_order_number(second_shop, first_day),
            "ANOTHER-SHOP-ORD-17062026-120000-1",
        )
        self.assertEqual(
            generate_shop_order_number(first_shop, next_day),
            "COUNTER-SHOP-ORD-18062026-093000-1",
        )

    def test_shop_order_numbers_disambiguate_matching_shop_names(self):
        from .views import generate_shop_order_number

        first_shop = Shop.objects.create(name="Shared Shop")
        second_shop = Shop.objects.create(name="Shared Shop")
        order_time = timezone.make_aware(datetime(2026, 6, 17, 12, 0, 0))

        first_number = generate_shop_order_number(first_shop, order_time)
        second_number = generate_shop_order_number(second_shop, order_time)

        self.assertNotEqual(first_number, second_number)
        self.assertIn(f"SHARED-SHOP-{second_shop.pk}-ORD-", second_number)

    def test_shop_order_report_calculates_today_and_selected_period_totals(self):
        owner = User.objects.create_user(username="sales_report_owner", password="test-password")
        shop = Shop.objects.create(name="Sales Report Shop", owner=owner)
        today = timezone.localdate()
        previous_day = today - timedelta(days=1)
        for suffix, amount, status, created_day in (
            ("TODAY-1", "10.00", "DELIVERED", today),
            ("TODAY-2", "30.00", "PLACED", today),
            ("YESTERDAY", "40.00", "DELIVERED", previous_day),
            ("CANCELLED", "500.00", "CANCELLED", today),
        ):
            order = Order.objects.create(
                order_id=f"ORD-REPORT-{suffix}",
                user="report_customer",
            )
            shop_order = ShopOrder.objects.create(
                order=order,
                shop=shop,
                total_amount=Decimal(amount),
                status=status,
            )
            ShopOrder.objects.filter(pk=shop_order.pk).update(
                created_at=timezone.make_aware(
                    datetime.combine(created_day, time.min),
                ),
            )

        self.client.force_login(owner)
        today_response = self.client.get(reverse("shop_order_report", kwargs={"shop_id": shop.id}))
        period_response = self.client.get(
            reverse("shop_order_report", kwargs={"shop_id": shop.id}),
            {
                "date_from": previous_day.isoformat(),
                "date_to": today.isoformat(),
            },
        )

        self.assertEqual(today_response.status_code, 200)
        self.assertEqual(today_response.context["today_summary"]["order_count"], 2)
        self.assertEqual(today_response.context["today_summary"]["total_amount"], Decimal("40.00"))
        self.assertEqual(today_response.context["today_summary"]["average_order_value"], Decimal("20.00"))
        self.assertEqual(period_response.status_code, 200)
        self.assertEqual(period_response.context["period_summary"]["order_count"], 3)
        self.assertEqual(period_response.context["period_summary"]["total_amount"], Decimal("80.00"))
        self.assertEqual(period_response.context["period_summary"]["average_order_value"], Decimal("80") / Decimal("3"))

    def test_my_orders_only_lists_customers_own_orders_with_tracking_links(self):
        customer = User.objects.create_user(username="my_orders_customer", password="my-orders-pass")
        owner = User.objects.create_user(username="my_orders_owner", password="my-orders-pass")
        shop = Shop.objects.create(name="Orders Test Shop", owner=owner)
        own_order = Order.objects.create(
            order_id="ORD-MY-OWN",
            user=customer.username,
            delivery_address="Customer Road",
            status="PLACED",
            shop=shop,
        )
        ShopOrder.objects.create(
            order=own_order,
            shop=shop,
            total_amount="25.00",
            status="PLACED",
        )
        Order.objects.create(
            order_id="ORD-OTHER-CUSTOMER",
            user="another_customer",
            delivery_address="Other Road",
            status="PLACED",
            shop=shop,
        )

        self.client.force_login(customer)
        response = self.client.get(reverse("order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ORD-MY-OWN")
        self.assertContains(response, reverse("order_tracking", kwargs={"order_id": own_order.order_id}))
        self.assertNotContains(response, "ORD-OTHER-CUSTOMER")

    def test_shop_staff_orders_link_opens_relevant_shop_order_details(self):
        owner = User.objects.create_user(username="my_orders_staff_owner", password="my-orders-pass")
        staff_user = User.objects.create_user(username="my_orders_staff", password="my-orders-pass")
        shop = Shop.objects.create(name="Staff Orders Shop", owner=owner)
        ShopStaff.objects.create(shop=shop, user=staff_user, role="MANAGER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-STAFF-DETAIL",
            user="customer_for_staff",
            delivery_address="Staff Customer Road",
            status="PLACED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="45.00",
            status="PLACED",
        )

        self.client.force_login(staff_user)
        response = self.client.get(reverse("order_list"))

        self.assertRedirects(response, reverse("shop_order_list"))
        staff_orders = self.client.get(reverse("shop_order_list"))
        self.assertEqual(staff_orders.status_code, 200)
        self.assertContains(staff_orders, "ORD-STAFF-DETAIL")
        self.assertContains(
            staff_orders,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
        )

    def test_shop_owner_customer_portal_uses_only_customer_marketplace_and_orders(self):
        owner = User.objects.create_user(
            username="dual_portal_owner",
            password="test-password",
        )
        ClientProfile.objects.create(user=owner, name="Dual Portal Customer")
        shop = Shop.objects.create(name="Dual Portal Shop", owner=owner)
        own_order = Order.objects.create(
            order_id="ORD-DUAL-PORTAL",
            user=owner.username,
            delivery_address="Customer Home",
            status="PLACED",
            shop=shop,
        )
        ShopOrder.objects.create(
            order=own_order,
            shop=shop,
            total_amount="25.00",
            status="PLACED",
        )

        self.client.force_login(owner)
        session = self.client.session
        session["active_portal"] = "CUSTOMER"
        session.save()

        marketplace_response = self.client.get(reverse("marketplace"))
        orders_response = self.client.get(reverse("order_list"))
        home_response = self.client.get(reverse("home"))

        self.assertEqual(marketplace_response.status_code, 200)
        self.assertEqual(orders_response.status_code, 200)
        self.assertContains(orders_response, "ORD-DUAL-PORTAL")
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "Customer account")
        self.assertNotContains(home_response, "My shops")

    def test_b2b_portal_shows_own_orders_and_keeps_shop_orders_out_of_view(self):
        owner = User.objects.create_user(
            username="dual_portal_b2b_owner",
        )
        ClientProfile.objects.create(
            user=owner,
            name="B2B Shop Owner",
            gstin="27ABCDE1234F1Z5",
        )
        shop = Shop.objects.create(name="B2B Owner Shop", owner=owner)
        own_order = Order.objects.create(
            order_id="ORD-B2B-OWN",
            user=owner.username,
            delivery_address="Customer Home",
            status="PLACED",
            shop=shop,
            is_b2b_order=True,
        )
        own_shop_order = ShopOrder.objects.create(
            order=own_order,
            shop=shop,
            total_amount="25.00",
            status="PLACED",
        )
        other_customer_order = Order.objects.create(
            order_id="ORD-B2B-OTHER-CUSTOMER",
            user="another_b2b_shop_customer",
            delivery_address="Another Customer Home",
            status="PLACED",
            shop=shop,
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_customer_order,
            shop=shop,
            shop_order_number="SHOP-ONLY-ORDER-123",
            total_amount="45.00",
            status="PLACED",
        )

        self.client.force_login(owner)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()

        own_orders_response = self.client.get(reverse("order_list"))

        self.assertEqual(own_orders_response.status_code, 200)
        self.assertContains(own_orders_response, own_order.order_id)
        self.assertNotContains(own_orders_response, other_customer_order.order_id)
        self.assertNotContains(own_orders_response, other_shop_order.shop_order_number)
        self.assertRedirects(
            self.client.get(reverse("shop_order_list")),
            reverse("marketplace"),
        )
        self.assertRedirects(
            self.client.get(
                reverse(
                    "shop_order_detail",
                    kwargs={"shop_order_id": own_shop_order.pk},
                ),
            ),
            reverse("marketplace"),
        )
        self.assertRedirects(
            self.client.get(reverse("dashboard")),
            reverse("marketplace"),
        )

    def test_customer_reorder_adds_available_quantities_and_reports_unavailable_items(self):
        customer = User.objects.create_user(
            username="partial_reorder_customer",
            password="test-password",
        )
        owner = User.objects.create_user(
            username="partial_reorder_owner",
            password="test-password",
        )
        shop = Shop.objects.create(name="Partial Reorder Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-PARTIAL-REORDER",
            user=customer.username,
            status="DELIVERED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="50.00",
            status="DELIVERED",
        )
        available_product = Product.objects.create(
            created_by=owner,
            product_category="FRUITS",
            product_name="Available Apple",
            price="10.00",
            unit="piece",
        )
        available_assignment = ProductShop.objects.create(
            product=available_product,
            shop=shop,
            stock=2,
            is_active=True,
        )
        unavailable_product = Product.objects.create(
            created_by=owner,
            product_category="FRUITS",
            product_name="Out of Stock Pear",
            price="15.00",
            unit="piece",
        )
        unavailable_assignment = ProductShop.objects.create(
            product=unavailable_product,
            shop=shop,
            stock=0,
            is_active=True,
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=available_product,
            product_shop=available_assignment,
            product_name="Available Apple",
            quantity=4,
            price="10.00",
            total="40.00",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=unavailable_product,
            product_shop=unavailable_assignment,
            product_name="Out of Stock Pear",
            quantity=1,
            price="15.00",
            total="15.00",
        )

        self.client.force_login(customer)
        response = self.client.post(
            reverse("customer_reorder", kwargs={"order_id": order.order_id}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session["cart"],
            {str(available_assignment.pk): 2},
        )
        self.assertContains(response, "Out of Stock Pear")
        self.assertContains(response, "only 2 available")

    def test_favorites_page_shows_available_products_from_favorite_shops(self):
        customer = User.objects.create_user(
            username="favorites_page_customer",
            password="test-password",
        )
        owner = User.objects.create_user(
            username="favorites_page_owner",
            password="test-password",
        )
        ClientProfile.objects.create(user=customer, name="Favorites Customer")
        shop = Shop.objects.create(name="Favorite Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_category="FRUITS",
            product_name="Favorite Shop Apple",
            price="35.00",
            unit="piece",
        )
        assignment = ProductShop.objects.create(
            product=product,
            shop=shop,
            stock=4,
            is_active=True,
        )
        shop_only_product = Product.objects.create(
            created_by=owner,
            product_category="FRUITS",
            product_name="Another Favorite Shop Product",
            price="22.00",
            unit="piece",
        )
        ProductShop.objects.create(
            product=shop_only_product,
            shop=shop,
            stock=2,
            is_active=True,
        )
        FavoriteShop.objects.create(user=customer, shop=shop)
        FavoriteProduct.objects.create(user=customer, product_shop=assignment)

        self.client.force_login(customer)
        response = self.client.get(reverse("customer_favorites"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Favorite Shop Apple")
        self.assertContains(response, "Another Favorite Shop Product")
        self.assertContains(response, "Browse products from your saved shops and products.")
        self.assertNotContains(response, "<h2 class=\"h4 fw-bold mb-3\">Favourite shops")

    def test_customer_profile_displays_wallet_balance_without_edit_control(self):
        user = User.objects.create_user(username="wallet_profile_customer", password="wallet-profile-pass")
        ClientProfile.objects.create(
            user=user,
            name="Wallet Profile Customer",
            wallet_balance=Decimal("123.45"),
        )
        self.client.force_login(user)

        response = self.client.get(reverse("my_profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Personal details")
        self.assertContains(response, "Delivery addresses")
        self.assertContains(response, "My orders")
        self.assertContains(response, "Delivery verification")
        self.assertLess(
            response.content.index(b"Delivery verification"),
            response.content.index(b"Personal details"),
        )
        self.assertContains(response, "WALLET BALANCE")
        self.assertContains(response, "Wallet &amp; payment history")
        self.assertContains(response, "₹123.45")
        self.assertNotContains(response, 'name="wallet_balance"')
        self.assertNotContains(response, "Preferred Delivery Time")

    def test_customer_profile_displays_active_login_role(self):
        user = User.objects.create_user(
            username="profile_active_role_customer",
            password="test-password",
        )
        ClientProfile.objects.create(user=user, name="Role Profile Customer")
        self.client.force_login(user)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()

        response = self.client.get(reverse("client_profile"))

        self.assertContains(response, "CURRENT ROLE")
        self.assertContains(response, "B2B customer")

    def test_customer_can_change_permanent_delivery_otp_from_profile(self):
        user = User.objects.create_user(
            username="change_profile_otp_customer",
            password="test-password",
        )
        profile = ClientProfile.objects.create(
            user=user,
            name="OTP Profile Customer",
            verification_otp="2468",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("client_profile"),
            {"action": "change_verification_otp"},
        )

        self.assertRedirects(response, reverse("client_profile"))
        profile.refresh_from_db()
        self.assertRegex(profile.verification_otp, r"^[1-9]\d{3}$")
        self.assertNotEqual(profile.verification_otp, "2468")
        self.assertContains(self.client.get(reverse("client_profile")), "Change permanent OTP")

    def test_customer_cannot_change_permanent_otp_with_undelivered_order(self):
        user = User.objects.create_user(
            username="pending_order_otp_customer",
            password="test-password",
        )
        profile = ClientProfile.objects.create(
            user=user,
            name="Pending Order OTP Customer",
            verification_otp="2468",
        )
        shop = Shop.objects.create(
            name="Pending OTP Shop",
            owner=User.objects.create_user(
                username="pending_order_otp_owner",
                password="test-password",
            ),
        )
        order = Order.objects.create(
            order_id="ORD-PENDING-OTP",
            user=user.username,
        )
        ShopOrder.objects.create(order=order, shop=shop, status="OUT_FOR_DELIVERY")
        self.client.force_login(user)

        response = self.client.post(
            reverse("client_profile"),
            {"action": "change_verification_otp"},
            follow=True,
        )

        profile.refresh_from_db()
        self.assertEqual(profile.verification_otp, "2468")
        self.assertContains(response, "cannot be changed while you have an undelivered order")
        self.assertContains(response, "after all your current orders are delivered or cancelled")
        self.assertContains(response, "disabled")

    def test_customer_wallet_history_shows_only_their_wallet_and_order_payments(self):
        customer = User.objects.create_user(
            username="history_customer",
            password="test-password",
        )
        profile = ClientProfile.objects.create(
            user=customer,
            name="History Customer",
            wallet_balance=Decimal("24.00"),
        )
        ClientWalletTransaction.objects.create(
            client=profile,
            transaction_type="CREDIT",
            amount=Decimal("30.00"),
            balance_after=Decimal("30.00"),
            reference="WALLET-TOPUP",
            description="Wallet credit for testing.",
        )
        ClientWalletTransaction.objects.create(
            client=profile,
            transaction_type="DEBIT",
            amount=Decimal("6.00"),
            balance_after=Decimal("24.00"),
            reference="ORD-HISTORY-1",
            description="Wallet payment for order.",
        )
        order = Order.objects.create(
            order_id="ORD-HISTORY-1",
            user=customer.username,
            payment_type="COD",
            payment_status="COD",
            wallet_amount_used=Decimal("6.00"),
            finally_received_amount=Decimal("4.00"),
            payment_amount=Decimal("10.00"),
            final_billing_amount=Decimal("20.00"),
        )
        shop_owner = User.objects.create_user(
            username="history_shop_owner",
            password="test-password",
        )
        shop = Shop.objects.create(name="History Shop", owner=shop_owner)
        shop_order = ShopOrder.objects.create(order=order, shop=shop)
        CODBalancePayment.objects.create(
            shop_order=shop_order,
            amount=Decimal("10.00"),
            received_by=shop_owner,
        )
        other_user = User.objects.create_user(
            username="other_history_customer",
            password="test-password",
        )
        other_profile = ClientProfile.objects.create(
            user=other_user,
            name="Other Customer",
        )
        ClientWalletTransaction.objects.create(
            client=other_profile,
            transaction_type="CREDIT",
            amount=Decimal("99.00"),
            balance_after=Decimal("99.00"),
            reference="PRIVATE-TOPUP",
            description="Private transaction.",
        )
        Order.objects.create(
            order_id="ORD-PRIVATE-HISTORY",
            user=other_user.username,
            final_billing_amount=Decimal("99.00"),
        )

        self.client.force_login(customer)
        response = self.client.get(reverse("customer_wallet_history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wallet transactions")
        self.assertContains(response, "Order payment history")
        self.assertContains(response, "WALLET-TOPUP")
        self.assertContains(response, "ORD-HISTORY-1")
        self.assertContains(response, "History Shop payment activity")
        self.assertContains(response, "COD balance received by")
        self.assertContains(response, "₹24.00")
        self.assertNotContains(response, "PRIVATE-TOPUP")
        self.assertNotContains(response, "ORD-PRIVATE-HISTORY")

    def test_rider_dashboard_shows_customer_otp_payment_status_and_order_security(self):
        owner = User.objects.create_user(username="owner_otp", password="pass1234")
        rider_user = User.objects.create_user(username="rider_otp_user", password="pass1234")
        customer_user = User.objects.create_user(
            username="customer_otp",
            password="test-password",
        )
        ClientProfile.objects.create(user=customer_user, name="Customer OTP Name")
        shop = Shop.objects.create(name="Otp Shop", owner=owner)
        rider = ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-OTP-1",
            user="customer_otp",
            delivery_address="Green Avenue",
            delivery_location="23.1,72.5",
            delivery_contact_no="9988776655",
            status="READY",
            payment_status="PAID",
            payment_type="ONLINE",
            payment_amount="200.00",
            platform_charge_amount="10.00",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop, total_amount="180.00", status="READY", assigned_rider=rider)
        second_shop = Shop.objects.create(name="Other OTP Shop", owner=owner)
        ShopOrder.objects.create(
            order=order,
            shop=second_shop,
            total_amount="120.00",
            status="PLACED",
        )
        completed_order = Order.objects.create(
            order_id="ORD-OTP-DONE",
            user="customer_otp",
            delivery_address="Completed Street",
            delivery_location="23.2,72.6",
            delivery_contact_no="9988776655",
            status="OUT_FOR_DELIVERY",
            shop=shop,
        )
        ShopOrder.objects.create(
            order=completed_order,
            shop=shop,
            total_amount="75.00",
            status="OUT_FOR_DELIVERY",
            assigned_rider=rider,
            delivery_completed_at=timezone.now(),
        )

        self.client.force_login(rider_user)
        response = self.client.get(reverse("rider_dashboard"), {"view": "active"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paid online")
        self.assertContains(response, "ORD-OTP-1")
        self.assertContains(response, "Customer OTP Name")
        self.assertContains(response, "OTP")
        self.assertContains(response, "Navigate from my current location")
        self.assertContains(response, "Open Delivery Location")
        self.assertContains(response, "Share coordinates with delivery app")
        self.assertContains(response, "Copy coordinates")
        self.assertContains(response, "Open in installed map app")
        self.assertContains(response, "geo:0,0?q=23.1,72.5")
        self.assertContains(response, "120.00")
        self.assertContains(response, "6.00")
        self.assertContains(response, "maps/dir/")
        self.assertContains(response, "maps/search/")
        self.assertContains(response, "23.1,72.5")
        self.assertNotContains(response, "ORD-OTP-DONE")

        order.payment_status = "COD"
        order.payment_type = "COD"
        order.save(update_fields=["payment_status", "payment_type"])
        cod_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(cod_response, "Cash on delivery")
        self.assertContains(cod_response, "Collect from customer (COD)")

        completed_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertEqual(completed_response.status_code, 200)
        self.assertContains(completed_response, "ORD-OTP-DONE")
        self.assertContains(completed_response, "Delivery completed")
        self.assertNotContains(completed_response, "Navigate from my current location")
        self.assertEqual(len(completed_response.context["orders"]), 1)

    def test_rider_dashboard_exposes_customer_otp_verification_form(self):
        owner = User.objects.create_user(username="owner_verify_form", password="pass1234")
        rider_user = User.objects.create_user(username="rider_verify_form", password="pass1234")
        customer_user = User.objects.create_user(username="customer_verify_form", password="pass1234")
        ClientProfile.objects.create(user=customer_user, name="Customer Verify", verification_otp="5678")
        shop = Shop.objects.create(name="Verify Form Shop", owner=owner, require_customer_otp_verification=True)
        rider = ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-VERIFY-FORM-1",
            user=customer_user.username,
            delivery_address="Form Street",
            delivery_location="24.0,73.0",
            delivery_contact_no="9000000001",
            status="OUT_FOR_DELIVERY",
            payment_status="PAID",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop, total_amount="150.00", status="OUT_FOR_DELIVERY", assigned_rider=rider)

        self.client.force_login(rider_user)
        response = self.client.get(reverse("rider_dashboard"), {"view": "active"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="rider-next-step ')
        self.assertContains(response, "Reach the customer and verify their OTP")
        self.assertContains(response, "Verify OTP")
        self.assertContains(response, 'name="otp"')
        self.assertContains(response, reverse("verify_customer_otp", kwargs={"shop_order_id": shop_order.id}))

    def test_shop_cancellation_records_source_and_restocks_inventory(self):
        owner = User.objects.create_user(username="shop_cancel_owner", password="test-password")
        shop = Shop.objects.create(name="Cancellation Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Cancelled Item",
            price="10.00",
            unit="piece",
            product_category="OTHER",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            stock=3,
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-SHOP-CANCEL-1",
            user="shop_cancel_customer",
            status="PLACED",
            payment_type="COD",
            payment_status="COD",
            payment_amount="20.00",
            final_billing_amount="20.00",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="20.00",
            status="PLACED",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=product,
            product_shop=assignment,
            product_name=product.product_name,
            quantity=2,
            price="10.00",
            total="20.00",
        )

        self.client.force_login(owner)
        response = self.client.post(
            reverse("shop_order_status_update", kwargs={"shop_order_id": shop_order.id}),
            {"status": "CANCELLED"},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(shop_order.cancellation_source, "SHOP")
        self.assertEqual(order.cancellation_source, "SHOP")
        self.assertEqual(assignment.stock, 5)
        self.assertTrue(
            StockMovement.objects.filter(
                product_shop=assignment,
                movement_type="ADJUSTMENT",
                reference=f"CANCEL-RESTOCK-{shop_order.id}",
            ).exists()
        )

    def test_delivered_and_cancelled_order_statuses_are_final(self):
        owner = User.objects.create_user(username="final_order_status_owner")
        shop = Shop.objects.create(name="Final Order Status Shop", owner=owner)
        self.client.force_login(owner)
        status_urls = (
            "shop_order_status",
            "shop_order_status_update",
        )

        for terminal_status in ("DELIVERED", "CANCELLED"):
            with self.subTest(status=terminal_status):
                order = Order.objects.create(
                    order_id=f"ORD-FINAL-{terminal_status}",
                    user="final_status_customer",
                    status=terminal_status,
                    shop=shop,
                )
                shop_order = ShopOrder.objects.create(
                    order=order,
                    shop=shop,
                    status=terminal_status,
                )
                initial_history_count = shop_order.status_history.count()

                for status_url in status_urls:
                    with self.subTest(endpoint=status_url):
                        response = self.client.post(
                            reverse(status_url, kwargs={"shop_order_id": shop_order.pk}),
                            {"status": "PLACED"},
                        )
                        self.assertEqual(response.status_code, 302)
                        shop_order.refresh_from_db()
                        order.refresh_from_db()
                        self.assertEqual(shop_order.status, terminal_status)
                        self.assertEqual(order.status, terminal_status)
                        self.assertEqual(
                            shop_order.status_history.count(),
                            initial_history_count,
                        )

                detail_response = self.client.get(
                    reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
                )
                self.assertEqual(detail_response.status_code, 200)
                self.assertNotContains(detail_response, 'id="orderStatusSelect"')
                self.assertContains(detail_response, "Its status is final and cannot be changed.")

    @override_settings(RAZORPAY_KEY_ID="rzp_test_public", RAZORPAY_KEY_SECRET="razorpay-test-secret")
    def test_shop_cancellation_refunds_only_affected_paid_shop_order(self):
        owner = User.objects.create_user(username="paid_shop_cancel_owner", password="test-password")
        shop = Shop.objects.create(name="Paid Cancellation Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-PAID-SHOP-CANCEL-1",
            user="paid_shop_cancel_customer",
            status="PLACED",
            payment_type="RAZORPAY",
            payment_status="PAID",
            payment_id="pay_shop_cancel_123",
            is_payment_done=True,
            payment_amount="0.00",
            finally_received_amount="130.00",
            final_billing_amount="130.00",
            platform_charge_amount="10.00",
            razorpay_fee_amount="2.00",
            gst_amount="18.00",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="100.00",
            taxable_amount="100.00",
            cgst_amount="9.00",
            sgst_amount="9.00",
            status="PLACED",
            seller_gstin="24ABCDE1234F1Z5",
        )
        refund_response = Mock()
        refund_response.raise_for_status.return_value = None

        self.client.force_login(owner)
        with patch("seller.views.requests.post", return_value=refund_response) as refund_request:
            response = self.client.post(
                reverse("shop_order_status_update", kwargs={"shop_order_id": shop_order.id}),
                {"status": "CANCELLED"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(refund_request.call_args.kwargs["json"]["amount"], 11800)
        order.refresh_from_db()
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.razorpay_refund_amount, Decimal("118.00"))
        self.assertEqual(order.final_billing_amount, Decimal("12.00"))
        self.assertEqual(order.finally_received_amount, Decimal("12.00"))
        self.assertEqual(order.payment_status, "PARTIALLY_REFUNDED")

    def test_packer_can_move_order_from_packing_to_ready_for_pickup(self):
        owner = User.objects.create_user(username="owner_packer_status", password="pass1234")
        packer_user = User.objects.create_user(username="packer_status", password="pass1234")
        shop = Shop.objects.create(name="Packer Status Shop", owner=owner)
        packer = ShopStaff.objects.create(
            shop=shop,
            user=packer_user,
            role="PACKER",
            is_active=True,
            can_pack_orders=True,
            can_change_order_status=True,
        )
        order = Order.objects.create(
            order_id="ORD-PACKER-READY",
            user="packer_customer",
            delivery_address="Packing Road",
            delivery_location="25.0,74.0",
            delivery_contact_no="9111111111",
            status="PACKING",
            payment_status="PAID",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="100.00",
            status="PACKING",
            assigned_packer=packer,
        )
        rider_user = User.objects.create_user(username="ready_rider", password="ready-rider-test-password")
        ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)

        self.client.force_login(packer_user)
        response = self.client.post(
            reverse("shop_order_status_update", kwargs={"shop_order_id": shop_order.id}),
            {"status": "READY", "remark": "Packed and ready"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('packer_dashboard')}?view=ready#order-{shop_order.id}",
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "READY")
        self.assertTrue(
            shop_order.status_history.filter(
                status="READY",
                remark="Packed and ready",
            ).exists()
        )
        history_response = self.client.get(
            reverse("packer_dashboard"),
            {"view": "history"},
        )
        self.assertContains(history_response, "ORD-PACKER-READY")
        self.assertContains(history_response, "Packing history")
        self.assertContains(history_response, "Marked ready")
        ready_notification = Notification.objects.get(
            user=rider_user,
            title="Delivery ready for pickup",
        )
        self.assertEqual(
            ready_notification.related_url,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.id}),
        )
        self.client.force_login(rider_user)
        open_notification_response = self.client.get(
            reverse("mark_notification_read", kwargs={"notification_id": ready_notification.id})
        )
        self.assertRedirects(open_notification_response, ready_notification.related_url)
        ready_notification.refresh_from_db()
        self.assertTrue(ready_notification.is_read)

    def test_packer_can_verify_rider_pickup_and_view_recently_verified_orders(self):
        owner = User.objects.create_user(
            username="owner_packer_verify_rider",
            password="test-pass",
        )
        packer_user = User.objects.create_user(
            username="packer_verify_rider",
            password="test-pass",
        )
        rider_user = User.objects.create_user(
            username="rider_verify_rider",
            password="test-pass",
        )
        shop = Shop.objects.create(name="Packer Rider Verification Shop", owner=owner)
        packer = ShopStaff.objects.create(
            shop=shop,
            user=packer_user,
            role="PACKER",
            is_active=True,
            can_pack_orders=True,
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-PACKER-VERIFY-RIDER",
            user="packer_rider_customer",
            status="RIDER_ASSIGNED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=Decimal("50.00"),
            status="RIDER_ASSIGNED",
            assigned_packer=packer,
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
            rider_order_acknowledged=True,
        )

        self.client.force_login(packer_user)
        verification_queue = self.client.get(
            reverse("packer_dashboard"),
            {"view": "verify"},
        )
        self.assertEqual(verification_queue.status_code, 200)
        self.assertContains(verification_queue, "ORD-PACKER-VERIFY-RIDER")
        self.assertContains(verification_queue, "Verify pickup")

        pickup_otp = seller_views._ensure_rider_pickup_otp(shop_order)
        verification_response = self.client.post(
            reverse("verify_rider_pickup_otp"),
            {"pickup_otp": pickup_otp},
        )
        self.assertEqual(verification_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

        recently_verified = self.client.get(
            reverse("packer_dashboard"),
            {"view": "verified"},
        )
        self.assertEqual(recently_verified.status_code, 200)
        self.assertContains(recently_verified, "ORD-PACKER-VERIFY-RIDER")
        self.assertContains(recently_verified, "Rider and parcel verified")

    def test_owner_can_assign_and_packer_can_accept_order(self):
        owner = User.objects.create_user(username="owner_packer_assign", password="pass1234")
        packer_user = User.objects.create_user(username="packer_assigner", password="pass1234")
        customer_user = User.objects.create_user(
            username="packer_customer_assign",
            password="test-password",
        )
        ClientProfile.objects.create(user=customer_user, name="Packer Customer Name")
        shop = Shop.objects.create(name="Packer Assign Shop", owner=owner)
        packer = ShopStaff.objects.create(shop=shop, user=packer_user, role="PACKER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-PACKER-ASSIGN",
            user="packer_customer_assign",
            delivery_address="Assign Road",
            delivery_location="26.0,75.0",
            delivery_contact_no="9222222222",
            status="CONFIRMED",
            payment_status="PAID",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(order=order, shop=shop, total_amount="120.00", status="CONFIRMED")

        self.client.force_login(owner)
        assign_response = self.client.post(
            reverse("assign_packer", kwargs={"shop_order_id": shop_order.id}),
            {"packer": packer.id},
        )
        self.assertEqual(assign_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_packer_id, packer.id)

        self.client.force_login(packer_user)
        accept_response = self.client.post(
            reverse("accept_packer_order", kwargs={"shop_order_id": shop_order.id}),
        )
        self.assertEqual(accept_response.status_code, 302)
        self.assertEqual(
            accept_response["Location"],
            f"{reverse('packer_dashboard')}?view=active#order-{shop_order.id}",
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_packer_id, packer.id)
        second_order = Order.objects.create(
            order_id="ORD-PACKER-ASSIGN-SECOND",
            user="packer_customer_assign",
            status="CONFIRMED",
            payment_status="PAID",
            shop=shop,
        )
        second_shop_order = ShopOrder.objects.create(
            order=second_order,
            shop=shop,
            total_amount="60.00",
            status="CONFIRMED",
        )
        second_accept_response = self.client.post(
            reverse("accept_packer_order", kwargs={"shop_order_id": second_shop_order.id}),
        )
        self.assertEqual(second_accept_response.status_code, 302)
        second_shop_order.refresh_from_db()
        self.assertEqual(second_shop_order.assigned_packer_id, packer.id)
        multi_order_dashboard = self.client.get(
            reverse("packer_dashboard"),
            {"view": "active"},
        )
        self.assertContains(multi_order_dashboard, "ORD-PACKER-ASSIGN")
        self.assertContains(multi_order_dashboard, "ORD-PACKER-ASSIGN-SECOND")

        older_order = Order.objects.create(
            order_id="ORD-PACKER-OLDER",
            user="packer_customer_assign",
            status="PACKING",
            payment_status="PAID",
            shop=shop,
        )
        older_shop_order = ShopOrder.objects.create(
            order=older_order,
            shop=shop,
            total_amount="80.00",
            status="PACKING",
            assigned_packer=packer,
        )
        ShopOrder.objects.filter(pk=older_shop_order.pk).update(
            created_at=timezone.now() - timedelta(days=1),
        )
        packer_dashboard_response = self.client.get(
            reverse("packer_dashboard"),
            {"view": "active"},
        )
        self.assertContains(packer_dashboard_response, shop_order.order.order_id)
        self.assertContains(packer_dashboard_response, "Packer Customer Name")
        self.assertContains(packer_dashboard_response, "Mark ready for pickup")
        self.assertContains(packer_dashboard_response, "My active packing")
        dashboard_html = packer_dashboard_response.content.decode()
        self.assertLess(
            dashboard_html.index(f'id="order-{older_shop_order.id}"'),
            dashboard_html.index(f'id="order-{shop_order.id}"'),
        )
        self.assertLess(
            dashboard_html.index('aria-label="Packing order tabs"'),
            dashboard_html.index(f'id="order-{older_shop_order.id}"'),
        )
        self.assertLess(
            dashboard_html.index(f'id="order-{older_shop_order.id}"'),
            dashboard_html.index("Dashboard overview and performance"),
        )
        self.assertRegex(
            dashboard_html,
            r'<details class="card border-0 shadow-sm mt-3 mb-4">\s*<summary',
        )

    def test_shop_can_cancel_public_rider_assignment_and_retry_offers(self):
        owner = User.objects.create_user(
            username="owner_cancel_public_assignment",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="rider_cancel_public_assignment",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Cancel Public Assignment Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Public Cancel Rider",
            phone="9876543210",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-CANCEL-PUBLIC",
            work_area={
                "type": "circle",
                "center": {"lat": 23.0, "lng": 72.0},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
            is_online=True,
        )
        from .public_riders import sync_public_rider_shop_memberships

        sync_public_rider_shop_memberships(application)
        rider = ShopStaff.objects.get(
            shop=shop,
            user=rider_user,
            is_public_rider=True,
        )
        rider.last_location = {
            "lat": 23.0,
            "lng": 72.0,
            "observed_at": timezone.now().timestamp(),
        }
        rider.save(update_fields=["last_location", "updated_at"])
        order = Order.objects.create(
            order_id="ORD-CANCEL-PUBLIC-ASSIGNMENT",
            user="cancel_public_assignment_customer",
            status="RIDER_ASSIGNED",
            placed_location={"lat": 23.001, "lng": 72.001},
            delivery_location="23.001,72.001",
            assigned_rider=rider_user.username,
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="210.00",
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
            public_rider_network_requested=True,
            rider_pickup_otp="123456",
        )

        self.client.force_login(owner)
        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertContains(detail_response, "Cancel rider assignment")
        response = self.client.post(
            reverse(
                "cancel_rider_assignment",
                kwargs={"shop_order_id": shop_order.pk},
            ),
            {"reason": "Need another available rider"},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "READY")
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertIsNone(shop_order.rider_accepted_at)
        self.assertEqual(shop_order.rider_pickup_otp, "")
        self.assertTrue(shop_order.public_rider_network_requested)
        self.assertFalse(shop_order.public_rider_fallback_to_staff)
        self.assertIsNone(shop_order.public_rider_offer_user_id)
        self.assertIsNotNone(shop_order.public_rider_offer_retry_at)
        self.assertEqual(
            set(shop_order.public_rider_offer_declined_until),
            {str(rider_user.pk)},
        )
        self.assertEqual(order.status, "READY")
        self.assertTrue(
            Notification.objects.filter(
                user=rider_user,
                title="Delivery assignment cancelled",
            ).exists()
        )

    def test_authorized_shop_staff_can_cancel_shop_rider_assignment(self):
        owner = User.objects.create_user(
            username="owner_cancel_staff_assignment",
            password="test-password",
        )
        manager_user = User.objects.create_user(
            username="manager_cancel_assignment",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="rider_cancel_staff_assignment",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Cancel Staff Assignment Shop",
            owner=owner,
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        ShopStaff.objects.create(
            shop=shop,
            user=manager_user,
            role="MANAGER",
            is_active=True,
            can_assign_staff=True,
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-CANCEL-STAFF-ASSIGNMENT",
            user="cancel_staff_assignment_customer",
            status="RIDER_ASSIGNED",
            delivery_location="23.001,72.001",
            assigned_rider=rider_user.username,
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
        )

        unauthorized_user = User.objects.create_user(
            username="staff_without_assign_permission",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=shop,
            user=unauthorized_user,
            role="PACKER",
            is_active=True,
            can_assign_staff=False,
        )
        self.client.force_login(unauthorized_user)
        denied_response = self.client.post(
            reverse(
                "cancel_rider_assignment",
                kwargs={"shop_order_id": shop_order.pk},
            ),
        )
        self.assertEqual(denied_response.status_code, 403)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, rider.pk)

        self.client.force_login(manager_user)
        response = self.client.post(
            reverse(
                "cancel_rider_assignment",
                kwargs={"shop_order_id": shop_order.pk},
            ),
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "READY")
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertFalse(shop_order.public_rider_network_requested)
        self.assertTrue(shop_order.public_rider_fallback_to_staff)
        self.assertEqual(order.status, "READY")

    def test_rider_can_release_assigned_delivery_before_pickup(self):
        owner = User.objects.create_user(username="owner_cancel", password="pass1234")
        rider_user = User.objects.create_user(username="rider_cancel", password="pass1234")
        shop = Shop.objects.create(name="Cancel Shop", owner=owner)
        rider = ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-CANCEL-1",
            user="customer_cancel",
            delivery_address="Lane 2",
            delivery_location="21.0,71.0",
            delivery_contact_no="9911223344",
            status="READY",
            payment_status="COD",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="210.00",
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
        )

        self.client.force_login(rider_user)
        dashboard_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(dashboard_response, "Release delivery")
        self.assertContains(
            dashboard_response,
            "until shop staff verifies the pickup OTP",
        )
        response = self.client.post(
            reverse("rider_cancel_order", kwargs={"shop_order_id": shop_order.id}),
            {"reason": "Vehicle issue", "latitude": "22.2", "longitude": "73.2"},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "READY")
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertIsNone(shop_order.rider_accepted_at)
        self.assertEqual(shop_order.rider_cancel_reason, "Vehicle issue")
        self.assertEqual(shop_order.rider_last_location["lat"], 22.2)
        self.assertEqual(order.status, "READY")
        self.assertEqual(order.cancellation_source, "")
        self.assertEqual(
            response["Location"],
            f"{reverse('rider_dashboard')}?view=available",
        )
        self.assertFalse(OrderStatusHistory.objects.filter(
            shop_order=shop_order,
            status="CANCELLED",
        ).exists())

        next_rider_user = User.objects.create_user(
            username="rider_cancel_reoffer",
            password="test-pass",
        )
        ShopStaff.objects.create(
            shop=shop,
            user=next_rider_user,
            role="RIDER",
            is_active=True,
        )
        self.client.force_login(next_rider_user)
        available_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertContains(available_response, "ORD-CANCEL-1")

    def test_rider_can_release_after_five_minutes_but_not_after_pickup(self):
        owner = User.objects.create_user(
            username="owner_cancel_window",
            password="test-pass",
        )
        rider_user = User.objects.create_user(
            username="rider_cancel_window",
            password="test-pass",
        )
        shop = Shop.objects.create(name="Cancel Window Shop", owner=owner)
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-CANCEL-WINDOW",
            user="customer_cancel_window",
            status="RIDER_ASSIGNED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="100.00",
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
            rider_accepted_at=timezone.now() - timedelta(minutes=5, seconds=1),
        )

        self.client.force_login(rider_user)
        expired_response = self.client.post(
            reverse("rider_cancel_order", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(expired_response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "READY")
        self.assertIsNone(shop_order.assigned_rider_id)
        self.assertEqual(order.status, "READY")

        shop_order.status = "PICKED_UP"
        shop_order.assigned_rider = rider
        shop_order.rider_accepted_at = timezone.now()
        shop_order.save(update_fields=[
            "status",
            "assigned_rider",
            "rider_accepted_at",
        ])
        picked_up_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertNotContains(picked_up_dashboard, "Release delivery")
        pickup_response = self.client.post(
            reverse("rider_cancel_order", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(pickup_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")
        self.assertEqual(shop_order.assigned_rider_id, rider.pk)

    def test_signup_creates_client_profile_with_random_otp(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "newotpuser",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
                "accept_legal_documents": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="newotpuser")
        self.assertTrue(hasattr(user, "client_profile"))
        self.assertRegex(user.client_profile.verification_otp, r"^\d{4}$")
        self.assertEqual(
            LegalAcceptance.objects.filter(user=user).count(),
            5,
        )

    def test_signup_requires_legal_acceptance(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "declinedtermsuser",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="declinedtermsuser").exists())
        self.assertContains(response, "This field is required.")

    def test_first_shop_creation_requires_and_records_legal_acceptance(self):
        owner = User.objects.create_user(
            username="first_shop_legal_owner",
            password="test-password",
        )
        self.client.force_login(owner)
        shop_add_url = reverse("shop_add")
        form_response = self.client.get(shop_add_url)
        self.assertEqual(form_response.status_code, 200)
        self.assertTrue(form_response.context["requires_legal_acceptance"])
        self.assertContains(form_response, reverse("terms"))

        shop_data = {
            "name": "Legal Consent Shop",
            "storefront_slug": "legal-consent-shop",
            "market_category": "DAILY MARKET",
            "shop_category": "OTHER",
            "delivery_fee": "0.00",
        }
        rejected = self.client.post(shop_add_url, shop_data)
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(Shop.objects.filter(owner=owner).exists())

        shop_data["accept_legal_documents"] = "on"
        accepted = self.client.post(shop_add_url, shop_data)
        self.assertRedirects(accepted, reverse("shop_list"))
        self.assertTrue(Shop.objects.filter(owner=owner).exists())
        self.assertEqual(
            LegalAcceptance.objects.filter(user=owner).count(),
            5,
        )

    def test_shop_owner_can_enable_customer_otp_verification(self):
        owner = User.objects.create_user(username="owner_verify_opt", password="pass1234")
        shop = Shop.objects.create(name="Verify OTP Shop", owner=owner)

        self.client.force_login(owner)
        settings_page = self.client.get(
            reverse("shop_settings", kwargs={"shop_id": shop.id})
        )
        self.assertContains(settings_page, 'name="allow_scheduled_delivery"')
        response = self.client.post(
            reverse("shop_settings", kwargs={"shop_id": shop.id}),
            {
                "require_customer_otp_verification": "on",
                "delivery_eta_reminder_minutes": "4",
                "rider_nearby_notification_meters": "250",
                "delivery_fee": "4.25",
                "allow_scheduled_delivery": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        shop.refresh_from_db()
        self.assertTrue(shop.require_customer_otp_verification)
        self.assertEqual(shop.delivery_eta_reminder_minutes, 4)
        self.assertEqual(shop.rider_nearby_notification_meters, 250)
        self.assertEqual(shop.delivery_fee, Decimal("4.25"))
        self.assertTrue(shop.allow_scheduled_delivery)

    def test_gst_registered_shop_can_configure_delivery_tax_in_shop_settings(self):
        owner = User.objects.create_user(
            username="delivery_tax_settings_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Delivery Tax Settings Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
        )
        self.client.force_login(owner)

        settings_response = self.client.get(
            reverse("shop_settings", kwargs={"shop_id": shop.pk})
        )
        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, 'name="delivery_hsn_code"')
        self.assertContains(settings_response, 'name="delivery_gst_rate"')

        save_response = self.client.post(
            reverse("shop_settings", kwargs={"shop_id": shop.pk}),
            {
                "delivery_fee": "12.00",
                "delivery_hsn_code": "996511",
                "delivery_gst_rate": "18.00",
                "delivery_eta_reminder_minutes": "3",
                "rider_nearby_notification_meters": "100",
            },
        )
        self.assertEqual(save_response.status_code, 302)
        shop.refresh_from_db()
        self.assertEqual(shop.delivery_hsn_code, "996511")
        self.assertEqual(shop.delivery_gst_rate, Decimal("18.00"))

        invalid_response = self.client.post(
            reverse("shop_settings", kwargs={"shop_id": shop.pk}),
            {
                "delivery_fee": "12.00",
                "delivery_hsn_code": "bad-code",
                "delivery_gst_rate": "18.00",
                "delivery_eta_reminder_minutes": "3",
                "rider_nearby_notification_meters": "100",
            },
        )
        self.assertEqual(invalid_response.status_code, 302)
        shop.refresh_from_db()
        self.assertEqual(shop.delivery_hsn_code, "996511")
        self.assertEqual(shop.delivery_gst_rate, Decimal("18.00"))

    def test_shop_owner_cannot_save_negative_delivery_fee(self):
        owner = User.objects.create_user(
            username="owner_negative_delivery_fee",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Negative Fee Shop",
            owner=owner,
            delivery_fee=Decimal("5.00"),
        )
        self.client.force_login(owner)

        response = self.client.post(
            reverse("shop_settings", kwargs={"shop_id": shop.id}),
            {
                "delivery_eta_reminder_minutes": "3",
                "rider_nearby_notification_meters": "100",
                "delivery_fee": "-1.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        shop.refresh_from_db()
        self.assertEqual(shop.delivery_fee, Decimal("5.00"))

    def test_tracking_updates_notify_once_inside_shop_eta_lead_window(self):
        from .models import OrderStatusHistory

        customer = User.objects.create_user(username="eta_customer", password="test-password")
        ClientProfile.objects.create(user=customer, name="ETA Customer")
        owner = User.objects.create_user(username="eta_owner", password="test-password")
        shop = Shop.objects.create(name="ETA Shop", owner=owner, delivery_eta_reminder_minutes=3)
        order = Order.objects.create(
            order_id="ORD-ETA-REMINDER",
            user=customer.username,
            status="CONFIRMED",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="PACKING",
            packing_minutes=5,
            rider_eta_minutes=10,
        )
        history = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="CONFIRMED",
        )
        OrderStatusHistory.objects.filter(pk=history.pk).update(
            created_at=timezone.now() - timedelta(minutes=12),
        )
        packing_history = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="PACKING",
        )
        OrderStatusHistory.objects.filter(pk=packing_history.pk).update(
            created_at=timezone.now() - timedelta(minutes=7),
        )

        self.client.force_login(customer)
        url = reverse("order_tracking_updates", kwargs={"order_id": order.order_id})
        first_response = self.client.post(url)
        second_response = self.client.post(url)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        packing_timer = first_response.json()["shop_orders"][0]["timers"]["packing"]
        self.assertGreaterEqual(packing_timer["elapsed_seconds"], 7 * 60)
        self.assertEqual(packing_timer["estimated_seconds"], 5 * 60)
        self.assertEqual(
            Notification.objects.filter(user=customer, title="Your delivery is almost due").count(),
            1,
        )
        tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.order_id}),
        )
        self.assertContains(tracking_response, "Packing time")
        self.assertContains(tracking_response, "Delivery time")
        self.assertContains(tracking_response, "Time taken:")
        self.assertContains(tracking_response, "window.setInterval(updateVisibleTimers, 1000);")
        self.assertContains(tracking_response, 'new WebSocket(`${scheme}//${window.location.host}/ws/live/`)')
        self.assertContains(tracking_response, "Live updates connected")
        self.assertContains(tracking_response, "Reconnecting to live updates")
        self.assertContains(tracking_response, "300000")
        self.assertNotContains(tracking_response, "window.setInterval(refresh, 20000);")
        self.assertNotContains(tracking_response, "Calculating")
        shop_order.refresh_from_db()
        self.assertIsNotNone(shop_order.eta_reminder_sent_at)

    def test_tracking_eta_changes_to_delivery_countdown_after_pickup(self):
        from .models import OrderStatusHistory
        from .views import _get_customer_eta

        customer = User.objects.create_user(username="pickup_eta_customer", password="test-password")
        ClientProfile.objects.create(user=customer, name="Pickup ETA Customer")
        owner = User.objects.create_user(username="pickup_eta_owner", password="test-password")
        shop = Shop.objects.create(name="Pickup ETA Shop", owner=owner)
        order = Order.objects.create(
            order_id="ORD-PICKUP-ETA",
            user=customer.username,
            status="OUT_FOR_DELIVERY",
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="OUT_FOR_DELIVERY",
            packing_minutes=20,
            rider_eta_minutes=15,
        )
        history = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="OUT_FOR_DELIVERY",
        )
        changed_at = timezone.now() - timedelta(minutes=5)
        OrderStatusHistory.objects.filter(pk=history.pk).update(created_at=changed_at)

        eta = _get_customer_eta(shop_order)

        self.assertEqual(eta["mode"], "delivery")
        self.assertEqual(eta["target"], changed_at + timedelta(minutes=15))
        self.assertGreater(eta["remaining_seconds"], 0)

        pickup_history = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="PICKED_UP",
        )
        OrderStatusHistory.objects.filter(pk=pickup_history.pk).update(
            created_at=changed_at - timedelta(minutes=2),
        )
        eta_after_pickup = _get_customer_eta(shop_order)
        self.assertEqual(
            eta_after_pickup["target"],
            changed_at + timedelta(minutes=15),
        )

        self.client.force_login(customer)
        tracking_response = self.client.get(
            reverse("order_tracking", kwargs={"order_id": order.order_id}),
        )
        self.assertContains(tracking_response, "Delivery time")
        self.assertContains(tracking_response, "Delivery time")

    def test_picked_up_eta_waits_until_all_parcels_are_collected(self):
        from .models import OrderStatusHistory
        from .views import _get_customer_eta

        owner = User.objects.create_user(username="picked_eta_owner", password="test-password")
        shop = Shop.objects.create(name="Picked ETA Shop", owner=owner)
        order = Order.objects.create(order_id="ORD-PICKED-ETA", user="picked_eta_customer")
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="PICKED_UP",
            packing_minutes=20,
            rider_eta_minutes=15,
        )
        picked_up_at = timezone.now() - timedelta(minutes=4)
        history = OrderStatusHistory.objects.create(shop_order=shop_order, status="PICKED_UP")
        OrderStatusHistory.objects.filter(pk=history.pk).update(created_at=picked_up_at)

        eta = _get_customer_eta(shop_order)

        self.assertEqual(eta["mode"], "")
        self.assertIsNone(eta["target"])
        self.assertIsNone(eta["remaining_seconds"])

    def test_multi_order_rider_eta_schedules_near_stop_first_with_five_minute_dropoffs(self):
        import math

        from .models import OrderStatusHistory
        from .utils import distance_km_between_points
        from .views import _get_customer_eta, _get_rider_route_delivery_targets

        owner = User.objects.create_user(username="route_eta_owner", password="route-eta-test-password")
        rider_user = User.objects.create_user(username="route_eta_rider", password="route-eta-test-password")
        shop = Shop.objects.create(
            name="Route ETA Shop",
            owner=owner,
            location={"lat": 23.0, "lng": 72.0},
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        route_started_at = timezone.now() - timedelta(minutes=1)
        route_orders = []
        for order_id, location in (
            ("ORD-ROUTE-FAR", "23.02,72.0"),
            ("ORD-ROUTE-NEAR", "23.001,72.0"),
        ):
            order = Order.objects.create(
                order_id=order_id,
                user="route_eta_customer",
                delivery_location=location,
            )
            shop_order = ShopOrder.objects.create(
                order=order,
                shop=shop,
                assigned_rider=rider,
                status="OUT_FOR_DELIVERY",
                rider_eta_minutes=10,
            )
            history = OrderStatusHistory.objects.create(
                shop_order=shop_order,
                status="OUT_FOR_DELIVERY",
            )
            OrderStatusHistory.objects.filter(pk=history.pk).update(
                created_at=route_started_at,
            )
            route_orders.append(shop_order)

        targets = _get_rider_route_delivery_targets(rider, shop)
        near_order = next(item for item in route_orders if item.order.order_id.endswith("NEAR"))
        far_order = next(item for item in route_orders if item.order.order_id.endswith("FAR"))

        self.assertLess(targets[near_order.id], targets[far_order.id])
        near_eta_minutes = (targets[near_order.id] - route_started_at).total_seconds() / 60
        first_leg_minutes = math.ceil(
            distance_km_between_points(23.0, 72.0, 23.001, 72.0) / 20 * 60
        )
        next_leg_minutes = math.ceil(
            distance_km_between_points(23.001, 72.0, 23.02, 72.0) / 20 * 60
        )
        self.assertEqual(near_eta_minutes, first_leg_minutes + 5)
        self.assertEqual(
            (targets[far_order.id] - targets[near_order.id]).total_seconds() / 60,
            next_leg_minutes + 5,
        )
        self.assertEqual(
            _get_customer_eta(far_order)["target"],
            targets[far_order.id],
        )

    def test_stage_timers_start_from_packing_and_out_for_delivery_status_changes(self):
        from .models import OrderStatusHistory
        from .views import _get_customer_timers

        owner = User.objects.create_user(username="stage_timer_owner", password="test-password")
        shop = Shop.objects.create(name="Stage Timer Shop", owner=owner)
        order = Order.objects.create(order_id="ORD-STAGE-TIMER", user="stage_timer_customer")
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            status="PACKING",
            packing_minutes=12,
            rider_eta_minutes=18,
        )
        now = timezone.now()
        packing_changed_at = now - timedelta(minutes=2)
        packing_history = OrderStatusHistory.objects.create(shop_order=shop_order, status="PACKING")
        OrderStatusHistory.objects.filter(pk=packing_history.pk).update(created_at=packing_changed_at)

        timers = _get_customer_timers(shop_order, now)
        self.assertEqual(timers["packing"]["state"], "active")
        self.assertEqual(timers["packing"]["remaining_seconds"], 10 * 60)
        self.assertEqual(timers["packing"]["elapsed_seconds"], 2 * 60)
        self.assertEqual(timers["packing"]["estimated_seconds"], 12 * 60)
        self.assertEqual(timers["delivery"]["state"], "waiting")

        shop_order.status = "OUT_FOR_DELIVERY"
        shop_order.save(update_fields=["status"])
        delivery_changed_at = now - timedelta(minutes=3)
        delivery_history = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="OUT_FOR_DELIVERY",
        )
        OrderStatusHistory.objects.filter(pk=delivery_history.pk).update(created_at=delivery_changed_at)
        timers = _get_customer_timers(shop_order, now)
        self.assertEqual(timers["packing"]["state"], "complete")
        self.assertEqual(timers["delivery"]["state"], "active")
        self.assertEqual(timers["delivery"]["remaining_seconds"], 15 * 60)
        self.assertEqual(timers["delivery"]["elapsed_seconds"], 3 * 60)
        self.assertEqual(timers["delivery"]["estimated_seconds"], 18 * 60)

        overdue_timers = _get_customer_timers(
            shop_order,
            delivery_changed_at + timedelta(minutes=18),
        )
        self.assertLess(overdue_timers["delivery"]["remaining_seconds"], 0)

        shop_order.status = "DELIVERED"
        terminal_timers = _get_customer_timers(shop_order, now)
        self.assertEqual(terminal_timers["packing"]["remaining_seconds"], 0)
        self.assertEqual(terminal_timers["delivery"]["remaining_seconds"], 0)
        self.assertFalse(terminal_timers["delivery"]["elapsed_active"])

    def test_tracking_hides_eta_for_pending_and_terminal_orders_and_collapses_older_statuses(self):
        from .models import OrderStatusHistory

        customer = User.objects.create_user(username="history_customer", password="test-password")
        ClientProfile.objects.create(user=customer, name="History Customer")
        owner = User.objects.create_user(username="history_owner", password="test-password")
        shop = Shop.objects.create(name="History Shop", owner=owner)
        self.client.force_login(customer)

        for order_status in ("PENDING", "CANCELLED", "DELIVERED"):
            order = Order.objects.create(
                order_id=f"ORD-HISTORY-{order_status}",
                user=customer.username,
                status=order_status,
            )
            shop_order = ShopOrder.objects.create(
                order=order,
                shop=shop,
                status=order_status,
                packing_minutes=5,
                rider_eta_minutes=10,
            )
            OrderStatusHistory.objects.create(shop_order=shop_order, status="CONFIRMED")
            OrderStatusHistory.objects.create(shop_order=shop_order, status=order_status)
            response = self.client.get(
                reverse("order_tracking", kwargs={"order_id": order.order_id}),
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "data-shop-order-eta=")
            self.assertContains(response, "Show earlier updates (1)")
            self.assertContains(response, "data-stage-timer-comparison")
            self.assertContains(response, "data-timer-estimated-seconds")
            expected_latest_label = "PENDING" if order_status == "PENDING" else order_status.title()
            self.assertContains(response, expected_latest_label)

    def test_rider_nearby_update_notifies_customer_once_with_configured_radius(self):
        customer = User.objects.create_user(username="nearby_customer", password="test-password")
        owner = User.objects.create_user(username="nearby_owner", password="test-password")
        rider_user = User.objects.create_user(username="nearby_rider", password="test-password")
        shop = Shop.objects.create(
            name="Nearby Shop",
            owner=owner,
            rider_nearby_notification_meters=100,
        )
        rider = ShopStaff.objects.create(
            shop=shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-NEARBY",
            user=customer.username,
            delivery_location="23.0,72.0",
            placed_location={"lat": 23.0, "lng": 72.0},
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            assigned_rider=rider,
            status="OUT_FOR_DELIVERY",
        )
        self.client.force_login(rider_user)
        url = reverse("update_rider_location", kwargs={"shop_order_id": shop_order.id})
        first_response = self.client.post(url, {"latitude": "23.0", "longitude": "72.0"})
        second_response = self.client.post(url, {"latitude": "23.0", "longitude": "72.0"})

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(
            Notification.objects.filter(user=customer, title="Your rider is nearby").count(),
            1,
        )
        shop_order.refresh_from_db()
        self.assertIsNotNone(shop_order.rider_nearby_notified_at)

    def test_rider_can_complete_order_after_customer_otp_verification(self):
        owner = User.objects.create_user(username="owner_complete", password="pass1234")
        rider_user = User.objects.create_user(username="rider_complete", password="pass1234")
        customer_user = User.objects.create_user(username="customer_complete", password="pass1234")
        client_profile = ClientProfile.objects.create(user=customer_user, name="Customer Complete", verification_otp="1234")
        shop = Shop.objects.create(name="Complete Shop", owner=owner, require_customer_otp_verification=True)
        rider = ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)
        order = Order.objects.create(
            order_id="ORD-COMPLETE-1",
            user=customer_user.username,
            delivery_address="Complete Way",
            delivery_location="23.0,72.0",
            delivery_contact_no="9090909090",
            status="OUT_FOR_DELIVERY",
            payment_status="PAID",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount="200.00",
            status="OUT_FOR_DELIVERY",
            assigned_rider=rider,
        )

        self.client.force_login(rider_user)
        response = self.client.post(
            reverse("verify_customer_otp", kwargs={"shop_order_id": shop_order.id}),
            {"otp": "1234", "remark": "Delivered safely", "latitude": "22.5", "longitude": "72.5"},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertTrue(shop_order.customer_otp_verified)
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        self.assertIsNone(shop_order.delivery_completed_at)
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "DELIVERED")
        self.assertIsNotNone(shop_order.delivery_completed_at)

    def test_job_routes_resolve(self):
        self.assertEqual(reverse("job_openings"), "/jobs/")
        self.assertEqual(reverse("my_job_applications"), "/jobs/my-applications/")
        self.assertEqual(reverse("job_apply", kwargs={"job_id": 1}), "/jobs/1/apply/"),


class OrderItemEditingTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="order_edit_customer",
            password="test-password",
        )
        self.client_profile = ClientProfile.objects.create(
            user=self.customer,
            wallet_balance=Decimal("100.00"),
        )
        self.owner = User.objects.create_user(
            username="order_edit_owner",
            password="test-password",
        )
        self.shop = Shop.objects.create(
            name="Order Edit Shop",
            owner=self.owner,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Order Edit Product",
            price=Decimal("20.00"),
            unit="piece",
            product_category="OTHER",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            price=Decimal("20.00"),
            stock=8,
            is_active=True,
        )

    def make_order(self, payment_type="COD", *, with_gst=False, with_coupon=False):
        if with_gst:
            self.shop.gstin = "24ABCDE1234F1Z5"
            self.shop.gst_state_code = "24"
            self.shop.save(update_fields=["gstin", "gst_state_code"])
            self.product.gst_rate = Decimal("5.00")
            self.product.hsn_code = "12345678"
            self.product.save(update_fields=["gst_rate", "hsn_code"])
        coupon_code = ""
        discount = Decimal("0.00")
        if with_coupon:
            coupon_code = "EDIT10"
            now = timezone.now()
            Coupon.objects.create(
                code=coupon_code,
                discount_type="PERCENT",
                discount_value=Decimal("10.00"),
                minimum_order_amount=Decimal("0.00"),
                valid_from=now - timedelta(days=1),
                valid_until=now + timedelta(days=1),
            )
            discount = Decimal("4.00")

        address = type("AddressSnapshot", (), {"state_code": "24", "gstin": ""})()
        gst_item = {
            "item": {"total": Decimal("40.00")},
            "total": Decimal("40.00"),
            "shop": self.shop,
            "product": self.product,
            "gst_rate": self.product.gst_rate,
            "hsn_code": self.product.hsn_code,
        }
        gst = seller_views._build_gst_breakdown(
            [gst_item],
            address,
            discount,
            require_state=with_gst,
        )
        extra = gst["total_gst"] + gst["delivery_total"]
        charges = seller_views._checkout_charge_totals(
            Decimal("40.00"),
            discount,
            Decimal("0.00"),
            payment_type,
            extra,
        )
        total = (
            Decimal("40.00")
            - discount
            + charges["platform_charge"]
            + charges["razorpay_fee"]
            + extra
        )
        wallet_used = total if payment_type == "WALLET" else Decimal("0.00")
        if wallet_used:
            self.client_profile.wallet_balance -= wallet_used
            self.client_profile.save(update_fields=["wallet_balance", "updated_at"])
        order = Order.objects.create(
            order_id=f"ORDER-EDIT-{payment_type}-{with_gst}-{with_coupon}",
            user=self.customer.username,
            payment_type=payment_type,
            payment_status="PAID" if payment_type == "RAZORPAY" else "COD",
            is_payment_done=payment_type == "RAZORPAY",
            payment_id="pay_existing123" if payment_type == "RAZORPAY" else "",
            payment_amount=total if payment_type != "WALLET" else Decimal("0.00"),
            finally_received_amount=total if payment_type == "RAZORPAY" else Decimal("0.00"),
            final_billing_amount=total,
            wallet_amount_used=wallet_used,
            coupon_code=coupon_code,
            coupon_discount_amount=discount,
            platform_charge_amount=charges["platform_charge"],
            razorpay_fee_amount=charges["razorpay_fee"],
            gst_amount=gst["total_gst"],
        )
        gst_shop = gst["shops"][self.shop.pk]
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="PLACED",
            total_amount=Decimal("40.00"),
            product_count=2,
            delivery_fee=gst_shop["delivery_fee"],
            seller_gstin=self.shop.gstin,
            seller_state_code=self.shop.gst_state_code,
            buyer_state_code="24" if with_gst else "",
            taxable_amount=gst_shop["taxable_amount"],
            cgst_amount=gst_shop["cgst_amount"],
            sgst_amount=gst_shop["sgst_amount"],
            igst_amount=gst_shop["igst_amount"],
            delivery_gst_amount=gst_shop["delivery_gst_amount"],
        )
        self.item = OrderItem.objects.create(
            shop_order=shop_order,
            product=self.product,
            product_shop=self.assignment,
            product_name=self.product.product_name,
            sku="EDIT-1",
            quantity=2,
            price=Decimal("20.00"),
            total=Decimal("40.00"),
            hsn_code=self.product.hsn_code,
            gst_rate=self.product.gst_rate,
            taxable_amount=gst_item.get("taxable_amount", Decimal("40.00")),
            cgst_amount=gst_item.get("cgst_amount", Decimal("0.00")),
            sgst_amount=gst_item.get("sgst_amount", Decimal("0.00")),
            igst_amount=gst_item.get("igst_amount", Decimal("0.00")),
        )
        return order, shop_order

    def test_customer_quantity_increase_updates_reserved_stock_and_cod_due(self):
        order, _shop_order = self.make_order()
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "3"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assignment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.item.quantity, 3)
        self.assertEqual(self.assignment.stock, 7)
        self.assertEqual(order.payment_amount, order.final_billing_amount)
        self.assertEqual(order.platform_charge_amount, seller_views._checkout_charge_totals(
            Decimal("60.00"), Decimal("0.00"), Decimal("0.00"), "COD",
            Decimal(str(settings.DELIVERY_SERVICE_FEE)),
        )["platform_charge"])
        self.assertTrue(StockMovement.objects.filter(
            product_shop=self.assignment,
            movement_type="ADJUSTMENT",
            quantity=-1,
        ).exists())

    def test_customer_order_edit_returns_json_for_immediate_submission(self):
        order, _shop_order = self.make_order()
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "3"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["redirect_url"], reverse("order_list"))
        self.assertEqual(
            response.json()["revised_total"],
            str(Order.objects.get(pk=order.pk).final_billing_amount),
        )

    def test_shop_manager_quantity_decrease_restores_stock_and_notifies_customer(self):
        order, shop_order = self.make_order()
        manager = User.objects.create_user(
            username="order_edit_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager,
            role="MANAGER",
            is_active=True,
        )
        self.client.force_login(manager)

        response = self.client.post(
            reverse("shop_edit_order_items", kwargs={"shop_order_id": shop_order.id}),
            {f"quantity_{self.item.id}": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assignment.refresh_from_db()
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.assignment.stock, 9)
        self.assertEqual(shop_order.product_count, 1)
        self.assertTrue(Notification.objects.filter(
            user=self.customer,
            title="Your order was updated by the shop",
            related_url=f"/orders/{order.order_id}/tracking/",
        ).exists())
        self.assertTrue(StockMovement.objects.filter(
            product_shop=self.assignment,
            movement_type="ADJUSTMENT",
            quantity=1,
        ).exists())

    def test_authorized_order_manager_can_edit_shop_items(self):
        _order, shop_order = self.make_order()
        order_manager = User.objects.create_user(
            username="order_edit_order_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=order_manager,
            role="ORDER",
            can_view_orders=True,
            is_active=True,
        )
        self.client.force_login(order_manager)

        response = self.client.post(
            reverse("shop_edit_order_items", kwargs={"shop_order_id": shop_order.id}),
            {f"quantity_{self.item.id}": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)

    def test_wallet_order_increase_is_debited_from_wallet(self):
        order, _shop_order = self.make_order("WALLET")
        old_total = order.final_billing_amount
        old_balance = self.client_profile.wallet_balance
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "3"},
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.client_profile.refresh_from_db()
        self.assertGreater(order.final_billing_amount, old_total)
        self.assertEqual(order.wallet_amount_used, order.final_billing_amount)
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertEqual(
            self.client_profile.wallet_balance,
            old_balance - (order.final_billing_amount - old_total),
        )

    @override_settings(RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="rzp_test_secret")
    def test_razorpay_increase_is_rejected_without_changing_order_or_stock(self):
        order, _shop_order = self.make_order("RAZORPAY")
        old_total = order.final_billing_amount
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "3"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assignment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.item.quantity, 2)
        self.assertEqual(self.assignment.stock, 8)
        self.assertEqual(order.final_billing_amount, old_total)
        self.assertContains(
            self.client.get(reverse("order_list")),
            "checkout verification only creates a new cart order",
            status_code=200,
        )


    @override_settings(RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="rzp_test_secret")
    @patch("seller.views.requests.post")
    def test_razorpay_decrease_requires_confirmed_refund_and_updates_billing(self, post):
        post.return_value = Mock()
        order, _shop_order = self.make_order("RAZORPAY")
        old_total = order.final_billing_amount
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assignment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.assignment.stock, 9)
        self.assertLess(order.final_billing_amount, old_total)
        self.assertEqual(order.payment_status, "PARTIALLY_REFUNDED")
        self.assertEqual(order.finally_received_amount, order.final_billing_amount)
        self.assertIn("/payments/pay_existing123/refund", post.call_args.args[0])

    @override_settings(RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="rzp_test_secret")
    @patch("seller.views.requests.post")
    def test_unconfirmed_razorpay_refund_rolls_back_order_and_stock_edits(self, post):
        post.side_effect = seller_views.requests.RequestException("refund unavailable")
        order, _shop_order = self.make_order("RAZORPAY")
        old_total = order.final_billing_amount
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assignment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.item.quantity, 2)
        self.assertEqual(self.assignment.stock, 8)
        self.assertEqual(order.final_billing_amount, old_total)

    def test_customer_edit_recalculates_coupon_gst_and_delivery_snapshots(self):
        order, _shop_order = self.make_order(with_gst=True, with_coupon=True)
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.coupon_discount_amount, Decimal("2.00"))
        self.assertEqual(self.item.taxable_amount, Decimal("18.00"))
        self.assertEqual(self.item.cgst_amount + self.item.sgst_amount, Decimal("0.90"))
        self.assertEqual(order.gst_amount, Decimal("0.90") + order.shop_orders.first().delivery_gst_amount)
        self.assertGreater(order.platform_charge_amount, Decimal("0.00"))

    def test_customer_cannot_edit_after_a_shop_enters_packing(self):
        order, shop_order = self.make_order()
        shop_order.status = "PACKING"
        shop_order.save(update_fields=["status"])
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("customer_edit_placed_order", kwargs={"order_id": order.order_id}),
            {f"quantity_{self.item.id}": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(self.item.quantity, 2)
        self.assertEqual(self.assignment.stock, 8)


class RiderPartialReturnTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="partial_return_owner",
            password="test-password",
        )
        self.rider_user = User.objects.create_user(
            username="partial_return_rider",
            password="test-password",
        )
        self.customer_user = User.objects.create_user(
            username="partial_return_customer",
            password="test-password",
        )
        self.profile = ClientProfile.objects.create(
            user=self.customer_user,
            wallet_balance=Decimal("0.00"),
        )
        self.shop = Shop.objects.create(
            name="Partial Return Shop",
            owner=self.owner,
        )
        self.rider = ShopStaff.objects.create(
            shop=self.shop,
            user=self.rider_user,
            role="RIDER",
            is_active=True,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Return Test Item",
            price=Decimal("20.00"),
            unit="piece",
            product_category="OTHER",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            price=Decimal("20.00"),
            stock=0,
            is_active=True,
        )

    def make_delivery(self, payment_type):
        total_items = Decimal("100.00")
        gst = seller_views._build_gst_breakdown(
            [{
                "total": total_items,
                "shop": self.shop,
                "product": self.product,
                "gst_rate": self.product.gst_rate,
                "hsn_code": self.product.hsn_code,
            }],
            type("BuyerAddress", (), {"state_code": "", "gstin": ""})(),
            delivery_fees={self.shop.pk: Decimal(str(settings.DELIVERY_SERVICE_FEE))},
        )
        charges = seller_views._checkout_charge_totals(
            total_items,
            Decimal("0.00"),
            Decimal("0.00"),
            payment_type,
            gst["total_gst"] + gst["delivery_total"],
        )
        billing_total = (
            total_items
            + charges["platform_charge"]
            + charges["razorpay_fee"]
            + gst["total_gst"]
            + gst["delivery_total"]
        ).quantize(Decimal("0.01"))
        order = Order.objects.create(
            order_id=f"PARTIAL-RETURN-{payment_type}-{Order.objects.count() + 1}",
            user=self.customer_user.username,
            payment_type=payment_type,
            payment_status="PAID" if payment_type == "RAZORPAY" else "COD",
            payment_id="pay_partialreturn123" if payment_type == "RAZORPAY" else "",
            is_payment_done=payment_type == "RAZORPAY",
            payment_amount=Decimal("0.00") if payment_type == "RAZORPAY" else billing_total,
            finally_received_amount=billing_total if payment_type == "RAZORPAY" else Decimal("0.00"),
            final_billing_amount=billing_total,
            platform_charge_amount=charges["platform_charge"],
            razorpay_fee_amount=charges["razorpay_fee"],
            gst_amount=gst["total_gst"],
            status="OUT_FOR_DELIVERY",
        )
        tax_shop = gst["shops"][self.shop.pk]
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="OUT_FOR_DELIVERY",
            assigned_rider=self.rider,
            total_amount=total_items,
            product_count=5,
            delivery_fee=tax_shop["delivery_fee"],
            taxable_amount=tax_shop["taxable_amount"],
            cgst_amount=tax_shop["cgst_amount"],
            sgst_amount=tax_shop["sgst_amount"],
            igst_amount=tax_shop["igst_amount"],
            delivery_gst_amount=tax_shop["delivery_gst_amount"],
        )
        item = OrderItem.objects.create(
            shop_order=shop_order,
            product=self.product,
            product_shop=self.assignment,
            product_name=self.product.product_name,
            quantity=5,
            price=Decimal("20.00"),
            total=total_items,
            taxable_amount=total_items,
        )
        return order, shop_order, item

    def test_payment_manager_can_review_only_owner_authorized_shop_payments(self):
        order, shop_order, _item = self.make_delivery("COD")
        payment_request = CODPaymentApprovalRequest.objects.create(
            shop_order=shop_order,
            shop=self.shop,
            rider=self.rider,
            qr_amount=order.payment_amount,
            settlement_amount=order.payment_amount,
            original_cod_amount=order.payment_amount,
            upi_id="shop@example",
            status="PENDING",
        )
        payment_manager_user = User.objects.create_user(
            username="authorized_payment_manager",
        )
        payment_manager = ShopStaff.objects.create(
            shop=self.shop,
            user=payment_manager_user,
            role="PAYMENT_MANAGER",
            can_confirm_rider_payments=False,
            is_active=True,
        )
        self.client.force_login(payment_manager_user)

        self.assertEqual(
            self.client.get(
                reverse("staff_payment_manager_dashboard"),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "approve_cod_payment",
                    kwargs={"payment_request_id": payment_request.pk},
                ),
            ).status_code,
            403,
        )

        payment_manager.can_confirm_rider_payments = True
        payment_manager.save(update_fields=["can_confirm_rider_payments"])
        dashboard = self.client.get(
            reverse("staff_payment_manager_dashboard"),
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Rider payment confirmations")
        self.assertContains(dashboard, "Confirm received")
        self.assertEqual(
            list(dashboard.context["pending_payment_requests"]),
            [payment_request],
        )

        other_shop = Shop.objects.create(
            name="Not Authorized Shop",
            owner=self.owner,
        )
        other_order = Order.objects.create(
            order_id="PAYMENT-MANAGER-OTHER-SHOP",
            user=self.customer_user.username,
            payment_type="COD",
            payment_status="COD",
            payment_amount=Decimal("30.00"),
            final_billing_amount=Decimal("30.00"),
            status="OUT_FOR_DELIVERY",
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_order,
            shop=other_shop,
            status="OUT_FOR_DELIVERY",
            assigned_rider=self.rider,
            total_amount=Decimal("30.00"),
        )
        unauthorized_request = CODPaymentApprovalRequest.objects.create(
            shop_order=other_shop_order,
            shop=other_shop,
            rider=self.rider,
            qr_amount=Decimal("30.00"),
            settlement_amount=Decimal("30.00"),
            original_cod_amount=Decimal("30.00"),
            upi_id="shop@example",
            status="PENDING",
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "approve_cod_payment",
                    kwargs={"payment_request_id": unauthorized_request.pk},
                ),
            ).status_code,
            403,
        )

        approval_response = self.client.post(
            reverse(
                "approve_cod_payment",
                kwargs={"payment_request_id": payment_request.pk},
            ),
        )
        self.assertEqual(approval_response.status_code, 302)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, "APPROVED")
        self.assertEqual(payment_request.reviewed_by, payment_manager_user)

    def test_rider_can_remind_shop_every_thirty_seconds_about_pending_payment(self):
        order, shop_order, _item = self.make_delivery("COD")
        payment_request = CODPaymentApprovalRequest.objects.create(
            shop_order=shop_order,
            shop=self.shop,
            rider=self.rider,
            qr_amount=order.payment_amount,
            settlement_amount=order.payment_amount,
            original_cod_amount=order.payment_amount,
            upi_id="shop@example",
            status="PENDING",
        )
        self.client.force_login(self.rider_user)

        dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(dashboard, "Remind shop")
        self.assertContains(dashboard, reverse(
            "remind_cod_payment_reviewers",
            kwargs={"payment_request_id": payment_request.pk},
        ))

        reminder_url = reverse(
            "remind_cod_payment_reviewers",
            kwargs={"payment_request_id": payment_request.pk},
        )
        first_reminder = self.client.post(reminder_url)
        self.assertEqual(first_reminder.status_code, 302)
        payment_request.refresh_from_db()
        first_reminded_at = payment_request.last_reminded_at
        self.assertIsNotNone(first_reminded_at)
        self.assertEqual(
            Notification.objects.filter(
                user=self.owner,
                title="Rider payment reminder",
            ).count(),
            1,
        )

        throttled_reminder = self.client.post(reminder_url)
        self.assertEqual(throttled_reminder.status_code, 302)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.last_reminded_at, first_reminded_at)
        self.assertEqual(
            Notification.objects.filter(
                user=self.owner,
                title="Rider payment reminder",
            ).count(),
            1,
        )

        payment_request.last_reminded_at = timezone.now() - timedelta(seconds=31)
        payment_request.save(update_fields=["last_reminded_at"])
        second_reminder = self.client.post(reminder_url)
        self.assertEqual(second_reminder.status_code, 302)
        self.assertEqual(
            Notification.objects.filter(
                user=self.owner,
                title="Rider payment reminder",
            ).count(),
            2,
        )

    def test_public_rider_gets_no_offer_during_or_within_ten_seconds_of_delivery(self):
        from .public_rider_dispatch import eligible_public_riders
        from .public_riders import sync_public_rider_shop_memberships

        now = timezone.now()
        public_shop = Shop.objects.create(
            name="Public Rider Cooldown Shop",
            owner=self.owner,
            location={"lat": 21.17, "lng": 72.83},
            public_rider_network_enabled=True,
            rider_delivery_preference="PUBLIC",
        )
        rider_user = User.objects.create_user(
            username="public_rider_offer_cooldown",
        )
        application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Cooldown Rider",
            phone="9876543212",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-COOLDOWN-1",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        sync_public_rider_shop_memberships(application)
        rider_membership = ShopStaff.objects.get(
            shop=public_shop,
            user=rider_user,
            is_public_rider=True,
        )
        rider_membership.last_location = {
            "lat": 21.17,
            "lng": 72.83,
            "observed_at": now.timestamp(),
        }
        rider_membership.save(update_fields=["last_location", "updated_at"])

        def create_public_order(order_id, *, status="READY"):
            order = Order.objects.create(
                order_id=order_id,
                user=self.customer_user.username,
                status=status,
                placed_location={"lat": 21.18, "lng": 72.84},
                delivery_location="21.18,72.84",
            )
            return ShopOrder.objects.create(
                order=order,
                shop=public_shop,
                status=status,
                public_rider_network_requested=True,
            )

        active_order = create_public_order(
            "PUBLIC-RIDER-COOLDOWN-ACTIVE",
            status="OUT_FOR_DELIVERY",
        )
        active_order.assigned_rider = rider_membership
        active_order.save(update_fields=["assigned_rider"])
        candidate_order = create_public_order("PUBLIC-RIDER-COOLDOWN-CANDIDATE")

        with (
            patch(
                "seller.public_rider_dispatch.public_rider_service_enabled",
                return_value=True,
            ),
            patch(
                "seller.public_rider_dispatch.timezone.now",
                return_value=now,
            ),
        ):
            self.assertEqual(eligible_public_riders(candidate_order), [])

            active_order.status = "DELIVERED"
            active_order.delivery_completed_at = now
            active_order.save(update_fields=["status", "delivery_completed_at"])
            self.assertEqual(eligible_public_riders(candidate_order), [])

            active_order.delivery_completed_at = now - timedelta(seconds=12)
            active_order.save(update_fields=["delivery_completed_at"])
            self.assertEqual(
                [rider.pk for rider in eligible_public_riders(candidate_order)],
                [rider_membership.pk],
            )

    def test_cod_partial_return_reduces_final_charge_without_restocking(self):
        order, shop_order, item = self.make_delivery("COD")
        original_total = order.final_billing_amount
        self.client.force_login(self.rider_user)
        dashboard = self.client.get(reverse("rider_dashboard"), {"view": "active"})
        self.assertContains(dashboard, f'name="returned_quantity_{item.id}"')
        self.assertContains(dashboard, "Complete Delivery with Partial Return")

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {f"returned_quantity_{item.id}": "2"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.id}",
        )
        item.refresh_from_db()
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(item.returned_quantity, 2)
        self.assertEqual(shop_order.total_amount, Decimal("60.00"))
        self.assertEqual(shop_order.product_count, 3)
        self.assertEqual(shop_order.status, "DELIVERED")
        self.assertLess(order.final_billing_amount, original_total)
        self.assertEqual(order.finally_received_amount, order.final_billing_amount)
        self.assertEqual(order.payment_status, "PAID")
        self.assertTrue(Notification.objects.filter(
            user=self.customer_user,
            title="Returned items and bill updated",
        ).exists())
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )

        cash_collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        self.assertEqual(cash_collection.amount, order.finally_received_amount)
        rider_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "handover"},
        )
        self.assertContains(rider_dashboard, "Total cash COD awaiting shop handover")
        self.assertContains(rider_dashboard, f"₹{cash_collection.amount:.2f}")
        self.assertNotContains(
            self.client.get(reverse("rider_dashboard")),
            "Total cash COD awaiting shop handover",
        )

        manager_user = User.objects.create_user(
            username="cod_cash_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager_user,
            role="MANAGER",
            is_active=True,
        )
        self.client.force_login(manager_user)
        order_detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertContains(order_detail, "Confirm amount received")
        shop_orders_response = self.client.get(reverse("shop_order_list"))
        self.assertContains(shop_orders_response, "COD cash handover pending")
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(handover_response.status_code, 302)
        commission = SiteWalletTransaction.objects.get(shop_order=shop_order)
        self.assertEqual(commission.commission_rate_percent, Decimal("3.00"))
        self.assertEqual(commission.amount, Decimal("1.80"))
        wallet, _ = ShopWallet.objects.get_or_create(shop=self.shop)
        self.assertEqual(wallet.balance, Decimal("58.20"))
        handover = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="HANDOVER",
        )
        self.assertEqual(handover.amount, cash_collection.amount)
        self.assertEqual(handover.created_by, manager_user)

        duplicate_handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(duplicate_handover_response.status_code, 302)
        self.assertEqual(
            RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="HANDOVER",
            ).count(),
            1,
        )
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="CREDIT",
            ).count(),
            1,
        )
        self.client.force_login(self.rider_user)
        settled_rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(settled_rider_dashboard.context["cash_in_hand"], Decimal("0.00"))
        self.assertNotContains(settled_rider_dashboard, "Handover details")
        self.assertEqual(self.assignment.stock, 0)
        self.assertFalse(StockMovement.objects.filter(
            product_shop=self.assignment,
            movement_type="ADJUSTMENT",
        ).exists())

    def test_rider_order_item_displays_estimated_then_final_weight(self):
        _order, _shop_order, item = self.make_delivery("COD")
        item.is_approximate_weight = True
        item.estimated_weight = Decimal("1.250")
        item.unit = "kg"
        item.save(update_fields=[
            "is_approximate_weight",
            "estimated_weight",
            "unit",
        ])
        self.client.force_login(self.rider_user)

        estimated_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(estimated_response.status_code, 200)
        self.assertContains(estimated_response, "Estimated weight: 1.250 kg")
        self.assertContains(estimated_response, "Estimated")

        item.final_weight = Decimal("4.750")
        item.save(update_fields=["final_weight"])
        final_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(final_response.status_code, 200)
        self.assertContains(final_response, "Final weight: 4.750 kg")
        self.assertContains(final_response, "Final")

    def test_rider_can_return_full_order_without_customer_otp_or_cod_collection(self):
        order, shop_order, item = self.make_delivery("COD")
        self.shop.require_customer_otp_verification = True
        self.shop.save(update_fields=["require_customer_otp_verification"])
        self.client.force_login(self.rider_user)

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {
                "return_to_shop": "1",
                "return_reason": "Customer could not be found",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('rider_dashboard')}?view=completed#order-{shop_order.id}",
        )
        shop_order.refresh_from_db()
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(shop_order.status, "RETURNED")
        self.assertIsNone(shop_order.delivery_completed_at)
        self.assertEqual(shop_order.delivery_remark, "Customer could not be found")
        self.assertEqual(item.returned_quantity, item.quantity)
        self.assertEqual(order.status, "RETURNED")
        self.assertFalse(
            RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="COLLECTION",
            ).exists()
        )
        self.assertTrue(
            OrderStatusHistory.objects.filter(
                shop_order=shop_order,
                status="RETURNED",
            ).exists()
        )

        self.client.force_login(self.owner)
        receive_response = self.client.post(
            reverse(
                "confirm_returned_items_received",
                kwargs={"shop_order_id": shop_order.id},
            ),
        )
        self.assertEqual(receive_response.status_code, 302)
        item.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(item.returned_received_quantity, item.quantity)
        self.assertEqual(self.assignment.stock, item.quantity)

    def test_cod_qr_is_attached_to_its_payment_method_choice(self):
        self.make_delivery("COD")
        self.shop.upi_id = "shop@example"
        self.shop.location = {"lat": 23.0225, "lng": 72.5714}
        self.shop.save(update_fields=["upi_id", "location"])
        self.rider.upi_id = "rider@example"
        self.rider.save(update_fields=["upi_id"])
        self.client.force_login(self.rider_user)
        self.assertEqual(
            seller_views._get_shop_coordinates(self.shop),
            {"lat": 23.0225, "lng": 72.5714},
        )

        response = self.client.get(reverse("rider_dashboard"), {"view": "active"})
        self.assertEqual(
            (
                response.context["orders"][0].shop_id,
                response.context["orders"][0].status,
                response.context["orders"][0].shop.location,
                response.context["orders"][0].nearest_order_coordinates,
            ),
            (
                self.shop.pk,
                "OUT_FOR_DELIVERY",
                {"lat": 23.0225, "lng": 72.5714},
                {"lat": 23.0225, "lng": 72.5714},
            ),
        )

        self.assertContains(response, "Pay shop UPI (shop approval required)")
        self.assertNotContains(response, 'data-cod-qr="SHOP_UPI"')
        self.assertContains(response, 'data-cod-qr="STAFF_UPI"')
        self.assertContains(response, "updateCodQr")
        self.assertContains(response, 'id="showNearbyRiderOrders"')
        self.assertContains(response, 'data-location-latitude="23.0225"')
        self.assertContains(response, 'data-location-longitude="72.5714"')
        self.assertContains(response, "other shown orders are within 100 m")
        self.assertContains(response, 'data-fill-full-cod="')
        self.assertContains(response, "Fill full payment received")

    def test_cod_overpayment_credits_customer_and_debits_shop_wallet(self):
        order, shop_order, item = self.make_delivery("COD")
        overpayment = Decimal("25.00")
        actual_cash_received = order.payment_amount + overpayment
        self.client.force_login(self.rider_user)

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                f"returned_quantity_{item.pk}": "0",
                "received_amount": str(actual_cash_received),
            },
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertTrue(order.is_payment_done)
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertEqual(self.profile.wallet_balance, overpayment)
        customer_refund = self.profile.wallet_transactions.get(
            reference=order.order_id,
            description="Change returned from rider payment.",
        )
        self.assertEqual(customer_refund.amount, overpayment)

        collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        self.assertEqual(collection.amount, actual_cash_received)
        shop_wallet = ShopWallet.objects.get(shop=self.shop)
        self.assertEqual(shop_wallet.balance, -overpayment)
        shop_debit = ShopWalletTransaction.objects.get(
            shop_order=shop_order,
            transaction_type="DEBIT",
        )
        self.assertEqual(shop_debit.amount, overpayment)
        self.assertIn("Customer overpayment refunded", shop_debit.description)

        default_rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertNotContains(
            default_rider_dashboard,
            "Personal UPI COD outstanding",
        )
        rider_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "handover"},
        )
        self.assertEqual(
            rider_dashboard.context["cash_in_hand"],
            actual_cash_received,
        )
        completed_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertContains(completed_dashboard, "Customer payment summary")
        self.assertContains(completed_dashboard, "Cash collected from customer")
        self.assertContains(completed_dashboard, "Credited to customer wallet")
        self.assertContains(completed_dashboard, f"₹{actual_cash_received:.2f}")
        self.assertEqual(
            completed_dashboard.context["orders"][0].customer_wallet_credits,
            overpayment,
        )
        self.assertEqual(
            completed_dashboard.context["orders"][0].customer_wallet_debits,
            Decimal("0.00"),
        )

        self.client.force_login(self.owner)
        order_detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertContains(
            order_detail,
            "Extra payment credited to customer wallet and debited from shop wallet",
        )
        self.assertContains(order_detail, f"−₹{overpayment:.2f}")
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(handover_response.status_code, 302)
        shop_wallet.refresh_from_db()
        self.assertEqual(shop_wallet.balance, Decimal("72.00"))
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="DEBIT",
            ).count(),
            1,
        )

    def test_personal_upi_cod_is_tracked_as_staff_wallet_balance_until_handover(self):
        order, shop_order, _ = self.make_delivery("COD")
        self.rider.upi_id = "rider@example"
        self.rider.save(update_fields=["upi_id"])
        self.shop.upi_id = "shop@example"
        self.shop.save(update_fields=["upi_id"])
        amount_received = order.payment_amount
        self.client.force_login(self.rider_user)
        with patch.object(
            seller_views,
            "_upi_payment_qr_data_uri",
            wraps=seller_views._upi_payment_qr_data_uri,
        ) as qr_generator:
            delivery_dashboard = self.client.get(
                reverse("rider_dashboard"),
                {"view": "active"},
            )
        self.assertContains(
            delivery_dashboard,
            "Pay shop UPI (shop approval required)",
        )
        self.assertContains(delivery_dashboard, "Paid to my personal UPI")
        self.assertFalse(any(
            args[0] == "shop@example" and args[2] == amount_received
            for args, _kwargs in qr_generator.call_args_list
        ))
        self.assertTrue(any(
            args[0] == "rider@example" and args[2] == amount_received
            for args, _kwargs in qr_generator.call_args_list
        ))

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                "received_amount": str(amount_received),
                "cod_payment_method": "STAFF_UPI",
            },
        )

        self.assertEqual(response.status_code, 302)
        collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        self.assertEqual(collection.payment_method, "STAFF_UPI")
        wallet = ShopStaffWallet.objects.get(staff=self.rider)
        self.assertEqual(wallet.balance, amount_received)
        self.assertEqual(
            ShopStaffWalletTransaction.objects.get(
                shop_order=shop_order,
                transaction_type="COD_UPI",
            ).balance_after,
            amount_received,
        )
        default_rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertNotContains(
            default_rider_dashboard,
            "Personal UPI COD outstanding",
        )
        rider_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "handover"},
        )
        self.assertEqual(rider_dashboard.context["cash_in_hand"], Decimal("0.00"))
        self.assertEqual(
            rider_dashboard.context["staff_upi_cod_balance"],
            amount_received,
        )
        dashboard_html = rider_dashboard.content.decode()
        self.assertIn("Personal UPI COD outstanding", dashboard_html)
        self.assertLess(
            dashboard_html.index("Personal UPI COD outstanding"),
            dashboard_html.index('class="card border-0 shadow-sm mb-4 rider-cash-panel"'),
        )
        self.assertEqual(
            rider_dashboard.context["cash_in_hand"],
            Decimal("0.00"),
            "Personal UPI must not be counted as physical cash.",
        )
        self.assertEqual(
            rider_dashboard.context["cod_collected_30_days"],
            amount_received,
        )
        self.assertEqual(
            rider_dashboard.context["cod_collected_total"],
            amount_received,
        )
        completed_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        completed_order = completed_dashboard.context["orders"][0]
        self.assertEqual(completed_order.customer_cash_collected, Decimal("0.00"))
        self.assertEqual(
            completed_order.customer_personal_upi_collected,
            amount_received,
        )
        self.assertEqual(completed_order.customer_cod_collected, amount_received)
        self.assertContains(completed_dashboard, "Received in personal UPI")
        self.assertContains(completed_dashboard, f"₹{amount_received:.2f}")

        manager = User.objects.create_user(
            username="partial_return_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager,
            role="MANAGER",
            is_active=True,
        )
        self.client.force_login(manager)
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk}),
        )

        self.assertEqual(handover_response.status_code, 302)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))
        handover = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="HANDOVER",
        )
        self.assertEqual(handover.payment_method, "STAFF_UPI")
        self.assertEqual(
            ShopStaffWalletTransaction.objects.get(
                shop_order=shop_order,
                transaction_type="HANDOVER",
            ).balance_after,
            Decimal("0.00"),
        )
        self.client.force_login(self.rider_user)
        rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(
            rider_dashboard.context["cod_handed_over_30_days"],
            amount_received,
        )
        self.assertEqual(
            rider_dashboard.context["cod_handed_over_total"],
            amount_received,
        )

    def test_shop_upi_cod_is_recorded_without_creating_rider_cash_debt(self):
        order, shop_order, _ = self.make_delivery("COD")
        self.shop.upi_id = "shop@example"
        self.shop.save(update_fields=["upi_id"])
        self.client.force_login(self.rider_user)

        direct_completion_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                "received_amount": str(order.payment_amount),
                "cod_payment_method": "SHOP_UPI",
            },
        )
        self.assertEqual(direct_completion_response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        self.assertFalse(
            CODBalancePayment.objects.filter(shop_order=shop_order).exists()
        )

        prepare_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
                "received_amount": "",
            },
        )

        self.assertEqual(prepare_response.status_code, 302)
        payment_request = CODPaymentApprovalRequest.objects.get(
            shop_order=shop_order,
        )
        self.assertEqual(payment_request.status, "QR_READY")
        self.assertEqual(payment_request.qr_amount, order.payment_amount)
        self.assertEqual(payment_request.settlement_amount, order.payment_amount)
        qr_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(
            qr_dashboard,
            f"Customer pays ₹{payment_request.qr_amount:.2f}",
        )
        self.assertContains(qr_dashboard, "Customer paid — request shop approval")
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        order.refresh_from_db()
        self.assertFalse(order.is_payment_done)

        request_approval_response = self.client.post(
            reverse(
                "request_cod_payment_approval",
                kwargs={"payment_request_id": payment_request.pk},
            ),
        )
        self.assertEqual(request_approval_response.status_code, 302)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, "PENDING")
        self.assertEqual(
            shop_order.status,
            "OUT_FOR_DELIVERY",
        )
        self.assertTrue(
            Notification.objects.filter(
                user=self.owner,
                title="COD UPI payment needs approval",
            ).exists()
        )
        pending_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(pending_dashboard, "Waiting for shop approval")

        order_manager = User.objects.create_user(
            username="cod_upi_order_manager",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=order_manager,
            role="ORDER",
            can_view_orders=True,
            is_active=True,
        )
        self.client.force_login(order_manager)
        order_detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertContains(
            order_detail_response,
            "Approve payment and complete delivery",
        )
        approval_response = self.client.post(
            reverse(
                "approve_cod_payment",
                kwargs={"payment_request_id": payment_request.pk},
            ),
        )
        self.assertEqual(approval_response.status_code, 302)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, "APPROVED")
        self.assertEqual(payment_request.reviewed_by, order_manager)

        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "DELIVERED")
        self.client.force_login(self.rider_user)
        completed_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertContains(completed_dashboard, "Paid to shop UPI (approved)")
        self.assertEqual(
            completed_dashboard.context["orders"][0].customer_shop_upi_collected,
            payment_request.qr_amount,
        )
        order.refresh_from_db()
        self.assertTrue(order.is_payment_done)
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertFalse(
            RiderCashLedgerEntry.objects.filter(shop_order=shop_order).exists()
        )
        self.assertFalse(ShopStaffWallet.objects.filter(staff=self.rider).exists())
        payment = CODBalancePayment.objects.get(shop_order=shop_order)
        self.assertEqual(payment.amount, payment_request.qr_amount)
        self.assertEqual(payment.received_by, self.rider_user)
        self.assertTrue(
            Notification.objects.filter(
                user=self.rider_user,
                title="Shop approved COD payment",
            ).exists()
        )

    def test_shop_upi_qr_amount_is_unique_per_shop_while_pending(self):
        self.shop.upi_id = "shop@example"
        self.shop.save(update_fields=["upi_id"])
        self.client.force_login(self.rider_user)

        first_order, first_shop_order, _ = self.make_delivery("COD")
        first_prepare_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": first_shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
            },
        )
        self.assertEqual(first_prepare_response.status_code, 302)
        first_request = CODPaymentApprovalRequest.objects.get(
            shop_order=first_shop_order,
        )

        second_order, second_shop_order, _ = self.make_delivery("COD")
        self.assertEqual(second_order.payment_amount, first_order.payment_amount)
        second_prepare_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": second_shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
            },
        )
        self.assertEqual(second_prepare_response.status_code, 302)
        second_request = CODPaymentApprovalRequest.objects.get(
            shop_order=second_shop_order,
        )
        self.assertEqual(
            first_request.qr_amount - second_request.qr_amount,
            Decimal("0.01"),
        )
        self.assertGreaterEqual(
            second_request.qr_amount,
            second_request.settlement_amount - Decimal("1.00"),
        )

    def test_rejected_shop_upi_request_releases_its_shop_amount(self):
        self.shop.upi_id = "shop@example"
        self.shop.save(update_fields=["upi_id"])
        self.client.force_login(self.rider_user)

        _first_order, first_shop_order, _ = self.make_delivery("COD")
        self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": first_shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
            },
        )
        first_request = CODPaymentApprovalRequest.objects.get(
            shop_order=first_shop_order,
        )
        self.client.post(
            reverse(
                "request_cod_payment_approval",
                kwargs={"payment_request_id": first_request.pk},
            ),
        )

        self.client.force_login(self.owner)
        with patch("seller.views.queue_order_update") as publish_order_update:
            reject_response = self.client.post(
                reverse(
                    "reject_cod_payment",
                    kwargs={"payment_request_id": first_request.pk},
                ),
            )
        self.assertEqual(reject_response.status_code, 302)
        publish_order_update.assert_called_once_with(first_shop_order)
        first_request.refresh_from_db()
        self.assertEqual(first_request.status, "REJECTED")

        self.client.force_login(self.rider_user)
        rejected_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(
            rejected_dashboard,
            'data-cod-payment-status="REJECTED"',
        )
        self.assertContains(
            rejected_dashboard,
            'data-sellerhelper-app-refresh',
        )
        self.assertContains(
            rejected_dashboard,
            "The shop rejected the previous payment confirmation.",
        )
        self.assertNotContains(
            rejected_dashboard,
            "Waiting for shop approval",
        )
        self.assertContains(
            rejected_dashboard,
            "window.setTimeout(() => window.sellerHelperRefreshPageContent(), 3000)",
        )
        self.assertContains(
            rejected_dashboard,
            "currentMain.replaceChildren",
        )

        _second_order, second_shop_order, _ = self.make_delivery("COD")
        self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": second_shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
            },
        )
        second_request = CODPaymentApprovalRequest.objects.get(
            shop_order=second_shop_order,
        )
        self.assertEqual(second_request.qr_amount, first_request.qr_amount)

    def test_shop_upi_approval_settles_revised_balance_after_partial_return(self):
        order, shop_order, item = self.make_delivery("COD")
        original_total = order.final_billing_amount
        self.shop.upi_id = "shop@example"
        self.shop.save(update_fields=["upi_id"])
        self.client.force_login(self.rider_user)

        prepare_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
                f"returned_quantity_{item.pk}": "2",
            },
        )
        self.assertEqual(prepare_response.status_code, 302)
        item.refresh_from_db()
        order.refresh_from_db()
        payment_request = CODPaymentApprovalRequest.objects.get(
            shop_order=shop_order,
        )
        self.assertEqual(item.returned_quantity, 0)
        self.assertEqual(order.final_billing_amount, original_total)
        self.assertEqual(payment_request.original_cod_amount, order.payment_amount)
        self.assertLess(payment_request.settlement_amount, order.payment_amount)

        self.client.post(
            reverse(
                "request_cod_payment_approval",
                kwargs={"payment_request_id": payment_request.pk},
            ),
        )
        self.client.force_login(self.owner)
        approve_response = self.client.post(
            reverse(
                "approve_cod_payment",
                kwargs={"payment_request_id": payment_request.pk},
            ),
        )
        self.assertEqual(approve_response.status_code, 302)
        order.refresh_from_db()
        shop_order.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(order.is_payment_done)
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertEqual(item.returned_quantity, 2)
        self.assertLess(order.final_billing_amount, original_total)
        self.assertEqual(shop_order.status, "DELIVERED")

    def test_cancelling_shop_upi_qr_allows_customer_to_keep_previously_selected_return(self):
        order, shop_order, item = self.make_delivery("COD")
        original_total = order.final_billing_amount
        original_due = order.payment_amount
        self.shop.upi_id = "shop@example"
        self.shop.save(update_fields=["upi_id"])
        self.client.force_login(self.rider_user)

        prepare_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                "cod_payment_method": "SHOP_UPI",
                "prepare_shop_upi_payment": "1",
                f"returned_quantity_{item.pk}": "1",
            },
        )
        self.assertEqual(prepare_response.status_code, 302)
        payment_request = CODPaymentApprovalRequest.objects.get(
            shop_order=shop_order,
        )
        item.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(item.returned_quantity, 0)
        self.assertEqual(order.final_billing_amount, original_total)
        self.assertEqual(payment_request.return_quantities[str(item.pk)], 1)

        cancel_response = self.client.post(
            reverse(
                "cancel_cod_payment_qr",
                kwargs={"payment_request_id": payment_request.pk},
            ),
        )
        self.assertEqual(cancel_response.status_code, 302)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, "CANCELLED")

        dashboard_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(
            dashboard_response,
            f'name="returned_quantity_{item.pk}"',
        )
        self.assertContains(dashboard_response, f'min="0"')

        complete_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                f"returned_quantity_{item.pk}": "0",
                "cod_payment_method": "CASH",
                "received_amount": str(original_due),
            },
        )
        self.assertEqual(complete_response.status_code, 302)
        item.refresh_from_db()
        order.refresh_from_db()
        shop_order.refresh_from_db()
        self.assertEqual(item.returned_quantity, 0)
        self.assertEqual(order.final_billing_amount, original_total)
        self.assertEqual(shop_order.status, "DELIVERED")

    def test_staff_personal_upi_settings_are_private_and_validated(self):
        self.client.force_login(self.rider_user)

        response = self.client.get(reverse("staff_payment_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Personal UPI payment settings")

        invalid_response = self.client.post(
            reverse("staff_payment_settings"),
            {"upi_id": "not a valid UPI ID"},
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertFormError(invalid_response.context["form"], "upi_id", "Enter a valid staff UPI ID.")

        valid_response = self.client.post(
            reverse("staff_payment_settings"),
            {"upi_id": "rider@example"},
        )
        self.assertEqual(valid_response.status_code, 302)
        self.rider.refresh_from_db()
        self.assertEqual(self.rider.upi_id, "rider@example")

    def test_rider_orders_are_compact_with_separate_sticky_performance_tab(self):
        _order, shop_order, _item = self.make_delivery("RAZORPAY")
        self.client.force_login(self.rider_user)

        orders_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertEqual(orders_response.status_code, 200)
        self.assertContains(orders_response, "Stats")
        self.assertContains(orders_response, "Items · 1")
        self.assertContains(orders_response, "View details")
        self.assertContains(orders_response, "Items subtotal")
        self.assertNotContains(orders_response, "My delivery performance · last 30 days")
        self.assertIn(
            '<details class="rider-order-items">',
            orders_response.content.decode(),
        )

        performance_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "performance"},
        )
        self.assertEqual(performance_response.status_code, 200)
        self.assertContains(
            performance_response,
            "My delivery performance · last 30 days",
        )
        self.assertNotContains(
            performance_response,
            f'id="order-{shop_order.pk}"',
        )

    def test_wallet_proceeds_release_before_cod_and_remaining_proceeds_settle_once(self):
        order, shop_order, item = self.make_delivery("COD")
        wallet_payment = Decimal("40.00")
        order.wallet_amount_used = wallet_payment
        order.payment_amount -= wallet_payment
        order.save(update_fields=["wallet_amount_used", "payment_amount"])
        rider_payment = Decimal("5.00")
        self.client.force_login(self.rider_user)

        delivery_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                f"returned_quantity_{item.pk}": "0",
                "received_amount": str(rider_payment),
            },
        )

        self.assertEqual(delivery_response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        shop_wallet = ShopWallet.objects.get(shop=self.shop)
        self.assertEqual(shop_order.wallet_funded_proceeds, Decimal("40.00"))
        self.assertEqual(shop_wallet.balance, Decimal("40.00"))
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="CREDIT",
            ).aggregate(total=Sum("amount"))["total"],
            Decimal("40.00"),
        )
        self.assertFalse(SiteWalletTransaction.objects.filter(shop_order=shop_order).exists())
        completed_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertEqual(
            completed_dashboard.context["orders"][0].customer_cash_collected,
            rider_payment,
        )
        self.assertEqual(
            completed_dashboard.context["orders"][0].customer_wallet_debits,
            wallet_payment,
        )

        self.client.force_login(self.owner)
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(handover_response.status_code, 302)
        shop_wallet.refresh_from_db()
        self.assertEqual(shop_wallet.balance, Decimal("40.00"))
        self.assertFalse(SiteWalletTransaction.objects.filter(shop_order=shop_order).exists())

        balance_due = order.payment_amount
        balance_response = self.client.post(
            reverse("collect_cod_balance", kwargs={"shop_order_id": shop_order.pk}),
            {"amount": str(balance_due)},
        )
        self.assertEqual(balance_response.status_code, 302)
        order.refresh_from_db()
        shop_order.refresh_from_db()
        shop_wallet.refresh_from_db()
        self.assertTrue(order.is_payment_done)
        self.assertEqual(shop_wallet.balance, Decimal("97.00"))
        self.assertEqual(shop_order.wallet_funded_proceeds, Decimal("40.00"))
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="CREDIT",
            ).aggregate(total=Sum("amount"))["total"],
            Decimal("100.00"),
        )
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="COMMISSION",
            ).count(),
            1,
        )
        self.assertEqual(
            SiteWalletTransaction.objects.get(shop_order=shop_order).amount,
            Decimal("3.00"),
        )
        seller_views._credit_shop_wallet_for_delivery(shop_order)
        shop_wallet.refresh_from_db()
        self.assertEqual(shop_wallet.balance, Decimal("97.00"))

    def test_otp_is_verified_before_returns_and_wallet_credits_only_kept_items(self):
        order, shop_order, item = self.make_delivery("COD")
        self.shop.require_customer_otp_verification = True
        self.shop.save(update_fields=["require_customer_otp_verification"])
        self.profile.verification_otp = "1234"
        self.profile.save(update_fields=["verification_otp"])
        self.client.force_login(self.rider_user)

        invalid_otp_response = self.client.post(
            reverse("verify_customer_otp", kwargs={"shop_order_id": shop_order.pk}),
            {"otp": "9999"},
        )
        self.assertEqual(invalid_otp_response.status_code, 302)
        self.assertEqual(
            invalid_otp_response["Location"],
            f"{reverse('rider_dashboard')}?view=active"
            f"&otp_error={shop_order.pk}#order-{shop_order.pk}",
        )
        shop_order.refresh_from_db()
        self.assertFalse(shop_order.customer_otp_verified)
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )
        unverified_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active", "otp_error": str(shop_order.pk)},
        )
        self.assertContains(unverified_dashboard, "Incorrect customer OTP")
        self.assertContains(unverified_dashboard, "Incorrect OTP")
        self.assertContains(
            unverified_dashboard,
            f'id="customer-otp-error-{shop_order.pk}"',
        )
        self.assertContains(
            unverified_dashboard,
            'aria-invalid="true"',
        )
        self.assertNotContains(
            unverified_dashboard,
            f'name="returned_quantity_{item.pk}"',
        )

        verify_response = self.client.post(
            reverse("verify_customer_otp", kwargs={"shop_order_id": shop_order.pk}),
            {"otp": "1234"},
        )

        self.assertEqual(verify_response.status_code, 302)
        self.assertEqual(
            verify_response["Location"],
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}",
        )
        shop_order.refresh_from_db()
        self.assertTrue(shop_order.customer_otp_verified)
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )
        dashboard_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(dashboard_response, "OTP verified. Record any returned items")
        self.assertContains(dashboard_response, f'name="returned_quantity_{item.pk}"')

        complete_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {f"returned_quantity_{item.pk}": "2"},
        )

        self.assertEqual(complete_response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        item.refresh_from_db()
        wallet, _ = ShopWallet.objects.get_or_create(shop=self.shop)
        self.assertEqual(shop_order.status, "DELIVERED")
        self.assertEqual(item.returned_quantity, 2)
        self.assertEqual(shop_order.total_amount, Decimal("60.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )

        self.client.force_login(self.owner)
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(handover_response.status_code, 302)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("58.20"))
        seller_credit = ShopWalletTransaction.objects.get(
            shop_order=shop_order,
            transaction_type="CREDIT",
        )
        self.assertEqual(seller_credit.amount, Decimal("60.00"))
        commission = SiteWalletTransaction.objects.get(shop_order=shop_order)
        self.assertEqual(commission.amount, Decimal("1.80"))
        self.assertEqual(order.payment_status, "PAID")

    def test_cod_return_keeps_unpaid_balance_due_when_customer_pays_less(self):
        order, shop_order, item = self.make_delivery("COD")
        self.client.force_login(self.rider_user)

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                f"returned_quantity_{item.pk}": "2",
                "received_amount": "5.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "DELIVERED")
        self.assertEqual(order.payment_status, "COD")
        self.assertFalse(order.is_payment_done)
        self.assertGreater(order.payment_amount, Decimal("0.00"))
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )
        collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        self.assertEqual(collection.amount, Decimal("5.00"))
        rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertContains(rider_dashboard, "₹5.00")
        self.assertEqual(rider_dashboard.context["cash_in_hand"], Decimal("5.00"))

        denied_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(denied_response.status_code, 403)

        self.client.force_login(self.owner)
        handover_response = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(handover_response.status_code, 302)
        self.assertTrue(
            RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="HANDOVER",
                amount=Decimal("5.00"),
            ).exists()
        )
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )
        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertContains(detail_response, "This shop’s items subtotal")
        self.assertContains(detail_response, "Full customer order bill")
        self.assertContains(detail_response, "Remaining COD balance")
        self.assertContains(
            detail_response,
            f"₹{order.payment_amount:.2f} is still due on the full customer order",
        )

        balance_due = order.payment_amount
        manager_user = User.objects.create_user(
            username="cod_balance_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager_user,
            role="MANAGER",
            is_active=True,
        )
        self.client.force_login(manager_user)
        denied_overpayment = self.client.post(
            reverse("collect_cod_balance", kwargs={"shop_order_id": shop_order.pk}),
            {"amount": str(balance_due + Decimal("0.01"))},
        )
        self.assertEqual(denied_overpayment.status_code, 302)
        self.assertFalse(CODBalancePayment.objects.filter(shop_order=shop_order).exists())
        self.assertEqual(order.payment_amount, balance_due)

        first_payment = (balance_due / 2).quantize(Decimal("0.01"))
        first_collection = self.client.post(
            reverse("collect_cod_balance", kwargs={"shop_order_id": shop_order.pk}),
            {"amount": str(first_payment)},
        )
        self.assertEqual(first_collection.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.payment_amount, balance_due - first_payment)
        self.assertFalse(order.is_payment_done)
        self.assertFalse(ShopWalletTransaction.objects.filter(shop_order=shop_order).exists())

        final_collection = self.client.post(
            reverse("collect_cod_balance", kwargs={"shop_order_id": shop_order.pk}),
            {"amount": str(order.payment_amount)},
        )
        self.assertEqual(final_collection.status_code, 302)
        order.refresh_from_db()
        shop_order.refresh_from_db()
        self.assertTrue(order.is_payment_done)
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertEqual(
            CODBalancePayment.objects.filter(shop_order=shop_order).count(),
            2,
        )
        self.assertEqual(
            sum(
                CODBalancePayment.objects.filter(shop_order=shop_order).values_list(
                    "amount",
                    flat=True,
                ),
                Decimal("0.00"),
            ),
            balance_due,
        )
        wallet = ShopWallet.objects.get(shop=self.shop)
        self.assertEqual(wallet.balance, Decimal("58.20"))
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="CREDIT",
            ).count(),
            1,
        )
        payment_history = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertContains(payment_history, "COD payments recorded")
        self.assertContains(payment_history, "₹" + f"{first_payment:.2f}")

    @patch("seller.views.requests.post")
    def test_prepaid_partial_return_credits_customer_wallet_without_gateway_refund(self, post):
        order, shop_order, item = self.make_delivery("RAZORPAY")
        original_received = order.finally_received_amount
        self.client.force_login(self.rider_user)

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {f"returned_quantity_{item.id}": "2"},
        )

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(item.returned_quantity, 2)
        expected_wallet_credit = original_received - order.final_billing_amount
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.wallet_balance, expected_wallet_credit)
        self.assertEqual(shop_order.wallet_refund_amount, expected_wallet_credit)
        self.assertEqual(shop_order.razorpay_refund_amount, Decimal("0.00"))
        self.assertEqual(order.payment_status, "PAID")
        self.assertTrue(order.is_payment_done)
        self.assertEqual(
            order.finally_received_amount + order.wallet_amount_used,
            order.final_billing_amount,
        )
        post.assert_not_called()
        wallet_transaction_count = self.profile.wallet_transactions.count()

        repeated_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {f"returned_quantity_{item.id}": "2"},
        )

        self.assertEqual(repeated_response.status_code, 302)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(self.profile.wallet_transactions.count(), wallet_transaction_count)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.wallet_refund_amount, expected_wallet_credit)

    def test_shop_stock_staff_confirms_rider_return_and_clears_pending_list(self):
        order, shop_order, item = self.make_delivery("COD")
        self.client.force_login(self.rider_user)

        complete_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {f"returned_quantity_{item.id}": "2"},
        )
        self.assertEqual(complete_response.status_code, 302)
        item.refresh_from_db()
        shop_order.refresh_from_db()
        self.assertEqual(item.returned_quantity, 2)
        self.assertEqual(item.returned_received_quantity, 0)
        self.assertEqual(shop_order.status, "DELIVERED")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 0)

        self.client.force_login(self.rider_user)
        denied_receive_response = self.client.post(
            reverse("confirm_returned_items_received", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(denied_receive_response.status_code, 403)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 0)

        rider_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertContains(rider_dashboard, "2 item(s) to hand in")

        staff_user = User.objects.create_user(
            username="return_receiving_stock_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=staff_user,
            role="MANAGER",
            is_active=True,
            can_manage_stock=True,
        )
        self.client.force_login(staff_user)
        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertContains(detail_response, "Returned items are with the rider")
        self.assertContains(detail_response, "Confirm returned items received")
        shop_orders_response = self.client.get(reverse("shop_order_list"))
        self.assertContains(shop_orders_response, "Rider return: 2")

        receive_response = self.client.post(
            reverse("confirm_returned_items_received", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(receive_response.status_code, 302)
        item.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(item.returned_received_quantity, 2)
        self.assertEqual(self.assignment.stock, 2)
        movement = StockMovement.objects.get(
            product_shop=self.assignment,
            movement_type="ADJUSTMENT",
        )
        self.assertEqual(movement.quantity, 2)
        self.assertEqual(movement.created_by, staff_user)

        duplicate_response = self.client.post(
            reverse("confirm_returned_items_received", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(duplicate_response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 2)
        self.assertEqual(
            StockMovement.objects.filter(
                product_shop=self.assignment,
                movement_type="ADJUSTMENT",
            ).count(),
            1,
        )
        self.client.force_login(self.rider_user)
        cleared_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertNotContains(cleared_dashboard, "2 item(s) to hand in")
        self.assertEqual(order.order_id, shop_order.order.order_id)

    def test_invalid_return_quantity_is_rejected_without_completing_delivery(self):
        order, shop_order, item = self.make_delivery("COD")
        self.client.force_login(self.rider_user)

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.id}),
            {f"returned_quantity_{item.id}": "6"},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        item.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        self.assertEqual(item.returned_quantity, 0)
        self.assertFalse(order.is_payment_done)

    def test_assigned_rider_can_open_shared_shop_order_details(self):
        other_owner = User.objects.create_user(
            username="shared_detail_shop_owner",
            password="test-password",
        )
        other_shop = Shop.objects.create(
            name="Shared Detail Shop",
            owner=other_owner,
        )
        order = Order.objects.create(
            order_id="SHARED-DETAIL-ORDER",
            user=self.customer_user.username,
            payment_type="COD",
            status="OUT_FOR_DELIVERY",
            shop=other_shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=other_shop,
            status="OUT_FOR_DELIVERY",
            assigned_rider=self.rider,
            total_amount=Decimal("20.00"),
            product_count=1,
        )
        self.client.force_login(self.rider_user)

        response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SHARED-DETAIL-ORDER")

    def test_rider_can_correct_cod_until_full_handover_then_shop_can_confirm_remaining(self):
        order, shop_order, _item = self.make_delivery("COD")
        self.client.force_login(self.rider_user)
        delivery_response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": str(order.payment_amount)},
        )
        self.assertEqual(delivery_response.status_code, 302)
        order.refresh_from_db()
        collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        original_amount = collection.amount
        corrected_amount = Decimal("50.00")

        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Correct COD amount received from customer")
        edit_response = self.client.post(
            reverse("edit_rider_cod_collection", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": str(corrected_amount)},
        )
        self.assertEqual(edit_response.status_code, 302)
        collection.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(collection.amount, corrected_amount)
        self.assertEqual(order.finally_received_amount, corrected_amount)
        self.assertEqual(
            order.payment_amount,
            order.final_billing_amount - order.wallet_amount_used - corrected_amount,
        )
        self.assertNotEqual(collection.amount, original_amount)

        self.client.force_login(self.owner)
        partial_handover = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": "20.00"},
        )
        self.assertEqual(partial_handover.status_code, 302)
        self.assertEqual(
            RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="HANDOVER",
            ).aggregate(total=Sum("amount"))["total"],
            Decimal("20.00"),
        )
        self.client.force_login(self.rider_user)
        partial_edit = self.client.post(
            reverse("edit_rider_cod_collection", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": "45.00"},
        )
        self.assertEqual(partial_edit.status_code, 302)
        collection.refresh_from_db()
        self.assertEqual(collection.amount, Decimal("45.00"))

        self.client.force_login(self.owner)
        final_handover = self.client.post(
            reverse("confirm_rider_cash_handover", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": "25.00"},
        )
        self.assertEqual(final_handover.status_code, 302)
        self.assertEqual(
            RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="HANDOVER",
            ).aggregate(total=Sum("amount"))["total"],
            Decimal("45.00"),
        )
        self.client.force_login(self.rider_user)
        completed_edit = self.client.post(
            reverse("edit_rider_cod_collection", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": "40.00"},
        )
        self.assertEqual(completed_edit.status_code, 302)
        collection.refresh_from_db()
        self.assertEqual(collection.amount, Decimal("45.00"))
        self.client.force_login(self.rider_user)
        dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(dashboard.context["cash_in_hand"], Decimal("0.00"))

    def test_rider_cod_overpayment_correction_updates_customer_wallet(self):
        order, shop_order, _item = self.make_delivery("COD")
        self.client.force_login(self.rider_user)
        completed = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": str(order.payment_amount)},
        )
        self.assertEqual(completed.status_code, 302)
        order.refresh_from_db()
        collection = RiderCashLedgerEntry.objects.get(
            shop_order=shop_order,
            entry_type="COLLECTION",
        )
        bill_due = order.final_billing_amount - order.wallet_amount_used
        corrected_total = bill_due + Decimal("25.00")

        detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertContains(
            detail,
            'max="999999999.99"',
        )
        self.assertContains(
            detail,
            "Amount above the bill is credited to the customer's wallet.",
        )
        dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertContains(dashboard, "Correct collected amount")
        self.assertContains(dashboard, "name=\"received_amount\"")
        correction = self.client.post(
            reverse("edit_rider_cod_collection", kwargs={"shop_order_id": shop_order.pk}),
            {"received_amount": str(corrected_total)},
        )
        self.assertEqual(correction.status_code, 302)

        collection.refresh_from_db()
        order.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(collection.amount, corrected_total)
        self.assertEqual(order.finally_received_amount, bill_due)
        self.assertEqual(order.payment_amount, Decimal("0.00"))
        self.assertEqual(self.profile.wallet_balance, Decimal("25.00"))
        self.assertTrue(
            ClientWalletTransaction.objects.filter(
                client=self.profile,
                transaction_type="CREDIT",
                amount=Decimal("25.00"),
                description__contains="Extra payment from rider collection",
            ).exists()
        )
        self.assertTrue(
            ShopWalletTransaction.objects.filter(
                shop_order=shop_order,
                transaction_type="DEBIT",
                amount=Decimal("25.00"),
                description__contains="COD overpayment adjustment",
            ).exists()
        )
        dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertContains(dashboard, f"Collected ₹{corrected_total:.2f}")
        self.assertEqual(dashboard.context["cash_in_hand"], corrected_total)

    def test_shop_can_confirm_one_partial_total_across_rider_orders(self):
        deliveries = []
        for order_id in ("BATCH-COD-ONE", "BATCH-COD-TWO"):
            order, shop_order, _item = self.make_delivery("COD")
            order.order_id = order_id
            order.save(update_fields=["order_id"])
            deliveries.append((order, shop_order))

        self.client.force_login(self.rider_user)
        for order, shop_order in deliveries:
            completed = self.client.post(
                reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
                {"received_amount": str(order.payment_amount)},
            )
            self.assertEqual(completed.status_code, 302)

        collections = [
            RiderCashLedgerEntry.objects.get(
                shop_order=shop_order,
                entry_type="COLLECTION",
            )
            for _order, shop_order in deliveries
        ]
        total_collected = sum(
            (collection.amount for collection in collections),
            Decimal("0.00"),
        )
        self.client.force_login(self.owner)
        shop_orders_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "handover"},
        )
        self.assertContains(shop_orders_response, "Rider COD handovers")
        self.assertContains(shop_orders_response, f"₹{total_collected:.2f} remains to hand over")
        self.assertContains(shop_orders_response, 'value="0.00"')
        self.assertContains(shop_orders_response, "Fill full amount")
        self.assertContains(shop_orders_response, "Confirm received")
        self.assertContains(shop_orders_response, "₹ Amount received")
        self.assertContains(shop_orders_response, "View order details (2)")
        self.assertContains(shop_orders_response, "BATCH-COD-ONE")
        self.assertContains(shop_orders_response, "BATCH-COD-TWO")

        partial = self.client.post(
            reverse(
                "confirm_rider_cod_handover_total",
                kwargs={"shop_id": self.shop.pk, "rider_id": self.rider.pk},
            ),
            {"payment_method": "CASH", "received_amount": "30.00"},
        )
        self.assertEqual(partial.status_code, 302)
        self.assertEqual(
            RiderCashLedgerEntry.objects.filter(entry_type="HANDOVER").aggregate(
                total=Sum("amount"),
            )["total"],
            Decimal("30.00"),
        )

        remaining = total_collected - Decimal("30.00")
        completed_handover = self.client.post(
            reverse(
                "confirm_rider_cod_handover_total",
                kwargs={"shop_id": self.shop.pk, "rider_id": self.rider.pk},
            ),
            {
                "payment_method": "CASH",
                "received_amount": str(remaining),
            },
        )
        self.assertEqual(completed_handover.status_code, 302)
        for _order, shop_order in deliveries:
            collected = RiderCashLedgerEntry.objects.get(
                shop_order=shop_order,
                entry_type="COLLECTION",
            )
            handed_over = RiderCashLedgerEntry.objects.filter(
                shop_order=shop_order,
                entry_type="HANDOVER",
            ).aggregate(total=Sum("amount"))["total"]
            self.assertEqual(handed_over, collected.amount)


class AdminCatalogWorkspaceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="catalog_workspace_admin",
            email="catalog@example.com",
            password="test-password",
        )

    def test_superuser_login_opens_catalog_workspace_by_default(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": self.admin.username,
                "password": "test-password",
                "login_as": "ADMIN",
            },
        )

        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        self.assertEqual(self.client.session["active_portal"], "ADMIN")

    def test_superuser_can_select_customer_workspace_on_login(self):
        ClientProfile.objects.create(user=self.admin, name="Superuser Customer Profile")

        response = self.client.post(
            reverse("login"),
            {
                "username": self.admin.username,
                "password": "test-password",
                "login_as": "CUSTOMER",
            },
        )

        self.assertRedirects(response, reverse("marketplace"))
        self.assertEqual(self.client.session["active_portal"], "CUSTOMER")

    def test_zoom_lock_control_is_hidden_in_customer_portals_even_for_shop_owners(self):
        owner = User.objects.create_user(
            username="zoom_lock_shop_owner",
            password="test-password",
        )
        Shop.objects.create(name="Zoom Lock Shop", owner=owner)
        self.client.force_login(owner)
        session = self.client.session

        for portal in ("CUSTOMER", "B2B_CUSTOMER"):
            with self.subTest(portal=portal):
                session["active_portal"] = portal
                session.save()
                response = self.client.get(reverse("terms"))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "App zoom lock controls")
                self.assertTrue(response.context["can_manage_app_zoom"])
                self.assertFalse(response.context["show_app_zoom_controls"])

        session["active_portal"] = "SHOP_OWNER"
        session.save()
        owner_response = self.client.get(reverse("terms"))
        self.assertContains(owner_response, "App zoom lock controls")
        self.assertTrue(owner_response.context["can_manage_app_zoom"])
        self.assertTrue(owner_response.context["show_app_zoom_controls"])

    def test_admin_workspace_radio_is_only_shown_to_superusers(self):
        regular_user = User.objects.create_user(
            username="login_radio_regular",
            password="test-password",
        )
        regular_response = self.client.get(reverse("login"))
        self.assertContains(regular_response, 'value="CUSTOMER"')
        self.assertContains(regular_response, 'value="B2B_CUSTOMER"')
        self.assertContains(regular_response, 'value="SHOP_OWNER"')
        self.assertContains(regular_response, 'value="SHOP_STAFF"')
        self.assertNotContains(regular_response, 'value="ADMIN"')

        self.client.force_login(regular_user)
        self.client.logout()
        self.client.force_login(
            User.objects.create_superuser(
                username="login_radio_admin",
                email="login-radio@example.com",
                password="test-password",
            )
        )
        admin_response = self.client.get(reverse("login"))
        self.assertContains(admin_response, 'value="ADMIN"')

    def test_catalog_workspace_requires_superuser(self):
        response = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.get(reverse("admin_catalog_product_export")).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(reverse("admin_catalog_product_import")).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                reverse("admin_catalog_hsn_reference_import"),
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.get(
                reverse("admin_catalog_hsn_suggestions"),
                {"q": "mango"},
            ).status_code,
            302,
        )

        regular_user = User.objects.create_user(
            username="catalog_workspace_user",
            password="test-password",
        )
        self.client.force_login(regular_user)
        response = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.get(reverse("admin_catalog_product_export")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("admin_catalog_product_import")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("admin_catalog_hsn_reference_import"),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("admin_catalog_hsn_suggestions"),
                {"q": "mango"},
            ).status_code,
            403,
        )
        response = self.client.get(reverse("admin_site_theme_builder"))
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_save_site_theme_and_apply_it_globally(self):
        self.client.force_login(self.admin)
        builder_url = reverse("admin_site_theme_builder")
        builder = self.client.get(builder_url)
        self.assertContains(builder, "Site-wide theme builder")
        self.assertContains(builder, "Hide app top bar while scrolling down")
        self.assertNotContains(builder, "Master: Allow pinch-to-zoom in app content")
        self.assertNotContains(builder, "Allow content zoom for Customer login")
        self.assertNotContains(builder, "Allow content zoom for B2B Customer login")
        self.assertNotContains(builder, "Allow content zoom for Shop Owner login")
        self.assertNotContains(builder, "Allow content zoom for Shop Staff login")
        self.assertContains(builder, "Mobile app: load website as desktop site")
        self.assertContains(builder, "Hide app bottom bar while scrolling down")
        self.assertNotContains(builder, "Disable initial zoom-out in the app by login type")
        self.assertContains(builder, "Per-component size and spacing")
        self.assertContains(builder, '["marketplace_category", "Marketplace product category"]')
        self.assertContains(builder, "layoutProperties")
        self.assertContains(builder, "function fillCurrentComponentValues()")
        self.assertContains(builder, "element.getBoundingClientRect()")
        self.assertContains(builder, 'input.placeholder = "Current"')
        self.assertContains(builder, 'input.dataset.layoutDirty = "true"')
        self.assertContains(builder, "input.dataset.layoutSaved === \"true\"")
        self.assertContains(builder, "Top margin")
        self.assertContains(builder, "Right margin")
        self.assertContains(builder, "Bottom padding")
        self.assertContains(builder, "Display on ${breakpoint}")
        self.assertContains(builder, '["margin_left", "marginLeft"]')
        self.assertContains(builder, '["padding_bottom", "paddingBottom"]')
        self.assertContains(builder, 'id="theme-live-frame"')
        self.assertContains(builder, 'data-preview-page="marketplace"')
        self.assertContains(builder, "function highlightLiveComponent(component)")
        self.assertContains(builder, "targetDocumentTop * frameScale")
        self.assertContains(builder, 'liveFrameWrap.scrollTo({ top: targetScrollTop, behavior: "smooth" })')
        self.assertContains(builder, "function selectLiveComponent(component, breakpoint = null)")
        self.assertContains(builder, "themeComponentFocusPulse")
        self.assertContains(builder, "summary.addEventListener(\"click\", () => selectLiveComponent(component))")
        self.assertContains(builder, 'input.addEventListener("focus"')
        home_preview = self.client.get(
            reverse("home"),
            {"theme_preview": "1"},
        )
        self.assertEqual(home_preview.status_code, 200)
        self.assertTrue(home_preview.context["theme_preview"])
        self.assertEqual(home_preview.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(
            self.client.get(reverse("home")).headers["X-Frame-Options"],
            "DENY",
        )
        self.assertContains(
            self.client.get(reverse("admin_catalog_dashboard")),
            builder_url,
        )
        response = self.client.post(builder_url, {
            "primary_color": "#123abc",
            "accent_color": "#22aa55",
            "page_background": "#f0f1f2",
            "surface_color": "#ffffff",
            "text_color": "#101820",
            "font_family": "Arial, sans-serif",
            "desktop_content_width": "1440",
            "desktop_page_padding": "20",
            "mobile_page_padding": "10",
            "mobile_navigation_fields_present": "1",
            "mobile_topbar_scroll_hide_enabled": "on",
            "mobile_app_zoom_fields_present": "1",
            "mobile_app_customer_no_zoom_enabled": "on",
            "mobile_app_b2b_customer_no_zoom_enabled": "on",
            "mobile_app_shop_owner_no_zoom_enabled": "on",
            "mobile_app_admin_no_zoom_enabled": "on",
            "mobile_app_content_zoom_fields_present": "1",
            "mobile_app_content_zoom_enabled": "on",
            "mobile_app_customer_content_zoom_enabled": "on",
            "mobile_app_b2b_customer_content_zoom_enabled": "on",
            "mobile_app_shop_owner_content_zoom_enabled": "on",
            "mobile_app_desktop_view_fields_present": "1",
            "mobile_app_desktop_view_enabled": "on",
            "component_radius": "16",
            "header_padding": "12",
            "content_padding": "30",
            "footer_padding": "20",
            "header_margin": "2",
            "content_margin": "4",
            "footer_margin": "6",
            "desktop_layout_order": "content,header,footer",
            "mobile_layout_order": "header,footer,content",
            "component_layouts": json.dumps({
                "home_hero": {
                    "desktop": {
                        "width": 1200,
                        "height": 400,
                        "margin_x": 12,
                        "margin_y": 16,
                        "margin_top": 4,
                        "margin_right": 6,
                        "margin_bottom": 8,
                        "margin_left": 10,
                        "padding_x": 24,
                        "padding_y": 32,
                        "padding_top": 2,
                        "padding_right": 4,
                        "padding_bottom": 6,
                        "padding_left": 8,
                        "gap_x": 8,
                        "gap_y": 10,
                    },
                    "mobile": {"width": 360, "padding_x": 12},
                },
                "marketplace_category": {
                    "desktop": {
                        "width": 1100,
                        "margin_top": 18,
                        "padding_left": 20,
                    },
                    "mobile": {
                        "width": 350,
                        "padding_x": 12,
                        "visible": False,
                    },
                },
                "marketplace_product_image": {
                    "desktop": {
                        "width": 180,
                        "height": 160,
                        "padding_x": 12,
                        "margin_bottom": 8,
                        "object_fit": "cover",
                    },
                    "mobile": {
                        "width": 120,
                        "height": 110,
                        "object_fit": "contain",
                    },
                },
            }),
        })
        self.assertRedirects(response, builder_url)
        theme = SiteThemeSettings.objects.get(pk=1)
        self.assertEqual(theme.primary_color, "#123abc")
        self.assertEqual(theme.desktop_content_width, 1440)
        self.assertTrue(theme.mobile_topbar_scroll_hide_enabled)
        self.assertFalse(theme.mobile_bottombar_scroll_hide_enabled)
        self.assertFalse(theme.mobile_app_customer_no_zoom_enabled)
        self.assertFalse(theme.mobile_app_b2b_customer_no_zoom_enabled)
        self.assertFalse(theme.mobile_app_shop_owner_no_zoom_enabled)
        self.assertFalse(theme.mobile_app_shop_staff_no_zoom_enabled)
        self.assertFalse(theme.mobile_app_admin_no_zoom_enabled)
        self.assertTrue(theme.mobile_app_content_zoom_enabled)
        self.assertTrue(theme.mobile_app_customer_content_zoom_enabled)
        self.assertTrue(theme.mobile_app_b2b_customer_content_zoom_enabled)
        self.assertTrue(theme.mobile_app_shop_owner_content_zoom_enabled)
        self.assertTrue(theme.mobile_app_shop_staff_content_zoom_enabled)
        self.assertTrue(theme.mobile_app_desktop_view_enabled)
        self.assertEqual(theme.desktop_layout_order, "content,header,footer")
        self.assertEqual(theme.mobile_layout_order, "header,footer,content")
        self.assertEqual(
            theme.component_layouts["home_hero"]["desktop"]["gap_y"],
            10,
        )
        self.assertEqual(
            theme.component_layouts["marketplace_category"]["desktop"]["margin_top"],
            18,
        )
        self.assertEqual(
            theme.component_layouts["marketplace_category"]["desktop"]["padding_left"],
            20,
        )
        self.assertFalse(
            theme.component_layouts["marketplace_category"]["mobile"]["visible"],
        )
        self.assertEqual(
            theme.component_layouts["marketplace_product_image"]["desktop"]["object_fit"],
            "cover",
        )
        self.assertEqual(
            theme.component_layouts["marketplace_product_image"]["mobile"]["height"],
            110,
        )
        self.assertEqual(
            theme.component_layouts["home_hero"]["desktop"]["margin_left"],
            10,
        )
        self.assertEqual(
            theme.component_layouts["home_hero"]["desktop"]["padding_bottom"],
            6,
        )

        page = self.client.get(reverse("terms"))
        self.assertContains(page, 'data-sellerhelper-customer-no-zoom="false"')
        self.assertContains(page, 'data-sellerhelper-app-content-zoom-enabled="true"')
        self.assertContains(page, 'data-sellerhelper-portal-content-zoom-enabled="true"')
        self.assertContains(page, 'data-sellerhelper-active-portal="CUSTOMER"')
        self.assertContains(page, 'data-sellerhelper-mobile-app-desktop-view="true"')
        session = self.client.session
        for portal, expected_zoom in (
            ("B2B_CUSTOMER", "false"),
            ("SHOP_OWNER", "false"),
            ("SHOP_STAFF", "false"),
            ("ADMIN", "false"),
        ):
            with self.subTest(portal=portal):
                session["active_portal"] = portal
                session.save()
                portal_page = self.client.get(reverse("terms"))
                self.assertContains(
                    portal_page,
                    f'data-sellerhelper-customer-no-zoom="{expected_zoom}"',
                )
                self.assertContains(
                    portal_page,
                    'data-sellerhelper-portal-content-zoom-enabled="true"',
                )
                self.assertContains(
                    portal_page,
                    f'data-sellerhelper-active-portal="{portal}"',
                )
        theme.mobile_app_content_zoom_enabled = False
        theme.save(update_fields=["mobile_app_content_zoom_enabled"])
        self.assertContains(
            self.client.get(reverse("terms")),
            'data-sellerhelper-app-content-zoom-enabled="false"',
        )
        session = self.client.session
        session["active_portal"] = "CUSTOMER"
        session.save()
        self.assertContains(page, "--site-primary: #123abc")
        self.assertContains(
            page,
            'class="navbar navbar-expand-lg main-navbar"\n     data-theme-component="site_header"\n     data-mobile-scroll-hide-enabled="true"',
        )
        self.assertRegex(
            page.content.decode(),
            r'class="mobile-bottom-nav mobile-only"\s+data-mobile-scroll-hide-enabled="false"',
        )
        self.assertContains(page, "if (delta > 4) setHidden(true);")
        self.assertContains(page, "else if (delta < -4) setHidden(false);")
        self.assertContains(page, "topBar?.classList.toggle")
        self.assertContains(page, "bottomBar?.classList.toggle")
        self.assertContains(
            page,
            'body.sellerhelper-bars-hidden .main-navbar[data-mobile-scroll-hide-enabled="false"]',
        )
        self.assertContains(
            page,
            'body.sellerhelper-bars-hidden .mobile-bottom-nav[data-mobile-scroll-hide-enabled="false"]',
        )
        self.assertContains(page, "order: 2")
        self.assertContains(page, "order: 3")
        home_page = self.client.get(
            reverse("home"),
            {"theme_preview": "1"},
        )
        self.assertContains(home_page, 'data-theme-component="home_hero"')
        self.assertContains(home_page, "window.applySiteComponentLayouts")

        invalid_response = self.client.post(builder_url, {
            "primary_color": "#123abc",
            "accent_color": "#22aa55",
            "page_background": "#f0f1f2",
            "surface_color": "#ffffff",
            "text_color": "#101820",
            "font_family": "Arial, sans-serif",
            "desktop_content_width": "1440",
            "desktop_page_padding": "20",
            "mobile_page_padding": "10",
            "component_radius": "16",
            "header_padding": "12",
            "content_padding": "30",
            "footer_padding": "20",
            "header_margin": "2",
            "content_margin": "4",
            "footer_margin": "6",
            "desktop_layout_order": "header,header,footer",
            "mobile_layout_order": "header,content,footer",
            "component_layouts": json.dumps({
                "home_hero": {"desktop": {"width": -1}},
            }),
        })
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "Arrange header, main content, and footer exactly once.")
        self.assertContains(invalid_response, "Component layout values must be whole pixels")
        theme.refresh_from_db()
        self.assertEqual(theme.desktop_layout_order, "content,header,footer")

    def test_superuser_can_open_read_only_marketplace_theme_preview_without_customer_profile(self):
        self.client.force_login(self.admin)
        self.client.session["active_portal"] = "ADMIN"
        self.client.session.save()

        response = self.client.get(
            reverse("marketplace"),
            {"theme_preview": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_theme_preview"])
        self.assertContains(response, "marketplace-shell")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_theme_saves_are_recorded_and_previous_revision_can_be_restored(self):
        self.client.force_login(self.admin)
        builder_url = reverse("admin_site_theme_builder")
        base_data = {
            "accent_color": "#16a34a",
            "page_background": "#f5f7fb",
            "surface_color": "#ffffff",
            "text_color": "#1f2937",
            "font_family": "Inter, system-ui, sans-serif",
            "desktop_content_width": "1280",
            "desktop_page_padding": "24",
            "mobile_page_padding": "12",
            "component_radius": "12",
            "header_padding": "8",
            "content_padding": "25",
            "footer_padding": "18",
            "header_margin": "0",
            "content_margin": "0",
            "footer_margin": "0",
            "desktop_layout_order": "header,content,footer",
            "mobile_layout_order": "header,content,footer",
        }
        first_save = self.client.post(builder_url, {
            **base_data,
            "primary_color": "#123abc",
            "component_layouts": json.dumps({
                "marketplace_product": {
                    "mobile": {
                        "width": 150,
                        "height": 220,
                        "min_width": 120,
                        "max_width": 320,
                        "gap_x": 6,
                        "gap_y": 8,
                        "border_radius": 14,
                        "visible": False,
                    },
                },
            }),
        })
        self.assertRedirects(first_save, builder_url)
        first_revision = SiteThemeRevision.objects.get()
        self.assertEqual(first_revision.action, "SAVE")
        self.assertEqual(first_revision.created_by, self.admin)
        self.assertEqual(first_revision.snapshot["primary_color"], "#123abc")
        self.assertEqual(
            first_revision.snapshot["component_layouts"]["marketplace_product"]["mobile"]["width"],
            150,
        )
        self.assertFalse(
            first_revision.snapshot["component_layouts"]["marketplace_product"]["mobile"]["visible"],
        )
        self.assertEqual(
            first_revision.snapshot["component_layouts"]["marketplace_product"]["mobile"]["max_width"],
            320,
        )

        second_save = self.client.post(builder_url, {
            **base_data,
            "primary_color": "#aabbcc",
            "component_layouts": json.dumps({
                "marketplace_product": {
                    "desktop": {"width": 210},
                },
            }),
        })
        self.assertRedirects(second_save, builder_url)
        theme = SiteThemeSettings.objects.get(pk=1)
        self.assertEqual(theme.primary_color, "#aabbcc")
        self.assertEqual(SiteThemeRevision.objects.count(), 2)

        history_response = self.client.get(builder_url)
        self.assertContains(history_response, "Theme change history")
        self.assertContains(history_response, "Restore")

        restore_response = self.client.post(builder_url, {
            "restore_theme_revision": str(first_revision.pk),
        })
        self.assertRedirects(restore_response, builder_url)
        theme.refresh_from_db()
        self.assertEqual(theme.primary_color, "#123abc")
        self.assertEqual(
            theme.component_layouts["marketplace_product"]["mobile"]["width"],
            150,
        )
        restored_revision = SiteThemeRevision.objects.latest("pk")
        self.assertEqual(restored_revision.action, "RESTORE")
        self.assertEqual(restored_revision.created_by, self.admin)
        self.assertEqual(restored_revision.restored_from, first_revision)
        self.assertEqual(SiteThemeRevision.objects.count(), 3)

    def test_theme_history_deletes_manually_and_prunes_by_retention_days(self):
        self.client.force_login(self.admin)
        theme, _ = SiteThemeSettings.objects.get_or_create(pk=1)
        theme.theme_revision_retention_days = 5
        theme.save(update_fields=["theme_revision_retention_days"])
        old_revision = SiteThemeRevision.objects.create(
            theme_settings=theme,
            snapshot={"primary_color": "#111111"},
            created_by=self.admin,
        )
        recent_revision = SiteThemeRevision.objects.create(
            theme_settings=theme,
            snapshot={"primary_color": "#222222"},
            created_by=self.admin,
        )
        SiteThemeRevision.objects.filter(pk=old_revision.pk).update(
            created_at=timezone.now() - timedelta(days=6),
        )

        builder_url = reverse("admin_site_theme_builder")
        response = self.client.get(builder_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SiteThemeRevision.objects.filter(pk=old_revision.pk).exists())
        self.assertTrue(SiteThemeRevision.objects.filter(pk=recent_revision.pk).exists())
        self.assertContains(response, "Delete")

        delete_response = self.client.post(
            builder_url,
            {"delete_theme_revision": str(recent_revision.pk)},
        )
        self.assertRedirects(delete_response, builder_url)
        self.assertFalse(SiteThemeRevision.objects.filter(pk=recent_revision.pk).exists())

    def test_superuser_can_customize_public_homepage_sections(self):
        self.client.force_login(self.admin)
        builder_url = reverse("admin_site_theme_builder")
        response = self.client.post(builder_url, {
            "primary_color": "#2563eb",
            "accent_color": "#16a34a",
            "page_background": "#f5f7fb",
            "surface_color": "#ffffff",
            "text_color": "#1f2937",
            "font_family": "Inter, system-ui, sans-serif",
            "desktop_content_width": "1280",
            "desktop_page_padding": "24",
            "mobile_page_padding": "12",
            "component_radius": "12",
            "header_padding": "8",
            "content_padding": "25",
            "footer_padding": "18",
            "header_margin": "0",
            "content_margin": "0",
            "footer_margin": "0",
            "desktop_layout_order": "header,content,footer",
            "mobile_layout_order": "header,content,footer",
            "homepage_content_fields_present": "1",
            "homepage_hero_enabled": "on",
            "homepage_features_enabled": "on",
            "homepage_hero_badge": "Welcome nearby",
            "homepage_hero_title": "Fresh shops around you",
            "homepage_hero_text": "Find local products and delivery.",
            "homepage_primary_cta": "Log in",
            "homepage_secondary_cta": "Join today",
            "homepage_feature_1_title": "Find groceries",
            "homepage_feature_1_text": "Browse local food.",
            "homepage_feature_2_title": "Fast delivery",
            "homepage_feature_2_text": "Track every order.",
            "homepage_feature_3_title": "Open a shop",
            "homepage_feature_3_text": "Reach nearby customers.",
            "homepage_section_order": "features,hero",
        })
        self.assertRedirects(response, builder_url)
        theme = SiteThemeSettings.objects.get(pk=1)
        self.assertEqual(theme.homepage_hero_title, "Fresh shops around you")
        self.assertEqual(theme.homepage_section_order, "features,hero")

        self.client.logout()
        home_response = self.client.get(reverse("home"))
        self.assertContains(home_response, "Fresh shops around you")
        self.assertContains(home_response, "Welcome nearby")
        self.assertContains(home_response, "Find groceries")
        self.assertContains(home_response, 'class="row g-3 homepage-feature-cards"')
        self.assertContains(home_response, "order: 1")
        self.assertContains(home_response, "order: 2")

        self.client.force_login(self.admin)
        response = self.client.post(builder_url, {
            "primary_color": "#2563eb",
            "accent_color": "#16a34a",
            "page_background": "#f5f7fb",
            "surface_color": "#ffffff",
            "text_color": "#1f2937",
            "font_family": "Inter, system-ui, sans-serif",
            "desktop_content_width": "1280",
            "desktop_page_padding": "24",
            "mobile_page_padding": "12",
            "component_radius": "12",
            "header_padding": "8",
            "content_padding": "25",
            "footer_padding": "18",
            "header_margin": "0",
            "content_margin": "0",
            "footer_margin": "0",
            "desktop_layout_order": "header,content,footer",
            "mobile_layout_order": "header,content,footer",
            "homepage_content_fields_present": "1",
            "homepage_features_enabled": "on",
            "homepage_hero_badge": "Welcome nearby",
            "homepage_hero_title": "Fresh shops around you",
            "homepage_hero_text": "Find local products and delivery.",
            "homepage_primary_cta": "Log in",
            "homepage_secondary_cta": "Join today",
            "homepage_feature_1_title": "Find groceries",
            "homepage_feature_1_text": "Browse local food.",
            "homepage_feature_2_title": "Fast delivery",
            "homepage_feature_2_text": "Track every order.",
            "homepage_feature_3_title": "Open a shop",
            "homepage_feature_3_text": "Reach nearby customers.",
            "homepage_section_order": "hero,features",
        })
        self.assertRedirects(response, builder_url)
        self.client.logout()
        hidden_hero_response = self.client.get(reverse("home"))
        self.assertNotContains(hidden_hero_response, "Fresh shops around you")
        self.assertContains(hidden_hero_response, "Find groceries")

    def test_marketplace_builder_saves_merchandising_design_and_revision(self):
        self.client.force_login(self.admin)
        catalog_product = AdminProductCatalog.objects.create(
            product_category="DAIRY",
            product_name="Featured milk",
            image_url="https://example.test/milk.jpg",
        )
        builder_url = reverse("admin_site_theme_builder")
        response = self.client.post(builder_url, {
            "primary_color": "#2563eb",
            "accent_color": "#16a34a",
            "page_background": "#f5f7fb",
            "surface_color": "#ffffff",
            "text_color": "#1f2937",
            "font_family": "Inter, system-ui, sans-serif",
            "desktop_content_width": "1280",
            "desktop_page_padding": "24",
            "mobile_page_padding": "12",
            "component_radius": "12",
            "header_padding": "8",
            "content_padding": "25",
            "footer_padding": "18",
            "header_margin": "0",
            "content_margin": "0",
            "footer_margin": "0",
            "desktop_layout_order": "header,content,footer",
            "mobile_layout_order": "header,content,footer",
            "homepage_content_fields_present": "1",
            "homepage_hero_enabled": "on",
            "homepage_features_enabled": "on",
            "homepage_hero_badge": "Welcome",
            "homepage_hero_title": "Local marketplace",
            "homepage_hero_text": "Shop nearby.",
            "homepage_primary_cta": "Sign in",
            "homepage_secondary_cta": "Join",
            "homepage_feature_1_title": "Local",
            "homepage_feature_1_text": "Nearby shops.",
            "homepage_feature_2_title": "Fast",
            "homepage_feature_2_text": "Track orders.",
            "homepage_feature_3_title": "Grow",
            "homepage_feature_3_text": "Sell more.",
            "homepage_section_order": "hero,features",
            "marketplace_content_fields_present": "1",
            "marketplace_hero_enabled": "on",
            "marketplace_nearby_shops_enabled": "",
            "marketplace_category_nav_enabled": "on",
            "marketplace_products_enabled": "on",
            "marketplace_hero_title": "Fresh picks",
            "marketplace_hero_text": "From your local shops",
            "marketplace_section_order": "search,hero,nearby,products,category_nav",
            "marketplace_category_order": "DAIRY,KIRANA",
            "marketplace_featured_product_ids": [str(catalog_product.pk)],
            "marketplace_primary_color": "#145d52",
            "marketplace_accent_color": "#16835f",
            "marketplace_header_start_color": "#102030",
            "marketplace_header_end_color": "#306050",
            "marketplace_card_radius": "20",
            "marketplace_section_gap": "16",
            "marketplace_product_gap": "10",
            "marketplace_content_width": "1920",
            "marketplace_product_width": "220",
            "marketplace_desktop_padding": "20",
            "marketplace_mobile_padding": "4",
        })
        self.assertRedirects(response, builder_url)
        theme = SiteThemeSettings.objects.get(pk=1)
        self.assertFalse(theme.marketplace_nearby_shops_enabled)
        self.assertEqual(theme.marketplace_hero_title, "Fresh picks")
        self.assertEqual(
            theme.marketplace_section_order,
            "search,hero,nearby,products,category_nav",
        )
        self.assertEqual(theme.marketplace_featured_product_ids, [str(catalog_product.pk)])
        self.assertEqual(theme.marketplace_card_radius, 20)
        self.assertEqual(theme.marketplace_content_width, 1920)
        self.assertEqual(theme.marketplace_product_width, 220)
        self.assertEqual(theme.marketplace_desktop_padding, 20)
        self.assertEqual(theme.marketplace_mobile_padding, 4)
        saved_revision = SiteThemeRevision.objects.latest("pk")
        self.assertEqual(
            saved_revision.snapshot["marketplace_hero_title"],
            "Fresh picks",
        )
        self.assertEqual(saved_revision.snapshot["marketplace_content_width"], 1920)

        theme.marketplace_content_width = 1400
        theme.marketplace_hero_title = "Temporary heading"
        theme.save()
        restore_response = self.client.post(builder_url, {
            "restore_theme_revision": str(saved_revision.pk),
        })
        self.assertRedirects(restore_response, builder_url)
        theme.refresh_from_db()
        self.assertEqual(theme.marketplace_hero_title, "Fresh picks")
        self.assertEqual(theme.marketplace_content_width, 1920)
        self.assertEqual(theme.marketplace_featured_product_ids, [str(catalog_product.pk)])

    def test_superuser_can_configure_mobile_marketplace_layout(self):
        self.client.force_login(self.admin)
        dashboard_url = reverse("admin_catalog_dashboard")
        dashboard = self.client.get(dashboard_url)
        self.assertContains(dashboard, "Marketplace mobile layout")
        self.assertContains(dashboard, "Products per row")
        self.assertContains(dashboard, "Rows per category")

        response = self.client.post(
            dashboard_url,
            {
                "save_marketplace_layout": "1",
                "products_per_row": "6",
                "rows_per_category": "3",
            },
        )
        self.assertRedirects(response, dashboard_url)
        layout = MarketplaceLayoutSettings.objects.get(pk=1)
        self.assertEqual(layout.products_per_row, 6)
        self.assertEqual(layout.rows_per_category, 3)

        invalid_response = self.client.post(
            dashboard_url,
            {
                "save_marketplace_layout": "1",
                "products_per_row": "9",
                "rows_per_category": "3",
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "Ensure this value is less than or equal to 8.")
        layout.refresh_from_db()
        self.assertEqual(layout.products_per_row, 6)

    def test_admin_controls_fields_copied_from_marketplace_products(self):
        self.client.force_login(self.admin)
        dashboard_url = reverse("admin_catalog_dashboard")
        dashboard = self.client.get(dashboard_url)
        self.assertContains(dashboard, "Shop product copy settings")
        self.assertContains(dashboard, "Description")

        response = self.client.post(
            dashboard_url,
            {
                "save_product_clone_settings": "1",
                "allowed_fields": ["unit", "image_url"],
            },
        )

        self.assertRedirects(response, dashboard_url)
        settings_obj = ProductCloneSettings.objects.get(pk=1)
        self.assertEqual(
            set(settings_obj.allowed_fields),
            {"unit", "image_url"},
        )

    def test_shop_owner_can_add_marketplace_product_with_zero_stock_and_allowed_fields(self):
        source_owner = User.objects.create_user(
            username="marketplace_clone_source_owner",
            password="test-password",
        )
        source_shop = Shop.objects.create(
            name="Marketplace Source Shop",
            owner=source_owner,
            market_category="DAILY MARKET",
        )
        category = ProductCategory.objects.create(
            name="MARKETPLACE CLONE FRUITS",
            market_category="DAILY MARKET",
        )
        source_product = Product.objects.create(
            created_by=source_owner,
            product_category=category.name,
            product_name="Clone Test Mango",
            product_title="Source title should not copy",
            product_description="Source description should not copy",
            sku="SOURCE-MANGO",
            price="90.00",
            unit="kg",
            image_url="https://example.com/source-mango.jpg",
            hsn_code="080450",
            gst_rate="5.00",
        )
        source_listing = ProductShop.objects.create(
            shop=source_shop,
            product=source_product,
            price="95.00",
            stock=4,
            is_active=True,
        )
        legacy_category = ProductCategory.objects.create(
            name="MARKETPLACE CLONE LEGACY",
            market_category="",
        )
        legacy_market_product = Product.objects.create(
            created_by=source_owner,
            product_category=legacy_category.name,
            product_name="Legacy Market Product",
            price="31.00",
            unit="each",
        )
        legacy_market_listing = ProductShop.objects.create(
            shop=source_shop,
            product=legacy_market_product,
            price="31.00",
            stock=2,
            is_active=True,
        )
        service_category = ProductCategory.objects.create(
            name="MARKETPLACE CLONE SERVICE",
            market_category="SERVICE MARKET",
        )
        service_product = Product.objects.create(
            created_by=source_owner,
            product_category=service_category.name,
            product_name="Out of Market Service",
            price="40.00",
            unit="visit",
        )
        out_of_market_listing = ProductShop.objects.create(
            shop=source_shop,
            product=service_product,
            price="40.00",
            stock=2,
            is_active=True,
        )
        other_market_shop = Shop.objects.create(
            name="Service Market Shop",
            owner=source_owner,
            market_category="SERVICE MARKET",
        )
        other_market_product = Product.objects.create(
            created_by=source_owner,
            product_category=category.name,
            product_name="Listing From Wrong Market",
            price="50.00",
            unit="kg",
        )
        other_market_listing = ProductShop.objects.create(
            shop=other_market_shop,
            product=other_market_product,
            price="50.00",
            stock=2,
            is_active=True,
        )
        owner = User.objects.create_user(
            username="marketplace_clone_destination_owner",
            password="test-password",
        )
        destination_shop = Shop.objects.create(
            name="Marketplace Destination Shop",
            owner=owner,
        )
        ProductCloneSettings.objects.create(
            allowed_fields=["unit", "image_url", "hsn_code", "gst_rate"],
        )

        self.client.force_login(owner)
        browse = self.client.get(
            reverse("add_marketplace_products"),
            {"shop": destination_shop.pk},
        )
        self.assertEqual(browse.status_code, 200)
        self.assertContains(browse, source_product.product_name)
        self.assertContains(browse, legacy_market_product.product_name)
        self.assertNotContains(browse, service_product.product_name)
        self.assertNotContains(browse, other_market_product.product_name)
        self.assertContains(browse, "DAILY MARKET · active listings with available stock")

        mismatched_response = self.client.post(
            reverse("add_marketplace_products"),
            {
                "shop": destination_shop.pk,
                "source_listing": [
                    source_listing.pk,
                    legacy_market_listing.pk,
                    out_of_market_listing.pk,
                ],
            },
        )
        self.assertRedirects(
            mismatched_response,
            f"{reverse('add_marketplace_products')}?shop={destination_shop.pk}",
        )
        self.assertFalse(
            Product.objects.filter(
                created_by=owner,
                product_name=service_product.product_name,
            ).exists()
        )

        response = self.client.post(
            reverse("add_marketplace_products"),
            {
                "shop": destination_shop.pk,
                "source_listing": [
                    source_listing.pk,
                    legacy_market_listing.pk,
                ],
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('product_list')}?focus_shop={destination_shop.pk}",
        )
        cloned_product = Product.objects.get(
            created_by=owner,
            product_category=source_product.product_category,
            product_name=source_product.product_name,
        )
        assignment = ProductShop.objects.get(
            shop=destination_shop,
            product=cloned_product,
        )
        self.assertEqual(assignment.stock, Decimal("0.0000"))
        self.assertEqual(assignment.price, Decimal("95.00"))
        self.assertEqual(cloned_product.price, Decimal("95.00"))
        self.assertEqual(cloned_product.unit, "kg")
        self.assertEqual(cloned_product.image_url, "https://example.com/source-mango.jpg")
        self.assertEqual(cloned_product.hsn_code, "080450")
        self.assertEqual(cloned_product.gst_rate, Decimal("5.00"))
        self.assertEqual(cloned_product.product_description, "")
        self.assertEqual(cloned_product.product_title, "")
        self.assertEqual(cloned_product.sku, "")
        legacy_clone = Product.objects.get(
            created_by=owner,
            product_category=legacy_category.name,
            product_name=legacy_market_product.product_name,
        )
        self.assertTrue(
            ProductShop.objects.filter(
                shop=destination_shop,
                product=legacy_clone,
                stock=0,
            ).exists()
        )

        repeated_response = self.client.post(
            reverse("add_marketplace_products"),
            {
                "shop": destination_shop.pk,
                "source_listing": [
                    source_listing.pk,
                    legacy_market_listing.pk,
                ],
            },
        )
        self.assertRedirects(
            repeated_response,
            f"{reverse('product_list')}?focus_shop={destination_shop.pk}",
        )
        self.assertEqual(
            ProductShop.objects.filter(
                shop=destination_shop,
                product=cloned_product,
            ).count(),
            1,
        )
        self.assertEqual(
            ProductShop.objects.filter(
                shop=destination_shop,
                product=legacy_clone,
            ).count(),
            1,
        )

    def test_superuser_can_update_site_branding_and_upload_a_logo(self):
        self.client.force_login(self.admin)
        logo_data = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\xdac\xf8\x0f\x00"
            b"\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND"
            b"\xaeB`\x82"
        )
        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
        ):
            response = self.client.post(
                reverse("admin_catalog_dashboard"),
                {
                    "save_site_branding": "1",
                    "site_name": "Market Hub",
                    "tagline": "Local shops made easy",
                    "support_email": "help@example.test",
                    "support_phone": "1234567890",
                    "support_address": "Main Street",
                    "logo": SimpleUploadedFile(
                        "logo.png",
                        logo_data,
                        content_type="image/png",
                    ),
                },
            )

            self.assertRedirects(response, reverse("admin_catalog_dashboard"))
            branding = SiteBrandingSettings.objects.get(pk=1)
            self.assertEqual(branding.site_name, "Market Hub")
            self.assertEqual(branding.tagline, "Local shops made easy")
            self.assertTrue(branding.logo.name.endswith("logo.png"))
            self.assertContains(
                self.client.get(reverse("admin_catalog_dashboard")),
                "Product catalog · Market Hub",
                html=False,
            )
            self.assertContains(
                self.client.get(reverse("terms")),
                "Market Hub",
            )

    def test_admin_can_configure_android_and_ios_app_downloads(self):
        self.client.force_login(self.admin)
        dashboard_url = reverse("admin_catalog_dashboard")
        response = self.client.post(
            dashboard_url,
            {
                "save_site_branding": "1",
                "site_name": "Market Hub",
                "android_app_url": "https://downloads.example.test/sellerhelper.apk",
                "ios_app_url": "https://apps.apple.com/app/example",
            },
        )

        self.assertRedirects(response, dashboard_url)
        branding = SiteBrandingSettings.objects.get(pk=1)
        self.assertEqual(
            branding.android_app_url,
            "https://downloads.example.test/sellerhelper.apk",
        )
        self.assertEqual(
            branding.ios_app_url,
            "https://apps.apple.com/app/example",
        )

        downloads = self.client.get(reverse("app_download_page"))
        self.assertEqual(downloads.status_code, 200)
        self.assertEqual(len(downloads.context["app_downloads"]), 2)
        self.assertEqual(
            [app["url"] for app in downloads.context["app_downloads"]],
            [branding.android_app_url, branding.ios_app_url],
        )
        self.assertTrue(all(
            app["qr_code_data_uri"].startswith("data:image/png;base64,")
            for app in downloads.context["app_downloads"]
        ))
        homepage = self.client.get(reverse("home"))
        self.assertContains(homepage, 'alt="QR code for android app download"')
        self.assertContains(homepage, 'alt="QR code for ios app download"')
        self.assertContains(homepage, "Download app")
        self.assertNotContains(homepage, "app_download_page")
        self.assertContains(downloads, 'alt="QR code to download the app for android"')
        self.assertContains(downloads, 'alt="QR code to download the app for ios"')
        self.assertContains(downloads, "Download for Android")
        self.assertContains(downloads, "Download for iPhone or iPad")

    def test_app_download_page_and_footer_hide_unconfigured_platforms(self):
        branding = SiteBrandingSettings.objects.create(
            android_app_url="https://downloads.example.test/sellerhelper.apk",
        )
        downloads = self.client.get(reverse("app_download_page"))
        self.assertEqual(downloads.status_code, 200)
        self.assertEqual(
            [app["platform"] for app in downloads.context["app_downloads"]],
            ["android"],
        )
        self.assertContains(downloads, "Download for Android")
        self.assertNotContains(downloads, "Download for iPhone or iPad")

        homepage = self.client.get(reverse("home"))
        self.assertContains(homepage, "Download app")
        self.assertContains(homepage, 'alt="QR code for android app download"')
        self.assertNotContains(homepage, "app_download_page")
        self.assertNotContains(
            homepage,
            '<a class="nav-link" href="/download-app/">',
        )
        self.assertNotContains(homepage, "Download for iPhone or iPad")

        branding.android_app_url = ""
        branding.ios_app_url = "https://apps.apple.com/app/example"
        branding.save(update_fields=["android_app_url", "ios_app_url"])
        ios_downloads = self.client.get(reverse("app_download_page"))
        self.assertEqual(ios_downloads.status_code, 200)
        self.assertEqual(
            [app["platform"] for app in ios_downloads.context["app_downloads"]],
            ["ios"],
        )
        self.assertContains(ios_downloads, "Download for iPhone or iPad")
        self.assertNotContains(ios_downloads, "Download for Android")

        branding.ios_app_url = ""
        branding.save(update_fields=["ios_app_url"])
        self.assertEqual(
            self.client.get(reverse("app_download_page")).status_code,
            404,
        )
        homepage_without_links = self.client.get(reverse("home"))
        self.assertNotContains(homepage_without_links, "Download app")
        self.assertNotContains(
            homepage_without_links,
            "QR code for android app download",
        )

    def test_django_admin_can_manage_product_categories_and_catalog_products(self):
        self.client.force_login(self.admin)

        category_add_url = reverse("admin:seller_productcategory_add")
        category_list_url = reverse("admin:seller_productcategory_changelist")
        product_list_url = reverse("admin:seller_adminproductcatalog_changelist")

        self.assertEqual(self.client.get(category_add_url).status_code, 200)
        self.assertEqual(self.client.get(product_list_url).status_code, 200)
        response = self.client.post(
            category_add_url,
            {"name": "ADMIN CREATED", "sort_order": "4", "_save": "Save"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(ProductCategory.objects.filter(name="ADMIN CREATED").exists())
        self.assertContains(response, "ADMIN CREATED")

    def test_catalog_product_list_is_paginated_and_keeps_filters(self):
        AdminProductCatalog.objects.bulk_create([
            AdminProductCatalog(
                product_category="PAGINATION",
                product_name=f"Page Product {index:02d}",
            )
            for index in range(26)
        ])
        self.client.force_login(self.admin)

        first_page = self.client.get(
            reverse("admin_catalog_dashboard"),
            {"category": "PAGINATION", "status": "active", "q": "Page Product"},
        )
        second_page = self.client.get(
            reverse("admin_catalog_dashboard"),
            {
                "category": "PAGINATION",
                "status": "active",
                "q": "Page Product",
                "page": "2",
            },
        )

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context["products"].paginator.count, 26)
        self.assertEqual(len(first_page.context["products"]), 25)
        self.assertContains(
            first_page,
            "category=PAGINATION&amp;status=active&amp;page=2",
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.context["products"].number, 2)
        self.assertEqual(len(second_page.context["products"]), 1)
        self.assertEqual(
            second_page.context["products"][0].product_name,
            "Page Product 25",
        )

    def test_catalog_products_are_editable_in_table_and_export_to_excel(self):
        fruits_product = AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Catalog Export Mango",
            image_url="https://example.com/mango.jpg",
            image_url_2="https://example.com/mango-2.jpg",
            hsn_code="080450",
            sac_code="995411",
            gst_rate=Decimal("5.00"),
        )
        AdminProductCatalog.objects.create(
            product_category="DAIRY",
            product_name="Catalog Export Milk",
            image_url="https://example.com/milk.jpg",
            hsn_code="040120",
            gst_rate=Decimal("5.00"),
        )
        self.client.force_login(self.admin)

        dashboard = self.client.get(
            reverse("admin_catalog_dashboard"),
            {"q": "Catalog Export Mango"},
        )
        self.assertContains(
            dashboard,
            f'name="product-{fruits_product.pk}-product_name"',
        )
        self.assertContains(
            dashboard,
            f'name="product-{fruits_product.pk}-product_category"',
        )
        self.assertContains(dashboard, "Download all products")
        self.assertContains(
            dashboard,
            f'name="product-{fruits_product.pk}-image_url_2"',
        )
        self.assertContains(dashboard, "Save changes")
        self.assertContains(dashboard, "Add single product")
        self.assertContains(dashboard, 'name="save_product"', html=False)
        self.assertEqual(
            dashboard.content.decode().count("Save changes"),
            1,
        )
        self.assertContains(dashboard, "catalog-product-filters")
        category_dashboard = self.client.get(
            reverse("admin_catalog_dashboard"),
            {"category": "FRUITS"},
        )
        self.assertContains(category_dashboard, "Download FRUITS products")

        return_to = (
            reverse("admin_catalog_dashboard")
            + "?q=Updated&category=FRUITS&status=active&page=2"
        )
        edit_response = self.client.post(
            reverse(
                "admin_catalog_product_edit",
                kwargs={"pk": fruits_product.pk},
            ),
            {
                "product_category": "FRUITS",
                "product_name": "Updated Table Mango",
                "image_url": "https://example.com/updated-mango.jpg",
                "image_url_2": fruits_product.image_url_2,
                "image_url_3": "",
                "image_url_4": "",
                "image_url_5": "",
                "hsn_code": "080450",
                "gst_rate": "12.00",
                "is_active": "on",
                "return_to": return_to,
            },
        )
        self.assertRedirects(
            edit_response,
            return_to + "#catalog-products",
        )
        fruits_product.refresh_from_db()
        self.assertEqual(fruits_product.product_name, "Updated Table Mango")
        self.assertEqual(fruits_product.gst_rate, Decimal("12.00"))

        all_export = self.client.get(
            reverse("admin_catalog_product_export"),
        )
        self.assertEqual(all_export.status_code, 200)
        self.assertEqual(
            all_export["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        with zipfile.ZipFile(io.BytesIO(all_export.content)) as workbook:
            sheet = ElementTree.fromstring(
                workbook.read("xl/worksheets/sheet1.xml"),
            )
        exported_values = [
            cell.findtext(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}is/"
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
            ) or cell.findtext(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v",
            )
            for cell in sheet.findall(
                ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c",
            )
        ]
        self.assertIn("Updated Table Mango", exported_values)
        self.assertIn("Catalog Export Milk", exported_values)
        self.assertIn("12.00", exported_values)
        self.assertIn("995411", exported_values)

        category_export = self.client.get(
            reverse("admin_catalog_product_export"),
            {"category": "FRUITS"},
        )
        with zipfile.ZipFile(io.BytesIO(category_export.content)) as workbook:
            category_sheet = ElementTree.fromstring(
                workbook.read("xl/worksheets/sheet1.xml"),
            )
        category_values = [
            cell.findtext(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}is/"
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
            ) or cell.findtext(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v",
            )
            for cell in category_sheet.findall(
                ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c",
            )
        ]
        self.assertIn("Updated Table Mango", category_values)
        self.assertNotIn("Catalog Export Milk", category_values)

    def test_catalog_master_save_preserves_filters_and_saves_all_atomically(self):
        first = AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Master Save Apple",
            image_url="https://example.com/apple.jpg",
            hsn_code="080810",
            gst_rate=Decimal("5.00"),
        )
        second = AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Master Save Pear",
            image_url="https://example.com/pear.jpg",
            hsn_code="080830",
            gst_rate=Decimal("5.00"),
        )
        self.client.force_login(self.admin)
        return_to = (
            reverse("admin_catalog_dashboard")
            + "?q=Master&category=FRUITS&status=active&page=1"
        )

        def product_fields(product, *, name, gst_rate="12.00"):
            prefix = f"product-{product.pk}"
            return {
                f"{prefix}-product_category": product.product_category,
                f"{prefix}-product_name": name,
                f"{prefix}-image_url": product.image_url,
                f"{prefix}-image_url_2": "",
                f"{prefix}-image_url_3": "",
                f"{prefix}-image_url_4": "",
                f"{prefix}-image_url_5": "",
                f"{prefix}-hsn_code": product.hsn_code,
                f"{prefix}-gst_rate": gst_rate,
            }

        invalid_data = {
            "return_to": return_to,
            **product_fields(first, name="Changed Apple"),
            **product_fields(second, name="Changed Pear", gst_rate="101"),
            f"product-{first.pk}-is_active": "on",
            f"product-{second.pk}-is_active": "on",
        }
        invalid_response = self.client.post(
            reverse("admin_catalog_products_save_all"),
            invalid_data,
        )
        self.assertRedirects(
            invalid_response,
            return_to + "#catalog-products",
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.product_name, "Master Save Apple")
        self.assertEqual(second.product_name, "Master Save Pear")

        valid_data = {
            "return_to": return_to,
            **product_fields(first, name="Changed Apple"),
            **product_fields(second, name="Changed Pear"),
            f"product-{first.pk}-is_active": "on",
        }
        response = self.client.post(
            reverse("admin_catalog_products_save_all"),
            valid_data,
        )
        self.assertRedirects(response, return_to + "#catalog-products")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.product_name, "Changed Apple")
        self.assertEqual(first.gst_rate, Decimal("12.00"))
        self.assertTrue(first.is_active)
        self.assertEqual(second.product_name, "Changed Pear")
        self.assertFalse(second.is_active)

        row_save_data = {
            "return_to": return_to,
            "save_product": str(first.pk),
            **product_fields(first, name="Saved Only Apple"),
            **product_fields(second, name="Invalid Pear", gst_rate="101"),
            f"product-{first.pk}-is_active": "on",
        }
        row_response = self.client.post(
            reverse("admin_catalog_products_save_all"),
            row_save_data,
        )
        self.assertRedirects(
            row_response,
            return_to + "#catalog-products",
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.product_name, "Saved Only Apple")
        self.assertEqual(second.product_name, "Changed Pear")

    def test_excel_import_updates_by_catalog_id_and_adds_rows_atomically(self):
        product = AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Import Mango",
            image_url="https://example.com/import-mango.jpg",
            hsn_code="080450",
            gst_rate=Decimal("5.00"),
        )
        self.client.force_login(self.admin)
        export = self.client.get(reverse("admin_catalog_product_export"))
        with zipfile.ZipFile(io.BytesIO(export.content)) as source:
            files = {
                name: source.read(name)
                for name in source.namelist()
            }

        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        sheet = ElementTree.fromstring(files["xl/worksheets/sheet1.xml"])
        rows = sheet.findall(f".//{namespace}sheetData/{namespace}row")

        def inline_value(row, reference):
            return row.findtext(
                f"{namespace}c[@r='{reference}']/{namespace}is/"
                f"{namespace}t",
            )

        product_row = next(
            row
            for row in rows
            if inline_value(row, f"A{row.get('r')}") == str(product.pk)
        )
        product_row_number = int(product_row.get("r"))
        name_cell = product_row.find(
            f"{namespace}c[@r='C{product_row_number}']",
        )
        name_cell.find(f"{namespace}is/{namespace}t").text = "Renamed Import Mango"
        sheet_data = sheet.find(f"{namespace}sheetData")
        added_row_number = max(int(row.get("r")) for row in rows) + 1
        added_row = ElementTree.SubElement(
            sheet_data,
            f"{namespace}row",
            {"r": str(added_row_number)},
        )
        for cell_ref, value in (
            (f"B{added_row_number}", "FRUITS"),
            (f"C{added_row_number}", "New Imported Pear"),
            (f"D{added_row_number}", "https://example.com/import-pear.jpg"),
            (f"I{added_row_number}", "080830"),
            (f"J{added_row_number}", "5"),
            (f"K{added_row_number}", "Yes"),
        ):
            cell = ElementTree.SubElement(
                added_row,
                f"{namespace}c",
                {"r": cell_ref, "t": "inlineStr"},
            )
            inline = ElementTree.SubElement(cell, f"{namespace}is")
            text = ElementTree.SubElement(inline, f"{namespace}t")
            text.text = value
        files["xl/worksheets/sheet1.xml"] = ElementTree.tostring(
            sheet,
            encoding="utf-8",
            xml_declaration=True,
        )
        workbook_bytes = io.BytesIO()
        with zipfile.ZipFile(workbook_bytes, "w", zipfile.ZIP_DEFLATED) as output:
            for name, content in files.items():
                output.writestr(name, content)

        response = self.client.post(
            reverse("admin_catalog_product_import"),
            {
                "catalog_excel": SimpleUploadedFile(
                    "catalog.xlsx",
                    workbook_bytes.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
            follow=True,
        )
        import_messages = " ".join(
            str(message) for message in response.context["messages"]
        )
        self.assertIn("1 product(s) added and 1 product(s) updated;", import_messages)
        product.refresh_from_db()
        self.assertEqual(product.product_name, "Renamed Import Mango")
        self.assertTrue(
            AdminProductCatalog.objects.filter(
                product_category="FRUITS",
                product_name="New Imported Pear",
                hsn_code="080830",
            ).exists()
        )

        with zipfile.ZipFile(io.BytesIO(workbook_bytes.getvalue())) as source:
            invalid_files = {
                name: source.read(name)
                for name in source.namelist()
            }
        invalid_sheet = ElementTree.fromstring(
            invalid_files["xl/worksheets/sheet1.xml"],
        )
        invalid_hsn = invalid_sheet.find(
            f".//{namespace}c[@r='I{product_row_number}']/"
            f"{namespace}is/{namespace}t",
        )
        invalid_hsn.text = "invalid"
        invalid_files["xl/worksheets/sheet1.xml"] = ElementTree.tostring(
            invalid_sheet,
            encoding="utf-8",
            xml_declaration=True,
        )
        invalid_workbook_bytes = io.BytesIO()
        with zipfile.ZipFile(
            invalid_workbook_bytes,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as output:
            for name, content in invalid_files.items():
                output.writestr(name, content)
        failed_import = self.client.post(
            reverse("admin_catalog_product_import"),
            {
                "catalog_excel": SimpleUploadedFile(
                    "invalid-catalog.xlsx",
                    invalid_workbook_bytes.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
            follow=True,
        )
        self.assertContains(failed_import, "Import canceled; no products were changed.")
        self.assertEqual(
            AdminProductCatalog.objects.filter(
                product_category="FRUITS",
                product_name="New Imported Pear",
            ).count(),
            1,
        )

    def test_hsn_reference_upload_and_product_name_suggestions(self):
        self.client.force_login(self.admin)

        def build_reference_workbook(rows):
            namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            worksheet = ElementTree.Element(f"{{{namespace}}}worksheet")
            sheet_data = ElementTree.SubElement(
                worksheet,
                f"{{{namespace}}}sheetData",
            )
            for row_number, values in enumerate(rows, start=1):
                row = ElementTree.SubElement(
                    sheet_data,
                    f"{{{namespace}}}row",
                    {"r": str(row_number)},
                )
                for column_number, value in enumerate(values, start=1):
                    column_letter = chr(ord("A") + column_number - 1)
                    cell = ElementTree.SubElement(
                        row,
                        f"{{{namespace}}}c",
                        {
                            "r": f"{column_letter}{row_number}",
                            "t": "inlineStr",
                        },
                    )
                    inline = ElementTree.SubElement(
                        cell,
                        f"{{{namespace}}}is",
                    )
                    text = ElementTree.SubElement(
                        inline,
                        f"{{{namespace}}}t",
                    )
                    text.text = value

            package_files = {
                "[Content_Types].xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    "</Types>"
                ),
                "_rels/.rels": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                    "</Relationships>"
                ),
                "xl/workbook.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="HSN" sheetId="1" r:id="rId1"/></sheets>'
                    "</workbook>"
                ),
                "xl/_rels/workbook.xml.rels": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                    "</Relationships>"
                ),
                "xl/worksheets/sheet1.xml": ElementTree.tostring(
                    worksheet,
                    encoding="unicode",
                ),
            }
            workbook = io.BytesIO()
            with zipfile.ZipFile(workbook, "w", zipfile.ZIP_DEFLATED) as output:
                for name, content in package_files.items():
                    output.writestr(name, content)
            return workbook.getvalue()

        response = self.client.post(
            reverse("admin_catalog_hsn_reference_import"),
            {
                "hsn_reference_excel": SimpleUploadedFile(
                    "hsn-reference.xlsx",
                    build_reference_workbook([
                        ("HSN_CD", "HSN_Description"),
                        ("01", "LIVE ANIMALS"),
                        ("01011010", "LIVE HORSES, ASSES, MULES AND HINNIES"),
                        ("010121", "PURE-BRED BREEDING ANIMALS"),
                    ]),
                ),
            },
            follow=True,
        )
        self.assertIn(
            "Imported 3 HSN code description(s)",
            " ".join(str(message) for message in response.context["messages"]),
        )
        self.assertTrue(
            HSNCodeDescription.objects.filter(
                hsn_code="01",
                description="LIVE ANIMALS",
            ).exists()
        )
        catalog_page = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertContains(catalog_page, "Loaded HSN entries")
        self.assertContains(
            catalog_page,
            '<span class="badge text-bg-primary fs-6 ms-1">3</span>',
            html=True,
        )
        self.assertContains(catalog_page, "Preview uploaded HSN entries")
        self.assertContains(catalog_page, "LIVE ANIMALS")
        suggestions = self.client.get(
            reverse("admin_catalog_hsn_suggestions"),
            {"q": "live animal"},
        )
        self.assertEqual(suggestions.status_code, 200)
        self.assertEqual(suggestions.json()["results"][0]["code"], "01")
        plural_suggestions = self.client.get(
            reverse("admin_catalog_hsn_suggestions"),
            {"q": "horse"},
        )
        self.assertIn(
            "01011010",
            {
                item["code"]
                for item in plural_suggestions.json()["results"]
            },
        )
        self.assertEqual(
            self.client.get(
                reverse("admin_catalog_hsn_suggestions"),
                {"q": "x"},
            ).json()["results"],
            [],
        )
        create_form = self.client.get(reverse("admin_catalog_product_create"))
        self.assertContains(create_form, 'data-hsn-product-name="catalog-form"')
        self.assertContains(create_form, 'data-hsn-product-code="catalog-form"')
        self.assertContains(create_form, "/admin-panel/catalog/hsn/suggestions/")
        self.assertContains(create_form, "2, 4, 6, or 8 digits")
        selected_hsn_form = AdminProductCatalogForm(data={
            "product_category": "FRUITS",
            "product_name": "Live Animal",
            "image_url": "https://example.com/live-animal.jpg",
            "image_url_2": "",
            "image_url_3": "",
            "image_url_4": "",
            "image_url_5": "",
            "hsn_code": "01",
            "gst_rate": "0",
            "is_active": "on",
        })
        self.assertTrue(selected_hsn_form.is_valid(), selected_hsn_form.errors)

        invalid_response = self.client.post(
            reverse("admin_catalog_hsn_reference_import"),
            {
                "hsn_reference_excel": SimpleUploadedFile(
                    "invalid-hsn.xlsx",
                    build_reference_workbook([
                        ("HSN_CD", "HSN_Description"),
                        ("ABC", "INVALID CODE"),
                    ]),
                ),
            },
            follow=True,
        )
        self.assertIn(
            "HSN upload canceled; existing suggestions were not changed.",
            " ".join(str(message) for message in invalid_response.context["messages"]),
        )
        self.assertTrue(
            HSNCodeDescription.objects.filter(
                hsn_code="01",
                description="LIVE ANIMALS",
            ).exists()
        )

    def test_sac_workbook_upload_and_service_market_suggestions(self):
        self.client.force_login(self.admin)
        sac_workbook_path = Path(__file__).resolve().parents[1] / "sac_data.xlsx"
        upload_response = self.client.post(
            reverse("admin_catalog_sac_reference_import"),
            {
                "sac_reference_excel": SimpleUploadedFile(
                    sac_workbook_path.name,
                    sac_workbook_path.read_bytes(),
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                ),
            },
            follow=True,
        )
        messages_text = " ".join(
            str(message) for message in upload_response.context["messages"]
        )
        self.assertIn("Imported ", messages_text)
        self.assertGreater(SACCodeDescription.objects.count(), 600)
        self.assertTrue(
            SACCodeDescription.objects.filter(
                sac_code="9954",
                description="Construction services",
            ).exists()
        )

        service_category = ProductCategory.objects.create(
            name="SERVICE TEST",
            market_category="SERVICE MARKET",
        )
        regular_category = ProductCategory.objects.create(
            name="REGULAR TEST",
            market_category="DAILY MARKET",
        )
        suggestions = self.client.get(
            reverse("admin_catalog_sac_suggestions"),
            {
                "q": "construction services",
                "category": service_category.name,
            },
        )
        self.assertEqual(suggestions.status_code, 200)
        self.assertIn(
            "9954",
            {item["code"] for item in suggestions.json()["results"]},
        )
        regular_category_suggestions = self.client.get(
            reverse("admin_catalog_sac_suggestions"),
            {"q": "construction services", "category": regular_category.name},
        )
        self.assertEqual(regular_category_suggestions.json()["results"], [])

        product_form = self.client.get(reverse("admin_catalog_product_create"))
        self.assertContains(product_form, 'data-sac-product-code="catalog-form"')
        self.assertContains(
            product_form,
            reverse("admin_catalog_sac_suggestions"),
        )
        sac_product_form = AdminProductCatalogForm(data={
            "product_category": service_category.name,
            "product_name": "Construction service",
            "image_url": "https://example.com/construction-service.jpg",
            "hsn_code": "",
            "sac_code": "995411",
            "gst_rate": "18",
            "is_active": "on",
        })
        self.assertTrue(sac_product_form.is_valid(), sac_product_form.errors)
        self.assertEqual(sac_product_form.save().sac_code, "995411")

    def test_project_hsn_workbook_imports_more_than_5000_rows(self):
        self.client.force_login(self.admin)
        workbook_path = Path(__file__).resolve().parents[1] / "hsn_data.xlsx"
        response = self.client.post(
            reverse("admin_catalog_hsn_reference_import"),
            {
                "hsn_reference_excel": SimpleUploadedFile(
                    workbook_path.name,
                    workbook_path.read_bytes(),
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                ),
            },
            follow=True,
        )
        messages_text = " ".join(
            str(message) for message in response.context["messages"]
        )
        self.assertIn("Imported ", messages_text)
        self.assertIn("Skipped ", messages_text)
        self.assertGreater(HSNCodeDescription.objects.count(), 5_000)
        self.assertTrue(
            HSNCodeDescription.objects.filter(
                hsn_code="01",
                description="LIVE ANIMALS",
            ).exists()
        )

    def test_hsn_reference_csv_upload_and_zero_padded_excel_code(self):
        self.client.force_login(self.admin)
        csv_response = self.client.post(
            reverse("admin_catalog_hsn_reference_import"),
            {
                "hsn_reference_excel": SimpleUploadedFile(
                    "hsn-reference.csv",
                    (
                        "HSN_CD,HSN_Description\r\n"
                        "01,LIVE ANIMALS\r\n"
                    ).encode("utf-8"),
                    content_type="text/csv",
                ),
            },
            follow=True,
        )
        self.assertIn(
            "Imported 1 HSN code description(s)",
            " ".join(str(message) for message in csv_response.context["messages"]),
        )
        self.assertTrue(
            HSNCodeDescription.objects.filter(hsn_code="01").exists(),
        )

        semicolon_response = self.client.post(
            reverse("admin_catalog_hsn_reference_import"),
            {
                "hsn_reference_excel": SimpleUploadedFile(
                    "hsn-reference.csv",
                    (
                        "sep=;\r\n"
                        "HSN Code;Description\r\n"
                        '0101;LIVE ANIMALS, PURE-BRED\r\n'
                        "0102;LIVE BOVINE ANIMALS\r\n"
                    ).encode("utf-8"),
                    content_type="text/csv",
                ),
            },
            follow=True,
        )
        self.assertIn(
            "Imported 2 HSN code description(s)",
            " ".join(str(message) for message in semicolon_response.context["messages"]),
        )
        self.assertEqual(HSNCodeDescription.objects.count(), 2)
        self.assertTrue(
            HSNCodeDescription.objects.filter(
                hsn_code="0101",
                description="LIVE ANIMALS, PURE-BRED",
            ).exists()
        )

        numeric_cell = ElementTree.fromstring(
            '<c xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" s="1">'
            "<v>1</v></c>",
        )
        self.assertEqual(
            seller_views._catalog_excel_text(
                numeric_cell,
                [],
                ["", "00"],
            ),
            "01",
        )

    def test_common_grocery_products_are_in_shared_catalog(self):
        self.assertTrue(
            AdminProductCatalog.objects.filter(
                product_category="FRUITS",
                product_name="Mango",
                is_active=True,
            ).exists()
        )

    def test_every_configured_category_has_products_with_image_urls(self):
        categories = list(ProductCategory.objects.values_list("name", flat=True))
        self.assertTrue(categories)
        for category_name in categories:
            with self.subTest(category=category_name):
                products = AdminProductCatalog.objects.filter(
                    product_category=category_name,
                )
                self.assertTrue(products.exists())
                self.assertFalse(products.filter(image_url="").exists())
        self.assertTrue(
            AdminProductCatalog.objects.filter(
                product_category="SABJI",
                product_name="Tomato",
                is_active=True,
            ).exists()
        )

    def test_superuser_sees_catalog_shortcut_in_navigation_in_any_portal(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_catalog_dashboard"))
        self.assertContains(response, "Admin Catalog")

    def test_admin_can_create_category_and_catalog_product_for_shop_forms(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shared product catalog")
        self.assertContains(response, reverse("shop_list"))
        self.assertContains(response, reverse("product_list"))
        self.assertContains(response, reverse("shop_order_list"))
        self.assertContains(response, reverse("client_list"))

        response = self.client.post(
            reverse("admin_catalog_dashboard"),
            {
                "name": "ORGANIC FOODS",
                "market_category": "DAILY MARKET",
                "sort_order": "2",
            },
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        category = ProductCategory.objects.get(name="ORGANIC FOODS")

        form = AdminProductCatalogForm(data={
            "product_category": category.name,
            "product_name": "Organic Apple",
            "image_url": "https://example.com/organic-apple.jpg",
            "hsn_code": "080810",
            "gst_rate": "5.00",
            "is_active": "on",
        })
        self.assertTrue(form.is_valid(), form.errors)
        catalog_product = form.save()

        owner = User.objects.create_user(
            username="catalog_workspace_owner",
            password="test-password",
        )
        shop_product_form = ProductForm(
            user=owner,
            initial={"product_category": category.name},
        )
        self.assertIn(
            (category.name, category.name),
            shop_product_form.fields["product_category"].choices,
        )
        self.assertEqual(
            shop_product_form["product_category"].field.widget.__class__.__name__,
            "Select",
        )
        self.assertIn(
            ("Organic Apple", "Organic Apple"),
            shop_product_form.fields["product_name"].choices,
        )
        self.assertEqual(catalog_product.product_category, category.name)

        shop_form = ShopForm()
        self.assertIn(
            (category.name, category.name),
            shop_form.fields["shop_category"].choices,
        )
        mismatched_market_form = ShopForm(
            data={
                "market_category": "SERVICE MARKET",
                "shop_category": category.name,
            },
            instance=Shop(),
            require_legal_acceptance=False,
        )
        mismatched_market_form.is_valid()
        self.assertIn("shop_category", mismatched_market_form.errors)

        matching_market_form = ShopForm(
            data={
                "market_category": "DAILY MARKET",
                "shop_category": category.name,
            },
            instance=Shop(),
            require_legal_acceptance=False,
        )
        matching_market_form.is_valid()
        self.assertNotIn("shop_category", matching_market_form.errors)

    def test_disabling_category_removes_it_from_new_product_selections(self):
        category = ProductCategory.objects.create(name="SEASONAL GOODS")
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse(
                "admin_catalog_category_toggle",
                kwargs={"pk": category.pk},
            ),
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))

        category.refresh_from_db()
        self.assertFalse(category.is_active)
        self.assertNotIn(
            (category.name, category.name),
            ProductForm().fields["product_category"].choices,
        )

    def test_admin_can_create_edit_and_disable_catalog_product(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin_catalog_product_create"),
            {
                "product_category": "FRUITS",
                "product_name": "Workspace Orange",
                "image_url": "https://example.com/workspace-orange.jpg",
                "image_url_2": "",
                "image_url_3": "",
                "image_url_4": "",
                "image_url_5": "",
                "hsn_code": "080510",
                "gst_rate": "5.00",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        product = AdminProductCatalog.objects.get(product_name="Workspace Orange")
        shop_product = Product.objects.create(
            created_by=self.admin,
            product_category="FRUITS",
            product_name="Workspace Orange",
            price="25.00",
            unit="piece",
        )

        response = self.client.post(
            reverse(
                "admin_catalog_product_edit",
                kwargs={"pk": product.pk},
            ),
            {
                "product_category": "FRUITS",
                "product_name": "Edited Workspace Orange",
                "image_url": "https://example.com/edited-workspace-orange.jpg",
                "image_url_2": "",
                "image_url_3": "",
                "image_url_4": "",
                "image_url_5": "",
                "hsn_code": "080510",
                "gst_rate": "12.00",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        product.refresh_from_db()
        shop_product.refresh_from_db()
        self.assertEqual(product.product_name, "Edited Workspace Orange")
        self.assertEqual(shop_product.product_name, "Edited Workspace Orange")
        self.assertEqual(product.gst_rate, Decimal("12.00"))
        self.assertEqual(
            product.image_url,
            "https://example.com/edited-workspace-orange.jpg",
        )
        response = self.client.post(
            reverse(
                "admin_catalog_product_toggle",
                kwargs={"pk": product.pk},
            ),
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        product.refresh_from_db()
        self.assertFalse(product.is_active)

    def test_admin_catalog_requires_primary_image_and_accepts_four_fallback_urls(self):
        base_data = {
            "product_category": "FRUITS",
            "product_name": "Five Image Apple",
            "image_url_2": "https://example.com/apple-2.jpg",
            "image_url_3": "https://example.com/apple-3.jpg",
            "image_url_4": "https://example.com/apple-4.jpg",
            "image_url_5": "https://example.com/apple-5.jpg",
            "hsn_code": "080810",
            "gst_rate": "5.00",
            "is_active": "on",
        }
        missing_primary = AdminProductCatalogForm(data=base_data)
        self.assertFalse(missing_primary.is_valid())
        self.assertIn("image_url", missing_primary.errors)

        valid_form = AdminProductCatalogForm(data={
            **base_data,
            "image_url": "https://example.com/apple-1.jpg",
        })
        self.assertTrue(valid_form.is_valid(), valid_form.errors)
        catalog_product = valid_form.save()
        self.assertEqual(len(catalog_product.image_urls), 5)

    def test_catalog_dashboard_filters_products_by_status(self):
        active = AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Filter Active Apple",
            is_active=True,
        )
        inactive = AdminProductCatalog.objects.create(
            product_category="FRUITS",
            product_name="Filter Inactive Apple",
            is_active=False,
        )
        self.client.force_login(self.admin)

        active_response = self.client.get(
            reverse("admin_catalog_dashboard"),
            {"status": "active", "q": "Filter"},
        )
        inactive_response = self.client.get(
            reverse("admin_catalog_dashboard"),
            {"status": "inactive", "q": "Filter"},
        )

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(
            [product.product_name for product in active_response.context["products"]],
            [active.product_name],
        )
        self.assertContains(active_response, active.product_name)
        self.assertNotContains(active_response, inactive.product_name)
        self.assertContains(inactive_response, inactive.product_name)
        self.assertNotContains(inactive_response, active.product_name)

    def test_admin_can_rename_category_and_update_products_in_that_category(self):
        category = ProductCategory.objects.create(name="SEASONAL FRUIT")
        AdminProductCatalog.objects.create(
            product_category=category.name,
            product_name="Seasonal Pear",
        )
        shop_owner = User.objects.create_user(
            username="category_rename_product_owner",
            password="test-password",
        )
        product = Product.objects.create(
            created_by=shop_owner,
            product_category=category.name,
            product_name="Pear",
            price="20.00",
        )
        self.client.force_login(self.admin)

        edit_response = self.client.get(
            reverse("admin_catalog_category_edit", kwargs={"pk": category.pk})
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "updates this category")
        response = self.client.post(
            reverse("admin_catalog_category_edit", kwargs={"pk": category.pk}),
            {
                "name": "LOCAL FRUIT",
                "market_category": "SERVICE MARKET",
                "sort_order": "3",
            },
        )

        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        category.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(category.name, "LOCAL FRUIT")
        self.assertEqual(category.market_category, "SERVICE MARKET")
        self.assertEqual(product.product_category, "LOCAL FRUIT")
        self.assertTrue(
            AdminProductCatalog.objects.filter(
                product_category="LOCAL FRUIT",
                product_name="Seasonal Pear",
            ).exists()
        )

    def test_admin_catalog_can_grant_and_revoke_shop_image_permission(self):
        owner = User.objects.create_user(
            username="catalog_permission_owner",
            password="test-password",
        )
        shop = Shop.objects.create(name="Permission Shop", owner=owner)
        self.client.force_login(self.admin)

        dashboard = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertContains(dashboard, "Shop image override permissions")
        self.assertContains(dashboard, shop.name)

        permission_url = reverse(
            "admin_catalog_shop_image_permission",
            kwargs={"pk": shop.pk},
        )
        grant_response = self.client.post(permission_url, {"enabled": "1"})
        self.assertRedirects(grant_response, reverse("admin_catalog_dashboard"))
        shop.refresh_from_db()
        self.assertTrue(shop.allow_product_image_overrides)

        revoke_response = self.client.post(permission_url, {"enabled": "0"})
        self.assertRedirects(revoke_response, reverse("admin_catalog_dashboard"))
        shop.refresh_from_db()
        self.assertFalse(shop.allow_product_image_overrides)

    def test_admin_can_grant_and_revoke_shared_rider_group_creation(self):
        owner = User.objects.create_user(
            username="catalog_group_permission_owner",
            password="test-password",
        )
        shop = Shop.objects.create(name="Group Permission Shop", owner=owner)
        self.client.force_login(self.admin)
        permission_url = reverse(
            "admin_catalog_shop_group_permission",
            kwargs={"pk": shop.pk},
        )

        self.assertContains(
            self.client.get(reverse("admin_catalog_dashboard")),
            "Rider-group permission",
        )
        self.client.post(permission_url, {"enabled": "1"})
        shop.refresh_from_db()
        self.assertTrue(shop.allow_owner_create_shared_rider_groups)

        self.client.post(permission_url, {"enabled": "0"})
        shop.refresh_from_db()
        self.assertFalse(shop.allow_owner_create_shared_rider_groups)

        non_admin = User.objects.create_user(
            username="catalog_group_permission_non_admin",
            password="test-password",
        )
        self.client.force_login(non_admin)
        self.assertEqual(self.client.post(permission_url, {"enabled": "1"}).status_code, 403)
        shop.refresh_from_db()
        self.assertFalse(shop.allow_owner_create_shared_rider_groups)

        non_admin = User.objects.create_user(
            username="catalog_permission_non_admin",
            password="test-password",
        )
        self.client.force_login(non_admin)
        self.assertEqual(self.client.post(permission_url, {"enabled": "1"}).status_code, 403)
        shop.refresh_from_db()
        self.assertFalse(shop.allow_product_image_overrides)

    def test_admin_can_activate_and_deactivate_storefront_subscription(self):
        owner = User.objects.create_user(
            username="catalog_storefront_owner",
            password="test-password",
        )
        shop = Shop.objects.create(
            name="Storefront Subscription Shop",
            owner=owner,
            storefront_subscription_active=False,
            storefront_subscription_expires_at=timezone.localdate() - timedelta(days=1),
        )
        self.client.force_login(self.admin)
        dashboard = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertContains(dashboard, "Storefront")
        self.assertContains(dashboard, shop.storefront_slug)

        toggle_url = reverse(
            "admin_catalog_shop_storefront_toggle",
            kwargs={"pk": shop.pk},
        )
        self.assertRedirects(self.client.post(toggle_url), reverse("admin_catalog_dashboard"))
        shop.refresh_from_db()
        self.assertTrue(shop.storefront_subscription_active)
        self.assertIsNone(shop.storefront_subscription_expires_at)

        self.client.post(toggle_url)
        shop.refresh_from_db()
        self.assertFalse(shop.storefront_subscription_active)

        owner_user = User.objects.create_user(
            username="catalog_storefront_non_admin",
            password="test-password",
        )
        self.client.force_login(owner_user)
        self.assertEqual(self.client.post(toggle_url).status_code, 403)
        shop.refresh_from_db()
        self.assertFalse(shop.storefront_subscription_active)


class CartBasketTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="saved_basket_customer",
            password="test-password",
        )
        self.owner = User.objects.create_user(
            username="saved_basket_shop_owner",
            password="test-password",
        )
        self.shop = Shop.objects.create(
            name="Basket Shop",
            owner=self.owner,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Basket Apples",
            price=Decimal("10.00"),
            unit="piece",
            product_category="FRUITS",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            price=Decimal("10.00"),
            stock=4,
            is_active=True,
        )
        self.client.force_login(self.user)

    def set_cart(self, quantity):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): quantity}
        session.save()

    def test_cart_and_checkout_display_total_numeric_unit_for_quantity(self):
        self.product.unit = "500gm"
        self.product.save(update_fields=["unit"])
        self.set_cart(2)
        ClientProfile.objects.create(user=self.user, name="Saved Basket Customer")
        profile = self.user.client_profile
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Basket Road",
            location={"lat": 1, "lng": 2},
            is_active=True,
            is_default=True,
        )

        cart_response = self.client.get(reverse("cart"))
        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(cart_response.context["items"][0]["total_unit"], "1000gm")
        self.assertEqual(cart_response.context["items"][0]["unit_calculation"], "2 × 500gm = 1000gm")
        self.assertContains(cart_response, "2 × 500gm = 1000gm")

        checkout_response = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_response.status_code, 200)
        self.assertEqual(checkout_response.context["items"][0]["quantity_unit"], "1000gm")
        self.assertEqual(checkout_response.context["items"][0]["unit_calculation"], "2 × 500gm = 1000gm")
        self.assertContains(checkout_response, "2 × 500gm = 1000gm")

    def test_cart_displays_scheduled_delivery_for_closed_shop(self):
        self.shop.is_shop_open = False
        self.shop.allow_scheduled_delivery = True
        self.shop.save(update_fields=[
            "is_shop_open",
            "allow_scheduled_delivery",
        ])
        self.set_cart(1)

        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Delivery timing will be scheduled with this shop.",
        )
        self.assertContains(
            response,
            "Scheduled delivery — shop is currently closed",
        )
        self.assertTrue(response.context["items"][0]["is_scheduled_delivery"])
        self.assertTrue(
            response.context["cart_shop_summary"][0]["is_scheduled_delivery"],
        )

    def test_cart_sorts_unavailable_then_scheduled_then_available_products(self):
        profile = ClientProfile.objects.create(
            user=self.user,
            name="Saved Basket Customer",
        )
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Basket Road",
            pincode="100001",
            is_active=True,
            is_default=True,
        )
        self.shop.delivery_pincode = ["999999"]
        self.shop.save(update_fields=["delivery_pincode"])

        scheduled_shop = Shop.objects.create(
            name="Scheduled Basket Shop",
            owner=self.owner,
            is_shop_open=False,
            allow_scheduled_delivery=True,
            delivery_pincode=["100001"],
        )
        scheduled_assignment = ProductShop.objects.create(
            shop=scheduled_shop,
            product=self.product,
            price=Decimal("10.00"),
            stock=4,
            is_active=True,
        )
        available_shop = Shop.objects.create(
            name="Available Basket Shop",
            owner=self.owner,
            delivery_pincode=["100001"],
        )
        available_assignment = ProductShop.objects.create(
            shop=available_shop,
            product=self.product,
            price=Decimal("10.00"),
            stock=4,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {
            str(available_assignment.pk): 1,
            str(scheduled_assignment.pk): 1,
            str(self.assignment.pk): 1,
        }
        session.save()

        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["assignment"].pk for item in response.context["items"]],
            [
                self.assignment.pk,
                scheduled_assignment.pk,
                available_assignment.pk,
            ],
        )
        self.assertFalse(response.context["items"][0]["available_for_delivery"])
        self.assertTrue(response.context["items"][1]["is_scheduled_delivery"])
        self.assertTrue(response.context["items"][2]["available_for_delivery"])

    def test_cart_and_checkout_calculate_total_piece_measure(self):
        self.product.unit = "2pcs"
        self.product.save(update_fields=["unit"])
        self.set_cart(3)
        ClientProfile.objects.create(user=self.user, name="Saved Basket Customer")
        profile = self.user.client_profile
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Basket Road",
            location={"lat": 1, "lng": 2},
            is_active=True,
            is_default=True,
        )

        cart_response = self.client.get(reverse("cart"))
        cart_item = cart_response.context["items"][0]
        self.assertEqual(cart_item["total_unit"], "6pcs")
        self.assertEqual(cart_item["quantity_unit"], "6pcs")
        self.assertEqual(cart_item["unit_calculation"], "3 × 2pcs = 6pcs")
        self.assertContains(cart_response, "3 × 2pcs = 6pcs")

        checkout_response = self.client.get(reverse("checkout"))
        checkout_item = checkout_response.context["items"][0]
        self.assertEqual(checkout_item["quantity_unit"], "6pcs")
        self.assertEqual(checkout_item["unit_calculation"], "3 × 2pcs = 6pcs")

    def test_decrementing_to_zero_keeps_cart_line_until_delete_is_clicked(self):
        self.set_cart(1)

        update_response = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"quantity": "0"},
        )
        self.assertRedirects(update_response, reverse("cart"))
        self.assertEqual(self.client.session["cart"][str(self.assignment.pk)], 0)

        cart_response = self.client.get(reverse("cart"))
        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(cart_response.context["items"][0]["quantity"], 0)
        self.assertContains(
            cart_response,
            f'action="{reverse("cart_remove", kwargs={"product_shop_id": self.assignment.pk})}"',
        )
        self.assertContains(cart_response, "Increase at least one product quantity")
        self.assertNotContains(cart_response, "Proceed to Checkout")

        delete_response = self.client.post(
            reverse("cart_remove", kwargs={"product_shop_id": self.assignment.pk}),
        )
        self.assertRedirects(delete_response, reverse("cart"))
        self.assertNotIn(str(self.assignment.pk), self.client.session["cart"])

    def test_ajax_cart_quantity_update_returns_live_totals(self):
        self.shop.delivery_fee = Decimal("3.50")
        self.shop.save(update_fields=["delivery_fee"])
        self.set_cart(1)

        response = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"quantity": "2", "offer": "retail"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "success": True,
                "quantity": 2,
                "cart_count": 2,
                "line_total": "20.00",
                "unit_calculation": "2 piece",
                "bulk_minimum_satisfied": True,
                "active_item_count": 2,
                "subtotal": "20.00",
                "delivery_charge": "3.50",
                "grand_total": "23.50",
                "delivery_by_shop": [
                    {
                        "shop_name": self.shop.name,
                        "delivery_fee": "3.50",
                    }
                ],
            },
        )

        cart_page = self.client.get(reverse("cart"))
        self.assertContains(cart_page, 'class="d-flex align-items-center cart-quantity-form"')
        self.assertContains(cart_page, "data-cart-subtotal")
        self.assertContains(cart_page, "grid-template-areas:")
        self.assertContains(cart_page, "cart-product-image")

        zero_response = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"quantity": "0", "offer": "retail"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(zero_response.status_code, 200)
        self.assertEqual(zero_response.json()["quantity"], 0)
        self.assertEqual(zero_response.json()["line_total"], "0.00")
        self.assertEqual(zero_response.json()["active_item_count"], 0)
        self.assertEqual(zero_response.json()["delivery_charge"], "0.00")
        self.assertEqual(zero_response.json()["grand_total"], "0.00")

    def test_customer_can_save_basket_and_restore_merged_quantities_up_to_stock(self):
        self.set_cart(2)
        save_response = self.client.post(
            reverse("cart_basket_save"),
            {"name": "Weekly fruit"},
        )
        self.assertRedirects(save_response, reverse("cart"))
        basket = SavedBasket.objects.get(user=self.user, name="Weekly fruit")
        basket_item = SavedBasketItem.objects.get(basket=basket)
        self.assertEqual(basket_item.quantity, 2)
        self.assertEqual(basket_item.product_name, self.product.product_name)

        self.set_cart(3)
        restore_response = self.client.post(
            reverse("cart_basket_restore", kwargs={"basket_id": basket.pk}),
        )
        self.assertRedirects(restore_response, reverse("cart"))
        self.assertEqual(self.client.session["cart"][str(self.assignment.pk)], 4)
        self.assertTrue(
            any(
                "only 1 additional available" in message.message
                for message in restore_response.wsgi_request._messages
            )
        )

        cart_response = self.client.get(reverse("cart"))
        self.assertContains(cart_response, "Weekly fruit")
        self.assertContains(cart_response, "Basket Apples × 2")

    def test_cart_product_save_section_is_collapsed_and_product_cards_are_compact(self):
        self.set_cart(2)
        SavedBasket.objects.create(user=self.user, name="Weekly fruit")

        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<details class="cart-save-product mt-2 pt-2 border-top">')
        self.assertContains(response, "<summary>Save this product to</summary>")
        self.assertNotContains(response, "<details class=\"cart-save-product\" open>")
        self.assertContains(response, "width: 64px !important")
        self.assertContains(response, "height: 64px !important")
        self.assertContains(response, f'id="basket-{self.assignment.pk}"')

    def test_saved_baskets_are_private_and_can_be_deleted(self):
        basket = SavedBasket.objects.create(user=self.user, name="Private basket")
        other_user = User.objects.create_user(
            username="another_basket_customer",
            password="test-password",
        )
        self.client.force_login(other_user)
        restore_response = self.client.post(
            reverse("cart_basket_restore", kwargs={"basket_id": basket.pk}),
        )
        self.assertEqual(restore_response.status_code, 404)

        self.client.force_login(self.user)
        delete_response = self.client.post(
            reverse("cart_basket_delete", kwargs={"basket_id": basket.pk}),
        )
        self.assertRedirects(delete_response, reverse("cart"))
        self.assertFalse(SavedBasket.objects.filter(pk=basket.pk).exists())

    def test_zero_quantity_cart_cannot_be_saved_as_a_basket(self):
        self.set_cart(0)
        response = self.client.post(
            reverse("cart_basket_save"),
            {"name": "Empty basket"},
        )

        self.assertRedirects(response, reverse("cart"))
        self.assertFalse(SavedBasket.objects.filter(user=self.user).exists())

    def test_basket_names_must_be_unique_per_customer(self):
        self.set_cart(1)
        first_response = self.client.post(
            reverse("cart_basket_save"),
            {"name": "Weekly fruit"},
        )
        self.assertRedirects(first_response, reverse("cart"))

        duplicate_response = self.client.post(
            reverse("cart_basket_save"),
            {"name": "weekly FRUIT"},
        )
        self.assertRedirects(duplicate_response, reverse("cart"))
        self.assertEqual(SavedBasket.objects.filter(user=self.user).count(), 1)

    def test_cart_displays_each_shops_delivery_fee_and_applies_zero_as_free(self):
        self.shop.delivery_fee = Decimal("6.50")
        self.shop.save(update_fields=["delivery_fee"])
        self.set_cart(1)

        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["delivery_charge"], Decimal("6.50"))
        self.assertEqual(response.context["grand_total"], Decimal("16.50"))
        self.assertContains(response, "Delivery by shop")
        self.assertContains(response, "Basket Shop")
        self.assertContains(response, "₹6.50")

        self.shop.delivery_fee = Decimal("0.00")
        self.shop.save(update_fields=["delivery_fee"])
        free_response = self.client.get(reverse("cart"))
        self.assertEqual(free_response.context["delivery_charge"], Decimal("0.00"))
        self.assertContains(free_response, "FREE")

    def test_cart_line_can_be_added_to_selected_basket_and_merges_quantity(self):
        basket = SavedBasket.objects.create(user=self.user, name="Fruit basket")
        basket_item = SavedBasketItem.objects.create(
            basket=basket,
            product_shop=self.assignment,
            product_name=self.product.product_name,
            shop_name=self.shop.name,
            quantity=1,
        )
        self.set_cart(2)

        cart_response = self.client.get(reverse("cart"))
        self.assertContains(cart_response, "Add product to basket")
        self.assertContains(cart_response, "Fruit basket")

        add_response = self.client.post(
            reverse(
                "cart_basket_add_item",
                kwargs={"product_shop_id": self.assignment.pk},
            ),
            {"basket_id": basket.pk},
        )
        self.assertRedirects(add_response, reverse("cart"))
        basket_item.refresh_from_db()
        self.assertEqual(basket_item.quantity, 3)

        capped_response = self.client.post(
            reverse(
                "cart_basket_add_item",
                kwargs={"product_shop_id": self.assignment.pk},
            ),
            {"basket_id": basket.pk},
        )
        basket_item.refresh_from_db()
        self.assertEqual(basket_item.quantity, self.assignment.stock)
        self.assertTrue(
            any(
                "limited to current stock" in message.message
                for message in capped_response.wsgi_request._messages
            )
        )

    def test_customer_cannot_add_cart_item_to_another_users_basket(self):
        other_user = User.objects.create_user(
            username="basket_owner_customer",
            password="test-password",
        )
        basket = SavedBasket.objects.create(user=other_user, name="Not mine")
        self.set_cart(1)

        response = self.client.post(
            reverse(
                "cart_basket_add_item",
                kwargs={"product_shop_id": self.assignment.pk},
            ),
            {"basket_id": basket.pk},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SavedBasketItem.objects.filter(basket=basket).exists())


class B2BCustomerPricingTests(TestCase):
    def test_b2b_login_allows_missing_profile_gstin(self):
        user = User.objects.create_user(
            username="b2b_login_customer",
            password="test-password",
        )
        ClientProfile.objects.create(user=user, name="B2B Customer")

        response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "test-password",
                "login_as": "B2B_CUSTOMER",
            },
        )

        self.assertRedirects(response, reverse("marketplace"))
        self.assertEqual(self.client.session["active_portal"], "B2B_CUSTOMER")
        marketplace_response = self.client.get(reverse("marketplace"))
        self.assertFalse(marketplace_response.context["is_b2b_customer"])
        self.assertContains(marketplace_response, "Add GSTIN")

        ClientProfile.objects.filter(user=user).update(gstin="27ABCDE1234F1Z5")
        response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "test-password",
                "login_as": "B2B_CUSTOMER",
            },
        )
        self.assertRedirects(response, reverse("marketplace"))
        self.assertEqual(self.client.session["active_portal"], "B2B_CUSTOMER")

    def test_marketplace_prices_and_stock_follow_selected_customer_portal(self):
        customer = User.objects.create_user(username="b2b_profile_setup_customer")
        profile = ClientProfile.objects.create(user=customer, name="B2B Profile Customer")
        owner = User.objects.create_user(username="b2b_profile_setup_owner")
        shop = Shop.objects.create(
            name="B2B Profile Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Profile Setup Wholesale Item",
            price=Decimal("20.00"),
            unit="piece",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("18.00"),
            b2b_price=Decimal("12.50"),
            b2b_unit="bag",
            bulk_price=Decimal("80.00"),
            bulk_unit="case",
            b2b_bulk_price=Decimal("70.00"),
            b2b_bulk_unit="carton",
            bulk_min_quantity=2,
            stock=5,
            b2b_stock=4,
        )
        self.client.force_login(customer)
        profile_page = self.client.get(reverse("client_profile"))
        self.assertContains(profile_page, 'name="gstin"')

        save_response = self.client.post(
            reverse("client_profile"),
            {
                "name": "B2B Profile Customer",
                "contact_no": "",
                "email": "",
                "gstin": "27ABCDE1234F1Z5",
                "date_of_birth": "",
                "gender": "",
            },
        )
        self.assertRedirects(save_response, reverse("client_profile"))
        profile.refresh_from_db()
        self.assertEqual(profile.gstin, "27ABCDE1234F1Z5")
        self.assertNotEqual(self.client.session.get("active_portal"), "B2B_CUSTOMER")

        marketplace_response = self.client.get(reverse("marketplace"))
        self.assertFalse(marketplace_response.context["is_b2b_customer"])
        listed_assignment = next(
            item for item in marketplace_response.context["products"] if item.pk == assignment.pk
        )
        self.assertEqual(listed_assignment.marketplace_price, Decimal("18.00"))
        self.assertEqual(listed_assignment.marketplace_stock, 5)
        self.assertFalse(listed_assignment.has_b2b_price)
        self.assertContains(marketplace_response, "Bulk offer")
        self.assertNotContains(marketplace_response, "B2B price")

        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session["cart"] = {
            str(assignment.pk): 1,
            f"bulk:{assignment.pk}": 2,
        }
        session.save()
        b2b_marketplace_response = self.client.get(reverse("marketplace"))
        self.assertTrue(b2b_marketplace_response.context["is_b2b_customer"])
        b2b_assignment = next(
            item for item in b2b_marketplace_response.context["products"]
            if item.pk == assignment.pk
        )
        self.assertEqual(b2b_assignment.marketplace_price, Decimal("12.50"))
        self.assertEqual(b2b_assignment.marketplace_stock, 4)
        self.assertEqual(b2b_assignment.marketplace_unit, "bag")
        self.assertTrue(b2b_assignment.has_b2b_price)
        self.assertEqual(b2b_assignment.cart_quantity, 1)
        self.assertEqual(b2b_assignment.bulk_cart_quantity, 2)
        self.assertEqual(b2b_assignment.bulk_marketplace_price, Decimal("70.00"))
        self.assertEqual(b2b_assignment.bulk_marketplace_unit, "carton")
        self.assertTrue(b2b_assignment.has_bulk_offer)
        self.assertContains(b2b_marketplace_response, "B2B price")
        self.assertContains(b2b_marketplace_response, "B2B bulk offer")
        self.assertContains(b2b_marketplace_response, "₹70.00 / carton")
        self.assertContains(b2b_marketplace_response, "Min 2 units")
        self.assertContains(
            b2b_marketplace_response,
            f'id="qty-{assignment.pk}"',
        )
        self.assertContains(
            b2b_marketplace_response,
            f'id="bulk-qty-{assignment.pk}"',
        )
        self.assertContains(b2b_marketplace_response, "Update bulk")
        detail_response = self.client.get(
            reverse("market_product_detail", kwargs={"product_shop_id": assignment.pk})
        )
        self.assertEqual(detail_response.json()["price"], "12.50")
        self.assertTrue(detail_response.json()["has_b2b_price"])

    def test_zero_b2b_stock_does_not_hide_b2c_inventory(self):
        owner = User.objects.create_user(username="split_stock_owner")
        shop = Shop.objects.create(
            name="Split Stock Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Split Stock Product",
            price=Decimal("20.00"),
            unit="piece",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("20.00"),
            stock=5,
        )
        b2b_customer = User.objects.create_user(username="split_stock_b2b")
        ClientProfile.objects.create(
            user=b2b_customer,
            name="Split Stock B2B Customer",
            gstin="27ABCDE1234F1Z5",
        )
        self.client.force_login(b2b_customer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()
        b2b_marketplace = self.client.get(reverse("marketplace"))
        self.assertFalse(
            any(item.pk == assignment.pk for item in b2b_marketplace.context["products"])
        )

        b2c_customer = User.objects.create_user(username="split_stock_b2c")
        ClientProfile.objects.create(
            user=b2c_customer,
            name="Split Stock B2C Customer",
        )
        self.client.force_login(b2c_customer)
        b2c_marketplace = self.client.get(reverse("marketplace"))
        listed_assignment = next(
            item
            for item in b2c_marketplace.context["products"]
            if item.pk == assignment.pk
        )
        self.assertEqual(listed_assignment.marketplace_stock, 5)

    def test_closed_shop_products_are_visible_with_scheduled_delivery_enabled(self):
        customer = User.objects.create_user(username="scheduled_delivery_customer")
        ClientProfile.objects.create(
            user=customer,
            name="Scheduled Delivery Customer",
        )
        ClientDeliveryAddress.objects.create(
            client=customer.client_profile,
            label="Home",
            address="Market Road",
            pincode="380001",
            is_default=True,
        )
        owner = User.objects.create_user(username="scheduled_delivery_owner")
        scheduled_shop = Shop.objects.create(
            name="Scheduled Shop",
            owner=owner,
            is_shop_open=False,
            allow_scheduled_delivery=True,
            delivery_pincode=["380001"],
        )
        hidden_shop = Shop.objects.create(
            name="Closed Shop",
            owner=owner,
            is_shop_open=False,
        )
        scheduled_product = Product.objects.create(
            created_by=owner,
            product_name="Scheduled Item",
            price=Decimal("10.00"),
            unit="piece",
        )
        hidden_product = Product.objects.create(
            created_by=owner,
            product_name="Hidden Closed Item",
            price=Decimal("12.00"),
            unit="piece",
        )
        scheduled_assignment = ProductShop.objects.create(
            shop=scheduled_shop,
            product=scheduled_product,
            price=Decimal("10.00"),
            stock=3,
        )
        hidden_assignment = ProductShop.objects.create(
            shop=hidden_shop,
            product=hidden_product,
            price=Decimal("12.00"),
            stock=3,
        )
        self.client.force_login(customer)

        response = self.client.get(reverse("marketplace"))

        listed_assignment = next(
            item
            for item in response.context["products"]
            if item.pk == scheduled_assignment.pk
        )
        self.assertTrue(listed_assignment.shop.is_scheduled_delivery)
        self.assertTrue(listed_assignment.marketplace_detail["scheduled_delivery"])
        self.assertFalse(
            any(item.pk == hidden_assignment.pk for item in response.context["products"])
        )
        self.assertContains(response, "Scheduled delivery — shop is currently closed.")
        self.assertContains(
            response,
            "Delivery from this shop will need to be scheduled",
        )

    def test_market_product_detail_uses_b2b_price_and_retail_fallback(self):
        owner = User.objects.create_user(username="b2b_price_owner")
        customer = User.objects.create_user(username="b2b_price_customer")
        ClientProfile.objects.create(
            user=customer,
            name="B2B Price Customer",
            gstin="27ABCDE1234F1Z5",
        )
        shop = Shop.objects.create(
            name="B2B Price Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Wholesale Item",
            price=Decimal("20.00"),
            unit="piece",
        )
        wholesale = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("18.00"),
            b2b_price=Decimal("12.50"),
            stock=5,
            b2b_stock=5,
        )
        retail_fallback = ProductShop.objects.create(
            shop=shop,
            product=Product.objects.create(
                created_by=owner,
                product_name="Retail Fallback",
                price=Decimal("9.00"),
                unit="piece",
            ),
            price=Decimal("8.00"),
            stock=5,
            b2b_stock=5,
        )
        self.client.force_login(customer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()

        response = self.client.get(
            reverse("market_product_detail", kwargs={"product_shop_id": wholesale.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["price"], "12.50")
        self.assertTrue(response.json()["has_b2b_price"])

        response = self.client.get(
            reverse("market_product_detail", kwargs={"product_shop_id": retail_fallback.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["price"], "8.00")
        self.assertFalse(response.json()["has_b2b_price"])

    def test_shop_without_valid_gstin_uses_only_b2c_prices_for_b2b_customer(self):
        owner = User.objects.create_user(username="unregistered_b2b_shop_owner")
        customer = User.objects.create_user(username="unregistered_b2b_customer")
        ClientProfile.objects.create(
            user=customer,
            name="GST Registered Buyer",
            gstin="27ABCDE1234F1Z5",
        )
        shop = Shop.objects.create(
            name="Unregistered Seller",
            owner=owner,
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Unregistered Shop Product",
            price=Decimal("25.00"),
            unit="piece",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("20.00"),
            b2b_price=Decimal("15.00"),
            b2b_unit="bag",
            bulk_price=Decimal("90.00"),
            bulk_unit="case",
            b2b_bulk_price=Decimal("70.00"),
            b2b_bulk_unit="pallet",
            stock=6,
            b2b_stock=6,
        )
        self.client.force_login(customer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()
        marketplace = self.client.get(reverse("marketplace"))
        self.assertContains(marketplace, "Bulk offer")
        self.assertNotContains(marketplace, "B2B price")
        listed_assignment = next(
            item for item in marketplace.context["products"] if item.pk == assignment.pk
        )
        self.assertFalse(listed_assignment.has_b2b_bulk_offer)

        detail = self.client.get(
            reverse("market_product_detail", kwargs={"product_shop_id": assignment.pk})
        )

        self.assertEqual(detail.json()["price"], "20.00")
        self.assertEqual(detail.json()["unit"], "piece")
        self.assertEqual(detail.json()["bulk_price"], "90.00")
        self.assertEqual(detail.json()["bulk_unit"], "case")
        self.assertFalse(detail.json()["has_b2b_price"])
        self.assertFalse(detail.json()["has_b2b_bulk_offer"])

    def test_b2b_checkout_uses_b2b_taxable_price_and_profile_gstin_fallback(self):
        buyer = User.objects.create_user(username="b2b_checkout_buyer")
        profile = ClientProfile.objects.create(
            user=buyer,
            name="B2B Checkout Buyer",
            gstin="27ABCDE1234F1Z5",
        )
        owner = User.objects.create_user(username="b2b_checkout_owner")
        shop = Shop.objects.create(
            name="B2B GST Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
            gst_state_code="24",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="B2B Taxable Item",
            price=Decimal("100.00"),
            unit="piece",
            hsn_code="123456",
            gst_rate=Decimal("18.00"),
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("100.00"),
            b2b_price=Decimal("80.00"),
            stock=3,
            b2b_stock=3,
            is_active=True,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Business",
            address="GST Road",
            state_code="24",
            is_default=True,
            is_active=True,
        )
        self.client.force_login(buyer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session["cart"] = {str(assignment.pk): 1}
        session.save()

        checkout_response = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_response.status_code, 200)
        self.assertEqual(checkout_response.context["items"][0]["price"], Decimal("80.00"))

        response = self.client.post(
            reverse("checkout"),
            {"address": address.pk, "payment_method": "COD"},
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(user=buyer.username)
        shop_order = order.shop_orders.get()
        order_item = shop_order.order_items.get()
        self.assertEqual(order_item.total, Decimal("80.00"))
        self.assertEqual(shop_order.taxable_amount, Decimal("80.00"))
        self.assertEqual(shop_order.cgst_amount, Decimal("7.20"))
        self.assertEqual(shop_order.sgst_amount, Decimal("7.20"))
        self.assertEqual(shop_order.buyer_gstin, profile.gstin)

    def test_b2b_bulk_offer_requires_minimum_and_is_snapshotted_on_order(self):
        buyer = User.objects.create_user(username="b2b_bulk_buyer")
        profile = ClientProfile.objects.create(
            user=buyer,
            name="B2B Bulk Buyer",
            gstin="27ABCDE1234F1Z5",
        )
        owner = User.objects.create_user(username="b2b_bulk_owner")
        shop = Shop.objects.create(
            name="B2B Bulk Shop",
            owner=owner,
            gstin="24ABCDE1234F1Z5",
            gst_state_code="24",
        )
        product = Product.objects.create(
            created_by=owner,
            product_name="Bulk Rice",
            price=Decimal("30.00"),
            unit="kg",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("25.00"),
            b2b_price=Decimal("22.00"),
            b2b_unit="bag",
            bulk_price=Decimal("100.00"),
            bulk_unit="case",
            b2b_bulk_price=Decimal("75.00"),
            b2b_bulk_unit="pallet",
            bulk_min_quantity=3,
            stock=8,
            b2b_stock=4,
        )
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Warehouse",
            address="Bulk Road",
            state_code="24",
            is_default=True,
            is_active=True,
        )
        self.client.force_login(buyer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()

        detail = self.client.get(
            reverse("market_product_detail", kwargs={"product_shop_id": assignment.pk})
        )
        self.assertEqual(detail.json()["price"], "22.00")
        self.assertEqual(detail.json()["unit"], "bag")
        self.assertEqual(detail.json()["bulk_price"], "75.00")
        self.assertEqual(detail.json()["bulk_unit"], "pallet")
        self.assertEqual(detail.json()["bulk_min_quantity"], 3)

        too_few = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": assignment.pk}),
            {"offer": "bulk", "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertFalse(too_few.json()["success"])
        self.assertIn("at least 3", too_few.json()["message"])

        added = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": assignment.pk}),
            {"offer": "bulk", "quantity": 3},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(added.json()["success"])
        self.assertEqual(added.json()["quantity"], 3)

        cart_response = self.client.get(reverse("cart"))
        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(cart_response.context["items"][0]["unit"], "pallet")
        self.assertEqual(cart_response.context["items"][0]["price"], Decimal("75.00"))
        checkout_response = self.client.post(
            reverse("checkout"),
            {"address": address.pk, "payment_method": "COD"},
        )
        self.assertEqual(checkout_response.status_code, 302)

        order_item = OrderItem.objects.get(shop_order__order__user=buyer.username)
        self.assertEqual(order_item.quantity, 3)
        self.assertEqual(order_item.price, Decimal("75.00"))
        self.assertEqual(order_item.unit, "pallet")
        self.assertTrue(order_item.is_bulk_order)
        assignment.refresh_from_db()
        self.assertEqual(assignment.stock, 8)
        self.assertEqual(assignment.b2b_stock, 1)
        self.assertTrue(order_item.shop_order.order.is_b2b_order)

    def test_b2c_bulk_offer_uses_bulk_price_and_shared_unit(self):
        buyer = User.objects.create_user(username="b2c_bulk_buyer")
        profile = ClientProfile.objects.create(user=buyer, name="B2C Bulk Buyer")
        owner = User.objects.create_user(username="b2c_bulk_owner")
        shop = Shop.objects.create(name="B2C Bulk Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Bulk Apples",
            price=Decimal("12.00"),
            unit="kg",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("10.00"),
            bulk_price=Decimal("80.00"),
            bulk_unit="crate",
            b2b_bulk_price=Decimal("60.00"),
            b2b_bulk_unit="pallet",
            bulk_min_quantity=2,
            stock=8,
        )
        self.client.force_login(buyer)

        detail = self.client.get(
            reverse("market_product_detail", kwargs={"product_shop_id": assignment.pk})
        )
        self.assertEqual(detail.json()["price"], "10.00")
        self.assertEqual(detail.json()["unit"], "kg")
        self.assertEqual(detail.json()["bulk_price"], "80.00")
        self.assertEqual(detail.json()["bulk_unit"], "crate")
        self.assertContains(self.client.get(reverse("marketplace")), "Bulk offer")
        address = ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Bulk Street",
            is_default=True,
            is_active=True,
        )
        response = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": assignment.pk}),
            {"offer": "bulk", "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["success"])
        response = self.client.post(
            reverse("checkout"),
            {"address": address.pk, "payment_method": "COD"},
        )
        self.assertEqual(response.status_code, 302)
        order_item = OrderItem.objects.get(shop_order__order__user=buyer.username)
        self.assertEqual(order_item.price, Decimal("80.00"))
        self.assertEqual(order_item.unit, "crate")
        self.assertTrue(order_item.is_bulk_order)

    def test_retail_and_bulk_lines_cannot_exceed_shared_stock(self):
        buyer = User.objects.create_user(username="mixed_cart_buyer")
        ClientProfile.objects.create(user=buyer, name="Mixed Cart Buyer")
        owner = User.objects.create_user(username="mixed_cart_owner")
        shop = Shop.objects.create(name="Mixed Cart Shop", owner=owner)
        product = Product.objects.create(
            created_by=owner,
            product_name="Mixed Cart Product",
            price=Decimal("10.00"),
            unit="piece",
        )
        assignment = ProductShop.objects.create(
            shop=shop,
            product=product,
            price=Decimal("10.00"),
            bulk_price=Decimal("70.00"),
            bulk_unit="box",
            bulk_min_quantity=2,
            stock=4,
        )
        self.client.force_login(buyer)

        bulk_added = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": assignment.pk}),
            {"offer": "bulk", "quantity": 4},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(bulk_added.json()["success"])
        retail_add = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": assignment.pk}),
            {},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertFalse(retail_add.json()["success"])
        self.assertIn("stock", retail_add.json()["message"].lower())

        session_cart = self.client.session["cart"]
        self.assertEqual(session_cart[f"bulk:{assignment.pk}"], 4)
        self.assertNotIn(str(assignment.pk), session_cart)


@override_settings(
    SHOP_STOREFRONT_BASE_DOMAIN="sellerhelper.com",
    ALLOWED_HOSTS=["testserver", ".sellerhelper.com"],
)
class PublicShopStorefrontTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="public_storefront_owner",
            password="test-password",
        )
        self.shop = Shop.objects.create(
            name="Fresh Market",
            owner=self.owner,
            is_shop_open=True,
            storefront_slug="fresh-market",
            storefront_subscription_active=True,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Green Apples",
            price=Decimal("12.00"),
            unit="kg",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            stock=8,
        )
        self.other_shop = Shop.objects.create(
            name="Another Same-name Store",
            owner=self.owner,
            storefront_slug="another-store",
            storefront_subscription_active=True,
        )

    def test_owner_can_upload_shop_branding_shown_in_nearby_shops(self):
        from io import BytesIO

        from PIL import Image

        customer = User.objects.create_user(username="branding_nearby_customer")
        ClientProfile.objects.create(user=customer, name="Branding Customer")
        image_buffer = BytesIO()
        Image.new("RGB", (2, 2), color="green").save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()

        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
        ):
            self.client.force_login(self.owner)
            add_page = self.client.get(reverse("shop_add"))
            self.assertEqual(add_page.status_code, 200)
            self.assertContains(add_page, 'enctype="multipart/form-data"')
            self.assertContains(add_page, 'name="logo_image"')
            self.assertContains(add_page, 'name="cover_image"')

            edit_page = self.client.get(
                reverse("shop_edit", kwargs={"pk": self.shop.pk}),
            )
            self.assertEqual(edit_page.status_code, 200)
            self.assertContains(edit_page, 'enctype="multipart/form-data"')
            self.assertContains(edit_page, 'name="logo_image"')
            self.assertContains(edit_page, 'name="cover_image"')

            form = ShopForm(instance=self.shop)
            form_data = {
                field_name: (
                    ""
                    if form.initial.get(field_name) is None
                    else form.initial.get(field_name, "")
                )
                for field_name in form.fields
                if field_name != "accept_legal_documents"
            }
            form_data.update({
                "location_json": "null",
                "delivery_area_json": "[]",
                "delivery_pincode_json": "[]",
                "break_time_json": "[]",
                "pickup_locations_json": "[]",
                "logo_image": SimpleUploadedFile(
                    "shop-logo.png",
                    image_bytes,
                    content_type="image/png",
                ),
                "cover_image": SimpleUploadedFile(
                    "shop-cover.png",
                    image_bytes,
                    content_type="image/png",
                ),
            })
            response = self.client.post(
                reverse("shop_edit", kwargs={"pk": self.shop.pk}),
                form_data,
            )
            self.assertRedirects(response, reverse("shop_list"))
            self.shop.refresh_from_db()
            self.assertTrue(self.shop.logo_image.name.endswith("shop-logo.png"))
            self.assertTrue(self.shop.cover_image.name.endswith("shop-cover.png"))

            self.client.force_login(customer)
            marketplace = self.client.get(reverse("marketplace"))

            self.assertEqual(marketplace.status_code, 200)
            self.assertContains(marketplace, self.shop.logo_image.url)
            self.assertContains(marketplace, self.shop.cover_image.url)

    def test_new_storefront_requires_admin_activation_after_payment(self):
        shop = Shop.objects.create(name="Awaiting Subscription")

        self.assertFalse(shop.storefront_subscription_active)

    def test_public_subdomain_renders_only_its_shop_catalog(self):
        other_owner = User.objects.create_user(
            username="other_storefront_owner",
            password="test-password",
        )
        other_shop = Shop.objects.create(
            name="Other Shop",
            owner=other_owner,
            storefront_slug="other-shop",
            storefront_subscription_active=True,
        )
        other_product = Product.objects.create(
            created_by=other_owner,
            product_name="Other Product",
            price=Decimal("5.00"),
            unit="each",
        )
        ProductShop.objects.create(
            shop=other_shop,
            product=other_product,
            stock=3,
        )

        response = self.client.get("/", HTTP_HOST="fresh-market.sellerhelper.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fresh Market")
        self.assertContains(response, "Green Apples")
        self.assertNotContains(response, "Other Product")
        self.assertContains(response, "sellerhelper://shop/fresh-market")

    def test_inactive_storefront_waits_for_admin_subscription_activation(self):
        self.shop.storefront_subscription_active = False
        self.shop.save(update_fields=["storefront_subscription_active"])

        response = self.client.get("/", HTTP_HOST="fresh-market.sellerhelper.com")

        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "subscription is inactive or expired", status_code=503)

    def test_expired_storefront_subscription_is_unavailable(self):
        self.shop.storefront_subscription_expires_at = timezone.localdate() - timedelta(days=1)
        self.shop.save(update_fields=["storefront_subscription_expires_at"])

        response = self.client.get("/", HTTP_HOST="fresh-market.sellerhelper.com")

        self.assertEqual(response.status_code, 503)

    def test_slug_availability_allows_owners_to_keep_their_current_slug(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("shop_storefront_slug_available"),
            {"slug": "fresh-market", "shop_id": self.shop.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["available"])

    def test_slug_availability_rejects_reserved_and_taken_names(self):
        reserved = self.client.get(
            reverse("shop_storefront_slug_available"),
            {"slug": "admin"},
        )
        taken = self.client.get(
            reverse("shop_storefront_slug_available"),
            {"slug": "fresh-market"},
        )

        self.assertFalse(reserved.json()["available"])
        self.assertFalse(taken.json()["available"])

    def test_shop_owner_sees_advertising_link_and_qr(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("shop_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fresh-market.sellerhelper.com")
        self.assertContains(response, "Advertisement QR code")
        self.assertContains(response, "data:image/png;base64,")

    @override_settings(
        SHOP_STOREFRONT_BASE_DOMAIN="",
        ALLOWED_HOSTS=["testserver", ".example.test"],
        DEBUG=False,
    )
    def test_storefront_link_uses_current_site_domain(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("shop_list"),
            HTTP_HOST="example.test",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://fresh-market.example.test/")

    @override_settings(
        SHOP_STOREFRONT_BASE_DOMAIN="",
        ALLOWED_HOSTS=["testserver", ".example.test"],
        DEBUG=False,
    )
    def test_storefront_host_is_resolved_from_current_site_domain(self):
        response = self.client.get("/", HTTP_HOST="fresh-market.example.test")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Green Apples")

    @override_settings(
        SHOP_STOREFRONT_BASE_DOMAIN="",
        ALLOWED_HOSTS=["testserver", "192.168.1.12"],
        DEBUG=False,
    )
    def test_local_ip_uses_path_based_storefront_link(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("shop_list"),
            HTTP_HOST="192.168.1.12:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "http://192.168.1.12:8000/storefronts/fresh-market/",
        )

    def test_shop_owner_can_choose_custom_unique_slug_not_shop_name(self):
        form = ShopForm(instance=self.shop)
        self.assertTrue(form.fields["storefront_slug"].required)
        form.cleaned_data = {"storefront_slug": "my-custom-store"}

        self.assertEqual(form.clean_storefront_slug(), "my-custom-store")

        form.cleaned_data = {"storefront_slug": "another-store"}
        with self.assertRaises(ValidationError):
            form.clean_storefront_slug()

    def test_storefront_cart_rejects_products_from_other_shops(self):
        self.client.force_login(self.owner)
        other_owner = User.objects.create_user(
            username="cart_other_store_owner",
            password="test-password",
        )
        other_shop = Shop.objects.create(
            name="Other Cart Shop",
            owner=other_owner,
            storefront_slug="other-cart-shop",
            storefront_subscription_active=True,
        )
        other_product = Product.objects.create(
            created_by=other_owner,
            product_name="Other Cart Product",
            price=Decimal("7.00"),
            unit="each",
        )
        other_assignment = ProductShop.objects.create(
            shop=other_shop,
            product=other_product,
            stock=4,
        )

        response = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": other_assignment.pk}),
            {"offer": "retail"},
            HTTP_HOST="fresh-market.sellerhelper.com",
        )

        self.assertEqual(response.status_code, 404)

        update_response = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": other_assignment.pk}),
            {"offer": "retail", "quantity": 1},
            HTTP_HOST="fresh-market.sellerhelper.com",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(update_response.status_code, 404)

    def test_customer_can_add_its_shop_product_from_the_storefront(self):
        self.client.force_login(self.owner)
        profile = ClientProfile.objects.create(
            user=self.owner,
            name="Storefront Customer",
        )
        ClientDeliveryAddress.objects.create(
            client=profile,
            label="Home",
            address="Home Road",
            location={},
            is_default=True,
            is_active=True,
        )
        storefront_url = reverse("public_shop_storefront", kwargs={"slug": self.shop.storefront_slug})
        initial_page = self.client.get(
            storefront_url,
            HTTP_HOST="fresh-market.sellerhelper.com",
        )
        self.assertEqual(initial_page.status_code, 200)
        self.assertContains(initial_page, 'data-cart-count')
        self.assertContains(initial_page, "data-storefront-retail")
        self.assertContains(initial_page, ">Add</button>")

        response = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": self.assignment.pk}),
            {"offer": "retail"},
            HTTP_HOST="fresh-market.sellerhelper.com",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["cart_count"], 1)
        storefront_with_item = self.client.get(
            storefront_url,
            HTTP_HOST="fresh-market.sellerhelper.com",
        )
        self.assertContains(storefront_with_item, '<span data-quantity>1</span>')
        self.assertContains(storefront_with_item, "Remove one Green Apples")

        updated = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"offer": "retail", "quantity": 2},
            HTTP_HOST="fresh-market.sellerhelper.com",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(updated.json()["success"])
        self.assertEqual(updated.json()["quantity"], 2)
        self.assertEqual(updated.json()["cart_count"], 2)
        storefront_after_update = self.client.get(
            storefront_url,
            HTTP_HOST="fresh-market.sellerhelper.com",
        )
        self.assertContains(storefront_after_update, '<span data-quantity>2</span>')

    def test_storefront_shows_retail_b2b_and_total_stock(self):
        self.assignment.stock = 8
        self.assignment.b2b_stock = 3
        self.assignment.save(update_fields=["stock", "b2b_stock"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("public_shop_storefront", kwargs={"slug": self.shop.storefront_slug}),
            HTTP_HOST="fresh-market.sellerhelper.com",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Retail: 8")
        self.assertContains(response, "B2B: 3")
        self.assertContains(response, "Total: 11")
        self.assertContains(response, 'data-stock="8"')


class StorefrontProductEditingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="storefront_owner",
            password="test-pass-123",
        )
        self.manager = User.objects.create_user(
            username="storefront_manager",
            password="test-pass-123",
        )
        self.outsider = User.objects.create_user(
            username="storefront_outsider",
            password="test-pass-123",
        )
        self.shop = Shop.objects.create(name="Preview Shop", owner=self.owner)
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Fresh Apples",
            product_title="Red Apples",
            product_description="Crisp and sweet.",
            price=Decimal("12.00"),
            unit="kg",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            price=Decimal("15.00"),
            stock=3,
        )

    def test_shop_owner_can_preview_and_edit_assigned_product(self):
        self.client.force_login(self.owner)
        preview = self.client.get(
            reverse("shop_storefront", kwargs={"shop_id": self.shop.pk})
        )
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Red Apples")
        self.assertContains(preview, "Available stock:")
        self.assertContains(preview, "Retail 3")
        self.assertContains(preview, "B2B 0")
        self.assertContains(preview, "Total 3")
        self.assertNotContains(preview, "Out of stock")
        self.assertContains(
            preview,
            reverse("storefront_product_edit", kwargs={"product_shop_id": self.assignment.pk}),
        )

        response = self.client.post(
            reverse(
                "storefront_product_edit",
                kwargs={"product_shop_id": self.assignment.pk},
            ),
            {
                "product_name": "Changed outside catalog",
                "product_alias_name": "Local Sweet Apples",
                "product_title": "Premium Red Apples",
                "product_color": "Deep red",
                "product_size": "Large",
                "product_description": "Locally grown apples.",
                "search_keywords": "sweet red apples",
                "unit_value": "1",
                "unit_text": "kg",
                "image_url": "",
                "shop_price": "18.50",
                "b2b_price": "14.25",
                "b2b_unit_value": "500",
                "b2b_unit_text": "gm",
                "bulk_price": "150.00",
                "bulk_unit_value": "12",
                "bulk_unit_text": "packs",
                "b2b_bulk_price": "120.00",
                "b2b_bulk_unit_value": "24",
                "b2b_bulk_unit_text": "cartons",
                "bulk_min_quantity": "3",
                "stock": "5",
                "b2b_stock": "7",
            },
        )

        self.assertRedirects(
            response,
            reverse("shop_storefront", kwargs={"shop_id": self.shop.pk}),
        )
        self.product.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(self.product.product_name, "Fresh Apples")
        self.assertEqual(self.product.product_alias_name, "Local Sweet Apples")
        self.assertEqual(self.product.image_url, "")
        self.assertEqual(self.assignment.image_url_override, "")
        self.assertEqual(self.product.product_title, "Premium Red Apples")
        self.assertEqual(self.product.product_color, "Deep red")
        self.assertEqual(self.product.product_size, "Large")
        self.assertIn("Fresh Apples", self.product.search_keywords)
        self.assertIn("Local Sweet Apples", self.product.search_keywords)
        self.assertIn("Locally grown apples.", self.product.search_keywords)
        self.assertNotIn("sweet red apples", self.product.search_keywords.lower())
        self.assertEqual(self.product.unit, "1kg")
        self.assertEqual(self.product.price, Decimal("12.00"))
        self.assertEqual(self.assignment.price, Decimal("18.50"))
        self.assertEqual(self.assignment.b2b_price, Decimal("14.25"))
        self.assertEqual(self.assignment.b2b_unit, "500gm")
        self.assertEqual(self.assignment.bulk_price, Decimal("150.00"))
        self.assertEqual(self.assignment.bulk_unit, "12 packs")
        self.assertEqual(self.assignment.b2b_bulk_price, Decimal("120.00"))
        self.assertEqual(self.assignment.b2b_bulk_unit, "24 cartons")
        self.assertEqual(self.assignment.bulk_min_quantity, 3)
        self.assertEqual(self.assignment.stock, 5)
        self.assertEqual(self.assignment.b2b_stock, 7)
        edit_response = self.client.get(
            reverse(
                "storefront_product_edit",
                kwargs={"product_shop_id": self.assignment.pk},
            )
        )
        self.assertContains(edit_response, 'name="b2b_unit_value" value="500"')
        self.assertContains(edit_response, 'name="b2b_unit_text" value="gm"')
        self.assertContains(edit_response, 'name="bulk_unit_value" value="12"')
        self.assertContains(edit_response, 'name="bulk_unit_text" value="packs"')
        self.assertContains(edit_response, 'name="b2b_bulk_unit_value" value="24"')
        self.assertContains(edit_response, 'name="b2b_bulk_unit_text" value="cartons"')
        movement = StockMovement.objects.get(
            product_shop=self.assignment,
            reference__startswith="Storefront stock edit",
        )
        self.assertEqual(movement.quantity, 2)
        self.assertEqual(movement.movement_type, "ADJUSTMENT")
        self.assertEqual(movement.created_by, self.owner)
        b2b_movement = StockMovement.objects.get(
            product_shop=self.assignment,
            reference__startswith="Storefront B2B stock edit",
        )
        self.assertEqual(b2b_movement.quantity, 7)

    def test_shop_storefront_preview_shows_retail_b2b_and_total_available_stock(self):
        self.assignment.stock = Decimal("8")
        self.assignment.b2b_stock = Decimal("3")
        self.assignment.save(update_fields=["stock", "b2b_stock"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("shop_storefront", kwargs={"shop_id": self.shop.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Available stock:")
        self.assertContains(response, "Retail 8")
        self.assertContains(response, "B2B 3")
        self.assertContains(response, "Total 11")
        self.assertNotContains(response, "Out of stock")

    def test_shop_image_edit_is_unavailable_until_admin_enables_it(self):
        form = StorefrontProductEditForm(
            instance=self.product,
            product_shop=self.assignment,
        )
        self.assertIn("product_alias_name", form.fields)
        self.assertIn("product_color", form.fields)
        self.assertIn("product_size", form.fields)
        self.assertNotIn("search_keywords", form.fields)
        self.assertIn("b2b_stock", form.fields)
        self.assertIn("unit_value", form.fields)
        self.assertIn("unit_text", form.fields)
        self.assertIn("b2b_unit_value", form.fields)
        self.assertIn("b2b_unit_text", form.fields)
        self.assertIn("bulk_unit_value", form.fields)
        self.assertIn("bulk_unit_text", form.fields)
        self.assertIn("b2b_bulk_unit_value", form.fields)
        self.assertIn("b2b_bulk_unit_text", form.fields)
        self.assertIsNone(form["unit_value"].value())
        self.assertEqual(form["unit_text"].value(), "kg")
        self.assertIsNone(form["b2b_unit_value"].value())
        self.assertEqual(form["b2b_unit_text"].value(), "")
        self.assertIsNone(form["bulk_unit_value"].value())
        self.assertEqual(form["bulk_unit_text"].value(), "")
        self.assertIsNone(form["b2b_bulk_unit_value"].value())
        self.assertEqual(form["b2b_bulk_unit_text"].value(), "")
        self.assertNotIn("shop_image_url", form.fields)
        self.assertNotIn("allow_product_image_overrides", ShopForm().fields)

        self.client.force_login(self.owner)
        edit_response = self.client.get(
            reverse(
                "storefront_product_edit",
                kwargs={"product_shop_id": self.assignment.pk},
            )
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "B2C pricing")
        self.assertContains(edit_response, "B2B pricing")
        self.assertContains(edit_response, "B2B unit text")
        self.assertContains(edit_response, 'name="bulk_unit_value"')
        self.assertContains(edit_response, 'name="bulk_unit_text"')
        self.assertContains(edit_response, 'name="b2b_bulk_unit_value"')
        self.assertContains(edit_response, 'name="b2b_bulk_unit_text"')
        for field_name in (
            "shop_image_url",
            "shop_image_url_2",
            "shop_image_url_3",
            "shop_image_url_4",
            "shop_image_url_5",
        ):
            self.assertNotIn(field_name, form.fields)
            self.assertNotContains(edit_response, f'name="{field_name}"')

        response = self.client.post(
            reverse(
                "storefront_product_edit",
                kwargs={"product_shop_id": self.assignment.pk},
            ),
            {
                "product_title": "Apples",
                "product_description": "Fresh",
                "unit_value": "1",
                "unit_text": "kg",
                "shop_image_url": "https://example.com/unauthorized.png",
                "shop_price": "18.50",
                "stock": "5",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.image_url_override, "")

    def test_admin_permission_allows_shop_scoped_image_override(self):
        AdminProductCatalog.objects.create(
            product_category=self.product.product_category,
            product_name="Fresh Apples",
            image_url="https://example.com/catalog-apples.png",
        )
        self.shop.allow_product_image_overrides = True
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.client.force_login(self.owner)
        edit_url = reverse(
            "storefront_product_edit",
            kwargs={"product_shop_id": self.assignment.pk},
        )
        edit_page = self.client.get(edit_url)
        self.assertEqual(edit_page.status_code, 200)
        for field_name in (
            "shop_image_url",
            "shop_image_url_2",
            "shop_image_url_3",
            "shop_image_url_4",
            "shop_image_url_5",
        ):
            self.assertContains(edit_page, f'name="{field_name}"')

        response = self.client.post(
            edit_url,
            {
                "product_title": "Apples",
                "product_description": "Fresh",
                "unit_value": "1",
                "unit_text": "kg",
                "shop_image_url": "https://example.com/shop-apples.png",
                "shop_image_url_2": "https://example.com/shop-apples-2.png",
                "shop_image_url_3": "https://example.com/shop-apples-3.png",
                "shop_image_url_4": "https://example.com/shop-apples-4.png",
                "shop_image_url_5": "https://example.com/shop-apples-5.png",
                "shop_price": "18.50",
                "stock": "5",
            },
        )

        self.assertRedirects(
            response,
            reverse("shop_storefront", kwargs={"shop_id": self.shop.pk}),
        )
        self.product.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(self.product.image_url, "")
        self.assertEqual(
            self.assignment.image_url_override,
            "https://example.com/shop-apples.png",
        )
        self.assertEqual(
            [
                self.assignment.image_url_override_2,
                self.assignment.image_url_override_3,
                self.assignment.image_url_override_4,
                self.assignment.image_url_override_5,
            ],
            [
                "https://example.com/shop-apples-2.png",
                "https://example.com/shop-apples-3.png",
                "https://example.com/shop-apples-4.png",
                "https://example.com/shop-apples-5.png",
            ],
        )
        self.assertEqual(
            self.assignment.display_image_url,
            "https://example.com/shop-apples.png",
        )
        self.assertEqual(
            self.assignment.display_image_urls,
            [
                "https://example.com/shop-apples.png",
                "https://example.com/shop-apples-2.png",
                "https://example.com/shop-apples-3.png",
                "https://example.com/shop-apples-4.png",
                "https://example.com/shop-apples-5.png",
                "https://example.com/catalog-apples.png",
            ],
        )
        edit_page = self.client.get(edit_url)
        for image_number in range(1, 6):
            image_url = (
                "https://example.com/shop-apples.png"
                if image_number == 1
                else f"https://example.com/shop-apples-{image_number}.png"
            )
            field_name = "shop_image_url" if image_number == 1 else f"shop_image_url_{image_number}"
            self.assertContains(edit_page, f'name="{field_name}" value="{image_url}"')
        self.shop.allow_product_image_overrides = False
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.assignment.refresh_from_db()
        self.assertEqual(
            self.assignment.display_image_url,
            "https://example.com/catalog-apples.png",
        )

    def test_product_image_is_a_fallback_before_admin_catalog_images(self):
        catalog_product = AdminProductCatalog.objects.create(
            product_category=self.product.product_category,
            product_name=self.product.product_name,
            image_url="https://example.com/catalog-fallback.png",
        )
        self.shop.allow_product_image_overrides = True
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.product.image_url = "https://example.com/customer-provided.png"
        self.product.save(update_fields=["image_url"])

        self.assertEqual(
            self.assignment.display_image_urls,
            [
                "https://example.com/customer-provided.png",
                *catalog_product.image_urls,
            ],
        )

    def test_owner_cannot_change_product_name_from_regular_product_editor(self):
        self.client.force_login(self.owner)
        edit_url = reverse("product_edit", kwargs={"pk": self.product.pk})
        edit_page = self.client.get(edit_url)
        self.assertEqual(edit_page.status_code, 200)
        edit_content = edit_page.content.decode()
        self.assertLess(
            edit_content.index('id="id_product_category"'),
            edit_content.index('id="id_product_name"'),
        )
        self.assertLess(
            edit_content.index('id="id_price"'),
            edit_content.index("Auto-filled catalog details"),
        )
        details_start = edit_content.index("<details")
        details_tag_end = edit_content.index(">", details_start)
        self.assertNotIn("open", edit_content[details_start:details_tag_end])

        response = self.client.post(
            edit_url,
            {
                "sku": self.product.sku,
                "product_category": self.product.product_category,
                "product_name": "Changed outside catalog",
                "product_alias_name": self.product.product_alias_name,
                "product_title": "Updated product title",
                "product_color": self.product.product_color,
                "product_size": self.product.product_size,
                "price": str(self.product.price),
                "hsn_code": self.product.hsn_code,
                "gst_rate": str(self.product.gst_rate),
                "unit_value": "1",
                "unit_text": "kg",
                "image_url": self.product.image_url,
                "shelf_life_days": "",
                "expiry_price_drop_percent": str(self.product.expiry_price_drop_percent),
                "product_description": self.product.product_description,
                "is_active": "on",
                f"shop_{self.shop.pk}-shop": str(self.shop.pk),
            },
        )

        self.assertRedirects(response, reverse("product_list"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.product_name, "Fresh Apples")
        self.assertEqual(self.product.product_title, "Updated product title")

    def test_manager_with_product_permission_can_edit_but_other_users_cannot(self):
        ShopStaff.objects.create(
            shop=self.shop,
            user=self.manager,
            role="MANAGER",
            can_manage_products=True,
        )
        edit_url = reverse(
            "storefront_product_edit",
            kwargs={"product_shop_id": self.assignment.pk},
        )

        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(edit_url).status_code, 200)

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(edit_url).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("shop_storefront", kwargs={"shop_id": self.shop.pk})
            ).status_code,
            403,
        )


class ShopOwnerOperationsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="operations_owner",
            password="test-pass-123",
        )
        self.shop = Shop.objects.create(
            name="Operations Shop",
            owner=self.owner,
            location={"lat": 21.17, "lng": 72.83},
        )
        self.client.force_login(self.owner)

    def create_shop_order(self, order_id, *, status="PLACED", location="21.2,72.9"):
        order = Order.objects.create(
            order_id=order_id,
            user="operations_customer",
            delivery_address="Main Road",
            delivery_location=location,
            delivery_contact_no="9876543210",
            status=status,
            shop=self.shop,
        )
        return ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            total_amount=Decimal("120.00"),
            status=status,
        )

    def test_rider_delivery_shortcut_counts_available_and_active_separately(self):
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=["rider_delivery_preference"])
        rider_user = User.objects.create_user(
            username="operations_delivery_count_rider",
            password="test-password",
        )
        rider_staff = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        available_order = self.create_shop_order(
            "ORD-DELIVERY-COUNT-AVAILABLE",
            status="READY",
        )
        active_order = self.create_shop_order(
            "ORD-DELIVERY-COUNT-ACTIVE",
            status="RIDER_ASSIGNED",
        )
        active_order.assigned_rider = rider_staff
        active_order.save(update_fields=["assigned_rider"])

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()

        counts_response = self.client.get(reverse("notifications_api"))
        self.assertEqual(counts_response.status_code, 200)
        self.assertEqual(
            counts_response.json()["available_delivery_count"],
            1,
        )
        self.assertEqual(
            counts_response.json()["active_delivery_count"],
            1,
        )

        dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertContains(dashboard, "data-rider-available-count")
        self.assertContains(dashboard, "data-rider-active-count")
        self.assertEqual(
            {order.pk for order in dashboard.context["orders"]},
            {available_order.pk},
        )
        self.assertEqual(dashboard.context["active_order_count"], 1)

    def test_staff_rider_sees_available_orders_from_all_assigned_shops(self):
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=["rider_delivery_preference"])
        second_shop = Shop.objects.create(
            name="Second Assigned Rider Shop",
            owner=self.owner,
            rider_delivery_preference="STAFF",
        )
        rider_user = User.objects.create_user(
            username="multi_shop_staff_rider",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        ShopStaff.objects.create(
            shop=second_shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        second_shop_order = self.create_shop_order(
            "ORD-SECOND-ASSIGNED-RIDER-SHOP",
            status="READY",
        )
        second_shop_order.shop = second_shop
        second_shop_order.save(update_fields=["shop"])

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        dashboard = self.client.get(reverse("rider_dashboard"))

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context["available_order_count"], 1)
        self.assertEqual(
            list(dashboard.context["orders"].values_list("pk", flat=True)),
            [second_shop_order.pk],
        )
        self.assertContains(dashboard, "Accept this delivery")
        notification_counts = self.client.get(
            reverse("notifications_api"),
        ).json()
        self.assertEqual(
            notification_counts["available_delivery_count"],
            1,
        )
        accept_response = self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": second_shop_order.pk},
            ),
        )
        self.assertEqual(accept_response.status_code, 302)
        second_shop_order.refresh_from_db()
        second_shop_rider = ShopStaff.objects.get(
            shop=second_shop,
            user=rider_user,
        )
        self.assertEqual(
            second_shop_order.assigned_rider_id,
            second_shop_rider.pk,
        )

    def test_started_delivery_hides_offers_until_ten_seconds_after_completion(self):
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=["rider_delivery_preference"])
        rider_user = User.objects.create_user(
            username="offer_cooldown_rider",
            password="testpass123",
        )
        rider_staff = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        active_order = self.create_shop_order(
            "ORD-OFFER-COOLDOWN-ACTIVE",
            status="OUT_FOR_DELIVERY",
        )
        active_order.assigned_rider = rider_staff
        active_order.save(update_fields=["assigned_rider"])
        available_order = self.create_shop_order(
            "ORD-OFFER-COOLDOWN-AVAILABLE",
            status="READY",
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()

        active_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertEqual(active_dashboard.status_code, 200)
        self.assertTrue(active_dashboard.context["active_delivery_in_progress"])
        self.assertTrue(active_dashboard.context["hide_available_tab"])
        self.assertEqual(active_dashboard.context["available_order_count"], 0)
        self.assertEqual(active_dashboard.context["selected_view"], "active")
        self.assertContains(active_dashboard, "New orders are paused")

        active_order.status = "DELIVERED"
        active_order.delivery_completed_at = timezone.now()
        active_order.save(update_fields=["status", "delivery_completed_at"])
        cooldown_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertGreater(
            cooldown_dashboard.context["rider_offer_cooldown_seconds"],
            0,
        )
        self.assertEqual(cooldown_dashboard.context["available_order_count"], 0)
        self.assertEqual(cooldown_dashboard.context["selected_view"], "completed")

        active_order.delivery_completed_at = timezone.now() - timedelta(seconds=12)
        active_order.save(update_fields=["delivery_completed_at"])
        resumed_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )
        self.assertFalse(resumed_dashboard.context["hide_available_tab"])
        self.assertEqual(
            list(resumed_dashboard.context["orders"].values_list("pk", flat=True)),
            [available_order.pk],
        )

    def test_staff_rider_sees_available_order_when_hidden_invalid_order_is_assigned(self):
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=["rider_delivery_preference"])
        rider_user = User.objects.create_user(
            username="invalid_active_order_staff_rider",
            password="test-password",
        )
        rider_staff = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        invalid_active_order = self.create_shop_order(
            "ORD-INVALID-ACTIVE-STAFF-RIDER",
            status="OUT_FOR_DELIVERY",
        )
        invalid_active_order.order.is_valid_order = False
        invalid_active_order.order.save(update_fields=["is_valid_order"])
        invalid_active_order.assigned_rider = rider_staff
        invalid_active_order.save(update_fields=["assigned_rider"])
        available_order = self.create_shop_order(
            "ORD-AVAILABLE-AFTER-INVALID-ACTIVE-STAFF-RIDER",
            status="READY",
        )

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "available"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["active_delivery_in_progress"])
        self.assertEqual(response.context["available_order_count"], 1)
        self.assertEqual(
            list(response.context["orders"].values_list("pk", flat=True)),
            [available_order.pk],
        )

    def test_done_and_all_completed_tabs_show_their_expected_orders(self):
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=["rider_delivery_preference"])
        rider_user = User.objects.create_user(
            username="today_completed_rider",
            password="testpass123",
        )
        rider_staff = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        today_order = self.create_shop_order(
            "ORD-RIDER-DONE-TODAY",
            status="DELIVERED",
        )
        today_order.assigned_rider = rider_staff
        today_order.delivery_completed_at = timezone.now()
        today_order.save(
            update_fields=["assigned_rider", "delivery_completed_at"],
        )
        yesterday_order = self.create_shop_order(
            "ORD-RIDER-DONE-YESTERDAY",
            status="DELIVERED",
        )
        yesterday_order.assigned_rider = rider_staff
        yesterday_order.delivery_completed_at = timezone.now() - timedelta(days=2)
        yesterday_order.save(
            update_fields=["assigned_rider", "delivery_completed_at"],
        )

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        done_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )
        self.assertEqual(done_response.status_code, 200)
        self.assertEqual(
            list(done_response.context["orders"].values_list("pk", flat=True)),
            [today_order.pk],
        )
        self.assertEqual(done_response.context["completed_order_count"], 1)
        self.assertContains(
            done_response,
            f'href="{reverse("rider_dashboard")}?view=completed"',
        )
        self.assertContains(
            done_response,
            f'href="{reverse("rider_dashboard")}?view=all_completed"',
        )
        self.assertContains(
            done_response,
            f'href="{reverse("rider_dashboard")}?view=verified"',
        )
        self.assertContains(done_response, "All completed")

        all_completed_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "all_completed"},
        )
        self.assertEqual(all_completed_response.status_code, 200)
        self.assertEqual(
            set(all_completed_response.context["orders"].values_list("pk", flat=True)),
            {today_order.pk, yesterday_order.pk},
        )
        self.assertEqual(
            all_completed_response.context["all_completed_order_count"],
            2,
        )

    def test_ready_order_without_shop_rider_is_offered_to_public_rider(self):
        self.shop.public_rider_network_enabled = True
        self.shop.save(update_fields=["public_rider_network_enabled"])
        rider_user = User.objects.create_user(
            username="operations_public_rider",
            password="testpass123",
        )
        PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Operations Public Rider",
            phone="9876543210",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-OPERATIONS-PUBLIC",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        rider_membership = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
        )
        shop_order = self.create_shop_order(
            "ORD-OPERATIONS-AUTO-PUBLIC",
            status="PACKING",
        )

        response = self.client.post(
            reverse(
                "shop_order_status_update",
                kwargs={"shop_order_id": shop_order.pk},
            ),
            {"status": "READY"},
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "READY")
        self.assertTrue(shop_order.public_rider_network_requested)
        self.assertEqual(shop_order.public_rider_offer_user_id, rider_user.pk)
        self.assertTrue(
            Notification.objects.filter(
                user=rider_user,
                title="Delivery offer",
            ).exists()
        )

    def test_public_rider_gets_two_offers_and_another_after_twelve_seconds(self):
        from .public_rider_dispatch import public_rider_max_active_offers

        install_test_road_network(
            self,
            [
                [72.83, 21.17],
                [72.84, 21.18],
                [72.9, 21.2],
            ],
        )
        self.shop.public_rider_network_enabled = True
        self.shop.save(update_fields=["public_rider_network_enabled"])
        expected_active_offer_count = public_rider_max_active_offers()
        rider_user = User.objects.create_user(
            username="operations_two_offer_rider",
            password="testpass123",
        )
        rider_application = PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Operations Two Offer Rider",
            phone="9876543210",
            address="Rider address",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-OPERATIONS-TWO-OFFER",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 20,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        shop_orders = []
        for index in range(4):
            order = Order.objects.create(
                order_id=f"ORD-OPERATIONS-TWO-OFFER-{index}",
                user=f"operations_two_offer_customer_{index}",
                delivery_address="Main Road",
                delivery_location="21.18,72.84",
                placed_location={"lat": 21.18, "lng": 72.84},
                delivery_contact_no="9876543210",
                status="READY",
                shop=self.shop,
            )
            shop_orders.append(ShopOrder.objects.create(
                order=order,
                shop=self.shop,
                total_amount=Decimal("120.00"),
                status="READY",
                public_rider_network_requested=True,
            ))

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        first_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(first_dashboard.context["available_order_count"], 2)
        self.assertEqual(
            ShopOrder.objects.filter(
                pk__in=[shop_order.pk for shop_order in shop_orders],
                public_rider_offer_user=rider_user,
            ).count(),
            expected_active_offer_count,
        )
        self.assertContains(first_dashboard, "data-next-offer-at")
        self.assertContains(first_dashboard, "window.location.reload()")

        offered_order = ShopOrder.objects.filter(
            pk__in=[shop_order.pk for shop_order in shop_orders],
            public_rider_offer_user=rider_user,
        ).first()
        decline_response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": offered_order.pk}),
            {"action": "decline"},
        )
        self.assertEqual(decline_response.status_code, 302)
        rider_application.refresh_from_db()
        decline_cooldown_until = rider_application.public_rider_offer_cooldown_until
        self.assertIsNotNone(decline_cooldown_until)
        self.assertLessEqual(
            (decline_cooldown_until - timezone.now()).total_seconds(),
            12,
        )
        self.assertGreater(
            (decline_cooldown_until - timezone.now()).total_seconds(),
            11,
        )

        for shop_order in shop_orders:
            shop_order.refresh_from_db()
        declined_order_ids = {offered_order.pk}
        self.assertEqual(
            ShopOrder.objects.filter(
                pk__in=[shop_order.pk for shop_order in shop_orders],
                public_rider_offer_user=rider_user,
                public_rider_offer_expires_at__gt=timezone.now(),
            ).count(),
            1,
        )

        with patch(
            "seller.public_rider_dispatch.timezone.now",
            return_value=decline_cooldown_until + timedelta(seconds=1),
        ):
            after_decline_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(after_decline_dashboard.context["available_order_count"], 2)
        next_offered_order = ShopOrder.objects.filter(
            pk__in=[shop_order.pk for shop_order in shop_orders],
            public_rider_offer_user=rider_user,
        ).exclude(pk__in=declined_order_ids).order_by("-pk").first()
        self.assertIsNotNone(next_offered_order)

        acceptance_response = self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": next_offered_order.pk},
            ),
            {
                "rider_latitude": "21.17",
                "rider_longitude": "72.83",
                "rider_location_observed_at": str(timezone.now().timestamp()),
            },
        )
        self.assertEqual(acceptance_response.status_code, 302)
        active_batch_response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        remaining_offers = [
            row
            for row in active_batch_response.context["orders"]
            if row.is_collection_offer
        ]
        self.assertTrue(remaining_offers)
        self.assertContains(active_batch_response, "Accept delivery")
        rider_application.refresh_from_db()
        accept_cooldown_until = rider_application.public_rider_offer_cooldown_until
        self.assertIsNotNone(accept_cooldown_until)
        self.assertLessEqual(
            (accept_cooldown_until - timezone.now()).total_seconds(),
            12,
        )
        self.assertGreater(
            (accept_cooldown_until - timezone.now()).total_seconds(),
            11,
        )
        rider_application.public_rider_batch_started_at = (
            timezone.now() - timedelta(minutes=6)
        )
        rider_application.save(update_fields=[
            "public_rider_batch_started_at",
            "updated_at",
        ])
        expired_active_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertEqual(expired_active_dashboard.context["available_order_count"], 0)
        self.assertFalse(
            any(
                row.is_collection_offer
                for row in expired_active_dashboard.context["orders"]
            )
        )
        self.assertNotContains(expired_active_dashboard, "Accept delivery")
        rider_application.public_rider_batch_started_at = timezone.now()
        rider_application.save(update_fields=[
            "public_rider_batch_started_at",
            "updated_at",
        ])
        remaining_order = next(
            shop_order
            for shop_order in shop_orders
            if shop_order.pk not in declined_order_ids
            and shop_order.pk != next_offered_order.pk
            and shop_order.public_rider_offer_user_id is None
        )
        with patch(
            "seller.public_rider_dispatch.timezone.now",
            return_value=accept_cooldown_until + timedelta(seconds=1),
        ):
            after_accept_dashboard = self.client.get(reverse("rider_dashboard"))
        remaining_order.refresh_from_db()
        self.assertEqual(remaining_order.public_rider_offer_user_id, rider_user.pk)
        self.assertEqual(after_accept_dashboard.context["available_order_count"], 2)

    def test_rider_location_events_target_shop_owner_and_managers(self):
        manager_user = User.objects.create_user(
            username="operations_live_map_manager",
            password="test-live-map-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager_user,
            role="MANAGER",
            is_active=True,
        )
        shop_order = self.create_shop_order(
            "ORD-OPERATIONS-LIVE-LOCATION",
            status="OUT_FOR_DELIVERY",
        )
        with (
            patch("seller.live_updates._send_user_event") as send_event,
            patch(
                "seller.live_updates.submit_background_task",
                side_effect=lambda task, *args, **kwargs: task(*args),
            ),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                shop_order.rider_last_location = {"lat": 21.25, "lng": 72.91}
                shop_order.save(update_fields=["rider_last_location"])

        self.assertEqual(
            {call.args[0] for call in send_event.call_args_list},
            {self.owner.pk, manager_user.pk},
        )
        self.assertEqual(
            {call.args[1] for call in send_event.call_args_list},
            {"rider.location.updated"},
        )
        self.assertTrue(
            all(call.args[2]["shop_id"] == self.shop.pk for call in send_event.call_args_list)
        )

    def test_order_list_filters_by_date_and_order_number(self):
        first = self.create_shop_order("ORD-05-10-2026-11-08-28-2")
        second = self.create_shop_order("ORD-06-10-2026-11-08-28-1")
        first.shop_order_number = "OPERATIONS-SHOP-ORD-07102026-101010-1"
        first.save(update_fields=["shop_order_number"])
        today = timezone.localdate()
        previous_day = today - timedelta(days=1)
        ShopOrder.objects.filter(pk=first.pk).update(
            created_at=timezone.make_aware(
                datetime.combine(today, time(10, 0)),
            ),
        )
        ShopOrder.objects.filter(pk=second.pk).update(
            created_at=timezone.make_aware(
                datetime.combine(previous_day, time(10, 0)),
            ),
        )

        response = self.client.get(
            reverse("shop_order_list"),
            {
                "date": today.isoformat(),
                "order_number": "-2",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row.pk for row in response.context["shop_orders"]],
            [first.pk],
        )
        self.assertContains(response, "Order date")
        self.assertContains(response, "Order number")

        shop_number_response = self.client.get(
            reverse("shop_order_list"),
            {"order_number": "OPERATIONS-SHOP-ORD-07102026-101010-1"},
        )
        self.assertEqual(shop_number_response.status_code, 200)
        self.assertEqual(
            [row.pk for row in shop_number_response.context["shop_orders"]],
            [first.pk],
        )

    def test_order_item_count_column_follows_shop_column(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-ITEM-COUNT")
        shop_order.product_count = 4
        shop_order.save(update_fields=["product_count"])
        first_product = Product.objects.create(
            created_by=self.owner,
            product_name="First ordered product",
            price=Decimal("10.00"),
        )
        second_product = Product.objects.create(
            created_by=self.owner,
            product_name="Second ordered product",
            price=Decimal("20.00"),
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=first_product,
            product_name=first_product.product_name,
            quantity=1,
            price=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product=second_product,
            product_name=second_product.product_name,
            quantity=3,
            price=Decimal("20.00"),
            total=Decimal("60.00"),
        )

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        shop_column = page.index(">Shop <span")
        product_count_column = page.index(">Product Count <span")
        item_count_column = page.index(">Items <span")
        customer_column = page.index(">Customer <span")
        self.assertLess(shop_column, product_count_column)
        self.assertLess(product_count_column, item_count_column)
        self.assertLess(item_count_column, customer_column)
        self.assertContains(response, '<td data-sort-value="2">')
        self.assertContains(response, '<td data-sort-value="4">')

    def test_order_status_filter_lists_and_selects_all_order_statuses(self):
        placed_order = self.create_shop_order(
            "ORD-OPERATIONS-STATUS-PLACED",
            status="PLACED",
        )
        self.create_shop_order(
            "ORD-OPERATIONS-STATUS-RETURNED",
            status="RETURNED",
        )

        response = self.client.get(
            reverse("shop_order_list"),
            {"status": "PLACED"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row.pk for row in response.context["shop_orders"]],
            [placed_order.pk],
        )
        for status_value, status_label in [
            ("pending", "Pending"),
            *OrderStatusHistory.STATUS_CHOICES,
        ]:
            with self.subTest(status=status_value):
                self.assertContains(response, f'value="{status_value}"')
                self.assertContains(response, status_label)
        self.assertContains(
            response,
            '<option value="PLACED" selected>Order Placed</option>',
            html=True,
        )

    def test_double_click_order_row_opens_order_details(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-DOUBLE-CLICK")

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'data-order-url="{reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})}"',
        )
        self.assertContains(response, "Double-click to open order details")
        self.assertContains(response, "row.addEventListener('dblclick'")
        self.assertContains(response, "window.location.assign(orderUrl)")

    def test_all_orders_status_tabs_filter_and_count_requested_statuses(self):
        self.create_shop_order(
            "ORD-OPERATIONS-TAB-PLACED",
            status="PLACED",
        )
        ready_order = self.create_shop_order(
            "ORD-OPERATIONS-TAB-READY",
            status="READY",
        )
        self.create_shop_order(
            "ORD-OPERATIONS-TAB-DELIVERED",
            status="DELIVERED",
        )
        self.create_shop_order(
            "ORD-OPERATIONS-TAB-CONFIRMED",
            status="CONFIRMED",
        )
        self.create_shop_order(
            "ORD-OPERATIONS-TAB-RETURNED",
            status="RETURNED",
        )
        self.create_shop_order(
            "ORD-OPERATIONS-TAB-LEGACY-PENDING",
            status="pending",
        )

        all_orders_response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(all_orders_response.status_code, 200)
        self.assertContains(all_orders_response, 'aria-label="Filter orders by status"')
        self.assertContains(all_orders_response, "Ready for Pickup")
        self.assertContains(all_orders_response, "Confirmed")
        self.assertContains(all_orders_response, "Packing")
        self.assertContains(all_orders_response, "Accepted by Rider")
        self.assertContains(all_orders_response, "Picked Up")
        self.assertContains(all_orders_response, "Out for Delivery")
        self.assertContains(all_orders_response, "Delivered")
        self.assertContains(all_orders_response, "Returned")
        self.assertContains(all_orders_response, "Cancelled")
        self.assertContains(
            all_orders_response,
            "sellerhelper.shopOrders.statusTabScrollLeft",
        )
        self.assertContains(all_orders_response, "statusTabs.scrollLeft = savedScrollLeft")
        self.assertContains(all_orders_response, "statusTabs.addEventListener('scroll'")
        self.assertContains(all_orders_response, "sellerhelper.shopOrders.statusTab")
        self.assertContains(all_orders_response, "window.sessionStorage.setItem")
        self.assertContains(all_orders_response, "window.location.replace")
        self.assertContains(all_orders_response, "data-reset-order-filters")
        status_tabs = {
            tab["value"]: tab
            for tab in all_orders_response.context["order_status_tabs"]
        }
        self.assertEqual(status_tabs[""]["count"], 6)
        self.assertEqual(status_tabs["pending"]["count"], 1)
        self.assertEqual(status_tabs["CONFIRMED"]["count"], 1)
        self.assertEqual(status_tabs["READY"]["count"], 1)
        self.assertEqual(status_tabs["RETURNED"]["count"], 1)
        self.assertIn("status=READY", status_tabs["READY"]["url"])
        self.assertContains(all_orders_response, "flex-wrap: nowrap")
        self.assertContains(all_orders_response, "order-assignment-controls")

        ready_response = self.client.get(
            reverse("shop_order_list"),
            {"status": "READY"},
        )

        self.assertEqual(ready_response.status_code, 200)
        self.assertEqual(
            list(ready_response.context["shop_orders"]),
            [ready_order],
        )

    def test_order_list_shows_rider_name_and_number_in_separate_columns(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-RIDER-CONTACT")
        rider_user = User.objects.create_user(
            username="operations_assigned_rider",
            first_name="Assigned",
            last_name="Rider",
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            mobile_no="+91 98765 43210",
        )
        shop_order.assigned_rider = rider
        shop_order.save(update_fields=["assigned_rider"])

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned Rider")
        self.assertContains(response, "Rider Contact")
        self.assertContains(response, "+91 98765 43210")
        self.assertContains(response, 'href="tel:+919876543210"')

    def test_order_list_falls_back_to_legacy_rider_name_and_contact(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-LEGACY-RIDER-CONTACT")
        shop_order.order.assigned_rider = "legacy_rider_user"
        shop_order.order.rider_contact_no = "98765 11111"
        shop_order.order.save(update_fields=["assigned_rider", "rider_contact_no"])

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "legacy_rider_user")
        self.assertContains(response, "98765 11111")
        self.assertContains(response, 'href="tel:9876511111"')

    def test_order_list_resolves_legacy_rider_name_to_profile_phone(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-LEGACY-RIDER-PROFILE")
        rider_user = User.objects.create_user(
            username="legacy_profile_rider",
            first_name="Legacy",
            last_name="Rider",
        )
        rider_membership = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            mobile_no="98765 22222",
        )
        shop_order.order.assigned_rider = rider_user.username
        shop_order.order.rider_contact_no = ""
        shop_order.order.save(update_fields=["assigned_rider", "rider_contact_no"])

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Legacy Rider")
        self.assertContains(response, "98765 22222")
        self.assertContains(response, 'href="tel:9876522222"')

    def test_order_list_uses_rider_client_profile_contact_as_fallback(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-RIDER-CLIENT-PHONE")
        rider_user = User.objects.create_user(
            username="operations_rider_profile_contact",
            first_name="Profile",
            last_name="Rider",
        )
        ClientProfile.objects.create(
            user=rider_user,
            name="Profile Rider",
            contact_no="98765 33333",
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            mobile_no="",
        )
        shop_order.assigned_rider = rider
        shop_order.save(update_fields=["assigned_rider"])

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Profile Rider")
        self.assertContains(response, "98765 33333")
        self.assertContains(response, 'href="tel:9876533333"')

    def test_order_table_renders_sortable_headers_and_sort_values(self):
        self.create_shop_order("ORD-OPERATIONS-SORT-2")
        self.create_shop_order("ORD-OPERATIONS-SORT-10", status="READY")

        response = self.client.get(reverse("shop_order_list"))

        self.assertEqual(response.status_code, 200)
        for column in range(12):
            with self.subTest(column=column):
                self.assertContains(response, f'data-sort-column="{column}"')
        self.assertContains(response, 'data-sort-type="number">Product Count')
        self.assertContains(response, 'data-sort-type="number">Items')
        self.assertContains(response, 'data-sort-type="number">Amount')
        self.assertContains(response, 'data-sort-column="7" data-sort-type="text">Rider Contact')
        self.assertContains(response, 'class="shop-order-number-column"')
        self.assertContains(response, 'class="bi bi-eye"')
        self.assertContains(response, "<span>View</span>")
        self.assertContains(response, 'data-sort-value="120.00"')
        self.assertContains(response, "localeCompare")
        self.assertContains(response, 'aria-sort="none"')
        self.assertContains(
            response,
            "sortDirection === 1 ? 'ascending' : 'descending'",
        )

    def test_staff_recently_verified_orders_include_only_shop_pickup_otp_history(self):
        older = self.create_shop_order(
            "ORD-STAFF-VERIFIED-OLD",
            status="OUT_FOR_DELIVERY",
        )
        old_history = OrderStatusHistory.objects.create(
            shop_order=older,
            status="PICKED_UP",
            remark="Shop staff verified rider pickup_rider and confirmed parcel pickup by OTP.",
        )
        OrderStatusHistory.objects.filter(pk=old_history.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
        )
        newer = self.create_shop_order(
            "ORD-STAFF-VERIFIED-NEW",
            status="DELIVERED",
        )
        OrderStatusHistory.objects.create(
            shop_order=newer,
            status="PICKED_UP",
            remark="Shop staff verified rider pickup_rider and confirmed parcel pickup by OTP.",
        )
        self.create_shop_order(
            "ORD-STAFF-UNVERIFIED",
            status="PICKED_UP",
        )
        other_owner = User.objects.create_user(
            username="other_operations_owner",
        )
        other_shop = Shop.objects.create(
            name="Other Operations Shop",
            owner=other_owner,
        )
        other_order = Order.objects.create(
            order_id="ORD-STAFF-OTHER-SHOP",
            user="operations_customer",
            status="PICKED_UP",
            shop=other_shop,
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_order,
            shop=other_shop,
            status="PICKED_UP",
        )
        OrderStatusHistory.objects.create(
            shop_order=other_shop_order,
            status="PICKED_UP",
            remark="Shop staff verified rider pickup_rider and confirmed parcel pickup by OTP.",
        )
        manager = User.objects.create_user(
            username="operations_order_manager",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager,
            role="MANAGER",
            is_active=True,
        )
        self.client.force_login(manager)

        response = self.client.get(
            reverse("shop_order_list"),
            {"view": "verified", "status": "PLACED"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(
                response.context["shop_orders"].values_list(
                    "order__order_id",
                    flat=True,
                )
            ),
            ["ORD-STAFF-VERIFIED-NEW", "ORD-STAFF-VERIFIED-OLD"],
        )
        self.assertEqual(response.context["verified_order_count"], 2)
        self.assertContains(response, "Recently verified")
        self.assertContains(response, "Pickup verified")

    def test_owner_dashboard_summarizes_recent_orders_wallet_and_low_stock(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-DASHBOARD")
        ShopWallet.objects.create(shop=self.shop, balance=Decimal("75.25"))
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Dashboard Low Stock",
            price=Decimal("9.00"),
        )
        assignment = ProductShop.objects.create(
            shop=self.shop,
            product=product,
            price=Decimal("9.00"),
            stock=2,
            is_active=True,
        )

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["orders_count"], 1)
        self.assertEqual(response.context["wallet_balance"], Decimal("75.25"))
        self.assertEqual(response.context["low_stock_count"], 1)
        self.assertEqual(response.context["recent_shop_orders"][0].pk, shop_order.pk)
        self.assertContains(response, "Recent shop orders")
        self.assertContains(response, "Dashboard Low Stock")
        self.assertContains(
            response,
            f"?shop={self.shop.pk}&amp;focus_product={product.pk}&amp;focus_shop={self.shop.pk}",
        )

        products_response = self.client.get(
            reverse("product_list"),
            {
                "shop": self.shop.pk,
                "focus_product": product.pk,
                "focus_shop": self.shop.pk,
            },
        )
        self.assertEqual(products_response.status_code, 200)
        self.assertContains(
            products_response,
            f'id="product-row-{product.pk}" data-product-row="{product.pk}"',
        )
        self.assertContains(
            products_response,
            f'data-assignment-shop="{assignment.shop_id}"',
        )
        self.assertContains(products_response, "scrollIntoView")

    def test_product_list_renders_with_decimal_zero_for_unassigned_stock(self):
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Unassigned Stock Product",
            price=Decimal("5.00"),
            unit="piece",
        )

        response = self.client.get(reverse("product_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["products"].get(pk=product.pk).total_shop_stock,
            Decimal("0.0000"),
        )

    def test_product_list_allows_owner_to_edit_shop_price_and_stock(self):
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Editable Shop Listing",
            price=Decimal("15.00"),
            unit="piece",
        )
        assignment = ProductShop.objects.create(
            shop=self.shop,
            product=product,
            price=Decimal("14.00"),
            stock=Decimal("2.0000"),
            is_active=True,
        )

        response = self.client.get(reverse("product_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="shop-price-{assignment.pk}"')
        self.assertContains(response, f'id="shop-stock-{assignment.pk}"')
        self.assertContains(response, 'name="price"')
        self.assertContains(response, 'name="stock"')

        invalid_response = self.client.post(
            reverse("product_list"),
            {
                "assignment_id": assignment.pk,
                "price": "-1.00",
                "stock": "-2",
                "q": "Editable",
                "shop": self.shop.pk,
                "sort": "stock_low",
            },
        )

        self.assertRedirects(
            invalid_response,
            f"{reverse('product_list')}?focus_product={product.pk}"
            f"&focus_shop={self.shop.pk}&q=Editable&shop={self.shop.pk}"
            "&sort=stock_low",
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.price, Decimal("14.00"))
        self.assertEqual(assignment.stock, Decimal("2.0000"))
        self.assertTrue(
            any(
                "Could not save price and stock" in str(message)
                for message in invalid_response.wsgi_request._messages
            )
        )

        saved_response = self.client.post(
            reverse("product_list"),
            {
                "assignment_id": assignment.pk,
                "price": "18.50",
                "stock": "7.2500",
                "q": "Editable",
                "shop": self.shop.pk,
            },
        )

        self.assertRedirects(
            saved_response,
            f"{reverse('product_list')}?focus_product={product.pk}"
            f"&focus_shop={self.shop.pk}&q=Editable&shop={self.shop.pk}",
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.price, Decimal("18.50"))
        self.assertEqual(assignment.stock, Decimal("7.2500"))

    def test_product_list_quick_edit_rejects_another_owners_assignment(self):
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Foreign Shop Assignment",
            price=Decimal("15.00"),
        )
        other_owner = User.objects.create_user(
            username="another_quick_edit_owner",
            password="test-password",
        )
        other_shop = Shop.objects.create(
            name="Another Owner Shop",
            owner=other_owner,
        )
        assignment = ProductShop.objects.create(
            shop=other_shop,
            product=product,
            price=Decimal("14.00"),
            stock=2,
        )

        response = self.client.post(
            reverse("product_list"),
            {
                "assignment_id": assignment.pk,
                "price": "18.50",
                "stock": "7.2500",
            },
        )

        self.assertEqual(response.status_code, 404)
        assignment.refresh_from_db()
        self.assertEqual(assignment.price, Decimal("14.00"))
        self.assertEqual(assignment.stock, Decimal("2.0000"))

    def test_product_list_filters_and_sorts_stock_by_selected_shop(self):
        second_shop = Shop.objects.create(
            name="Second Operations Shop",
            owner=self.owner,
        )
        low_in_first_shop = Product.objects.create(
            created_by=self.owner,
            product_name="Low in first shop",
            price=Decimal("10.00"),
        )
        high_in_first_shop = Product.objects.create(
            created_by=self.owner,
            product_name="High in first shop",
            price=Decimal("20.00"),
        )
        other_shop_product = Product.objects.create(
            created_by=self.owner,
            product_name="Only in second shop",
            price=Decimal("30.00"),
        )
        ProductShop.objects.create(
            shop=self.shop,
            product=low_in_first_shop,
            stock=Decimal("2.0000"),
            is_active=True,
        )
        ProductShop.objects.create(
            shop=self.shop,
            product=high_in_first_shop,
            stock=Decimal("12.0000"),
            is_active=True,
        )
        ProductShop.objects.create(
            shop=second_shop,
            product=low_in_first_shop,
            stock=Decimal("50.0000"),
            is_active=True,
        )
        ProductShop.objects.create(
            shop=second_shop,
            product=other_shop_product,
            stock=Decimal("1.0000"),
            is_active=True,
        )

        all_shops_response = self.client.get(reverse("product_list"))
        self.assertEqual(
            set(all_shops_response.context["products"].values_list("pk", flat=True)),
            {
                low_in_first_shop.pk,
                high_in_first_shop.pk,
                other_shop_product.pk,
            },
        )
        self.assertContains(all_shops_response, "Low stock (5 or less) is highlighted.")

        shop_response = self.client.get(
            reverse("product_list"),
            {"shop": self.shop.pk},
        )
        self.assertEqual(
            set(shop_response.context["products"].values_list("pk", flat=True)),
            {low_in_first_shop.pk, high_in_first_shop.pk},
        )
        self.assertContains(shop_response, f"{self.shop.name} Stock")
        self.assertNotContains(shop_response, other_shop_product.product_name)
        self.assertEqual(
            shop_response.context["products"].get(pk=low_in_first_shop.pk).selected_shop_stock,
            Decimal("2.0000"),
        )
        self.assertContains(shop_response, "data-assignment-shop")

        sorted_shop_response = self.client.get(
            reverse("product_list"),
            {"shop": self.shop.pk, "sort": "stock_low"},
        )
        self.assertEqual(
            list(
                sorted_shop_response.context["products"].values_list(
                    "pk",
                    flat=True,
                )
            ),
            [low_in_first_shop.pk, high_in_first_shop.pk],
        )
        self.assertContains(sorted_shop_response, "Low stock to high")

    def test_product_list_searches_sorts_stock_and_expands_product_details(self):
        low_stock_product = Product.objects.create(
            created_by=self.owner,
            product_name="Needle Search Product",
            sku="NEEDLE-101",
            product_category="KIRANA",
            product_alias_name="Needle alias",
            product_description="Description searchable here",
            price=Decimal("15.00"),
            unit="piece",
        )
        high_stock_product = Product.objects.create(
            created_by=self.owner,
            product_name="Other Product",
            sku="OTHER-202",
            price=Decimal("20.00"),
            unit="piece",
        )
        ProductShop.objects.create(
            shop=self.shop,
            product=low_stock_product,
            stock=1,
            is_active=True,
        )
        ProductShop.objects.create(
            shop=self.shop,
            product=high_stock_product,
            stock=8,
            is_active=True,
        )
        session = self.client.session
        session["active_portal"] = "SHOP_OWNER"
        session.save()

        search_response = self.client.get(
            reverse("product_list"),
            {"q": "NEEDLE-101"},
        )

        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            list(search_response.context["products"].values_list("pk", flat=True)),
            [low_stock_product.pk],
        )
        self.assertContains(search_response, "Double-click to show product details")
        self.assertContains(search_response, "Description searchable here")
        self.assertContains(search_response, "Low stock to high")
        self.assertContains(search_response, "row.addEventListener(\"dblclick\"")
        self.assertContains(search_response, "Needle alias")
        self.assertContains(search_response, 'id="productSearch"')
        self.assertContains(search_response, 'class="product-search-field"')
        self.assertContains(search_response, 'class="product-search-voice"')
        self.assertContains(search_response, "right: .5rem")
        self.assertContains(search_response, 'aria-label="Search products by voice"')
        self.assertContains(search_response, 'data-voice-search-status role="status"')
        self.assertContains(search_response, 'html[data-sellerhelper-native-app="true"] .owner-all-shops')
        self.assertContains(search_response, reverse("shop_add"))
        self.assertContains(search_response, "owner-add-shop")

        sorted_response = self.client.get(
            reverse("product_list"),
            {"sort": "stock_low"},
        )

        self.assertEqual(
            list(sorted_response.context["products"].values_list("pk", flat=True)),
            [low_stock_product.pk, high_stock_product.pk],
        )

    def test_product_search_filters_loaded_rows_and_searches_server_on_no_match(self):
        Product.objects.create(
            created_by=self.owner,
            product_name="Client Filter Product",
            sku="CLIENT-SEARCH",
            price=Decimal("1.00"),
        )
        response = self.client.get(reverse("product_list"))

        self.assertContains(response, "filterLoadedProducts")
        self.assertContains(response, "if (matchingCount)")
        self.assertContains(response, "No loaded products match. Searching all products...")
        self.assertContains(response, "productSearchForm.requestSubmit(), 500")
        self.assertContains(response, 'data-product-search="Client Filter Product')
        self.assertContains(response, "CLIENT-SEARCH")
        self.assertContains(response, 'html[data-sellerhelper-native-app="true"] .owner-add-shop')

    def test_native_app_mobile_topbar_uses_compact_sizing(self):
        response = self.client.get(reverse("product_list"))

        self.assertContains(
            response,
            'html[data-sellerhelper-native-app="true"] .main-navbar .navbar-toggler',
        )
        self.assertContains(response, "max-height: 30px !important")
        self.assertContains(response, "min-height: 28px")
        self.assertContains(response, ".main-navbar .navbar-toggler-icon")
        self.assertContains(response, "width: 1.15em")
        self.assertContains(response, ".mobile-market-search [data-voice-search]")
        self.assertContains(response, "flex: 0 0 36px")
        self.assertContains(response, "color: #495057 !important")
        self.assertContains(response, ".mobile-market-search [data-voice-search] i")
        self.assertContains(response, "border: 0 !important")
        self.assertContains(response, 'button[type="submit"]')
        self.assertContains(response, "border-radius: .5rem !important")
        self.assertContains(
            response,
            'html[data-sellerhelper-native-app="true"] .mobile-market-search',
        )
        self.assertContains(response, "flex: 1 1 0")
        self.assertContains(response, "order: 2")

    def test_restore_sqlite_backup_restores_data_and_keeps_pre_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "db.sqlite3"
            backup_directory = root / "backups"
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("CREATE TABLE sample (value TEXT)")
                database.execute("INSERT INTO sample VALUES ('before-backup')")
                database.commit()

            selected_backup = create_sqlite_backup(
                database_path,
                backup_directory,
                retention_count=5,
            )
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("UPDATE sample SET value = 'current-data'")
                database.commit()

            safety_backup = restore_sqlite_backup(
                database_path,
                selected_backup,
                backup_directory,
                retention_count=5,
            )

            with closing(sqlite3.connect(database_path)) as database:
                restored_value = database.execute(
                    "SELECT value FROM sample"
                ).fetchone()[0]
            with closing(sqlite3.connect(safety_backup)) as database:
                safety_value = database.execute(
                    "SELECT value FROM sample"
                ).fetchone()[0]
            self.assertEqual(restored_value, "before-backup")
            self.assertEqual(safety_value, "current-data")
            self.assertTrue(safety_backup.is_file())

            invalid_backup = backup_directory / "sellerhelper-invalid.sqlite3"
            with closing(sqlite3.connect(invalid_backup)):
                pass
            with self.assertRaises(sqlite3.DatabaseError):
                restore_sqlite_backup(
                    database_path,
                    invalid_backup,
                    backup_directory,
                    retention_count=5,
                )
            with closing(sqlite3.connect(database_path)) as database:
                self.assertEqual(
                    database.execute("SELECT value FROM sample").fetchone()[0],
                    "before-backup",
                )

    def test_admin_restore_page_requires_superuser_and_explicit_confirmation(self):
        superuser = User.objects.create_superuser(
            username="sqlite_restore_admin",
            email="sqlite-restore@example.com",
            password="test-password",
        )
        restore_url = reverse("admin:seller_sqlitebackupsettings_restore")

        with tempfile.TemporaryDirectory() as temporary_directory:
            backup_path = Path(temporary_directory) / "sellerhelper-test-backup.sqlite3"
            backup_path.touch()
            self.owner.is_staff = True
            self.owner.save(update_fields=["is_staff"])
            with patch("seller.admin.list_sqlite_backups", return_value=[backup_path]):
                response = self.client.get(restore_url)
            self.assertEqual(response.status_code, 403)

            self.client.force_login(superuser)
            with patch("seller.admin.list_sqlite_backups", return_value=[backup_path]):
                response = self.client.get(restore_url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Type RESTORE to confirm")
            self.assertContains(response, "Backup file and created time")
            self.assertContains(response, "sellerhelper-test-backup.sqlite3")
            settings_url = reverse(
                "admin:seller_sqlitebackupsettings_change",
                args=[1],
            )
            response = self.client.get(settings_url)
            self.assertContains(response, restore_url)

            with (
                patch("seller.admin.list_sqlite_backups", return_value=[backup_path]),
                patch("seller.admin.restore_sqlite_backup") as restore,
            ):
                response = self.client.post(
                    restore_url,
                    {"backup": backup_path.name, "confirmation": "no"},
                )
            self.assertEqual(response.status_code, 200)
            restore.assert_not_called()

            with (
                patch("seller.admin.list_sqlite_backups", return_value=[backup_path]),
                patch("seller.admin.connections.close_all"),
                patch("seller.admin.restore_sqlite_backup") as restore,
            ):
                response = self.client.post(
                    restore_url,
                    {"backup": backup_path.name, "confirmation": "RESTORE"},
                )

        self.assertEqual(response.status_code, 302)
        restore.assert_called_once()

    def test_admin_can_create_immediate_sqlite_backup(self):
        superuser = User.objects.create_superuser(
            username="sqlite_immediate_backup_admin",
            email="sqlite-backup@example.com",
            password="test-password",
        )
        self.client.force_login(superuser)
        backup_url = reverse("admin:seller_sqlitebackupsettings_backup")
        settings_url = reverse(
            "admin:seller_sqlitebackupsettings_change",
            args=[1],
        )

        response = self.client.get(settings_url)
        self.assertContains(response, backup_url)
        self.assertContains(response, "Create immediate backup")

        backup_path = Path(settings.BASE_DIR) / "sqlite_backups" / "sellerhelper-now.sqlite3"
        with patch("seller.admin.create_sqlite_backup", return_value=backup_path) as create_backup:
            response = self.client.post(backup_url, follow=True)

        self.assertRedirects(response, settings_url)
        create_backup.assert_called_once_with(
            connections["default"].settings_dict["NAME"],
            Path(settings.BASE_DIR) / "sqlite_backups",
            SQLiteBackupSettings.objects.get(pk=1).retention_count,
        )
        self.assertContains(response, "Created SQLite backup")

    def test_order_detail_shows_customer_directions_and_auto_status(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-1")
        profile_user = User.objects.create_user(
            username="operations_customer",
            password="test-pass-123",
        )
        ClientProfile.objects.create(user=profile_user, name="Asha Customer")

        response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asha Customer")
        self.assertContains(response, "Copy coordinates")
        self.assertContains(response, 'id="orderLocationMap"')
        self.assertContains(response, 'id="shop-location-data"')
        self.assertContains(response, 'locationPin("#198754")')
        self.assertContains(response, 'locationPin("#dc3545")')
        self.assertContains(response, "Shop location")
        self.assertContains(response, "Customer delivery location")
        self.assertContains(response, '"Satellite": satelliteLayer')
        self.assertContains(response, "satelliteLayer.addTo(map)")
        self.assertContains(response, "Center map on my current location")
        self.assertContains(response, "Map tiles could not be loaded")
        self.assertContains(response, "requestSubmit()")
        self.assertContains(response, reverse(
            "shop_order_rider_location",
            kwargs={"shop_order_id": shop_order.pk},
        ))

    def test_shop_cannot_mark_parcel_picked_up_or_start_route_manually(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-RIDER-STATUS", status="READY")

        for route_name, status in (
            ("shop_order_status", "PICKED_UP"),
            ("shop_order_status_update", "OUT_FOR_DELIVERY"),
        ):
            with self.subTest(route=route_name, status=status):
                response = self.client.post(
                    reverse(route_name, kwargs={"shop_order_id": shop_order.pk}),
                    {"status": status},
                )
                self.assertEqual(response.status_code, 302)
                shop_order.refresh_from_db()
                self.assertEqual(shop_order.status, "READY")

    def test_owner_and_reporting_manager_can_read_wallet_but_only_owner_can_withdraw(self):
        wallet, _ = ShopWallet.objects.get_or_create(
            shop=self.shop,
            defaults={"balance": Decimal("50.00")},
        )
        wallet.balance = Decimal("50.00")
        wallet.save(update_fields=["balance", "updated_at"])
        manager = User.objects.create_user(
            username="operations_finance_manager",
            password="test-pass-123",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager,
            role="MANAGER",
            can_view_reports=True,
            can_manage_products=True,
        )

        owner_response = self.client.get(
            reverse("shop_wallet", kwargs={"shop_id": self.shop.pk})
        )
        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, "Financial totals")
        self.assertTrue(owner_response.context["can_withdraw"])
        self.assertNotContains(owner_response, "Read-only financial access")

        self.client.force_login(manager)
        manager_response = self.client.get(
            reverse("shop_wallet", kwargs={"shop_id": self.shop.pk})
        )
        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, "Read-only financial access")
        self.assertNotContains(manager_response, "Withdraw to bank")
        self.assertEqual(
            self.client.post(
                reverse("shop_wallet", kwargs={"shop_id": self.shop.pk}),
                {"amount": "10.00"},
            ).status_code,
            403,
        )

    def test_manager_can_view_active_rider_locations_and_other_user_cannot(self):
        rider_user = User.objects.create_user(
            username="operations_rider",
            password="test-pass-123",
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        shop_order = self.create_shop_order(
            "ORD-OPERATIONS-RIDER",
            status="OUT_FOR_DELIVERY",
        )
        shop_order.assigned_rider = rider
        shop_order.rider_last_location = {"lat": 21.25, "lng": 72.91}
        shop_order.save(update_fields=["assigned_rider", "rider_last_location"])
        manager = User.objects.create_user(
            username="operations_map_manager",
            password="test-pass-123",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager,
            role="MANAGER",
            is_active=True,
        )

        self.client.force_login(manager)
        map_response = self.client.get(
            reverse("shop_rider_map", kwargs={"shop_id": self.shop.pk})
        )
        location_response = self.client.get(
            reverse("shop_rider_map", kwargs={"shop_id": self.shop.pk}),
            {"format": "json"},
        )
        self.assertEqual(map_response.status_code, 200)
        self.assertEqual(location_response.status_code, 200)
        self.assertContains(map_response, '"Satellite": satelliteLayer')
        self.assertContains(map_response, "satelliteLayer.addTo(map)")
        self.assertContains(map_response, "Center map on my current location")
        self.assertContains(map_response, "Fit all rider locations")
        self.assertContains(map_response, "void refresh(true)")
        self.assertContains(map_response, "maximumAge: 0")
        self.assertContains(map_response, "rider.location.updated")
        self.assertNotContains(map_response, "window.setInterval(refresh, 15000)")
        self.assertEqual(
            location_response.json()["riders"][0]["location"],
            {"lat": 21.25, "lng": 72.91},
        )

        self.client.force_login(User.objects.create_user(
            username="operations_map_outsider",
            password="test-pass-123",
        ))
        self.assertEqual(
            self.client.get(
                reverse("shop_rider_map", kwargs={"shop_id": self.shop.pk})
            ).status_code,
            403,
        )


class ShopPickupLocationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="pickup_location_owner")
        self.shop = Shop.objects.create(
            name="Pickup Shop",
            owner=self.owner,
            location={"lat": 21.2, "lng": 72.8},
        )
        self.order = Order.objects.create(
            order_id="PICKUP-LOCATION-ORDER",
            tracking_code="PICKUP1",
            user="pickup_customer",
            delivery_address="Delivery address",
            delivery_location="21.31,72.91",
            placed_location={"lat": 21.31, "lng": 72.91},
        )

    def test_nearest_pickup_location_is_selected_and_snapshotted(self):
        self.shop.pickup_locations = [
            {
                "name": "North Branch",
                "address": "North market",
                "lat": 21.31,
                "lng": 72.91,
            },
            {
                "name": "South Branch",
                "address": "South market",
                "lat": 20.9,
                "lng": 72.5,
            },
        ]
        selected = seller_views._select_shop_pickup_location(self.shop, self.order)
        self.assertEqual(selected["name"], "North Branch")
        self.assertEqual(selected["address"], "North market")
        self.assertEqual(
            {"lat": selected["lat"], "lng": selected["lng"]},
            {"lat": 21.31, "lng": 72.91},
        )

        shop_order = ShopOrder.objects.create(
            order=self.order,
            shop=self.shop,
            pickup_location=selected,
        )
        self.shop.pickup_locations = []
        self.shop.save(update_fields=["pickup_locations"])
        self.assertEqual(
            seller_views._get_shop_order_pickup_location(shop_order)["name"],
            "North Branch",
        )

    def test_rider_route_estimate_uses_the_order_pickup_snapshot(self):
        pickup = {
            "name": "North Branch",
            "address": "North market",
            "lat": 21.31,
            "lng": 72.91,
        }
        shop_order = ShopOrder.objects.create(
            order=self.order,
            shop=self.shop,
            pickup_location=pickup,
        )
        pay_settings = Mock()
        pay_settings.road_network_geojson.name = "roads.geojson"
        pay_settings.road_network_files.values_list.return_value = []
        pay_settings.shared_rider_base_pay = Decimal("10.00")
        pay_settings.shared_rider_rate_per_km = Decimal("5.00")
        pay_settings.public_rider_pay_include_pickup_distance = True
        pay_settings.public_rider_pickup_distance_pay_percentage = Decimal(
            "100.00",
        )
        pay_settings.public_rider_pickup_threshold_bonus_enabled = False
        pay_settings.public_rider_pickup_threshold_km = Decimal("2.000")
        pay_settings.public_rider_pickup_threshold_rate_per_km = Decimal("0.00")
        pay_settings.public_rider_pickup_threshold_distance_method = "EITHER"

        with patch(
            "seller.views.local_road_route_distance_km",
            side_effect=[2.0, 3.0],
        ) as route_distance:
            estimate = seller_views._public_rider_route_estimate(
                shop_order,
                {"lat": 21.1, "lng": 72.7},
                pay_settings,
            )

        self.assertEqual(estimate["pickup_to_delivery_km"], Decimal("2.000"))
        self.assertEqual(estimate["rider_to_pickup_km"], Decimal("3.000"))
        self.assertEqual(route_distance.call_args_list[0].args[1], pickup)
        self.assertEqual(
            estimate["estimated_pay_distance_km"],
            Decimal("5.000"),
        )
        self.assertEqual(estimate["estimated_pay"], Decimal("35.00"))

        pay_settings.public_rider_pickup_threshold_bonus_enabled = True
        pay_settings.public_rider_pickup_threshold_rate_per_km = Decimal("3.00")
        pay_settings.public_rider_pickup_threshold_distance_method = "ROAD"
        with patch(
            "seller.views.local_road_route_distance_km",
            side_effect=[2.0, 3.0],
        ):
            road_threshold_estimate = seller_views._public_rider_route_estimate(
                shop_order,
                {"lat": 21.1, "lng": 72.7},
                pay_settings,
            )
        self.assertEqual(
            road_threshold_estimate["pickup_threshold_distance_source"],
            "road",
        )
        self.assertEqual(
            road_threshold_estimate["pickup_threshold_bonus_distance_km"],
            Decimal("1.000"),
        )
        self.assertEqual(road_threshold_estimate["pickup_threshold_bonus"], Decimal("3.00"))
        self.assertEqual(road_threshold_estimate["estimated_pay"], Decimal("38.00"))

        pay_settings.public_rider_pickup_threshold_distance_method = "STRAIGHT_LINE"
        pay_settings.public_rider_pickup_threshold_rate_per_km = Decimal("4.00")
        with patch(
            "seller.views.local_road_route_distance_km",
            side_effect=[2.0, 3.0],
        ):
            straight_threshold_estimate = (
                seller_views._public_rider_route_estimate(
                    shop_order,
                    {"lat": 21.1, "lng": 72.7},
                    pay_settings,
                )
            )
        self.assertEqual(
            straight_threshold_estimate["pickup_threshold_distance_source"],
            "straight_line",
        )
        expected_straight_bonus = (
            max(
                Decimal("0"),
                straight_threshold_estimate["straight_line_rider_to_pickup_km"]
                - Decimal("2.000"),
            )
            * Decimal("4.00")
        ).quantize(Decimal("0.01"))
        self.assertEqual(
            straight_threshold_estimate["pickup_threshold_bonus"],
            expected_straight_bonus,
        )

        pay_settings.public_rider_pickup_threshold_distance_method = "EITHER"
        with patch(
            "seller.views.local_road_route_distance_km",
            side_effect=[2.0, None],
        ):
            fallback_threshold_estimate = (
                seller_views._public_rider_route_estimate(
                    shop_order,
                    {"lat": 21.1, "lng": 72.7},
                    pay_settings,
                )
            )
        self.assertEqual(
            fallback_threshold_estimate["pickup_threshold_distance_source"],
            "straight_line",
        )
        self.assertEqual(
            fallback_threshold_estimate["estimated_pay"],
            (
                fallback_threshold_estimate[
                    "estimated_pay_before_threshold_bonus"
                ]
                + fallback_threshold_estimate["pickup_threshold_bonus"]
            ),
        )

        pay_settings.public_rider_pickup_threshold_bonus_enabled = False
        pay_settings.public_rider_pickup_threshold_rate_per_km = Decimal("0.00")
        pay_settings.public_rider_pickup_distance_pay_percentage = Decimal(
            "50.00",
        )
        with patch(
            "seller.views.local_road_route_distance_km",
            side_effect=[2.0, 3.0],
        ):
            half_pickup_estimate = seller_views._public_rider_route_estimate(
                shop_order,
                {"lat": 21.1, "lng": 72.7},
                pay_settings,
            )
        self.assertEqual(
            half_pickup_estimate["estimated_pay_distance_km"],
            Decimal("3.500"),
        )
        self.assertEqual(half_pickup_estimate["estimated_pay"], Decimal("27.50"))

        pay_settings.public_rider_pay_include_pickup_distance = False
        with patch(
            "seller.views.local_road_route_distance_km",
            return_value=2.0,
        ):
            excluded_pickup_estimate = seller_views._public_rider_route_estimate(
                shop_order,
                None,
                pay_settings,
            )
        self.assertEqual(
            excluded_pickup_estimate["estimated_pay_distance_km"],
            Decimal("2.000"),
        )
        self.assertEqual(excluded_pickup_estimate["estimated_pay"], Decimal("20.00"))
        self.assertFalse(excluded_pickup_estimate["pickup_distance_included"])

    def test_pickup_locations_are_validated_against_admin_limit(self):
        locations = json.dumps([{
            "name": "Branch One",
            "address": "One Road",
            "lat": 21.31,
            "lng": 72.91,
        }])
        self.assertEqual(
            seller_views._parse_shop_pickup_locations(
                locations,
                max_locations=1,
            )[0]["name"],
            "Branch One",
        )
        with self.assertRaisesMessage(
            ValueError,
            "up to 0 additional pickup locations",
        ):
            seller_views._parse_shop_pickup_locations(
                locations,
                max_locations=0,
            )
        with self.assertRaisesMessage(ValueError, "valid map coordinates"):
            seller_views._parse_shop_pickup_locations(
                json.dumps([{
                    "name": "Bad Branch",
                    "lat": 95,
                    "lng": 72,
                }]),
                max_locations=1,
            )

    def test_shop_owner_can_save_pickup_locations_within_admin_limit(self):
        DeliverySharingSettings.objects.update_or_create(
            pk=1,
            defaults={"max_additional_pickup_locations": 1},
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("shop_edit", kwargs={"pk": self.shop.pk}),
            {
                "name": self.shop.name,
                "storefront_slug": "pickup-shop",
                "delivery_fee": "0.00",
                "location_json": json.dumps({"lat": 21.2, "lng": 72.8}),
                "pickup_locations_json": json.dumps([{
                    "name": "North Branch",
                    "address": "North market",
                    "lat": 21.31,
                    "lng": 72.91,
                }]),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.pickup_locations[0]["name"], "North Branch")

        rejected_response = self.client.post(
            reverse("shop_edit", kwargs={"pk": self.shop.pk}),
            {
                "name": self.shop.name,
                "storefront_slug": "pickup-shop",
                "delivery_fee": "0.00",
                "location_json": json.dumps({"lat": 21.2, "lng": 72.8}),
                "pickup_locations_json": json.dumps([
                    {
                        "name": "Branch One",
                        "lat": 21.31,
                        "lng": 72.91,
                    },
                    {
                        "name": "Branch Two",
                        "lat": 21.32,
                        "lng": 72.92,
                    },
                ]),
            },
        )
        self.assertEqual(rejected_response.status_code, 200)
        self.assertContains(
            rejected_response,
            "Your shop may configure up to 1 additional pickup locations.",
        )
        self.shop.refresh_from_db()
        self.assertEqual(len(self.shop.pickup_locations), 1)


class MobileBackgroundLocationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="background_location_owner")
        self.customer = User.objects.create_user(username="background_location_customer")
        self.rider_user = User.objects.create_user(username="background_location_rider")
        self.shop = Shop.objects.create(
            name="Background Location Shop",
            owner=self.owner,
            location={"lat": 21.2, "lng": 72.8},
        )
        self.rider = ShopStaff.objects.create(
            shop=self.shop,
            user=self.rider_user,
            role="RIDER",
            is_active=True,
        )
        self.order = Order.objects.create(
            order_id="BG-LOCATION-ORDER",
            tracking_code="BGLOC1",
            user=self.customer.username,
            delivery_address="Delivery address",
            delivery_location="21.3,72.9",
            placed_location={"lat": 21.3, "lng": 72.9},
            is_valid_order=True,
            status="PLACED",
        )
        self.shop_order = ShopOrder.objects.create(
            order=self.order,
            shop=self.shop,
            assigned_rider=self.rider,
            status="OUT_FOR_DELIVERY",
        )

    def create_location_session(self, *, user, role, order_id=""):
        self.client.force_login(user)
        return self.client.post(
            reverse("mobile_location_session"),
            {"role": role, "consent": "true", "order_id": order_id},
        )

    def send_location(self, token, latitude=21.31, longitude=72.91):
        return self.client.post(
            reverse("mobile_live_location_update"),
            data=json.dumps({"latitude": latitude, "longitude": longitude}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_customer_opt_in_updates_live_location_until_order_is_terminal(self):
        session_response = self.create_location_session(
            user=self.customer,
            role="customer",
            order_id=self.order.tracking_code,
        )
        self.assertEqual(session_response.status_code, 200)
        token = session_response.json()["token"]

        update_response = self.send_location(token)
        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(update_response.json()["tracking_active"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_live_location["lat"], 21.31)
        self.assertEqual(self.order.customer_live_location["lng"], 72.91)
        self.assertIn("observed_at", self.order.customer_live_location)

        self.client.force_login(self.rider_user)
        rider_location_response = self.client.get(
            reverse(
                "shop_order_rider_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            )
        )
        self.assertEqual(rider_location_response.status_code, 200)
        self.assertEqual(
            rider_location_response.json()["customer_location"]["lat"],
            21.31,
        )

        self.client.force_login(
            User.objects.create_user(username="background_location_unassigned")
        )
        self.client.raise_request_exception = False
        unauthorized_location_response = self.client.get(
            reverse(
                "shop_order_rider_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            )
        )
        self.client.raise_request_exception = True
        self.assertEqual(unauthorized_location_response.status_code, 403)

        self.client.force_login(self.rider_user)
        self.shop_order.status = "DELIVERED"
        self.shop_order.delivery_completed_at = timezone.now()
        self.shop_order.save(update_fields=["status", "delivery_completed_at"])
        stopped_response = self.send_location(token, 21.32, 72.92)
        self.assertEqual(stopped_response.status_code, 200)
        self.assertFalse(stopped_response.json()["tracking_active"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_live_location["lat"], 21.31)

    def test_customer_can_stop_and_clear_shared_location(self):
        session_response = self.create_location_session(
            user=self.customer,
            role="customer",
            order_id=self.order.tracking_code,
        )
        token = session_response.json()["token"]
        self.assertEqual(self.send_location(token).status_code, 200)

        stop_response = self.client.post(
            reverse("mobile_live_location_update"),
            data=json.dumps({"stop": True}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(stop_response.status_code, 200)
        self.assertFalse(stop_response.json()["tracking_active"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_live_location, {})

    def test_customer_location_stops_being_visible_when_stale(self):
        session_response = self.create_location_session(
            user=self.customer,
            role="customer",
            order_id=self.order.tracking_code,
        )
        self.assertEqual(self.send_location(session_response.json()["token"]).status_code, 200)

        self.order.refresh_from_db()
        self.order.customer_live_location["observed_at"] = (
            timezone.now() - timedelta(minutes=3)
        ).isoformat()
        self.order.save(update_fields=["customer_live_location"])
        self.client.force_login(self.rider_user)
        response = self.client.get(
            reverse(
                "shop_order_rider_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["customer_location"])

    def test_accepted_rider_without_assignment_link_can_read_live_location(self):
        self.shop_order.assigned_rider = None
        self.shop_order.rider_accepted_at = timezone.now()
        self.shop_order.save(update_fields=["assigned_rider", "rider_accepted_at"])
        self.rider.delete()
        PublicRiderApplication.objects.create(
            user=self.rider_user,
            full_name="Background Location Public Rider",
            phone="9876543210",
            address="Rider route",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-BACKGROUND-LOCATION",
            work_area={
                "type": "circle",
                "center": {"lat": 21.2, "lng": 72.8},
                "radius_km": 10,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        self.order.assigned_rider = self.rider_user.username
        self.order.customer_live_location = {
            "lat": 21.31,
            "lng": 72.91,
            "observed_at": timezone.now().isoformat(),
        }
        self.order.save(update_fields=["assigned_rider", "customer_live_location"])

        self.client.force_login(self.rider_user)
        response = self.client.get(
            reverse(
                "shop_order_rider_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rider_name"], self.rider_user.username)
        self.assertEqual(
            response.json()["customer_location"]["lat"],
            21.31,
        )

    def test_rider_session_is_scoped_to_assigned_active_delivery(self):
        session_response = self.create_location_session(
            user=self.rider_user,
            role="rider",
        )
        self.assertEqual(session_response.status_code, 200)
        self.assertEqual(
            session_response.json()["destinations"],
            [{
                "latitude": 21.3,
                "longitude": 72.9,
                "identifier": f"shop-order-{self.shop_order.pk}",
            }],
        )

        update_response = self.send_location(session_response.json()["token"])
        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(update_response.json()["tracking_active"])
        self.shop_order.refresh_from_db()
        self.assertEqual(self.shop_order.rider_last_location["lat"], 21.31)

        self.shop_order.assigned_rider = None
        self.shop_order.save(update_fields=["assigned_rider"])
        inactive_response = self.send_location(session_response.json()["token"])
        self.assertEqual(inactive_response.status_code, 200)
        self.assertFalse(inactive_response.json()["tracking_active"])

    def test_location_update_rejects_invalid_coordinates_and_invalid_tokens(self):
        self.assertEqual(
            self.send_location("invalid-token").status_code,
            401,
        )
        outsider = User.objects.create_user(username="background_location_outsider")
        unauthorized_response = self.create_location_session(
            user=outsider,
            role="customer",
            order_id=self.order.tracking_code,
        )
        self.assertEqual(unauthorized_response.status_code, 404)

        self.client.force_login(self.customer)
        session_response = self.client.post(
            reverse("mobile_location_session"),
            {
                "role": "customer",
                "consent": "true",
                "order_id": self.order.tracking_code,
            },
        )
        token = session_response.json()["token"]
        self.assertEqual(
            self.send_location(token, 91, 72).status_code,
            400,
        )


class SharedRiderDeliveryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="shared_rider_base_owner",
            password="test-pass-123",
        )
        self.shared_owner = User.objects.create_user(
            username="shared_rider_optin_owner",
            password="test-pass-123",
        )
        self.rider_user = User.objects.create_user(
            username="shared_rider_user",
            password="test-pass-123",
        )
        self.base_shop = Shop.objects.create(
            name="Rider Home Shop",
            owner=self.owner,
            location={"lat": 21.2, "lng": 72.8},
        )
        self.shared_shop = Shop.objects.create(
            name="Nearby Opt-in Shop",
            owner=self.shared_owner,
            location={"lat": 21.201, "lng": 72.8},
            share_riders_with_nearby_shops=True,
        )
        self.rider = ShopStaff.objects.create(
            shop=self.base_shop,
            user=self.rider_user,
            role="RIDER",
            is_active=True,
        )
        DeliverySharingSettings.objects.update_or_create(
            pk=1,
            defaults={"shared_rider_radius_km": Decimal("0.35")},
        )
        self.order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-1",
            user="shared_customer",
            delivery_address="Shared delivery address",
            delivery_location="21.22,72.82",
            delivery_contact_no="9876543210",
            payment_type="PREPAID",
            payment_status="PAID",
            status="READY",
            shop=self.shared_shop,
        )
        self.shop_order = ShopOrder.objects.create(
            order=self.order,
            shop=self.shared_shop,
            total_amount=Decimal("80.00"),
            status="READY",
        )
        ShopOrder.objects.filter(pk=self.shop_order.pk).update(
            created_at=timezone.now() - timedelta(minutes=2),
        )
        self.client.force_login(self.rider_user)

    def test_recently_verified_tab_lists_only_this_riders_verified_pickups_newest_first(self):
        self.shop_order.assigned_rider = self.rider
        self.shop_order.status = "OUT_FOR_DELIVERY"
        self.shop_order.save(update_fields=["assigned_rider", "status"])
        older_verification = OrderStatusHistory.objects.create(
            shop_order=self.shop_order,
            status="PICKED_UP",
            remark="Shop staff verified rider shared_rider_user and confirmed parcel pickup by OTP.",
        )
        OrderStatusHistory.objects.filter(pk=older_verification.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
        )

        recent_order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-VERIFIED-NEW",
            user="shared_customer",
            delivery_address="Recent delivery address",
            delivery_location="21.22,72.82",
            delivery_contact_no="9876543210",
            status="DELIVERED",
            shop=self.shared_shop,
        )
        recent_shop_order = ShopOrder.objects.create(
            order=recent_order,
            shop=self.shared_shop,
            assigned_rider=self.rider,
            total_amount=Decimal("50.00"),
            status="DELIVERED",
        )
        OrderStatusHistory.objects.create(
            shop_order=recent_shop_order,
            status="PICKED_UP",
            remark="Shop staff verified rider shared_rider_user and confirmed parcel pickup by OTP.",
        )

        unverified_order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-UNVERIFIED",
            user="shared_customer",
            delivery_address="Unverified delivery address",
            status="PICKED_UP",
            shop=self.shared_shop,
        )
        ShopOrder.objects.create(
            order=unverified_order,
            shop=self.shared_shop,
            assigned_rider=self.rider,
            total_amount=Decimal("35.00"),
            status="PICKED_UP",
        )

        other_rider_user = User.objects.create_user(
            username="other_verified_rider",
        )
        other_rider = ShopStaff.objects.create(
            shop=self.base_shop,
            user=other_rider_user,
            role="RIDER",
            is_active=True,
        )
        other_order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-OTHER-VERIFIED",
            user="shared_customer",
            delivery_address="Other rider delivery address",
            status="PICKED_UP",
            shop=self.shared_shop,
        )
        other_shop_order = ShopOrder.objects.create(
            order=other_order,
            shop=self.shared_shop,
            assigned_rider=other_rider,
            total_amount=Decimal("25.00"),
            status="PICKED_UP",
        )
        OrderStatusHistory.objects.create(
            shop_order=other_shop_order,
            status="PICKED_UP",
            remark="Shop staff verified rider other_verified_rider and confirmed parcel pickup by OTP.",
        )

        response = self.client.get(reverse("rider_dashboard"), {"view": "verified"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recently verified")
        self.assertEqual(
            list(
                response.context["orders"].values_list(
                    "order__order_id",
                    flat=True,
                )
            ),
            ["ORD-SHARED-RIDER-VERIFIED-NEW", "ORD-SHARED-RIDER-1"],
        )
        self.assertEqual(response.context["verified_order_count"], 2)
        self.assertContains(response, "ORD-SHARED-RIDER-VERIFIED-NEW")
        self.assertContains(response, "ORD-SHARED-RIDER-1")
        self.assertContains(response, "Pickup verified:")
        self.assertNotContains(
            response,
            'class="small text-muted mb-2 rider-live-location"',
        )
        self.assertNotContains(response, ">Complete Delivery</button>")

    def test_capacity_assigns_primary_order_but_skips_overflowing_sibling(self):
        self.rider.max_load_weight_kg = Decimal("0.10")
        self.rider.max_load_volume_cubic_ft = Decimal("0.10")
        self.rider.save(update_fields=[
            "max_load_weight_kg",
            "max_load_volume_cubic_ft",
        ])
        sibling_order = ShopOrder.objects.create(
            order=self.order,
            shop=self.base_shop,
            total_amount=Decimal("80.00"),
            status="READY",
        )

        response = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": self.shop_order.pk}),
        )

        self.assertEqual(response.status_code, 302)
        self.shop_order.refresh_from_db()
        sibling_order.refresh_from_db()
        self.assertEqual(self.shop_order.assigned_rider_id, self.rider.pk)
        self.assertIsNone(sibling_order.assigned_rider_id)
        self.assertEqual(sibling_order.status, "READY")

    def test_group_shop_needs_owner_approval_then_rider_can_accept_its_order(self):
        self.base_shop.allow_owner_create_shared_rider_groups = True
        self.base_shop.save(update_fields=["allow_owner_create_shared_rider_groups"])
        self.client.force_login(self.owner)
        create_response = self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "create",
                "name": "Central delivery team",
                "created_from_shop": str(self.base_shop.pk),
            },
        )
        self.assertRedirects(create_response, reverse("shared_rider_groups"))
        group = SharedRiderGroup.objects.get(name="Central delivery team")
        self.assertTrue(
            SharedRiderGroupShop.objects.filter(
                group=group,
                shop=self.base_shop,
                status=SharedRiderGroupShop.STATUS_APPROVED,
            ).exists()
        )

        distant_owner = User.objects.create_user(
            username="shared_group_distant_owner",
            password="test-password",
        )
        distant_shop = Shop.objects.create(
            name="Distant Group Shop",
            owner=distant_owner,
        )
        distant_order = Order.objects.create(
            order_id="ORD-SHARED-GROUP-1",
            user="shared_customer",
            delivery_address="Group delivery address",
            delivery_location="21.22,72.82",
            delivery_contact_no="9876543210",
            status="READY",
            shop=distant_shop,
        )
        distant_shop_order = ShopOrder.objects.create(
            order=distant_order,
            shop=distant_shop,
            total_amount=Decimal("45.00"),
            status="READY",
        )
        ShopOrder.objects.filter(pk=distant_shop_order.pk).update(
            created_at=timezone.now() - timedelta(minutes=2),
        )

        add_response = self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "add-shop",
                "group_id": str(group.pk),
                "shop_id": str(distant_shop.pk),
            },
        )
        self.assertRedirects(add_response, reverse("shared_rider_groups"))
        membership = SharedRiderGroupShop.objects.get(group=group, shop=distant_shop)
        self.assertEqual(membership.status, SharedRiderGroupShop.STATUS_PENDING)

        self.client.force_login(self.rider_user)
        before_approval = self.client.get(reverse("rider_dashboard"))
        self.assertNotContains(before_approval, "ORD-SHARED-GROUP-1")
        self.assertEqual(
            self.client.post(
                reverse("accept_rider_order", kwargs={"shop_order_id": distant_shop_order.pk}),
            ).status_code,
            403,
        )

        self.client.force_login(distant_owner)
        self.assertEqual(
            self.client.get(reverse("shared_rider_groups")).status_code,
            200,
        )
        approval_response = self.client.post(
            reverse(
                "review_shared_rider_group_shop",
                kwargs={"membership_id": membership.pk},
            ),
            {"action": "approve"},
        )
        self.assertRedirects(approval_response, reverse("shared_rider_groups"))
        membership.refresh_from_db()
        self.assertEqual(membership.status, SharedRiderGroupShop.STATUS_APPROVED)

        self.client.force_login(self.rider_user)
        available = self.client.get(reverse("rider_dashboard"))
        self.assertContains(available, "ORD-SHARED-GROUP-1")
        self.assertContains(available, "Rider group")
        accepted = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": distant_shop_order.pk}),
        )
        self.assertRedirects(
            accepted,
            f"{reverse('rider_dashboard')}?view=active#order-{distant_shop_order.pk}",
            fetch_redirect_response=False,
        )
        distant_shop_order.refresh_from_db()
        self.assertEqual(distant_shop_order.assigned_rider_id, self.rider.pk)
        self.assertEqual(distant_shop_order.status, "RIDER_ASSIGNED")
        self.client.force_login(distant_owner)
        self.assertEqual(
            self.client.post(
                reverse("rider_pickup_order", kwargs={"shop_order_id": distant_shop_order.pk}),
                {
                    "pickup_otp": seller_views._ensure_rider_pickup_otp(
                        distant_shop_order,
                    ),
                },
            ).status_code,
            302,
        )
        distant_shop_order.refresh_from_db()
        self.assertEqual(distant_shop_order.status, "OUT_FOR_DELIVERY")

    def test_group_creation_requires_admin_permission_and_only_target_owner_can_review(self):
        self.client.force_login(self.owner)
        denied = self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "create",
                "name": "Unauthorized group",
                "created_from_shop": str(self.base_shop.pk),
            },
        )
        self.assertEqual(denied.status_code, 403)

        self.base_shop.allow_owner_create_shared_rider_groups = True
        self.base_shop.save(update_fields=["allow_owner_create_shared_rider_groups"])
        self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "create",
                "name": "Approval test group",
                "created_from_shop": str(self.base_shop.pk),
            },
        )
        group = SharedRiderGroup.objects.get(name="Approval test group")
        requested = Shop.objects.create(
            name="Approval target",
            owner=self.shared_owner,
        )
        self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "add-shop",
                "group_id": group.pk,
                "shop_id": requested.pk,
            },
        )
        membership = SharedRiderGroupShop.objects.get(group=group, shop=requested)

        self.assertEqual(
            self.client.post(
                reverse(
                    "review_shared_rider_group_shop",
                    kwargs={"membership_id": membership.pk},
                ),
                {"action": "approve"},
            ).status_code,
            403,
        )
        self.assertEqual(membership.status, SharedRiderGroupShop.STATUS_PENDING)

    def test_nearby_opted_in_delivery_can_be_accepted_and_tracked(self):
        available = self.client.get(reverse("rider_dashboard"))

        self.assertEqual(available.status_code, 200)
        self.assertContains(available, "ORD-SHARED-RIDER-1")
        self.assertContains(available, "Shared order")
        self.assertContains(available, "Nearby Opt-in Shop")

        accept_response = self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
        )
        self.assertRedirects(
            accept_response,
            f"{reverse('rider_dashboard')}?view=active#order-{self.shop_order.pk}",
            fetch_redirect_response=False,
        )
        self.shop_order.refresh_from_db()
        self.assertEqual(self.shop_order.assigned_rider_id, self.rider.pk)
        self.assertEqual(self.shop_order.status, "RIDER_ASSIGNED")

        active = self.client.get(reverse("rider_dashboard"), {"view": "active"})
        self.assertEqual(active.status_code, 200)
        self.assertContains(active, "ORD-SHARED-RIDER-1")
        self.client.force_login(self.shared_owner)
        pickup_response = self.client.post(
            reverse("rider_pickup_order", kwargs={"shop_order_id": self.shop_order.pk}),
            {
                "pickup_otp": seller_views._ensure_rider_pickup_otp(
                    self.shop_order,
                ),
            },
        )
        self.assertEqual(pickup_response.status_code, 302)
        self.client.force_login(self.rider_user)
        self.shop_order.refresh_from_db()
        self.assertEqual(self.shop_order.status, "OUT_FOR_DELIVERY")

        verify_response = self.client.post(
            reverse(
                "verify_customer_otp",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
            {"otp": ""},
        )
        self.assertEqual(verify_response.status_code, 302)
        self.shop_order.refresh_from_db()
        self.assertTrue(self.shop_order.customer_otp_verified)

        location_response = self.client.post(
            reverse(
                "update_rider_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
            {"latitude": "21.21", "longitude": "72.81"},
        )
        self.assertEqual(location_response.status_code, 200)
        self.shop_order.refresh_from_db()
        self.assertEqual(self.shop_order.rider_last_location["lat"], 21.21)

    def test_opt_out_and_out_of_radius_orders_are_hidden_and_direct_accept_is_denied(self):
        self.shared_shop.share_riders_with_nearby_shops = False
        self.shared_shop.save(update_fields=["share_riders_with_nearby_shops"])
        outside_shop = Shop.objects.create(
            name="Distant Opt-in Shop",
            owner=self.shared_owner,
            location={"lat": 21.3, "lng": 72.8},
            share_riders_with_nearby_shops=True,
        )
        outside_order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-OUTSIDE",
            user="shared_customer",
            delivery_address="Distant delivery address",
            delivery_location="21.31,72.81",
            delivery_contact_no="9876543210",
            status="READY",
            shop=outside_shop,
        )
        outside_shop_order = ShopOrder.objects.create(
            order=outside_order,
            shop=outside_shop,
            total_amount=Decimal("40.00"),
            status="READY",
        )

        available = self.client.get(reverse("rider_dashboard"))
        self.assertNotContains(available, "ORD-SHARED-RIDER-1")
        self.assertNotContains(available, "ORD-SHARED-RIDER-OUTSIDE")

        for shop_order in (self.shop_order, outside_shop_order):
            with self.subTest(order=shop_order.order.order_id):
                response = self.client.post(
                    reverse(
                        "accept_rider_order",
                        kwargs={"shop_order_id": shop_order.pk},
                    ),
                )
                self.assertEqual(response.status_code, 403)

    def test_missing_or_invalid_coordinates_cannot_expand_sharing_but_own_orders_remain(self):
        self.base_shop.location = {"lat": "invalid", "lng": 72.8}
        self.base_shop.save(update_fields=["location"])
        own_order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-OWN",
            user="shared_customer",
            delivery_address="Home-shop delivery",
            status="READY",
            shop=self.base_shop,
        )
        own_shop_order = ShopOrder.objects.create(
            order=own_order,
            shop=self.base_shop,
            total_amount=Decimal("30.00"),
            status="READY",
        )

        response = self.client.get(reverse("rider_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ORD-SHARED-RIDER-OWN")
        self.assertNotContains(response, "ORD-SHARED-RIDER-1")
        self.assertEqual(response.context["available_order_count"], 1)
        self.assertIsNone(seller_views._get_shop_coordinates(self.base_shop))
        self.assertIsNotNone(own_shop_order.pk)

    def test_exact_configured_radius_is_inclusive(self):
        import math

        base_location = {"lat": 0.0, "lng": 0.0}
        longitude_at_radius = math.degrees(0.35 / 6371)
        self.base_shop.location = base_location
        self.base_shop.save(update_fields=["location"])
        self.shared_shop.location = {"lat": 0.0, "lng": longitude_at_radius}
        self.shared_shop.save(update_fields=["location"])
        measured_distance = seller_views.distance_km_between_points(
            base_location["lat"],
            base_location["lng"],
            0.0,
            longitude_at_radius,
        )
        settings = DeliverySharingSettings.objects.get(pk=1)
        settings.shared_rider_radius_km = Decimal(str(measured_distance))
        settings.save(update_fields=["shared_rider_radius_km", "updated_at"])

        shared_shop_ids, _distances, radius = seller_views._nearby_shared_rider_shops(
            self.base_shop,
        )

        self.assertIn(self.shared_shop.pk, shared_shop_ids)
        self.assertAlmostEqual(radius, measured_distance)

    def test_shared_shop_order_is_offered_to_other_riders_after_one_minute(self):
        order = Order.objects.create(
            order_id="ORD-SHARED-RIDER-DELAY",
            user="shared_customer",
            delivery_address="Shared delivery address",
            status="READY",
            shop=self.shared_shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shared_shop,
            total_amount=Decimal("20.00"),
            status="READY",
        )

        before_delay = self.client.get(reverse("rider_dashboard"))
        self.assertNotContains(before_delay, "ORD-SHARED-RIDER-DELAY")
        self.assertGreater(
            before_delay.context["next_shared_order_available_in"],
            0,
        )
        early_accept = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertRedirects(
            early_accept,
            reverse("rider_dashboard"),
            fetch_redirect_response=False,
        )
        shop_order.refresh_from_db()
        self.assertIsNone(shop_order.assigned_rider_id)

        ShopOrder.objects.filter(pk=shop_order.pk).update(
            created_at=timezone.now() - timedelta(minutes=2),
        )
        after_delay = self.client.get(reverse("rider_dashboard"))
        self.assertContains(after_delay, "ORD-SHARED-RIDER-DELAY")
        accepted = self.client.post(
            reverse("accept_rider_order", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertRedirects(
            accepted,
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}",
            fetch_redirect_response=False,
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, self.rider.pk)
        self.assertIsNotNone(shop_order.rider_accepted_at)

    def test_shared_shop_radius_uses_the_riders_latest_accepted_pickup(self):
        self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
        )
        near_accepted_shop = Shop.objects.create(
            name="Near Latest Pickup",
            owner=self.shared_owner,
            location={"lat": 21.2035, "lng": 72.8},
            share_riders_with_nearby_shops=True,
        )
        near_home_shop = Shop.objects.create(
            name="Near Rider Home",
            owner=self.shared_owner,
            location={"lat": 21.1975, "lng": 72.8},
            share_riders_with_nearby_shops=True,
        )
        candidate_orders = []
        for order_id, shop in (
            ("ORD-NEAR-LAST-PICKUP", near_accepted_shop),
            ("ORD-NEAR-HOME", near_home_shop),
        ):
            order = Order.objects.create(
                order_id=order_id,
                user="shared_customer",
                delivery_address="Shared delivery address",
                status="READY",
                shop=shop,
            )
            candidate_orders.append(
                ShopOrder.objects.create(
                    order=order,
                    shop=shop,
                    total_amount=Decimal("20.00"),
                    status="READY",
                )
            )
        ShopOrder.objects.filter(
            pk__in=[candidate.pk for candidate in candidate_orders],
        ).update(created_at=timezone.now() - timedelta(minutes=2))

        available = self.client.get(reverse("rider_dashboard"))

        self.assertContains(available, "ORD-NEAR-LAST-PICKUP")
        self.assertNotContains(available, "ORD-NEAR-HOME")

    def test_multi_shop_order_uses_nearby_common_riders_and_splits_farther_shops(self):
        sibling_orders = []
        sibling_locations = (
            ("Nearby Shop One", 21.2012),
            ("Nearby Shop Two", 21.2008),
            ("Far Shop One", 21.2070),
            ("Far Shop Two", 21.2071),
        )
        for index, (shop_name, latitude) in enumerate(sibling_locations, start=2):
            shop = Shop.objects.create(
                name=shop_name,
                owner=self.shared_owner,
                location={"lat": latitude, "lng": 72.8},
                share_riders_with_nearby_shops=True,
            )
            sibling_orders.append(
                ShopOrder.objects.create(
                    order=self.order,
                    shop=shop,
                    total_amount=Decimal("20.00"),
                    status="READY",
                )
            )
        ShopOrder.objects.filter(pk__in=[item.pk for item in sibling_orders]).update(
            created_at=timezone.now() - timedelta(minutes=2),
        )

        first_rider_accept = self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
        )
        self.assertRedirects(
            first_rider_accept,
            f"{reverse('rider_dashboard')}?view=active#order-{self.shop_order.pk}",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ShopOrder.objects.filter(
                order=self.order,
                assigned_rider=self.rider,
            ).count(),
            3,
        )
        for sibling in sibling_orders[:2]:
            sibling.refresh_from_db()
            self.assertEqual(sibling.assigned_rider_id, self.rider.pk)
        for sibling in sibling_orders[2:]:
            sibling.refresh_from_db()
            self.assertIsNone(sibling.assigned_rider_id)

        second_owner = User.objects.create_user(username="shared_rider_second_owner")
        second_rider_user = User.objects.create_user(username="shared_rider_second_user")
        second_shop = Shop.objects.create(
            name="Far Rider Home",
            owner=second_owner,
            location={"lat": 21.207, "lng": 72.8},
        )
        second_rider = ShopStaff.objects.create(
            shop=second_shop,
            user=second_rider_user,
            role="RIDER",
            is_active=True,
        )
        self.client.force_login(second_rider_user)
        self.assertContains(
            self.client.get(reverse("rider_dashboard")),
            sibling_orders[2].order.order_id,
        )
        second_rider_accept = self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": sibling_orders[2].pk},
            ),
        )
        self.assertRedirects(
            second_rider_accept,
            f"{reverse('rider_dashboard')}?view=active#order-{sibling_orders[2].pk}",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ShopOrder.objects.filter(
                order=self.order,
                assigned_rider=second_rider,
            ).count(),
            2,
        )

    def test_shared_rider_fee_moves_configured_distance_pay_between_shop_wallets_once(self):
        sharing_settings = DeliverySharingSettings.objects.get(pk=1)
        sharing_settings.shared_rider_rate_per_km = Decimal("10.00")
        sharing_settings.shared_rider_base_pay = Decimal("10.00")
        sharing_settings.save()
        self.order.delivery_location = "21.231,72.8"
        self.order.save(update_fields=["delivery_location"])
        self.shop_order.assigned_rider = self.rider
        self.shop_order.status = "DELIVERED"
        self.shop_order.delivery_completed_at = timezone.now()
        self.shop_order.save(update_fields=[
            "assigned_rider",
            "status",
            "delivery_completed_at",
        ])
        paying_wallet = ShopWallet.objects.create(
            shop=self.shared_shop,
            balance=Decimal("50.00"),
        )
        home_wallet = ShopWallet.objects.create(
            shop=self.base_shop,
            balance=Decimal("20.00"),
        )

        payment = seller_views._record_shared_rider_delivery_payment(
            self.shop_order,
        )

        self.assertIsNotNone(payment)
        expected_distance = seller_views.distance_km_between_points(
            21.201,
            72.8,
            21.231,
            72.8,
        )
        self.assertEqual(
            payment.delivery_distance_km,
            Decimal(str(expected_distance)).quantize(Decimal("0.001")),
        )
        expected_amount = max(
            Decimal("20.00"),
            (
                payment.delivery_distance_km * payment.rate_per_km
                + sharing_settings.shared_rider_base_pay
            ).quantize(Decimal("0.01")),
        )
        self.assertGreater(expected_amount, Decimal("20.00"))
        self.assertEqual(payment.amount, expected_amount)
        paying_wallet.refresh_from_db()
        home_wallet.refresh_from_db()
        self.assertEqual(paying_wallet.balance, Decimal("50.00") - expected_amount)
        self.assertEqual(home_wallet.balance, Decimal("20.00") + expected_amount)
        self.assertEqual(
            seller_views._record_shared_rider_delivery_payment(self.shop_order).pk,
            payment.pk,
        )
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=self.shop_order,
                transaction_type="RIDER_FEE",
            ).count(),
            1,
        )
        self.assertEqual(
            ShopWalletTransaction.objects.filter(
                shop_order=self.shop_order,
                transaction_type="RIDER_INCOME",
            ).count(),
            1,
        )

        self.client.force_login(self.shared_owner)
        report_response = self.client.get(
            reverse("shop_detail", kwargs={"pk": self.shared_shop.pk}),
        )
        self.assertEqual(report_response.status_code, 200)
        report_row = report_response.context["shared_rider_payment_report"][0]
        self.assertEqual(report_row["rider"].pk, self.rider.pk)
        self.assertEqual(report_row["paid_today"], expected_amount)
        self.assertEqual(report_row["paid_7_days"], expected_amount)
        self.assertEqual(report_row["paid_30_days"], expected_amount)
        self.client.force_login(self.owner)
        home_report = self.client.get(
            reverse("shop_detail", kwargs={"pk": self.base_shop.pk}),
        )
        home_row = home_report.context["shared_rider_payment_report"][0]
        self.assertEqual(home_row["received_30_days"], expected_amount)

    def test_group_member_can_exit_and_group_owner_can_remove_any_shop(self):
        self.base_shop.allow_owner_create_shared_rider_groups = True
        self.base_shop.save(update_fields=["allow_owner_create_shared_rider_groups"])
        self.client.force_login(self.owner)
        self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "create",
                "name": "Flexible membership group",
                "created_from_shop": str(self.base_shop.pk),
            },
        )
        group = SharedRiderGroup.objects.get(name="Flexible membership group")
        exiting_shop = Shop.objects.create(
            name="Exiting shop",
            owner=self.shared_owner,
        )
        removable_shop = Shop.objects.create(
            name="Removable shop",
            owner=self.shared_owner,
        )
        memberships = [
            SharedRiderGroupShop.objects.create(
                group=group,
                shop=shop,
                status=SharedRiderGroupShop.STATUS_APPROVED,
                requested_by=self.owner,
                reviewed_by=self.owner,
                reviewed_at=timezone.now(),
            )
            for shop in (exiting_shop, removable_shop)
        ]

        self.client.force_login(self.shared_owner)
        exit_response = self.client.post(
            reverse(
                "exit_shared_rider_group",
                kwargs={"membership_id": memberships[0].pk},
            ),
        )
        self.assertRedirects(exit_response, reverse("dashboard"))
        self.assertFalse(
            SharedRiderGroupShop.objects.filter(pk=memberships[0].pk).exists(),
        )

        self.client.force_login(self.owner)
        remove_response = self.client.post(
            reverse("shared_rider_groups"),
            {
                "action": "remove-shop",
                "membership_id": memberships[1].pk,
            },
        )
        self.assertRedirects(remove_response, reverse("shared_rider_groups"))
        self.assertFalse(
            SharedRiderGroupShop.objects.filter(pk=memberships[1].pk).exists(),
        )

    def test_shared_rider_pay_is_not_recorded_without_delivery_coordinates(self):
        self.shared_shop.location = {}
        self.shared_shop.save(update_fields=["location"])
        self.order.delivery_location = ""
        self.order.save(update_fields=["delivery_location"])
        self.shop_order.assigned_rider = self.rider
        self.shop_order.status = "DELIVERED"
        self.shop_order.delivery_completed_at = timezone.now()
        self.shop_order.save(update_fields=[
            "assigned_rider",
            "status",
            "delivery_completed_at",
        ])

        with self.assertLogs("seller.views", level="ERROR") as captured:
            payment = seller_views._record_shared_rider_delivery_payment(
                self.shop_order,
            )

        self.assertIsNone(payment)
        self.assertIn("Cannot calculate shared-rider pay", captured.output[0])
        self.assertFalse(
            SharedRiderDeliveryPayment.objects.filter(
                shop_order=self.shop_order,
            ).exists()
        )

    def test_assigned_rider_can_correct_completed_order_address_and_coordinates(self):
        self.order.delivery_location = ""
        self.order.save(update_fields=["delivery_location"])
        self.shop_order.assigned_rider = self.rider
        self.shop_order.status = "DELIVERED"
        self.shop_order.delivery_completed_at = timezone.now()
        self.shop_order.save(update_fields=[
            "assigned_rider",
            "status",
            "delivery_completed_at",
        ])
        old_address = self.order.delivery_address
        response = self.client.post(
            reverse(
                "edit_completed_delivery_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
            {
                "delivery_address": "Corrected address after delivery",
                "latitude": "21.235",
                "longitude": "72.835",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('rider_dashboard')}?view=completed#order-{self.shop_order.pk}",
            fetch_redirect_response=False,
        )
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.delivery_address,
            "Corrected address after delivery",
        )
        self.assertEqual(self.order.delivery_location, "21.2350000, 72.8350000")
        history = OrderStatusHistory.objects.filter(
            shop_order=self.shop_order,
            status="DELIVERED",
        ).latest("created_at")
        self.assertNotIn(old_address, history.remark)
        self.assertIn("corrected the delivery location", history.remark)
        self.assertTrue(
            SharedRiderDeliveryPayment.objects.filter(
                shop_order=self.shop_order,
            ).exists(),
        )
        self.assertNotEqual(self.order.delivery_address, old_address)

    def test_rider_cannot_edit_another_riders_completed_delivery_location(self):
        other_rider_user = User.objects.create_user(
            username="shared_rider_other_location_editor",
            password="test-pass",
        )
        ShopStaff.objects.create(
            shop=self.base_shop,
            user=other_rider_user,
            role="RIDER",
            is_active=True,
        )
        self.shop_order.assigned_rider = self.rider
        self.shop_order.status = "DELIVERED"
        self.shop_order.delivery_completed_at = timezone.now()
        self.shop_order.save(update_fields=[
            "assigned_rider",
            "status",
            "delivery_completed_at",
        ])
        self.client.force_login(other_rider_user)

        response = self.client.post(
            reverse(
                "edit_completed_delivery_location",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
            {
                "delivery_address": "Unauthorized correction",
                "latitude": "21.235",
                "longitude": "72.835",
            },
        )

        self.assertEqual(response.status_code, 404)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.delivery_address,
            "Shared delivery address",
        )

    def test_order_grouping_includes_shops_forward_within_customer_direction_radius(self):
        sharing_settings = DeliverySharingSettings.objects.get(pk=1)
        sharing_settings.shared_rider_radius_km = Decimal("0.35")
        sharing_settings.customer_direction_radius_km = Decimal("0.65")
        sharing_settings.save()
        forward_shop = Shop.objects.create(
            name="Forward Customer Shop",
            owner=self.shared_owner,
            location={"lat": 21.205, "lng": 72.804},
            share_riders_with_nearby_shops=True,
        )
        forward_shop_order = ShopOrder.objects.create(
            order=self.order,
            shop=forward_shop,
            total_amount=Decimal("30.00"),
            status="READY",
        )
        ShopOrder.objects.filter(pk__in=[
            self.shop_order.pk,
            forward_shop_order.pk,
        ]).update(created_at=timezone.now() - timedelta(minutes=2))

        accepted = self.client.post(
            reverse(
                "accept_rider_order",
                kwargs={"shop_order_id": self.shop_order.pk},
            ),
        )

        self.assertEqual(accepted.status_code, 302)
        forward_shop_order.refresh_from_db()
        self.assertEqual(forward_shop_order.assigned_rider_id, self.rider.pk)

    def test_rider_efficiency_and_salary_suggestion_use_completed_work(self):
        sharing_settings = DeliverySharingSettings.objects.get(pk=1)
        sharing_settings.rider_monthly_base_salary = Decimal("12000.00")
        sharing_settings.rider_completed_order_bonus = Decimal("25.00")
        sharing_settings.save()
        now = timezone.now()
        completed_order = Order.objects.create(
            order_id="ORD-RIDER-EFFICIENCY",
            user="shared_customer",
            delivery_address="Performance test address",
            delivery_location="21.22,72.82",
            status="DELIVERED",
            shop=self.base_shop,
        )
        completed_shop_order = ShopOrder.objects.create(
            order=completed_order,
            shop=self.base_shop,
            assigned_rider=self.rider,
            total_amount=Decimal("100.00"),
            status="DELIVERED",
            rider_accepted_at=now - timedelta(hours=1),
            delivery_completed_at=now,
        )
        OrderItem.objects.create(
            shop_order=completed_shop_order,
            product_name="Efficiency item",
            quantity=4,
            price=Decimal("25.00"),
            total=Decimal("100.00"),
        )

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "completed"},
        )

        self.assertEqual(response.status_code, 200)
        metrics = response.context["efficiency"]
        self.assertEqual(metrics["completed_order_count"], 1)
        self.assertEqual(metrics["total_order_value"], Decimal("100.00"))
        self.assertEqual(metrics["total_quantity"], 4)
        self.assertEqual(metrics["average_minutes"], Decimal("60"))
        self.assertEqual(metrics["rank"], 1)
        self.assertEqual(metrics["salary_suggestion"], Decimal("12025.00"))
        self.assertContains(response, "Suggested monthly salary")
        self.assertContains(response, "Correct delivery address or pin")

    def test_packer_dashboard_shows_completion_rate_and_salary_suggestion(self):
        sharing_settings = DeliverySharingSettings.objects.get(pk=1)
        sharing_settings.packer_monthly_base_salary = Decimal("15000.00")
        sharing_settings.packer_completed_order_bonus = Decimal("15.00")
        sharing_settings.save()
        packer_user = User.objects.create_user(username="shared_rider_packer_user")
        packer = ShopStaff.objects.create(
            shop=self.base_shop,
            user=packer_user,
            role="PACKER",
            is_active=True,
        )
        now = timezone.now()
        order = Order.objects.create(
            order_id="ORD-PACKER-EFFICIENCY",
            user="shared_customer",
            status="READY",
            shop=self.base_shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.base_shop,
            assigned_packer=packer,
            total_amount=Decimal("200.00"),
            status="READY",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product_name="Packer efficiency item",
            quantity=8,
            price=Decimal("25.00"),
            total=Decimal("200.00"),
        )
        packing_started = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="PACKING",
        )
        ready_event = OrderStatusHistory.objects.create(
            shop_order=shop_order,
            status="READY",
        )
        OrderStatusHistory.objects.filter(pk=packing_started.pk).update(
            created_at=now - timedelta(minutes=90),
        )
        OrderStatusHistory.objects.filter(pk=ready_event.pk).update(
            created_at=now - timedelta(minutes=30),
        )
        self.client.force_login(packer_user)

        response = self.client.get(reverse("packer_dashboard"))

        self.assertEqual(response.status_code, 200)
        metrics = response.context["efficiency"]
        self.assertEqual(metrics["completed_order_count"], 1)
        self.assertEqual(metrics["total_order_value"], Decimal("200.00"))
        self.assertEqual(metrics["total_quantity"], 8)
        self.assertEqual(metrics["average_minutes"], Decimal("60"))
        self.assertEqual(metrics["rank"], 1)
        self.assertEqual(metrics["salary_suggestion"], Decimal("15015.00"))
        self.assertContains(response, "Suggested monthly salary")


class AutoGeneratedProductSearchKeywordsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="keyword_generation_owner",
            password="test-password",
        )

    def test_product_keywords_are_generated_from_listing_fields(self):
        product = Product.objects.create(
            created_by=self.owner,
            product_category="GROCERY",
            product_name="Chana",
            product_alias_name="Desi chickpea",
            product_title="Organic Bengal gram",
            product_description="Premium pulses",
            sku="CHANA-1",
            unit="1 kg",
            price=Decimal("75.00"),
        )

        self.assertIn("Chana", product.search_keywords)
        self.assertIn("Desi chickpea", product.search_keywords)
        self.assertIn("Premium pulses", product.search_keywords)
        self.assertIn("GROCERY", product.search_keywords)

    def test_keywords_regenerate_when_listing_data_is_saved_with_update_fields(self):
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Old Listing Name",
            price=Decimal("5.00"),
        )
        product.product_name = "New Listing Name"
        product.product_description = "Fresh listing details"
        product.save(update_fields=["product_name", "product_description"])
        product.refresh_from_db()

        self.assertNotIn("Old Listing Name", product.search_keywords)
        self.assertIn("New Listing Name", product.search_keywords)
        self.assertIn("Fresh listing details", product.search_keywords)
        self.assertIn("English (Latin)", product.search_keywords)
        self.assertIn("\n\nNew Listing Name", product.search_keywords)

    @patch("seller.offline_keyword_dictionary.lookup_product_keywords")
    def test_manual_keywords_are_preserved_until_listing_source_changes(self, translate):
        from .offline_keyword_dictionary import ProductKeywordMatches

        translate.return_value = ProductKeywordMatches([], False)
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Original product",
            price=Decimal("5.00"),
        )
        translate.assert_called_once()

        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from .admin import ProductAdmin

        superuser = User.objects.create_superuser(
            username="keyword_editor",
            email="keyword_editor@example.com",
            password="temporary-test-password",
        )
        request = RequestFactory().post("/admin/seller/product/")
        request.user = superuser
        product.product_name = "Admin updated listing"
        product.search_keywords = "Admin curated English आम કેરી"
        product_admin = ProductAdmin(Product, AdminSite())
        product_admin.save_model(
            request,
            product,
            Mock(changed_data=["product_name", "search_keywords"]),
            change=True,
        )
        self.assertEqual(translate.call_count, 1)

        product.refresh_from_db()
        self.assertEqual(product.search_keywords, "Admin curated English आम કેરી")
        self.assertEqual(product.product_name, "Admin updated listing")

        product.price = Decimal("6.00")
        product.save(update_fields=["price"])
        product.refresh_from_db()
        self.assertEqual(product.search_keywords, "Admin curated English आम કેરી")
        self.assertEqual(translate.call_count, 1)

        product.product_title = "Changed listing source"
        product.save(update_fields=["product_title"])
        product.refresh_from_db()
        self.assertIn("Admin updated listing", product.search_keywords)
        self.assertIn("Changed listing source", product.search_keywords)
        self.assertEqual(translate.call_count, 2)

    def test_product_admin_keyword_field_is_editable_only_for_superusers(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from .admin import ProductAdmin

        product_admin = ProductAdmin(Product, AdminSite())
        request = RequestFactory().get("/admin/seller/product/")
        request.user = self.owner
        self.assertIn("search_keywords", product_admin.get_readonly_fields(request))

        superuser = User.objects.create_superuser(
            username="keyword_admin",
            email="keyword_admin@example.com",
            password="temporary-test-password",
        )
        request.user = superuser
        self.assertNotIn("search_keywords", product_admin.get_readonly_fields(request))

    def test_dictionary_lookup_transliterates_when_dictionary_is_not_installed(self):
        from .offline_keyword_dictionary import lookup_product_keywords
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = str(Path(temp_dir) / "missing.sqlite3")
            with override_settings(PRODUCT_KEYWORD_DICTIONARY_PATH=missing_path):
                with patch(
                    "seller.offline_keyword_dictionary._transliterate_token",
                    side_effect=lambda source, target, word: f"{target}:{word}",
                ):
                    matches = lookup_product_keywords("Mango")
                self.assertIn("Devanagari:mango", matches.keywords)
                self.assertFalse(matches.dictionary_matched)

    def test_search_keywords_are_grouped_into_language_labeled_paragraphs(self):
        from .offline_keyword_dictionary import format_search_keywords_by_language

        formatted = format_search_keywords_by_language("Mango કેરી आम Mango")

        self.assertEqual(
            formatted,
            "English (Latin)\n\nMango\n\n"
            "Gujarati\n\nકેરી\n\n"
            "Hindi / Marathi / Nepali / Sanskrit (Devanagari)\n\nआम",
        )

    def test_product_looks_up_local_dictionary_matches_and_reverse_matches(self):
        import sqlite3
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            dictionary_file = Path(temp_dir) / "products.sqlite3"
            connection = sqlite3.connect(dictionary_file)
            try:
                connection.execute(
                    """
                    CREATE TABLE dictionary (
                        source_key TEXT NOT NULL,
                        source_word TEXT NOT NULL,
                        target_language TEXT NOT NULL,
                        target_key TEXT NOT NULL,
                        target_word TEXT NOT NULL,
                        PRIMARY KEY (source_key, target_language, target_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX dictionary_target_key_idx ON dictionary (target_key)"
                )
                connection.executemany(
                    "INSERT INTO dictionary VALUES (?, ?, ?, ?, ?)",
                    [
                        ("mango", "Mango", "hi", "आम", "आम"),
                        ("mango", "Mango", "gu", "કેરી", "કેરી"),
                        ("mango", "Mango", "bn", "আম", "আম"),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            with override_settings(
                PRODUCT_KEYWORD_DICTIONARY_PATH=str(dictionary_file),
            ):
                with patch(
                    "seller.offline_keyword_dictionary._transliterate_token",
                    side_effect=lambda source, target, word: f"{target}:{word}",
                ):
                    product = Product.objects.create(
                        created_by=self.owner,
                        product_name="Mango",
                        price=Decimal("5.00"),
                    )
                    hindi_product = Product.objects.create(
                        created_by=self.owner,
                        product_name="आम",
                        price=Decimal("5.00"),
                    )
                self.assertTrue(product.search_keywords_translated)
                self.assertIn("Gujarati\n\nકેરી", product.search_keywords)
                self.assertIn(
                    "Hindi / Marathi / Nepali / Sanskrit (Devanagari)\n\nआम",
                    product.search_keywords,
                )
                self.assertIn("आम", product.search_keywords)
                self.assertIn("કેરી", product.search_keywords)
                self.assertIn("আম", product.search_keywords)
                compound_product = Product.objects.create(
                    created_by=self.owner,
                    product_name="mango_yello",
                    price=Decimal("5.00"),
                )
                self.assertIn("કેરી", compound_product.search_keywords)
                self.assertIn("Mango", hindi_product.search_keywords)
                self.assertIn("કેરી", hindi_product.search_keywords)
                self.assertIn("আম", hindi_product.search_keywords)

    def test_dictionary_builder_indexes_supported_translations_and_ignores_bad_shapes(self):
        import gzip
        import json
        import sqlite3
        from pathlib import Path
        from seller.management.commands.build_product_dictionary import (
            _build_database,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.jsonl.gz"
            output_path = Path(temp_dir) / "dictionary.sqlite3"
            entries = [
                {
                    "lang_code": "en",
                    "word": "mango",
                    "translations": [
                        {"lang_code": "hi", "word": "आम"},
                        {"lang_code": "gu", "word": "કેરી"},
                        {"lang_code": "fr", "word": "mangue"},
                    ],
                },
                {"lang_code": "en", "word": "durian", "translations": None},
                {"lang_code": "fr", "word": "pomme", "translations": []},
            ]
            with gzip.open(source_path, "wt", encoding="utf-8") as source:
                source.write(
                    "\n".join(
                        json.dumps(entry, ensure_ascii=False)
                        for entry in entries
                    )
                )

            counts = _build_database(source_path, output_path)

            self.assertEqual(counts["hi"], 1)
            self.assertEqual(counts["gu"], 1)
            connection = sqlite3.connect(output_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT source_key, target_word FROM dictionary "
                        "WHERE target_language = 'hi'"
                    ).fetchone(),
                    ("mango", "आम"),
                )
            finally:
                connection.close()

    def test_local_script_transliteration_covers_all_configured_languages(self):
        from .offline_keyword_dictionary import (
            LANGUAGE_SCRIPTS,
            _transliterate_token,
        )

        self.assertEqual(len(LANGUAGE_SCRIPTS), 22)
        for target_script in set(LANGUAGE_SCRIPTS.values()):
            with self.subTest(target_script=target_script):
                alias = _transliterate_token(
                    "RomanReadable",
                    target_script,
                    "mango",
                )
                self.assertTrue(alias)
                self.assertNotEqual(alias.casefold(), "mango")

    def test_dictionary_downloader_resumes_existing_partial_file(self):
        from pathlib import Path
        from seller.management.commands.build_product_dictionary import (
            _download_dictionary,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            dictionary_path = Path(temp_dir) / "dictionary.jsonl"
            partial_path = dictionary_path.with_suffix(".jsonl.part")
            partial_path.write_bytes(b"before")
            response = Mock()
            response.status = 206
            response.headers = {
                "Content-Range": "bytes 6-10/11",
                "Content-Length": "5",
            }
            response.read.side_effect = [b"after", b""]
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)

            with patch(
                "seller.management.commands.build_product_dictionary."
                "urllib.request.urlopen",
                return_value=response,
            ) as urlopen:
                _download_dictionary(dictionary_path, Mock())

            request = urlopen.call_args.args[0]
            self.assertEqual(request.get_header("Range"), "bytes=6-")
            self.assertEqual(dictionary_path.read_bytes(), b"beforeafter")
            self.assertFalse(partial_path.exists())


class MarketplacePurchaseWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="workflow_shop_owner",
            password="test-password",
        )
        self.customer = User.objects.create_user(
            username="workflow_customer",
            password="test-password",
        )
        self.shop = Shop.objects.create(
            name="Workflow Shop",
            owner=self.owner,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Workflow Product",
            price=Decimal("80.00"),
            unit="piece",
            product_category="OTHER",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            price=Decimal("80.00"),
            stock=8,
            b2b_stock=3,
            is_active=True,
        )
        self.profile = ClientProfile.objects.create(
            user=self.customer,
            name="Workflow Customer",
            contact_no="9876543210",
        )
        self.home = ClientDeliveryAddress.objects.create(
            client=self.profile,
            label="Home",
            address="Home Road",
            pincode="100001",
            location={"lat": 1, "lng": 2},
            is_default=True,
            is_active=True,
        )
        self.office = ClientDeliveryAddress.objects.create(
            client=self.profile,
            label="Office",
            address="Office Road",
            pincode="100002",
            location={"lat": 3, "lng": 4},
            is_active=True,
        )
        self.client.force_login(self.customer)

    def test_marketplace_can_hide_products_requiring_scheduled_delivery(self):
        scheduled_shop = Shop.objects.create(
            name="Scheduled Delivery Shop",
            owner=self.owner,
            is_shop_open=False,
            allow_scheduled_delivery=True,
        )
        scheduled_product = Product.objects.create(
            created_by=self.owner,
            product_name="Scheduled Delivery Product",
            price=Decimal("25.00"),
            unit="piece",
            product_category="OTHER",
        )
        ProductShop.objects.create(
            shop=scheduled_shop,
            product=scheduled_product,
            price=Decimal("25.00"),
            stock=4,
            is_active=True,
        )

        default_response = self.client.get(reverse("marketplace"))
        self.assertEqual(default_response.status_code, 200)
        self.assertContains(default_response, "Scheduled Delivery Product")
        self.assertFalse(default_response.context["hide_scheduled_delivery"])
        self.assertContains(
            default_response,
            'id="hideScheduledDelivery"',
        )

        filtered_response = self.client.get(
            reverse("marketplace"),
            {"hide_scheduled": "1"},
        )
        self.assertEqual(filtered_response.status_code, 200)
        self.assertNotContains(filtered_response, "Scheduled Delivery Product")
        self.assertTrue(filtered_response.context["hide_scheduled_delivery"])
        self.assertContains(
            filtered_response,
            'name="hide_scheduled"\n            value="1"\n            checked',
        )

    def test_checkout_saves_separate_shop_and_rider_instructions(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 1}
        session.save()

        checkout_page = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(
            checkout_page,
            f'name="order_instruction_text_shop_{self.shop.pk}"',
        )
        self.assertContains(
            checkout_page,
            f'name="order_instruction_audio_shop_{self.shop.pk}"',
        )
        self.assertContains(checkout_page, 'name="shared_rider_instruction_text"')
        self.assertContains(checkout_page, "One instruction for all delivery riders")
        self.assertContains(checkout_page, "Shop instruction records")
        self.assertContains(checkout_page, "id=\"checkoutInstructionShop\"")
        self.assertNotContains(
            checkout_page,
            f'name="order_instruction_text_rider_{self.shop.pk}"',
        )
        self.assertContains(checkout_page, "Record shop instruction")
        self.assertContains(checkout_page, "data-shop-instruction-record-list")
        self.assertContains(
            checkout_page,
            "order_instruction_text_rider_${shopId}",
        )

        response = self.client.post(
            reverse("checkout"),
            {
                "address": self.home.pk,
                "payment_method": "COD",
                "order_instruction_shop": self.shop.pk,
                "order_instruction_text_shop": "Pack the vegetables separately.",
                "order_instruction_text_rider": "Call me at the gate.",
                "order_instruction_audio_shop": SimpleUploadedFile(
                    "shop-instruction.webm",
                    b"shop audio data",
                    content_type="audio/webm",
                ),
                "order_instruction_audio_rider": SimpleUploadedFile(
                    "rider-instruction.webm",
                    b"rider audio data",
                    content_type="audio/webm",
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        shop_order = ShopOrder.objects.get(order__user=self.customer.username)
        instructions = {
            instruction.recipient: instruction
            for instruction in OrderVoiceInstruction.objects.filter(
                shop_order=shop_order,
            )
        }
        self.assertEqual(
            instructions[OrderVoiceInstruction.RECIPIENT_SHOP].instruction_text,
            "Pack the vegetables separately.",
        )
        self.assertEqual(
            instructions[OrderVoiceInstruction.RECIPIENT_RIDER].instruction_text,
            "Call me at the gate.",
        )
        self.assertEqual(
            instructions[OrderVoiceInstruction.RECIPIENT_SHOP].audio_content_type,
            "audio/webm",
        )
        self.assertEqual(
            instructions[OrderVoiceInstruction.RECIPIENT_RIDER].audio_content_type,
            "audio/webm",
        )
        for instruction in instructions.values():
            self.addCleanup(
                instruction.audio_file.storage.delete,
                instruction.audio_file.name,
            )

        self.client.force_login(self.owner)
        shop_detail = self.client.get(
            reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": shop_order.pk},
            )
        )
        self.assertEqual(shop_detail.status_code, 200)
        self.assertContains(shop_detail, "Pack the vegetables separately.")
        self.assertNotContains(shop_detail, "Call me at the gate.")

    def test_one_rider_instruction_is_attached_to_each_shop_delivery(self):
        second_shop = Shop.objects.create(
            name="Workflow Second Shop",
            owner=self.owner,
        )
        second_product = Product.objects.create(
            created_by=self.owner,
            product_name="Second Workflow Product",
            price=Decimal("20.00"),
            unit="piece",
        )
        second_assignment = ProductShop.objects.create(
            shop=second_shop,
            product=second_product,
            price=Decimal("20.00"),
            stock=4,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {
            str(self.assignment.pk): 1,
            str(second_assignment.pk): 1,
        }
        session.save()

        checkout_page = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(checkout_page, "One instruction for all delivery riders")
        self.assertContains(checkout_page, "Workflow Second Shop")
        self.assertContains(
            checkout_page,
            "order_instruction_text_rider_${shopId}",
        )

        response = self.client.post(
            reverse("checkout"),
            {
                "address": self.home.pk,
                "payment_method": "COD",
                f"order_instruction_text_rider_{self.shop.pk}": "Call when arriving.",
                f"order_instruction_text_rider_{second_shop.pk}": "Call when arriving.",
            },
        )

        self.assertEqual(response.status_code, 302)
        rider_instructions = OrderVoiceInstruction.objects.filter(
            shop_order__order__user=self.customer.username,
            recipient=OrderVoiceInstruction.RECIPIENT_RIDER,
        ).select_related("shop_order")
        self.assertEqual(rider_instructions.count(), 2)
        self.assertEqual(
            {instruction.instruction_text for instruction in rider_instructions},
            {"Call when arriving."},
        )
        self.assertEqual(
            {instruction.shop_order.shop_id for instruction in rider_instructions},
            {self.shop.pk, second_shop.pk},
        )

    def test_checkout_prompts_for_location_or_address_when_none_are_saved(self):
        self.home.delete()
        self.office.delete()
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 1}
        session.pop("carts_by_delivery_address", None)
        session.pop("cart_delivery_address_id", None)
        session.save()

        response = self.client.get(reverse("checkout"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="noDeliveryAddressModal"')
        self.assertContains(response, "Use my current location")
        self.assertContains(response, "Add delivery address")
        self.assertContains(response, "?next=checkout")

    def test_current_location_becomes_default_address_and_keeps_cart(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.pop("carts_by_delivery_address", None)
        session.pop("cart_delivery_address_id", None)
        session.save()

        response = self.client.post(
            reverse("delivery_address_use_current_location"),
            {
                "latitude": "23.0225",
                "longitude": "72.5714",
                "state_name": "Gujarat",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        current_address = ClientDeliveryAddress.objects.get(
            client=self.profile,
            label="Current location",
        )
        self.assertTrue(current_address.is_default)
        self.assertEqual(current_address.state_code, "24")
        self.assertFalse(
            ClientDeliveryAddress.objects.filter(
                client=self.profile,
                is_default=True,
            ).exclude(pk=current_address.pk).exists(),
        )
        self.assertEqual(
            self.client.session["cart"],
            {str(self.assignment.pk): 2},
        )
        self.assertEqual(
            self.client.session["cart_delivery_address_id"],
            str(current_address.pk),
        )
        checkout_page = self.client.get(reverse("checkout"))
        self.assertContains(checkout_page, "State: Gujarat")

        self.shop.gstin = "24ABCDE1234F1Z5"
        self.shop.gst_state_code = "24"
        self.shop.save(update_fields=["gstin", "gst_state_code"])
        checkout_response = self.client.post(
            reverse("checkout"),
            {"address": current_address.pk, "payment_method": "COD"},
        )
        self.assertEqual(checkout_response.status_code, 302)
        shop_order = ShopOrder.objects.get(order__user=self.customer.username)
        self.assertEqual(shop_order.buyer_state_code, "24")

        unresolved_state_response = self.client.post(
            reverse("delivery_address_use_current_location"),
            {
                "latitude": "23.0225",
                "longitude": "72.5714",
                "state_name": "Unknown state",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(unresolved_state_response.status_code, 400)

        invalid_response = self.client.post(
            reverse("delivery_address_use_current_location"),
            {
                "latitude": "91",
                "longitude": "0",
                "state_name": "Gujarat",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(invalid_response.status_code, 400)

    def test_new_delivery_address_becomes_default_and_can_return_to_checkout(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.save()

        response = self.client.post(
            f"{reverse('delivery_address_add')}?next=checkout",
            {
                "label": "New Home",
                "address": "New Home Road",
                "city": "Ahmedabad",
                "state_code": "24",
                "pincode": "380001",
                "latitude": "23.0225000",
                "longitude": "72.5714000",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("checkout"))
        new_address = ClientDeliveryAddress.objects.get(
            client=self.profile,
            label="New Home",
        )
        self.assertTrue(new_address.is_default)
        self.home.refresh_from_db()
        self.assertFalse(self.home.is_default)
        self.office.refresh_from_db()
        self.assertFalse(self.office.is_default)
        self.assertEqual(
            self.client.session["cart"],
            {str(self.assignment.pk): 2},
        )
        self.assertEqual(
            self.client.session["cart_delivery_address_id"],
            str(new_address.pk),
        )

    def test_shop_orders_lists_checkout_attempts_after_configured_inactivity(self):
        self.shop.allow_product_image_overrides = True
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.assignment.image_url_override = "https://example.com/workflow-product.jpg"
        self.assignment.save(update_fields=["image_url_override"])
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.save()
        checkout_response = self.client.get(reverse("checkout"))

        self.assertEqual(checkout_response.status_code, 200)
        attempt = CheckoutAttempt.objects.get(
            customer=self.customer,
            shop=self.shop,
        )
        self.assertEqual(attempt.total_amount, Decimal("160.00"))
        self.assertEqual(attempt.cart_items[0]["product_name"], self.product.product_name)
        self.assertEqual(
            attempt.cart_items[0]["image_url"],
            "https://example.com/workflow-product.jpg",
        )
        self.assertEqual(
            attempt.cart_items[0]["image_urls"],
            ["https://example.com/workflow-product.jpg"],
        )
        legacy_item = {
            key: value
            for key, value in attempt.cart_items[0].items()
            if key not in {"image_url", "image_urls", "product_shop_id"}
        }
        attempt.cart_items = [legacy_item]
        attempt.save(update_fields=["cart_items"])

        self.client.force_login(self.owner)
        recent_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(recent_response.context["abandoned_checkout_count"], 0)

        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            last_activity_at=timezone.now() - timedelta(minutes=2),
        )
        still_active_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(still_active_response.context["abandoned_checkout_count"], 0)

        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            last_activity_at=timezone.now() - timedelta(minutes=4),
        )
        abandoned_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(abandoned_response.status_code, 200)
        self.assertEqual(abandoned_response.context["abandoned_checkout_count"], 1)
        self.assertContains(abandoned_response, "Checkout started, order not placed")
        self.assertContains(abandoned_response, "inactive for at least 3 minutes")
        self.assertContains(abandoned_response, self.product.product_name)
        self.assertContains(abandoned_response, "https://example.com/workflow-product.jpg")
        self.assertContains(abandoned_response, 'alt="Workflow Product"')
        self.assertContains(abandoned_response, 'class="abandoned-checkout-row border rounded mb-2"')
        self.assertContains(abandoned_response, "View 1 item")
        self.assertNotContains(abandoned_response, 'class="abandoned-checkout-row border rounded mb-2" open')
        self.assertContains(abandoned_response, 'class="abandoned-checkout-item-row mb-2"')
        self.assertContains(abandoned_response, "Workflow Customer")
        self.assertContains(abandoned_response, "9876543210")

        settings_obj, _ = DeliverySharingSettings.objects.get_or_create(pk=1)
        settings_obj.abandoned_checkout_timeout_minutes = 5
        settings_obj.save(update_fields=["abandoned_checkout_timeout_minutes"])
        configured_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(configured_response.context["abandoned_checkout_count"], 0)
        self.assertContains(configured_response, "inactive for at least 5 minutes")

        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            last_activity_at=timezone.now() - timedelta(minutes=6),
        )
        expired_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(expired_response.context["abandoned_checkout_count"], 1)

        order = Order.objects.create(
            order_id="WORKFLOW-CHECKOUT-PLACED",
            user=self.customer.username,
            status="PLACED",
        )
        attempt.order = order
        attempt.save(update_fields=["order"])
        placed_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(placed_response.context["abandoned_checkout_count"], 0)

    def test_marketplace_detail_reads_quantity_using_active_cart_line_key(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.save()

        response = self.client.get(
            reverse(
                "market_product_detail",
                kwargs={"product_shop_id": self.assignment.pk},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quantity"], 2)
        self.assertEqual(response.json()["stock"], 8)

    def test_marketplace_guest_can_browse_but_add_action_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("marketplace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.product_name)
        self.assertContains(response, "marketplaceLoginUrl")
        self.assertContains(response, "window.location.assign(marketplaceLoginUrl)")

    def test_customer_without_saved_address_can_add_to_current_location_cart(self):
        ClientDeliveryAddress.objects.filter(client=self.profile).update(
            is_active=False,
        )
        response = self.client.get(
            reverse("marketplace"),
            {"current_lat": "1.25", "current_lng": "2.5"},
        )
        self.assertEqual(response.status_code, 200)

        added = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": self.assignment.pk}),
            {
                "offer": "retail",
                "current_lat": "1.25",
                "current_lng": "2.5",
            },
        )

        self.assertEqual(added.status_code, 200)
        self.assertTrue(added.json()["success"])
        self.assertEqual(added.json()["quantity"], 1)
        self.assertEqual(
            self.client.session["cart"]["%s" % self.assignment.pk],
            1,
        )
        cart_response = self.client.get(reverse("cart"))
        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(len(cart_response.context["items"]), 1)
        self.assertTrue(cart_response.context["items"][0]["available_for_delivery"])
        self.assertIsNone(cart_response.context["active_delivery_address"])

    def test_shop_can_save_remark_without_removing_abandoned_checkout(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.save()
        self.client.get(reverse("checkout"))
        attempt = CheckoutAttempt.objects.get(
            customer=self.customer,
            shop=self.shop,
        )
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            last_activity_at=timezone.now() - timedelta(minutes=4),
        )

        outsider = User.objects.create_user(username="unrelated_checkout_viewer")
        self.client.force_login(outsider)
        unauthorized_response = self.client.post(
            reverse("save_abandoned_checkout_remark", args=[attempt.pk]),
            {"remark": "Not authorized."},
        )
        self.assertEqual(unauthorized_response.status_code, 404)

        self.client.force_login(self.owner)
        blank_remark_response = self.client.post(
            reverse("save_abandoned_checkout_remark", args=[attempt.pk]),
            {"remark": "  "},
        )
        self.assertRedirects(
            blank_remark_response,
            f"{reverse('shop_order_list')}?view=abandoned",
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.shop_remark, "")

        response = self.client.post(
            reverse("save_abandoned_checkout_remark", args=[attempt.pk]),
            {"remark": "Customer contacted; no longer interested."},
        )
        self.assertRedirects(
            response,
            f"{reverse('shop_order_list')}?view=abandoned",
        )
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.shop_remark,
            "Customer contacted; no longer interested.",
        )
        self.assertEqual(attempt.remark_updated_by, self.owner)
        self.assertIsNotNone(attempt.remark_updated_at)

        abandoned_response = self.client.get(
            reverse("shop_order_list"),
            {"view": "abandoned"},
        )
        self.assertEqual(abandoned_response.context["abandoned_checkout_count"], 1)
        self.assertContains(abandoned_response, "Customer contacted; no longer interested.")

    def test_marketplace_categories_have_voice_search_controls(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.save()

        response = self.client.get(reverse("marketplace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-category-voice-search")
        self.assertContains(response, "category-product-voice-status")
        self.assertContains(response, "marketplace-search-scroll")
        self.assertContains(response, "window.localStorage.removeItem(storageKey)")
        self.assertContains(response, "data-search-keywords=")
        self.assertContains(response, 'searchParams.set("q", input.value.trim())')
        self.assertTrue(response.context["products"][0].marketplace_search_keywords)
        self.assertNotIn(
            "search_keywords",
            response.context["products"][0].marketplace_detail,
        )

    def test_marketplace_product_search_has_voice_search_control(self):
        response = self.client.get(reverse("marketplace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'placeholder="Search products or shop names..."')
        self.assertContains(response, 'aria-label="Search by voice"')
        self.assertContains(response, "data-voice-search-status")
        self.assertContains(response, 'type: "voice.start"')
        self.assertContains(response, "sellerhelper:native-voice-result")

    def test_cart_buckets_are_restored_per_delivery_address(self):
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 2}
        session.save()
        self.client.get(reverse("cart"))

        switch = self.client.post(
            reverse("cart_select_delivery_address"),
            {"address_id": self.office.pk, "next": "cart"},
        )
        self.assertRedirects(switch, reverse("cart"))
        self.assertEqual(self.client.get(reverse("cart")).context["items"], [])

        added = self.client.post(
            reverse("cart_add", kwargs={"product_shop_id": self.assignment.pk}),
            {"offer": "retail"},
        )
        self.assertTrue(added.json()["success"])
        session = self.client.session
        carts = session["carts_by_delivery_address"]
        self.assertEqual(carts[str(self.home.pk)][str(self.assignment.pk)], 2)
        self.assertEqual(carts[str(self.office.pk)][str(self.assignment.pk)], 1)

        self.client.post(
            reverse("cart_select_delivery_address"),
            {"address_id": self.home.pk, "next": "cart"},
        )
        self.assertEqual(
            self.client.get(reverse("cart")).context["items"][0]["quantity"],
            2,
        )
        self.client.post(
            reverse("cart_select_delivery_address"),
            {"address_id": self.office.pk, "next": "cart"},
        )
        self.assertEqual(
            self.client.get(reverse("cart")).context["items"][0]["quantity"],
            1,
        )

    def test_cart_count_badge_is_available_on_pages_without_cart_view_context(self):
        session = self.client.session
        session["cart"] = {
            str(self.assignment.pk): 2,
            f"bulk:{self.assignment.pk}": 3,
        }
        session.save()

        response = self.client.get(reverse("notifications"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cart_count"], 5)
        self.assertContains(response, 'data-cart-count')
        self.assertContains(response, 'class="cart-badge cart-count"')

    def test_editing_delivery_address_keeps_only_deliverable_cart_items(self):
        unavailable_shop = self.shop
        unavailable_shop.delivery_pincode = ["100001"]
        unavailable_shop.save(update_fields=["delivery_pincode"])
        available_shop = Shop.objects.create(
            name="Available Workflow Shop",
            owner=self.owner,
            delivery_pincode=["100002"],
        )
        available_assignment = ProductShop.objects.create(
            shop=available_shop,
            product=self.product,
            price=Decimal("80.00"),
            stock=8,
            is_active=True,
        )
        session = self.client.session
        session["carts_by_delivery_address"] = {
            str(self.home.pk): {
                str(available_assignment.pk): 1,
                str(self.assignment.pk): 2,
            },
            str(self.office.pk): {str(self.assignment.pk): 3},
        }
        session["cart_delivery_address_id"] = str(self.home.pk)
        session["cart"] = {
            str(available_assignment.pk): 1,
            str(self.assignment.pk): 2,
        }
        session.save()

        response = self.client.post(
            reverse(
                "delivery_address_edit",
                kwargs={"pk": self.home.pk},
            ),
            {
                "label": "Updated home",
                "address": "Updated Home Road",
                "city": "Surat",
                "state_code": "",
                "gstin": "",
                "pincode": "100002",
                "latitude": "1",
                "longitude": "2",
                "is_active": "on",
                "is_default": "on",
            },
        )

        self.assertRedirects(response, reverse("delivery_address_list"))
        session = self.client.session
        self.assertEqual(
            session["carts_by_delivery_address"][str(self.home.pk)],
            {
                str(self.assignment.pk): 0,
                str(available_assignment.pk): 1,
            },
        )
        self.assertEqual(
            session["carts_by_delivery_address"][str(self.office.pk)],
            {str(self.assignment.pk): 3},
        )
        self.assertEqual(
            session["cart"],
            {
                str(self.assignment.pk): 0,
                str(available_assignment.pk): 1,
            },
        )

        cart_response = self.client.get(reverse("cart"))
        self.assertEqual(cart_response.status_code, 200)
        self.assertContains(
            cart_response,
            "Not available in this delivery location",
        )
        self.assertEqual(cart_response.context["subtotal"], Decimal("80.00"))
        cart_items = {
            item["assignment"].pk: item
            for item in cart_response.context["items"]
        }
        self.assertEqual(
            [
                item["assignment"].pk
                for item in cart_response.context["items"]
            ],
            [self.assignment.pk, available_assignment.pk],
        )
        self.assertEqual(cart_items[self.assignment.pk]["quantity"], 0)
        self.assertFalse(
            cart_items[self.assignment.pk]["available_for_delivery"],
        )
        self.assertEqual(
            cart_items[self.assignment.pk]["availability_label"],
            "Not available in this delivery location",
        )
        self.assertEqual(cart_items[self.assignment.pk]["total"], Decimal("0.00"))
        self.assertEqual(cart_items[available_assignment.pk]["quantity"], 1)
        self.assertEqual(
            self.client.session["cart"][str(self.assignment.pk)],
            0,
        )
        self.assertTrue(
            cart_items[available_assignment.pk]["available_for_delivery"],
        )
        checkout_response = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_response.status_code, 200)
        self.assertEqual(checkout_response.context["subtotal"], Decimal("80.00"))
        self.assertEqual(
            [
                item["assignment"].pk
                for item in checkout_response.context["items"]
            ],
            [available_assignment.pk],
        )

    def test_rider_scanning_packing_slip_qr_gets_assigned_order_page_url(self):
        rider_user = User.objects.create_user(
            username="workflow_qr_rider",
            password="test-password",
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-WORKFLOW-QR-OPEN",
            user=self.customer.username,
            status="RIDER_ASSIGNED",
            assigned_rider=rider_user.username,
            shop=self.shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
        )
        self.client.force_login(rider_user)
        qr_url = f"http://testserver{reverse('shop_order_detail', kwargs={'shop_order_id': shop_order.pk})}"

        response = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {"order_qr": qr_url},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(
            response.json()["order_url"],
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        detail_response = self.client.get(response.json()["order_url"])
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["shop_order"].pk, shop_order.pk)
        self.assertContains(
            detail_response,
            "window.location.assign(result.order_url);",
        )

    def test_approved_public_rider_can_scan_and_open_accepted_order(self):
        rider_user = User.objects.create_user(
            username="workflow_public_qr_rider",
            email="workflow_public_qr_rider@example.com",
            password="test-pass",
        )
        PublicRiderApplication.objects.create(
            user=rider_user,
            full_name="Workflow Public Rider",
            phone="9876543210",
            address="Rider route",
            vehicle_type="MOTORCYCLE",
            driving_license_number="DL-WORKFLOW-QR",
            work_area={
                "type": "circle",
                "center": {"lat": 21.17, "lng": 72.83},
                "radius_km": 10,
            },
            status=PublicRiderApplication.STATUS_APPROVED,
        )
        rider_membership = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
            is_public_rider=True,
        )
        order = Order.objects.create(
            order_id="ORD-WORKFLOW-PUBLIC-QR",
            user=self.customer.username,
            status="RIDER_ASSIGNED",
            assigned_rider=rider_user.username,
            shop=self.shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="RIDER_ASSIGNED",
            assigned_rider=rider_membership,
            rider_accepted_at=timezone.now(),
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()

        rider_dashboard = self.client.get(reverse("rider_dashboard"))
        self.assertEqual(rider_dashboard.status_code, 200)
        self.assertContains(rider_dashboard, 'id="riderOrderQrScanForm"')
        self.assertContains(
            rider_dashboard,
            'data-sellerhelper-scan-order="rider-pickup-order"',
        )
        self.assertContains(rider_dashboard, "navigator.mediaDevices.getUserMedia")
        self.assertContains(rider_dashboard, 'formats: ["qr_code"]')
        self.assertContains(rider_dashboard, 'facingMode: { ideal: "environment" }')
        self.assertContains(rider_dashboard, "sellerhelper:qr-scan-error")
        self.assertContains(rider_dashboard, "alert alert-danger")
        self.assertContains(rider_dashboard, "sellerhelper:qr-scan-error")
        self.assertNotContains(rider_dashboard, 'alt="Mark verified QR"')
        self.assertNotContains(rider_dashboard, 'alt="Open order QR"')

        response = self.client.post(
            reverse("rider_scan_pickup_qr"),
            {
                "order_qr": (
                    f"http://testserver"
                    f"{reverse('shop_order_detail', kwargs={'shop_order_id': shop_order.pk})}"
                ),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(response.json()["accepted_by_you"])
        shop_order.refresh_from_db()
        self.assertTrue(shop_order.rider_order_acknowledged)
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")
        pickup_otp = seller_views._ensure_rider_pickup_otp(shop_order)
        self.client.force_login(self.owner)
        shop_verification = self.client.post(
            reverse("verify_rider_pickup_otp"),
            {"pickup_otp": pickup_otp},
        )
        self.assertEqual(shop_verification.status_code, 302)
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")
        self.assertTrue(
            shop_order.status_history.filter(status="OUT_FOR_DELIVERY").exists()
        )
        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "RIDER"
        session.save()
        detail_response = self.client.get(response.json()["order_url"])
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["shop_order"].pk, shop_order.pk)
        self.assertTrue(detail_response.context["is_assigned_rider"])
        self.assertContains(detail_response, 'id="riderOrderQrScanForm"')
        self.assertContains(detail_response, "Delivery actions")
        start_delivery_response = self.client.post(
            reverse("rider_start_active_deliveries"),
        )
        self.assertRedirects(
            start_delivery_response,
            f"{reverse('rider_dashboard')}?view=active",
            fetch_redirect_response=False,
        )
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.status, "OUT_FOR_DELIVERY")

    def test_scanning_pickup_task_qr_auto_verifies_rider_and_confirms_pickup(self):
        rider_user = User.objects.create_user(
            username="workflow_pickup_task_rider",
            email="workflow_pickup_task_rider@example.com",
            password="test-pass",
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-WORKFLOW-PICKUP-TASK",
            user=self.customer.username,
            status="RIDER_ASSIGNED",
            assigned_rider=rider_user.username,
            shop=self.shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="RIDER_ASSIGNED",
            assigned_rider=rider,
            rider_accepted_at=timezone.now(),
        )
        pickup_otp = seller_views._ensure_rider_pickup_otp(shop_order)
        pickup_task_qr = (
            f"{seller_views.RIDER_PICKUP_QR_PREFIX}{pickup_otp}"
        )

        self.client.force_login(rider_user)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        with patch.object(
            seller_views,
            "_qr_code_data_uri",
            wraps=seller_views._qr_code_data_uri,
        ) as qr_generator:
            dashboard = self.client.get(
                reverse("rider_dashboard"),
                {"view": "active"},
            )
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(
            pickup_task_qr,
            [call.args[0] for call in qr_generator.call_args_list],
        )
        self.assertContains(
            dashboard,
            "Task in this QR: verify rider identity and confirm parcel pickup.",
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("verify_rider_pickup_otp"),
            {"pickup_otp": pickup_task_qr},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(
            response.json()["redirect_url"],
            reverse(
                "shop_order_detail",
                kwargs={"shop_order_id": shop_order.pk},
            ),
        )
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "PICKED_UP")
        self.assertEqual(order.status, "PICKED_UP")

    def test_legacy_mark_verified_qr_does_not_bypass_pickup_otp(self):
        order = Order.objects.create(
            order_id="ORD-WORKFLOW-QR-OTP-REQUIRED",
            user=self.customer.username,
            status="RIDER_ASSIGNED",
            shop=self.shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="RIDER_ASSIGNED",
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse(
                "shop_order_action",
                args=[shop_order.pk, "mark-verified"],
            )
        )

        self.assertEqual(response.status_code, 302)
        shop_order.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shop_order.status, "RIDER_ASSIGNED")
        self.assertEqual(order.status, "RIDER_ASSIGNED")

    def test_order_list_offers_inline_packer_and_rider_assignment(self):
        packer_user = User.objects.create_user(
            username="workflow_list_packer",
            password="test-password",
        )
        rider_user = User.objects.create_user(
            username="workflow_list_rider",
            password="test-password",
        )
        packer = ShopStaff.objects.create(
            shop=self.shop,
            user=packer_user,
            role="PACKER",
            is_active=True,
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        order = Order.objects.create(
            order_id="ORD-WORKFLOW-LIST-ASSIGN",
            user=self.customer.username,
            status="CONFIRMED",
            shop=self.shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            status="CONFIRMED",
        )
        self.client.force_login(self.owner)

        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, 'aria-label="Assign packer"')
        self.assertContains(detail_response, 'aria-label="Assign rider"')
        self.assertContains(detail_response, "onchange=\"this.form.requestSubmit()\"")
        self.assertNotContains(detail_response, "Assign Packer")
        self.assertNotContains(detail_response, "Assign Rider</button>")

        response = self.client.get(reverse("shop_order_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Assign packer"')
        self.assertContains(response, 'aria-label="Assign rider"')
        self.assertContains(response, "workflow_list_packer")
        self.assertContains(response, "workflow_list_rider")

        packer_response = self.client.post(
            reverse("assign_packer", kwargs={"shop_order_id": shop_order.pk}),
            {"packer": packer.pk, "next": reverse("shop_order_list")},
        )
        self.assertRedirects(packer_response, reverse("shop_order_list"))
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_packer_id, packer.pk)

        rider_response = self.client.post(
            reverse("assign_rider", kwargs={"shop_order_id": shop_order.pk}),
            {"rider": rider.pk, "next": reverse("shop_order_list")},
        )
        self.assertRedirects(rider_response, reverse("shop_order_list"))
        shop_order.refresh_from_db()
        self.assertEqual(shop_order.assigned_rider_id, rider.pk)

    def test_checkout_blocks_shops_that_do_not_serve_selected_address(self):
        self.shop.delivery_pincode = ["100002"]
        self.shop.save(update_fields=["delivery_pincode"])
        session = self.client.session
        session["cart"] = {str(self.assignment.pk): 1}
        session.save()

        checkout = self.client.get(reverse("checkout"))
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(checkout.context["delivery_issues"], [self.shop.name])

        response = self.client.post(
            reverse("checkout"),
            {"address": self.home.pk, "payment_method": "COD"},
        )
        self.assertRedirects(response, reverse("checkout"))
        self.assertFalse(Order.objects.filter(user=self.customer.username).exists())
        self.assertEqual(
            self.client.session["cart"],
            {str(self.assignment.pk): 1},
        )
        ajax_response = self.client.post(
            reverse("checkout"),
            {"address": self.home.pk, "payment_method": "COD"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(ajax_response.status_code, 409)
        self.assertFalse(ajax_response.json()["success"])
        self.assertIn("Delivery is unavailable", ajax_response.json()["message"])
        self.assertFalse(Order.objects.filter(user=self.customer.username).exists())
        self.assertEqual(
            self.client.session["cart"],
            {str(self.assignment.pk): 1},
        )

    def test_marketplace_cards_show_saved_retail_and_b2b_bulk_quantities(self):
        self.shop.gstin = "24ABCDE1234F1Z5"
        self.shop.gst_state_code = "24"
        self.shop.save(update_fields=["gstin", "gst_state_code"])
        self.assignment.b2b_price = Decimal("60.00")
        self.assignment.b2b_bulk_price = Decimal("100.00")
        self.assignment.b2b_bulk_unit = "case"
        self.assignment.bulk_min_quantity = 2
        self.assignment.b2b_stock = 5
        self.assignment.save(
            update_fields=[
                "b2b_price",
                "b2b_bulk_price",
                "b2b_bulk_unit",
                "bulk_min_quantity",
                "b2b_stock",
            ]
        )
        self.profile.gstin = "24ABCDE1234F1Z5"
        self.profile.save(update_fields=["gstin"])
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session["cart"] = {
            str(self.assignment.pk): 1,
            f"bulk:{self.assignment.pk}": 2,
        }
        session.save()

        response = self.client.get(reverse("marketplace"))

        self.assertEqual(response.status_code, 200)
        listed_assignment = next(
            item for item in response.context["products"]
            if item.pk == self.assignment.pk
        )
        self.assertEqual(listed_assignment.cart_quantity, 1)
        self.assertEqual(listed_assignment.bulk_cart_quantity, 2)
        self.assertTrue(listed_assignment.has_b2b_bulk_offer)
        self.assertContains(response, "B2B bulk offer")
        self.assertContains(response, f'id="qty-{self.assignment.pk}"')
        self.assertContains(response, f'id="bulk-qty-{self.assignment.pk}"')
        self.assertContains(response, 'value="1"')
        self.assertContains(response, 'value="2"')
        self.assertContains(response, "Update bulk")

        update_response = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"quantity": 3, "offer": "bulk"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(update_response.json()["success"])
        self.assertEqual(update_response.json()["quantity"], 3)
        self.assertEqual(
            self.client.session["cart"][f"bulk:{self.assignment.pk}"],
            3,
        )

    def test_purchase_manager_updates_shop_prices_and_separate_stock(self):
        manager = User.objects.create_user(
            username="workflow_purchase_manager",
            password="test-password",
        )
        ShopStaff.objects.create(
            shop=self.shop,
            user=manager,
            role="PURCHASE_MANAGER",
            is_active=True,
        )
        self.assignment.purchase_price = Decimal("100.00")
        self.assignment.save(update_fields=["purchase_price"])
        self.client.force_login(manager)
        dashboard = self.client.get(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            )
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "B2B bulk")
        self.assertContains(dashboard, "Lock zoom")
        self.assertNotContains(dashboard, "Zoom out")

        response = self.client.post(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
            {
                "action": "save_pricing",
                "product_shop_id": self.assignment.pk,
                "retail_margin_percent": "20",
                "retail_margin_fixed": "5",
                "bulk_margin_percent": "10",
                "bulk_margin_fixed": "2",
                "b2b_margin_percent": "5",
                "b2b_margin_fixed": "1",
                "b2b_bulk_margin_percent": "3",
                "b2b_bulk_margin_fixed": "0.50",
            },
        )

        self.assertRedirects(
            response,
            reverse("purchase_manager_dashboard", kwargs={"shop_id": self.shop.pk}),
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.price, Decimal("125.00"))
        self.assertEqual(self.assignment.bulk_price, Decimal("112.00"))
        self.assertEqual(self.assignment.b2b_price, Decimal("106.00"))
        self.assertEqual(self.assignment.b2b_bulk_price, Decimal("103.50"))
        self.assertEqual(self.assignment.stock, 8)
        self.assertEqual(self.assignment.b2b_stock, 3)
        self.assertFalse(
            StockMovement.objects.filter(
                product_shop=self.assignment,
                movement_type="IMPORT",
            ).exists(),
        )

    def test_vendor_invoice_tracks_separate_stock_prices_and_partial_payment(self):
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        vendor_response = self.client.post(
            dashboard_url,
            {"action": "add_vendor", "name": "Fresh Supplier", "phone": "123"},
        )
        self.assertRedirects(vendor_response, dashboard_url)
        vendor = ShopVendor.objects.get(shop=self.shop, name="Fresh Supplier")

        self.assignment.retail_margin_percent = Decimal("20.00")
        self.assignment.b2b_margin_percent = Decimal("5.00")
        self.assignment.save(update_fields=["retail_margin_percent", "b2b_margin_percent"])
        invoice_response = self.client.post(
            dashboard_url,
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "INV-001",
                "purchase_date": "2025-03-01",
                "product_shop_id": [self.assignment.pk, self.assignment.pk],
                "stock_type": ["RETAIL", "B2B"],
                "quantity": ["2", "3"],
                "unit_cost": ["100.00", "80.00"],
                "initial_payment": "140.00",
                "payment_method": "UPI",
                "payment_reference": "upi-ref",
            },
        )
        self.assertRedirects(invoice_response, dashboard_url + "?tab=purchase")
        purchase = VendorPurchase.objects.get(shop=self.shop, invoice_number="INV-001")
        self.assertEqual(purchase.total_amount, Decimal("440.00"))
        self.assertEqual(purchase.amount_paid, Decimal("140.00"))
        self.assertEqual(purchase.balance_due, Decimal("300.00"))
        self.assertEqual(purchase.payment_status, "PARTIAL")
        self.assertEqual(purchase.items.count(), 2)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 10)
        self.assertEqual(self.assignment.b2b_stock, 6)
        self.assertEqual(self.assignment.purchase_price, Decimal("100.00"))
        self.assertEqual(self.assignment.b2b_purchase_price, Decimal("80.00"))
        self.assertEqual(self.assignment.price, Decimal("120.00"))
        self.assertEqual(self.assignment.b2b_price, Decimal("84.00"))
        self.assertEqual(
            StockMovement.objects.filter(
                product_shop=self.assignment,
                movement_type="IMPORT",
            ).count(),
            2,
        )

        payment_response = self.client.post(
            dashboard_url,
            {
                "action": "record_payment",
                "purchase_id": purchase.pk,
                "amount": "300.00",
                "payment_method": "BANK",
                "reference": "bank-ref",
            },
        )
        self.assertRedirects(payment_response, dashboard_url)
        purchase.refresh_from_db()
        self.assertEqual(purchase.amount_paid, Decimal("440.00"))
        self.assertEqual(purchase.balance_due, Decimal("0.00"))
        self.assertEqual(purchase.payment_status, "PAID")

        dashboard = self.client.get(dashboard_url)
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Recent supplier price history")
        self.assertContains(dashboard, "Search product")
        self.assertContains(dashboard, "Retail stock")
        self.assertContains(dashboard, "Current ₹")
        self.assertContains(dashboard, "data-preview=\"b2b_price\"")
        self.assertContains(dashboard, "purchase-price-preview")
        self.assertContains(dashboard, "Invoice details")
        self.assertContains(dashboard, "Products received")
        self.assertContains(dashboard, "Delivery and payment")
        self.assertContains(dashboard, "purchase-line-fields")
        self.assertContains(dashboard, "Add another product")
        self.assertContains(dashboard, " current:")

    def test_purchase_wastage_landed_cost_gst_vendor_payments_and_reports(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Accounting Supplier")
        self.shop.allow_product_image_overrides = True
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.assignment.image_url_override = "https://example.com/workflow-product.jpg"
        self.assignment.save(update_fields=["image_url_override"])
        self.product.sku = "WORKFLOW-SKU"
        self.product.save(update_fields=["sku"])
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        purchase_date = timezone.localdate().isoformat()

        response = self.client.post(
            dashboard_url,
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "WASTE-001",
                "purchase_date": purchase_date,
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["RETAIL"],
                "quantity": ["100"],
                "wastage_quantity": ["10"],
                "unit_cost": ["10.00"],
                "gst_rate": ["5.00"],
                "delivery_expense": "100.00",
                "delivery_gst_rate": "10.00",
                "initial_payment": "100.00",
                "payment_method": "UPI",
            },
        )

        self.assertRedirects(response, dashboard_url + "?tab=purchase")
        purchase = VendorPurchase.objects.get(shop=self.shop, invoice_number="WASTE-001")
        item = purchase.items.get()
        self.assertEqual(item.purchased_quantity, 100)
        self.assertEqual(item.wastage_quantity, 10)
        self.assertEqual(item.quantity, 90)
        self.assertEqual(item.line_total, Decimal("1000.00"))
        self.assertEqual(item.gst_amount, Decimal("50.00"))
        self.assertEqual(purchase.subtotal_amount, Decimal("1000.00"))
        self.assertEqual(purchase.gst_amount, Decimal("60.00"))
        self.assertEqual(purchase.delivery_gst_amount, Decimal("10.00"))
        self.assertEqual(purchase.total_amount, Decimal("1160.00"))
        self.assertEqual(purchase.amount_paid, Decimal("100.00"))

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 98)
        self.assertEqual(self.assignment.purchase_price, Decimal("12.22"))
        self.assertEqual(
            list(
                StockMovement.objects.filter(
                    product_shop=self.assignment,
                )
                .order_by("pk")
                .values_list("movement_type", "quantity")
            ),
            [("IMPORT", 100), ("ADJUSTMENT", -10)],
        )

        report_response = self.client.get(
            dashboard_url,
            {"tab": "reports", "report_month": timezone.localdate().strftime("%Y-%m")},
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(
            {
                row["rate"]: (row["taxable"], row["gst"])
                for row in report_response.context["gst_rate_rows"]
            },
            {
                Decimal("5.00"): (Decimal("1000.00"), Decimal("50.00")),
                Decimal("10.00"): (Decimal("100.00"), Decimal("10.00")),
            },
        )
        self.assertEqual(
            report_response.context["payment_records"][0]["vendor"],
            vendor,
        )

        csv_response = self.client.get(
            dashboard_url,
            {
                "tab": "reports",
                "report_month": timezone.localdate().strftime("%Y-%m"),
                "report_format": "csv",
            },
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("1160.00", csv_response.content.decode())

        pricing_response = self.client.get(dashboard_url, {"tab": "products"})
        self.assertContains(pricing_response, "https://example.com/workflow-product.jpg")
        self.assertContains(pricing_response, "SKU: WORKFLOW-SKU")
        self.assertContains(pricing_response, "Current retail purchase cost (editable)")
        self.assertContains(pricing_response, "Current B2B purchase cost (editable)")
        self.assertContains(pricing_response, 'form.querySelector(\'[name="purchase_price"]\').value')
        self.assertContains(pricing_response, 'data-preview="b2b_bulk_price"')

        pricing_update = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "AUTO",
                "purchase_price": "13.50",
                "b2b_purchase_price": "",
                "retail_margin_percent": "20",
                "retail_margin_fixed": "2",
                "bulk_margin_percent": "10",
                "bulk_margin_fixed": "1",
                "b2b_margin_percent": "5",
                "b2b_margin_fixed": "3",
                "b2b_bulk_margin_percent": "8",
                "b2b_bulk_margin_fixed": "4",
            },
        )
        self.assertRedirects(pricing_update, dashboard_url + "?tab=products")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.purchase_price, Decimal("13.50"))
        self.assertEqual(self.assignment.price, Decimal("18.20"))
        self.assertEqual(self.assignment.bulk_price, Decimal("15.85"))
        self.assertEqual(self.assignment.b2b_price, Decimal("17.18"))
        self.assertEqual(self.assignment.b2b_bulk_price, Decimal("18.58"))

    def test_purchase_units_convert_received_stock_and_record_fractional_waste(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Unit Conversion Supplier")
        self.product.unit = "500g"
        self.product.save(update_fields=["unit"])
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )

        response = self.client.post(
            dashboard_url,
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "purchase_date": timezone.localdate().isoformat(),
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["RETAIL"],
                "quantity": ["2"],
                "purchase_unit_value": ["1"],
                "purchase_unit_text": ["kg"],
                "wastage_quantity": ["0.0001"],
                "unit_cost": ["20.00"],
                "gst_rate": ["0"],
            },
        )

        self.assertRedirects(response, dashboard_url + "?tab=purchase")
        item = VendorPurchaseItem.objects.get(
            purchase__vendor=vendor,
            product_shop=self.assignment,
        )
        self.assertEqual(item.purchased_quantity, 2)
        self.assertEqual(item.purchase_unit_value, Decimal("1.0000"))
        self.assertEqual(item.purchase_unit_text, "kg")
        self.assertEqual(item.inventory_unit_value, Decimal("500"))
        self.assertEqual(item.inventory_unit_text, "g")
        self.assertEqual(item.wastage_quantity, Decimal("0.0001"))
        self.assertEqual(item.quantity, Decimal("3.9999"))
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, Decimal("11.9999"))
        stock_response = self.client.get(
            reverse(
                "market_product_detail",
                kwargs={"product_shop_id": self.assignment.pk},
            )
        )
        self.assertEqual(stock_response.json()["stock"], 11.9999)
        self.assertEqual(
            list(
                StockMovement.objects.filter(product_shop=self.assignment)
                .order_by("pk")
                .values_list("movement_type", "quantity")
            ),
            [
                ("IMPORT", Decimal("4.0000")),
                ("ADJUSTMENT", Decimal("-0.0001")),
            ],
        )

    def test_purchase_manager_saves_all_product_unit_types(self):
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        pricing_page = self.client.get(dashboard_url, {"tab": "products"})
        self.assertContains(pricing_page, 'name="retail_unit_value"')
        self.assertContains(pricing_page, 'name="bulk_unit_text"')
        self.assertContains(pricing_page, 'name="b2b_unit_value"')
        self.assertNotContains(pricing_page, 'name="b2b_unit_text"')
        self.assertContains(pricing_page, "B2B (shared unit text)")
        self.assertContains(pricing_page, 'name="b2b_bulk_unit_text"')
        response = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "AUTO",
                "retail_unit_value": "0.5",
                "retail_unit_text": "kg",
                "bulk_unit_value": "12",
                "bulk_unit_text": "piece",
                "b2b_unit_value": "1",
                "b2b_bulk_unit_value": "10",
                "b2b_bulk_unit_text": "kg",
            },
        )

        self.assertRedirects(response, dashboard_url)
        self.product.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(self.product.unit, "0.5kg")
        self.assertEqual(self.assignment.bulk_unit, "12piece")
        self.assertEqual(self.assignment.b2b_unit, "1kg")
        self.assertEqual(self.assignment.b2b_bulk_unit, "10kg")

    def test_margin_prices_scale_with_sale_unit_value(self):
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        response = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "AUTO",
                "purchase_price": "10.00",
                "b2b_purchase_price": "8.00",
                "retail_unit_value": "500",
                "retail_unit_text": "g",
                "bulk_unit_value": "1",
                "bulk_unit_text": "kg",
                "b2b_unit_value": "1000",
                "b2b_bulk_unit_value": "5000",
                "b2b_bulk_unit_text": "g",
                "retail_margin_percent": "0",
                "retail_margin_fixed": "0",
                "bulk_margin_percent": "0",
                "bulk_margin_fixed": "0",
                "b2b_margin_percent": "0",
                "b2b_margin_fixed": "0",
                "b2b_bulk_margin_percent": "0",
                "b2b_bulk_margin_fixed": "0",
            },
        )

        self.assertRedirects(response, dashboard_url + "?tab=products")
        self.assignment.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.product.unit, "500g")
        self.assertEqual(self.assignment.price, Decimal("10.00"))
        self.assertEqual(self.assignment.bulk_price, Decimal("20.00"))
        self.assertEqual(self.assignment.b2b_price, Decimal("8.00"))
        self.assertEqual(self.assignment.b2b_bulk_price, Decimal("40.00"))

    def test_b2b_price_uses_retail_cost_scaled_to_b2b_unit_when_missing(self):
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        response = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "AUTO",
                "purchase_price": "10.00",
                "b2b_purchase_price": "",
                "retail_unit_value": "500",
                "retail_unit_text": "g",
                "b2b_unit_value": "1000",
                "b2b_margin_percent": "10",
                "b2b_margin_fixed": "2",
                "b2b_bulk_unit_value": "2000",
                "b2b_bulk_unit_text": "g",
                "b2b_bulk_margin_percent": "0",
                "b2b_bulk_margin_fixed": "0",
            },
        )

        self.assertRedirects(response, dashboard_url + "?tab=products")
        self.assignment.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.product.unit, "500g")
        self.assertEqual(self.assignment.b2b_unit, "1000g")
        self.assertEqual(self.assignment.b2b_price, Decimal("24.00"))
        self.assignment.b2b_unit = "1000kg"
        self.assignment.save(update_fields=["b2b_unit"])
        self.assertEqual(self.assignment.common_b2b_unit, "1000g")
        self.assertEqual(self.assignment.b2b_bulk_price, Decimal("40.00"))

    def test_margin_prices_convert_unit_types_and_reject_incompatible_units(self):
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        response = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "AUTO",
                "purchase_price": "10.00",
                "retail_unit_value": "1",
                "retail_unit_text": "lb",
                "bulk_unit_value": "32",
                "bulk_unit_text": "oz",
                "bulk_margin_percent": "10",
                "bulk_margin_fixed": "1",
            },
        )
        self.assertRedirects(response, dashboard_url + "?tab=products")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.bulk_price, Decimal("23.00"))

        invalid_units_response = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "AUTO",
                "purchase_price": "10.00",
                "retail_unit_value": "1",
                "retail_unit_text": "kg",
                "bulk_unit_value": "1",
                "bulk_unit_text": "pcs",
            },
            follow=True,
        )
        self.assertContains(
            invalid_units_response,
            "cannot be priced against stock unit",
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.bulk_unit, "32oz")

    def test_purchase_return_reduces_inventory_and_invoice_balance_with_vendor_credit(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Return Supplier")
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        self.client.post(
            dashboard_url,
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "RETURN-001",
                "purchase_date": timezone.localdate().isoformat(),
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["RETAIL"],
                "quantity": ["10"],
                "wastage_quantity": ["0"],
                "unit_cost": ["10.00"],
                "gst_rate": ["5.00"],
            },
        )
        purchase = VendorPurchase.objects.get(invoice_number="RETURN-001")
        purchase_item = purchase.items.get()
        return_url = dashboard_url

        response = self.client.post(
            return_url,
            {
                "action": "return_purchase",
                "tab": "history",
                "purchase_id": purchase.pk,
                "return_item_id": [purchase_item.pk],
                "return_quantity": ["4"],
                "return_reason": "Damaged packaging",
            },
        )
        self.assertRedirects(response, dashboard_url + "?tab=history")
        purchase.refresh_from_db()
        purchase_return = purchase.returns.get()
        self.assertEqual(purchase_return.subtotal_amount, Decimal("40.00"))
        self.assertEqual(purchase_return.gst_amount, Decimal("2.00"))
        self.assertEqual(purchase_return.total_amount, Decimal("42.00"))
        self.assertEqual(purchase.balance_due, Decimal("63.00"))
        self.assertEqual(purchase.payment_status, "PARTIAL")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 14)
        self.assertEqual(
            list(
                StockMovement.objects.filter(
                    product_shop=self.assignment,
                ).order_by("pk").values_list("movement_type", "quantity")
            ),
            [("IMPORT", 10), ("ADJUSTMENT", -4)],
        )

        invalid_return = self.client.post(
            return_url,
            {
                "action": "return_purchase",
                "tab": "history",
                "purchase_id": purchase.pk,
                "return_item_id": [purchase_item.pk],
                "return_quantity": ["7"],
            },
        )
        self.assertRedirects(invalid_return, dashboard_url + "?tab=history")
        self.assertEqual(purchase.returns.count(), 1)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 14)

        history_response = self.client.get(dashboard_url, {"tab": "history"})
        self.assertContains(history_response, "Vendor credits: ₹42.00")
        self.assertContains(history_response, "Damaged packaging")
        report_response = self.client.get(
            dashboard_url,
            {
                "tab": "reports",
                "report_month": timezone.localdate().strftime("%Y-%m"),
            },
        )
        self.assertEqual(
            report_response.context["monthly_reports"][0]["returns"],
            Decimal("42.00"),
        )
        self.assertEqual(
            report_response.context["gst_rate_rows"][0]["taxable"],
            Decimal("60.00"),
        )
        self.assertEqual(
            report_response.context["gst_rate_rows"][0]["gst"],
            Decimal("3.00"),
        )

    def test_purchase_manager_can_add_new_shop_product(self):
        self.client.force_login(self.owner)
        Product.objects.create(
            created_by=self.owner,
            product_name="Listed Catalog Product",
            product_category="FRUITS",
            price=Decimal("20.00"),
            unit="piece",
            sku="LISTED-001",
            product_alias_name="Catalog apple",
            product_title="Catalog Apple",
            product_description="A listed product with reusable details.",
            hsn_code="080810",
            gst_rate=Decimal("5.00"),
        )
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        add_product_url = reverse(
            "purchase_manager_add_product",
            kwargs={"shop_id": self.shop.pk},
        )
        purchase_page = self.client.get(dashboard_url, {"tab": "purchase"})
        self.assertContains(
            purchase_page,
            f'href="{add_product_url}"',
        )
        self.assertNotContains(purchase_page, 'name="product_name"')
        add_page = self.client.get(add_product_url)
        self.assertEqual(add_page.status_code, 200)
        self.assertContains(add_page, f"Add product to {self.shop.name}")
        self.assertContains(add_page, "Additional product details")
        self.assertContains(add_page, 'name="product_description"')
        self.assertContains(add_page, 'name="unit_value"')
        self.assertContains(add_page, 'data-product-name-autofill="true"')
        self.assertContains(add_page, '<datalist id="product-name-options">')
        self.assertContains(add_page, "product-listed-details")
        listed_product = next(
            item
            for item in add_page.context["form"].catalog_products
            if item["product_name"] == "Listed Catalog Product"
        )
        self.assertEqual(listed_product["product_category"], "FRUITS")
        self.assertEqual(listed_product["unit"], "piece")
        self.assertEqual(listed_product["product_description"], "A listed product with reusable details.")
        self.assertEqual(listed_product["sku"], "LISTED-001")

        wrong_category = self.client.post(
            add_product_url,
            {
                "product_category": "OTHER",
                "product_name": "Listed Catalog Product",
                "price": "20.00",
                "unit_value": "1",
                "unit_text": "piece",
                "gst_rate": "5.00",
                "hsn_code": "080810",
            },
        )
        self.assertEqual(wrong_category.status_code, 200)
        self.assertIn(
            "This listed product belongs to a different category",
            str(wrong_category.context["form"].errors),
        )

        response = self.client.post(
            add_product_url,
            {
                "product_category": "OTHER",
                "product_name": "New Manager Product",
                "sku": "PM-NEW-001",
                "product_alias_name": "",
                "product_title": "",
                "product_color": "",
                "product_size": "",
                "price": "32.00",
                "hsn_code": "",
                "gst_rate": "",
                "unit_value": "1",
                "unit_text": "piece",
                "image_url": "",
                "shelf_life_days": "",
                "expiry_price_drop_percent": "0.00",
                "product_description": "Added from the purchase manager.",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, dashboard_url + "?tab=purchase")
        assignment = ProductShop.objects.get(
            shop=self.shop,
            product__product_name="New Manager Product",
        )
        product = assignment.product
        self.assertEqual(product.created_by, self.owner)
        self.assertEqual(product.product_description, "Added from the purchase manager.")
        self.assertEqual(product.unit, "1piece")
        self.assertEqual(assignment.price, Decimal("32.00"))
        self.assertIsNone(assignment.purchase_price)
        self.assertEqual(assignment.stock, 0)
        self.assertEqual(assignment.pricing_mode, "AUTO")

        purchase_page = self.client.get(dashboard_url, {"tab": "purchase"})
        self.assertContains(purchase_page, "Add a product to this shop")
        self.assertContains(purchase_page, "New Manager Product")

    def test_products_and_pricing_can_add_retail_or_b2b_stock(self):
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        pricing_page = self.client.get(dashboard_url, {"tab": "products"})
        self.assertContains(pricing_page, "Add stock")
        self.assertContains(pricing_page, "Quick stock adjustment")

        for stock_type, expected_stock, added_quantity in (
            ("RETAIL", 13, 5),
            ("B2B", 7, 4),
        ):
            response = self.client.post(
                dashboard_url,
                {
                    "action": "add_stock",
                    "tab": "products",
                    "product_shop_id": self.assignment.pk,
                    "stock_type": stock_type,
                    "quantity": str(added_quantity),
                },
            )
            self.assertRedirects(response, dashboard_url + "?tab=products")
            self.assignment.refresh_from_db()
            self.assertEqual(
                self.assignment.stock if stock_type == "RETAIL" else self.assignment.b2b_stock,
                expected_stock,
            )
            self.assertEqual(
                StockMovement.objects.filter(
                    product_shop=self.assignment,
                    movement_type="IMPORT",
                    reference=f"Manual stock addition ({stock_type})",
                ).get().quantity,
                added_quantity,
            )

        invalid_response = self.client.post(
            dashboard_url,
            {
                "action": "add_stock",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "stock_type": "RETAIL",
                "quantity": "-1",
            },
        )
        self.assertRedirects(invalid_response, dashboard_url + "?tab=products")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 13)

    def test_shop_owner_can_open_purchase_and_vendor_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_shop_owner_access"])
        self.assertTrue(response.context["can_manage_app_zoom"])
        self.assertContains(response, "Add vendor")
        self.assertContains(response, 'data-purchase-tab="overview"')
        self.assertContains(response, "Overview of supplier balances and products that need replenishment.")
        self.assertContains(response, "No forecast shortages right now.")
        self.assertContains(response, 'data-purchase-tab="purchase"')
        self.assertContains(
            response,
            f'value="{timezone.localdate().isoformat()}" required',
        )
        self.assertContains(response, 'data-purchase-tab="history"')
        self.assertContains(response, 'data-purchase-tab="products"')
        products_response = self.client.get(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
            {"tab": "products"},
        )
        self.assertEqual(products_response.status_code, 200)
        self.assertEqual(products_response.context["active_tab"], "products")
        self.assertContains(
            products_response,
            'data-purchase-tab-panel="products"',
        )

    def test_b2b_receipt_can_set_b2b_cost_without_retail_cost(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="B2B Supplier")
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "B2B-001",
                "purchase_date": "2025-03-01",
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["B2B"],
                "quantity": ["2"],
                "unit_cost": ["70.00"],
                "initial_payment": "0",
            },
        )
        self.assertRedirects(
            response,
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ) + "?tab=purchase",
        )
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.purchase_price)
        self.assertEqual(self.assignment.b2b_purchase_price, Decimal("70.00"))
        self.assertEqual(self.assignment.b2b_price, Decimal("70.00"))
        self.assertEqual(self.assignment.b2b_stock, 5)

    def test_average_cost_includes_existing_inventory_and_new_receipt(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Average Supplier")
        self.assignment.purchase_price = Decimal("50.00")
        self.assignment.save(update_fields=["purchase_price"])
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "AVG-001",
                "purchase_date": "2025-03-01",
                "cost_method": "AVERAGE",
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["RETAIL"],
                "quantity": ["2"],
                "unit_cost": ["100.00"],
            },
        )
        self.assertRedirects(
            response,
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ) + "?tab=purchase",
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, 10)
        self.assertEqual(self.assignment.purchase_price, Decimal("60.00"))
        self.assertEqual(self.assignment.price, Decimal("60.00"))
        purchase = VendorPurchase.objects.get(invoice_number="AVG-001")
        self.assertEqual(purchase.cost_method, "AVERAGE")

    def test_latest_purchase_cost_replaces_saved_stock_cost(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Latest Cost Supplier")
        self.assignment.purchase_price = Decimal("50.00")
        self.assignment.save(update_fields=["purchase_price"])
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "NEW-001",
                "purchase_date": "2025-03-01",
                "cost_method": "NEW",
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["RETAIL"],
                "quantity": ["2"],
                "unit_cost": ["100.00"],
            },
        )
        self.assertRedirects(
            response,
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ) + "?tab=purchase",
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.purchase_price, Decimal("100.00"))
        self.assertEqual(
            VendorPurchase.objects.get(invoice_number="NEW-001").cost_method,
            "NEW",
        )

    def test_manual_sale_prices_persist_across_new_purchase(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Manual Price Supplier")
        self.assignment.purchase_price = Decimal("50.00")
        self.assignment.save(update_fields=["purchase_price"])
        self.client.force_login(self.owner)
        dashboard_url = reverse(
            "purchase_manager_dashboard",
            kwargs={"shop_id": self.shop.pk},
        )
        pricing_response = self.client.post(
            dashboard_url,
            {
                "action": "save_pricing",
                "tab": "products",
                "product_shop_id": self.assignment.pk,
                "pricing_mode": "MANUAL",
                "price": "90.00",
                "bulk_price": "85.00",
                "b2b_price": "80.00",
                "b2b_bulk_price": "75.00",
                "retail_margin_percent": "0",
                "retail_margin_fixed": "0",
                "bulk_margin_percent": "0",
                "bulk_margin_fixed": "0",
                "b2b_margin_percent": "0",
                "b2b_margin_fixed": "0",
                "b2b_bulk_margin_percent": "0",
                "b2b_bulk_margin_fixed": "0",
            },
        )
        self.assertRedirects(pricing_response, dashboard_url + "?tab=products")

        purchase_response = self.client.post(
            dashboard_url,
            {
                "action": "create_purchase",
                "tab": "purchase",
                "vendor_id": vendor.pk,
                "invoice_number": "MANUAL-001",
                "purchase_date": "2025-03-01",
                "cost_method": "AVERAGE",
                "product_shop_id": [self.assignment.pk],
                "stock_type": ["RETAIL"],
                "quantity": ["2"],
                "unit_cost": ["100.00"],
            },
        )
        self.assertRedirects(purchase_response, dashboard_url + "?tab=purchase")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.pricing_mode, "MANUAL")
        self.assertEqual(self.assignment.price, Decimal("90.00"))
        self.assertEqual(self.assignment.bulk_price, Decimal("85.00"))
        self.assertEqual(self.assignment.b2b_price, Decimal("80.00"))
        self.assertEqual(self.assignment.b2b_bulk_price, Decimal("75.00"))
        self.assertEqual(self.assignment.purchase_price, Decimal("60.00"))

    def test_vendor_payment_cannot_exceed_invoice_balance(self):
        vendor = ShopVendor.objects.create(shop=self.shop, name="Balance Supplier")
        purchase = VendorPurchase.objects.create(
            shop=self.shop,
            vendor=vendor,
            total_amount=Decimal("50.00"),
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
            {
                "action": "record_payment",
                "purchase_id": purchase.pk,
                "amount": "51.00",
                "payment_method": "CASH",
            },
        )
        self.assertRedirects(
            response,
            reverse(
                "purchase_manager_dashboard",
                kwargs={"shop_id": self.shop.pk},
            ),
        )
        self.assertFalse(VendorPurchasePayment.objects.filter(purchase=purchase).exists())

    def test_shop_owner_can_assign_purchase_manager_role(self):
        self.client.force_login(self.owner)
        create_url = reverse(
            "shop_staff_create_shop",
            kwargs={"shop_id": self.shop.pk},
        )
        form_response = self.client.get(create_url)
        self.assertEqual(form_response.status_code, 200)
        self.assertContains(form_response, "Purchase Manager")

        response = self.client.post(
            create_url,
            {
                "username": "assigned_purchase_manager",
                "password": "test-password",
                "role": "PURCHASE_MANAGER",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ShopStaff.objects.filter(
                shop=self.shop,
                user__username="assigned_purchase_manager",
                role="PURCHASE_MANAGER",
            ).exists()
        )
        staff = ShopStaff.objects.get(
            shop=self.shop,
            user__username="assigned_purchase_manager",
        )
        edit_response = self.client.get(
            reverse(
                "shop_staff_edit",
                kwargs={"shop_id": self.shop.pk, "staff_id": staff.pk},
            )
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, 'value="PURCHASE_MANAGER"')

    def test_shop_staff_edit_syncs_access_across_owned_shops(self):
        second_shop = Shop.objects.create(name="Second Staff Access Shop", owner=self.owner)
        third_shop = Shop.objects.create(name="Third Staff Access Shop", owner=self.owner)
        history_shop = Shop.objects.create(name="Historical Staff Access Shop", owner=self.owner)
        employee = User.objects.create_user(username="shop_access_employee")
        current_membership = ShopStaff.objects.create(
            shop=self.shop,
            user=employee,
            role="PACKER",
            can_pack_orders=True,
        )
        removed_membership = ShopStaff.objects.create(
            shop=second_shop,
            user=employee,
            role="ORDER",
        )
        historical_membership = ShopStaff.objects.create(
            shop=history_shop,
            user=employee,
            role="DELIVERY",
        )
        ShopStaffWallet.objects.create(
            staff=historical_membership,
            balance=Decimal("12.00"),
        )

        self.client.force_login(self.owner)
        edit_url = reverse(
            "shop_staff_edit",
            kwargs={
                "shop_id": self.shop.pk,
                "staff_id": current_membership.pk,
            },
        )
        form_response = self.client.get(edit_url)

        self.assertEqual(form_response.status_code, 200)
        self.assertContains(form_response, "Shop access")
        self.assertContains(form_response, "Second Staff Access Shop")
        self.assertContains(form_response, "Role in Third Staff Access Shop")

        response = self.client.post(
            edit_url,
            {
                "accessible_shop_ids": [
                    str(self.shop.pk),
                    str(third_shop.pk),
                ],
                f"shop_role_{third_shop.pk}": "PACKER",
                "role": "MANAGER",
                "can_pack_orders": "on",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        current_membership.refresh_from_db()
        self.assertEqual(current_membership.role, "MANAGER")
        self.assertFalse(
            ShopStaff.objects.filter(pk=removed_membership.pk).exists(),
        )
        historical_membership.refresh_from_db()
        self.assertFalse(historical_membership.is_active)
        new_membership = ShopStaff.objects.get(
            shop=third_shop,
            user=employee,
        )
        self.assertEqual(new_membership.role, "PACKER")
        self.assertTrue(new_membership.can_pack_orders)
        self.assertTrue(new_membership.is_active)

    def test_shop_settings_enable_public_riders_without_a_shop_rider(self):
        self.shop.public_rider_network_enabled = False
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=[
            "public_rider_network_enabled",
            "rider_delivery_preference",
        ])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("shop_settings", kwargs={"shop_id": self.shop.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.public_rider_network_enabled)
        self.assertEqual(self.shop.rider_delivery_preference, "PUBLIC")

    def test_ready_order_enables_public_dispatch_when_no_shop_rider_exists(self):
        self.shop.public_rider_network_enabled = False
        self.shop.rider_delivery_preference = "STAFF"
        self.shop.save(update_fields=[
            "public_rider_network_enabled",
            "rider_delivery_preference",
        ])
        order = Order.objects.create(
            order_id="WORKFLOW-PUBLIC-RIDER",
            user=self.customer.username,
            delivery_address=self.home.address,
            status="READY",
            shop=self.shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            total_amount=Decimal("10.00"),
            status="READY",
        )

        with patch("seller.views.advance_public_rider_offer") as advance_offer:
            routed = seller_views._route_ready_order_to_public_riders_if_no_staff(
                shop_order
            )

        self.assertTrue(routed)
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.public_rider_network_enabled)
        self.assertEqual(self.shop.rider_delivery_preference, "PUBLIC")
        shop_order.refresh_from_db()
        self.assertTrue(shop_order.public_rider_network_requested)
        advance_offer.assert_called_once_with(shop_order.pk)


class AdminUserPresenceTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="presence_customer",
            password="test-password",
        )
        ClientProfile.objects.create(
            user=self.customer,
            name="Presence Customer",
        )
        self.admin = User.objects.create_superuser(
            username="presence_admin",
            email="presence-admin@example.com",
            password="test-password",
        )

    def test_authenticated_heartbeat_saves_user_presence_and_portal(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.customer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()
        page = self.client.get(reverse("client_profile"))
        self.assertContains(page, reverse("user_presence_heartbeat"))

        response = self.client.post(
            reverse("user_presence_heartbeat"),
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        presence = UserPresence.objects.get(user=self.customer)
        self.assertEqual(presence.active_portal, "B2B_CUSTOMER")
        self.assertLess((timezone.now() - presence.last_seen).total_seconds(), 5)

    def test_heartbeat_rejects_non_post_requests(self):
        self.client.force_login(self.customer)

        self.assertEqual(
            self.client.get(reverse("user_presence_heartbeat")).status_code,
            405,
        )

    def test_admin_dashboard_shows_presence_role_and_login_metrics(self):
        self.client.force_login(self.customer)
        UserPresence.objects.create(
            user=self.customer,
            last_seen=timezone.now(),
            active_portal="CUSTOMER",
        )
        stale_user = User.objects.create_user(
            username="stale_presence_user",
            password="test-password",
        )
        UserPresence.objects.create(
            user=stale_user,
            last_seen=timezone.now() - timedelta(minutes=6),
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_user_presence_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["online_count"], 1)
        self.assertEqual(response.context["logged_in_today_count"], 2)
        self.assertEqual(response.context["last_login_today_count"], 2)
        customer_role = next(
            row for row in response.context["role_rows"]
            if row["role"] == "Customer"
        )
        self.assertEqual(customer_role["total"], 1)
        self.assertEqual(customer_role["online"], 1)
        stale_row = next(
            row for row in response.context["presence_rows"]
            if row["user"] == stale_user
        )
        self.assertFalse(stale_row["is_online"])
        self.assertContains(response, "User presence &amp; logins")
        self.assertContains(response, "presence_customer")

    def test_non_admin_cannot_open_admin_presence_dashboard(self):
        self.client.force_login(self.customer)

        response = self.client.get(reverse("admin_user_presence_dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_admin_panel_provides_buttons_to_admin_destinations(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_panel_dashboard"))

        self.assertEqual(response.status_code, 200)
        for field in response.context["public_rider_settings_form"]:
            self.assertContains(response, f'name="{field.html_name}"')
        for destination in (
            "admin_catalog_dashboard",
            "admin_user_presence_dashboard",
            "admin_catalog_product_create",
            "admin_catalog_product_export",
            "admin_site_theme_builder",
            "shop_list",
            "product_list",
            "shop_order_list",
            "client_list",
            "shared_rider_groups",
        ):
            self.assertContains(response, reverse(destination))
        self.assertContains(response, 'href="/admin/"')
        self.assertContains(response, "Public rider settings")
        self.assertContains(
            response,
            "public_rider_pickup_threshold_rate_per_km",
        )
        self.assertContains(
            response,
            reverse("admin:seller_deliverysharingsettings_changelist"),
        )

    def test_admin_panel_saves_public_rider_and_threshold_pay_settings(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("admin_panel_dashboard"),
            {
                "public_rider_service_enabled": "on",
                "max_additional_pickup_locations": "3",
                "shared_rider_radius_km": "0.35",
                "customer_direction_radius_km": "0.65",
                "public_rider_batch_radius_km": "1.0",
                "public_rider_decline_cooldown_minutes": "1.5",
                "public_rider_offer_timeout_seconds": "60",
                "public_rider_offer_riders_per_round": "5",
                "public_rider_max_active_offers": "2",
                "shared_rider_base_pay": "10.00",
                "shared_rider_rate_per_km": "10.00",
                "public_rider_pay_include_pickup_distance": "on",
                "public_rider_pickup_distance_pay_percentage": "100.00",
                "public_rider_pickup_threshold_bonus_enabled": "on",
                "public_rider_pickup_threshold_km": "1.732",
                "public_rider_pickup_threshold_rate_per_km": "4.00",
                "public_rider_pickup_threshold_distance_method": "STRAIGHT_LINE",
            },
        )

        self.assertRedirects(response, reverse("admin_panel_dashboard"))
        settings_obj = DeliverySharingSettings.objects.get(pk=1)
        self.assertTrue(settings_obj.public_rider_pickup_threshold_bonus_enabled)
        self.assertEqual(
            settings_obj.public_rider_pickup_threshold_km,
            Decimal("1.732"),
        )
        self.assertEqual(
            settings_obj.public_rider_pickup_threshold_rate_per_km,
            Decimal("4.00"),
        )
        self.assertEqual(
            settings_obj.public_rider_pickup_threshold_distance_method,
            "STRAIGHT_LINE",
        )

    def test_non_admin_cannot_open_admin_panel(self):
        self.client.force_login(self.customer)

        response = self.client.get(reverse("admin_panel_dashboard"))

        self.assertEqual(response.status_code, 403)


class StaffHRWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="staff_hr_owner",
            password="test-password",
        )
        self.employee = User.objects.create_user(
            username="staff_hr_employee",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            username="staff_hr_other",
            password="test-password",
        )
        self.shop = Shop.objects.create(
            name="Staff HR Shop",
            owner=self.owner,
        )
        self.staff = ShopStaff.objects.create(
            shop=self.shop,
            user=self.employee,
            role="PACKER",
        )

    def test_staff_clock_in_and_out_records_shift_and_caps_forgotten_clockout(self):
        self.client.force_login(self.employee)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()

        response = self.client.post(
            reverse("staff_duty_clock", kwargs={"staff_id": self.staff.pk}),
            {"action": "clock_in"},
        )
        self.assertRedirects(
            response,
            reverse("staff_hr_self_service", kwargs={"staff_id": self.staff.pk}),
        )
        duty_session = StaffDutySession.objects.get(staff=self.staff)
        self.assertIsNone(duty_session.ended_at)

        response = self.client.post(
            reverse("staff_duty_clock", kwargs={"staff_id": self.staff.pk}),
            {"action": "clock_out"},
        )
        self.assertEqual(response.status_code, 302)
        duty_session.refresh_from_db()
        self.assertIsNotNone(duty_session.ended_at)
        self.assertGreaterEqual(duty_session.ended_at, duty_session.started_at)

        old_session = StaffDutySession.objects.create(
            staff=self.staff,
            started_at=timezone.now() - timedelta(hours=17),
        )
        response = self.client.get(
            reverse("staff_hr_self_service", kwargs={"staff_id": self.staff.pk}),
        )
        self.assertEqual(response.status_code, 200)
        old_session.refresh_from_db()
        self.assertTrue(old_session.auto_closed)
        self.assertEqual(
            old_session.ended_at,
            old_session.started_at + timedelta(hours=16),
        )

    def test_shop_owner_calculates_monthly_pay_and_tracks_paid_status(self):
        now = timezone.localtime(timezone.now())
        month_start = date(now.year, now.month, 1)
        first_shift_start = timezone.make_aware(
            datetime.combine(month_start, datetime.min.time()),
        ) + timedelta(hours=9)
        second_shift_start = first_shift_start + timedelta(days=2)
        StaffDutySession.objects.create(
            staff=self.staff,
            started_at=first_shift_start,
            ended_at=first_shift_start + timedelta(hours=8),
        )
        StaffDutySession.objects.create(
            staff=self.staff,
            started_at=second_shift_start,
            ended_at=second_shift_start + timedelta(hours=8),
        )
        paid_leave_day = month_start + timedelta(days=1)
        StaffLeaveApplication.objects.create(
            staff=self.staff,
            start_date=paid_leave_day,
            end_date=paid_leave_day,
            reason="Approved personal leave",
            status=StaffLeaveApplication.STATUS_APPROVED,
            is_paid=True,
        )
        self.staff.salary_method = ShopStaff.SALARY_METHOD_MONTHLY_PRORATED
        self.staff.monthly_base_salary = Decimal("310.00")
        self.staff.completed_order_bonus = Decimal("5.00")
        self.staff.save(update_fields=[
            "salary_method",
            "monthly_base_salary",
            "completed_order_bonus",
        ])
        period = seller_views._staff_payroll_period(
            month_start.strftime("%Y-%m"),
        )
        calculated = seller_views._calculate_staff_payroll(self.staff, period)
        paid_days = min(
            period[-1],
            calculated["duty_days"] + calculated["paid_leave_days"],
        )
        expected_base = (
            Decimal("310.00") * Decimal(paid_days) / Decimal(period[-1])
        )
        self.assertEqual(calculated["duty_hours"], Decimal("16.00"))
        self.assertEqual(calculated["duty_days"], 2)
        self.assertEqual(calculated["paid_leave_days"], 1)
        self.assertEqual(
            calculated["gross_amount"],
            expected_base.quantize(Decimal("0.01")),
        )

        self.client.force_login(self.owner)
        dashboard_response = self.client.get(
            reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            {"month": month_start.strftime("%Y-%m")},
        )
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, "Staff attendance &amp; monthly payroll")
        response = self.client.post(
            reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            {
                "action": "calculate_one",
                "staff_id": str(self.staff.pk),
                "month": month_start.strftime("%Y-%m"),
            },
        )
        self.assertRedirects(
            response,
            f"{reverse('shop_staff_hr', kwargs={'shop_id': self.shop.pk})}?month={month_start:%Y-%m}",
        )
        payroll = StaffPayrollRecord.objects.get(staff=self.staff)
        self.assertEqual(payroll.gross_amount, calculated["gross_amount"])
        self.assertEqual(payroll.payment_status, StaffPayrollRecord.STATUS_UNPAID)

        response = self.client.post(
            reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            {
                "action": "set_payment_status",
                "record_id": str(payroll.pk),
                "payment_status": StaffPayrollRecord.STATUS_PAID,
                "month": month_start.strftime("%Y-%m"),
            },
        )
        self.assertEqual(response.status_code, 302)
        payroll.refresh_from_db()
        self.assertEqual(payroll.payment_status, StaffPayrollRecord.STATUS_PAID)
        self.assertEqual(payroll.paid_by, self.owner)
        self.assertIsNotNone(payroll.paid_at)

    def test_hourly_pay_excludes_paid_leave_and_owner_controls_salary_mode(self):
        start = timezone.now() - timedelta(hours=5)
        StaffDutySession.objects.create(
            staff=self.staff,
            started_at=start,
            ended_at=start + timedelta(hours=4),
        )
        today = timezone.localdate()
        StaffLeaveApplication.objects.create(
            staff=self.staff,
            start_date=today,
            end_date=today,
            reason="Paid leave",
            status=StaffLeaveApplication.STATUS_APPROVED,
            is_paid=True,
        )
        self.staff.salary_method = ShopStaff.SALARY_METHOD_HOURLY
        self.staff.hourly_rate = Decimal("10.00")
        self.staff.save(update_fields=["salary_method", "hourly_rate"])
        period = seller_views._staff_payroll_period(today.strftime("%Y-%m"))
        calculated = seller_views._calculate_staff_payroll(self.staff, period)
        self.assertEqual(calculated["gross_amount"], Decimal("40.00"))

        self.staff.salary_method = ShopStaff.SALARY_METHOD_MONTHLY_FIXED
        self.staff.monthly_base_salary = Decimal("500.00")
        self.staff.save(update_fields=["salary_method", "monthly_base_salary"])
        fixed = seller_views._calculate_staff_payroll(self.staff, period)
        self.assertEqual(fixed["gross_amount"], Decimal("500.00"))

    def test_staff_leave_resignation_notice_and_owner_approval(self):
        self.client.force_login(self.employee)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        today = timezone.localdate()
        resignation_url = reverse(
            "staff_hr_self_service",
            kwargs={"staff_id": self.staff.pk},
        )
        too_soon = self.client.post(
            resignation_url,
            {
                "action": "apply_resignation",
                "requested_last_working_date": (today + timedelta(days=14)).isoformat(),
                "reason": "Personal reasons",
            },
        )
        self.assertEqual(too_soon.status_code, 200)
        self.assertFalse(
            StaffResignationRequest.objects.filter(staff=self.staff).exists(),
        )

        leave_response = self.client.post(
            resignation_url,
            {
                "action": "apply_leave",
                "start_date": (today + timedelta(days=3)).isoformat(),
                "end_date": (today + timedelta(days=4)).isoformat(),
                "reason": "Family requirement",
            },
        )
        self.assertEqual(leave_response.status_code, 302)
        self.assertEqual(
            StaffLeaveApplication.objects.get(staff=self.staff).status,
            StaffLeaveApplication.STATUS_PENDING,
        )
        leave = StaffLeaveApplication.objects.get(staff=self.staff)
        self.client.force_login(self.owner)
        owner_dashboard = self.client.get(
            reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            {"month": today.strftime("%Y-%m")},
        )
        self.assertEqual(owner_dashboard.status_code, 200)
        leave_review = self.client.post(
            reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            {
                "action": "review_leave",
                "leave_id": str(leave.pk),
                "status": StaffLeaveApplication.STATUS_APPROVED,
                "is_paid": "on",
                "month": today.strftime("%Y-%m"),
            },
        )
        self.assertEqual(leave_review.status_code, 302)
        leave.refresh_from_db()
        self.assertEqual(leave.status, StaffLeaveApplication.STATUS_APPROVED)
        self.assertTrue(leave.is_paid)

        self.client.force_login(self.employee)
        session = self.client.session
        session["active_portal"] = "SHOP_STAFF"
        session.save()
        last_day = today + timedelta(days=15)
        resignation_response = self.client.post(
            resignation_url,
            {
                "action": "apply_resignation",
                "requested_last_working_date": last_day.isoformat(),
                "reason": "Personal reasons",
            },
        )
        self.assertEqual(resignation_response.status_code, 302)
        resignation = StaffResignationRequest.objects.get(staff=self.staff)
        self.client.force_login(self.owner)
        review = self.client.post(
            reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            {
                "action": "review_resignation",
                "resignation_id": str(resignation.pk),
                "status": StaffResignationRequest.STATUS_APPROVED,
                "month": today.strftime("%Y-%m"),
            },
        )
        self.assertEqual(review.status_code, 302)
        resignation.refresh_from_db()
        self.assertEqual(resignation.status, StaffResignationRequest.STATUS_APPROVED)
        self.assertTrue(self.staff.is_active)

        with patch(
            "seller.views.timezone.localdate",
            return_value=last_day + timedelta(days=1),
        ):
            seller_views._complete_due_staff_resignations()
        resignation.refresh_from_db()
        self.staff.refresh_from_db()
        self.assertEqual(resignation.status, StaffResignationRequest.STATUS_COMPLETED)
        self.assertFalse(self.staff.is_active)

    def test_staff_hr_pages_are_scoped_to_employee_and_shop_owner(self):
        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(
                reverse(
                    "staff_hr_self_service",
                    kwargs={"staff_id": self.staff.pk},
                ),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("shop_staff_hr", kwargs={"shop_id": self.shop.pk}),
            ).status_code,
            404,
        )


class ShopPointOfSaleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="pos_owner",
            password="test-password",
        )
        self.staff_user = User.objects.create_user(
            username="pos_staff",
            password="test-password",
        )
        self.no_order_access_user = User.objects.create_user(
            username="pos_no_orders",
            password="test-password",
        )
        self.shop = Shop.objects.create(name="POS Shop", owner=self.owner)
        self.staff = ShopStaff.objects.create(
            shop=self.shop,
            user=self.staff_user,
            role="ORDER",
            can_view_orders=True,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="POS Apples",
            product_category="FRUITS",
            price=Decimal("12.00"),
            unit="pcs",
        )
        self.assignment = ProductShop.objects.create(
            product=self.product,
            shop=self.shop,
            price=Decimal("12.00"),
            stock=Decimal("5.0000"),
            is_active=True,
        )
        self.pos_url = reverse(
            "shop_point_of_sale",
            kwargs={"shop_id": self.shop.pk},
        )

    def _save_cart(self, quantity):
        session = self.client.session
        session[f"shop_pos_cart_{self.shop.pk}"] = {
            str(self.assignment.pk): quantity,
        }
        session.save()

    def test_customer_bottom_navigation_opens_product_qr_scanner(self):
        customer = User.objects.create_user(username="pos_qr_customer")
        self.client.force_login(customer)

        page_url = reverse("terms")
        response = self.client.get(page_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-sellerhelper-scan-order="customer-product"')
        self.assertContains(response, "Scan product QR")
        self.assertContains(response, 'sellerhelper:open-qr-scanner')
        self.assertContains(response, '"customer-product"')
        self.assertContains(response, "legacyProductCode")
        self.assertContains(
            response,
            "`/shop/${legacyProductCode[1]}/products/${legacyProductCode[2]}/qr/`",
        )
        self.assertContains(response, "new URL(productPath, window.location.origin)")

        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()
        b2b_response = self.client.get(page_url)
        self.assertContains(
            b2b_response,
            'data-sellerhelper-scan-order="customer-product"',
        )

        owner = User.objects.create_user(username="pos_qr_owner")
        ClientProfile.objects.create(user=owner, name="Owner profile")
        Shop.objects.create(name="QR Owner Shop", owner=owner)
        self.client.force_login(owner)
        owner_session = self.client.session
        owner_session["active_portal"] = "SHOP_OWNER"
        owner_session.save()
        owner_response = self.client.get(page_url)
        self.assertNotContains(
            owner_response,
            'data-sellerhelper-scan-order="customer-product"',
        )

    def test_pos_page_adds_and_updates_a_product_in_the_staff_cart(self):
        self.client.force_login(self.staff_user)
        page = self.client.get(self.pos_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "POS Apples")
        self.assertContains(page, "Home delivery")
        self.assertContains(page, "Use camera")
        self.assertContains(page, 'purpose: "counter-sale-product"')
        self.assertContains(page, 'scanForm.requestSubmit()')
        self.assertContains(page, "Print QR")

        added = self.client.post(
            self.pos_url,
            {
                "action": "add_item",
                "product_shop_id": str(self.assignment.pk),
                "quantity": "2",
            },
        )
        self.assertRedirects(added, self.pos_url)
        self.assertEqual(
            self.client.session[f"shop_pos_cart_{self.shop.pk}"],
            {str(self.assignment.pk): 2},
        )

        changed = self.client.post(
            self.pos_url,
            {
                "action": "set_quantity",
                "product_shop_id": str(self.assignment.pk),
                "quantity": "1",
            },
        )
        self.assertRedirects(changed, self.pos_url)
        self.assertEqual(
            self.client.session[f"shop_pos_cart_{self.shop.pk}"],
            {str(self.assignment.pk): 1},
        )

    def test_scanning_shop_product_qr_adds_one_unit_and_rejects_other_shop_codes(self):
        self.client.force_login(self.staff_user)
        scan_payload = f"sellerhelper-pos:{self.shop.pk}:{self.assignment.pk}"

        first_scan = self.client.post(
            self.pos_url,
            {"action": "scan_item", "scan_code": scan_payload},
        )
        self.assertRedirects(first_scan, self.pos_url)
        self.assertEqual(
            self.client.session[f"shop_pos_cart_{self.shop.pk}"],
            {str(self.assignment.pk): 1},
        )

        self.client.post(
            self.pos_url,
            {"action": "scan_item", "scan_code": scan_payload},
        )
        self.assertEqual(
            self.client.session[f"shop_pos_cart_{self.shop.pk}"],
            {str(self.assignment.pk): 2},
        )

        rejected_scan = self.client.post(
            self.pos_url,
            {
                "action": "scan_item",
                "scan_code": f"sellerhelper-pos:{self.shop.pk + 1}:{self.assignment.pk}",
            },
        )
        self.assertRedirects(rejected_scan, self.pos_url)
        self.assertEqual(
            self.client.session[f"shop_pos_cart_{self.shop.pk}"],
            {str(self.assignment.pk): 2},
        )

    def test_staff_can_print_only_product_qr_labels_from_their_shop(self):
        self.client.force_login(self.staff_user)
        label_url = reverse(
            "shop_product_qr_label",
            kwargs={
                "shop_id": self.shop.pk,
                "product_shop_id": self.assignment.pk,
            },
        )

        response = self.client.get(label_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "POS Apples")
        self.assertContains(response, "Print 1 label")
        self.assertEqual(
            response.context["qr_payload"],
            f"http://testserver{reverse('shop_product_qr_detail', kwargs={'shop_id': self.shop.pk, 'product_shop_id': self.assignment.pk})}",
        )
        self.assertTrue(
            response.context["qr_code_data_uri"].startswith("data:image/png;base64,"),
        )
        self.assertContains(response, ".site-layout > :not(.main-container)")
        self.assertNotContains(response, "body > *:not(.main-container)")

        other_shop = Shop.objects.create(name="Other POS Shop", owner=self.owner)
        other_assignment = ProductShop.objects.create(
            product=self.product,
            shop=other_shop,
            price=Decimal("12.00"),
            stock=Decimal("2.0000"),
            is_active=True,
        )
        cross_shop_label = reverse(
            "shop_product_qr_label",
            kwargs={
                "shop_id": self.shop.pk,
                "product_shop_id": other_assignment.pk,
            },
        )
        self.assertEqual(self.client.get(cross_shop_label).status_code, 404)

    def test_product_qr_print_settings_repeat_labels_and_set_dimensions_and_page_size(self):
        self.client.force_login(self.staff_user)
        label_url = reverse(
            "shop_product_qr_label",
            kwargs={
                "shop_id": self.shop.pk,
                "product_shop_id": self.assignment.pk,
            },
        )

        response = self.client.get(
            label_url,
            {
                "label_count": "4",
                "label_width_mm": "40",
                "label_height_mm": "55",
                "qr_size_mm": "20",
                "paper_size": "A5",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["label_count"], 4)
        self.assertEqual(len(response.context["label_entries"]), 4)
        self.assertEqual(response.context["label_width_mm"], Decimal("4E+1"))
        self.assertEqual(response.context["label_height_mm"], Decimal("55"))
        self.assertEqual(response.context["qr_size_mm"], Decimal("2E+1"))
        self.assertEqual(response.context["paper_width_mm"], Decimal("148"))
        self.assertEqual(response.context["paper_height_mm"], Decimal("210"))
        self.assertContains(response, "Print 4 labels")
        self.assertContains(response, "40mm")
        self.assertContains(response, "55mm")
        self.assertContains(response, "20mm")
        self.assertContains(response, "148mm 210mm")
        self.assertContains(response, 'alt="QR code for POS Apples"', count=4)

        custom_page = self.client.get(
            label_url,
            {
                "paper_size": "CUSTOM",
                "paper_width_mm": "100",
                "paper_height_mm": "150",
            },
        )
        self.assertEqual(custom_page.status_code, 200)
        self.assertEqual(custom_page.context["paper_width_mm"], Decimal("1E+2"))
        self.assertEqual(custom_page.context["paper_height_mm"], Decimal("1.5E+2"))
        self.assertContains(custom_page, "100mm 150mm")

    def test_all_product_qr_labels_print_one_per_active_shop_product(self):
        self.client.force_login(self.staff_user)
        second_product = Product.objects.create(
            created_by=self.owner,
            product_name="POS Pears",
            product_category="FRUITS",
            price=Decimal("15.00"),
            unit="pcs",
        )
        second_assignment = ProductShop.objects.create(
            product=second_product,
            shop=self.shop,
            price=Decimal("15.00"),
            stock=Decimal("0.0000"),
            is_active=True,
        )
        inactive_product = Product.objects.create(
            created_by=self.owner,
            product_name="Inactive POS Product",
            product_category="OTHER",
            price=Decimal("1.00"),
            unit="pcs",
            is_active=False,
        )
        ProductShop.objects.create(
            product=inactive_product,
            shop=self.shop,
            price=Decimal("1.00"),
            stock=Decimal("1.0000"),
            is_active=True,
        )
        all_labels_url = reverse(
            "shop_product_qr_labels",
            kwargs={"shop_id": self.shop.pk},
        )

        response = self.client.get(
            all_labels_url,
            {"label_width_mm": "45", "label_height_mm": "60", "qr_size_mm": "25"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["all_products"])
        self.assertEqual(response.context["label_count"], 2)
        self.assertEqual(len(response.context["label_entries"]), 2)
        self.assertEqual(
            {
                entry["qr_payload"]
                for entry in response.context["label_entries"]
            },
            {
                f"http://testserver{reverse('shop_product_qr_detail', kwargs={'shop_id': self.shop.pk, 'product_shop_id': self.assignment.pk})}",
                f"http://testserver{reverse('shop_product_qr_detail', kwargs={'shop_id': self.shop.pk, 'product_shop_id': second_assignment.pk})}",
            },
        )
        self.assertEqual(response.context["qr_size_mm"], Decimal("25"))
        self.assertContains(response, "Print 2 labels")
        self.assertContains(response, "one for each active product")
        self.assertContains(response, 'alt="QR code for POS Apples"')
        self.assertContains(response, 'alt="QR code for POS Pears"')
        self.assertNotContains(response, "Inactive POS Product")

    def test_public_qr_url_displays_live_shop_listing_details_for_customers(self):
        detail_url = reverse(
            "shop_product_qr_detail",
            kwargs={
                "shop_id": self.shop.pk,
                "product_shop_id": self.assignment.pk,
            },
        )

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "POS Apples")
        self.assertContains(response, "POS Shop")
        self.assertContains(response, "₹12.00")
        self.assertContains(response, "5 available")
        self.assertEqual(response.context["price"], Decimal("12.00"))
        self.assertEqual(response.context["stock"], Decimal("5.0000"))

    def test_public_qr_detail_displays_multiple_listing_images_in_a_gallery(self):
        self.shop.allow_product_image_overrides = True
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.assignment.image_url_override = "https://example.com/pos-apples-front.jpg"
        self.assignment.image_url_override_2 = "https://example.com/pos-apples-back.jpg"
        self.assignment.save(
            update_fields=["image_url_override", "image_url_override_2"],
        )

        response = self.client.get(
            reverse(
                "shop_product_qr_detail",
                kwargs={
                    "shop_id": self.shop.pk,
                    "product_shop_id": self.assignment.pk,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["image_urls"],
            [
                "https://example.com/pos-apples-front.jpg",
                "https://example.com/pos-apples-back.jpg",
            ],
        )
        self.assertContains(response, 'id="qrProductImageCarousel"')
        self.assertContains(response, "Next product image")
        self.assertContains(response, "Show product image 2")
        self.assertContains(response, 'src="https://example.com/pos-apples-front.jpg"')
        self.assertContains(response, 'src="https://example.com/pos-apples-back.jpg"')

    def test_b2b_customer_can_add_bulk_offer_from_qr_product_detail(self):
        buyer = User.objects.create_user(username="pos_qr_b2b_customer")
        ClientProfile.objects.create(
            user=buyer,
            name="QR B2B Customer",
            gstin="27ABCDE1234F1Z5",
        )
        self.shop.gstin = "24ABCDE1234F1Z5"
        self.shop.is_shop_open = True
        self.shop.save(update_fields=["gstin", "is_shop_open"])
        self.assignment.b2b_price = Decimal("10.00")
        self.assignment.b2b_bulk_price = Decimal("8.00")
        self.assignment.b2b_bulk_unit = "case"
        self.assignment.bulk_min_quantity = 3
        self.assignment.b2b_stock = Decimal("5.0000")
        self.assignment.save(
            update_fields=[
                "b2b_price",
                "b2b_bulk_price",
                "b2b_bulk_unit",
                "bulk_min_quantity",
                "b2b_stock",
            ],
        )
        self.client.force_login(buyer)
        session = self.client.session
        session["active_portal"] = "B2B_CUSTOMER"
        session.save()
        detail_url = reverse(
            "shop_product_qr_detail",
            kwargs={
                "shop_id": self.shop.pk,
                "product_shop_id": self.assignment.pk,
            },
        )

        page = self.client.get(detail_url)

        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["is_b2b"])
        self.assertTrue(page.context["has_bulk_offer"])
        self.assertEqual(page.context["bulk_price"], Decimal("8.00"))
        self.assertEqual(page.context["bulk_unit"], "case")
        self.assertEqual(page.context["bulk_min_quantity"], 3)
        self.assertContains(page, "B2B bulk offer")
        self.assertContains(page, 'name="offer" value="bulk"')
        self.assertContains(page, 'min="3"')
        self.assertContains(page, "Minimum 3 case")

        added = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"offer": "bulk", "quantity": "3"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(added.status_code, 200)
        self.assertTrue(added.json()["success"])
        self.assertEqual(added.json()["quantity"], 3)
        self.assertEqual(
            self.client.session["cart"][f"bulk:{self.assignment.pk}"],
            3,
        )
        updated_page = self.client.get(detail_url)
        self.assertEqual(updated_page.context["bulk_cart_quantity"], 3)
        self.assertContains(updated_page, "Update bulk")

    def test_customer_can_add_qr_product_to_cart_from_its_detail_page(self):
        customer = User.objects.create_user(username="pos_qr_cart_customer")
        ClientProfile.objects.create(user=customer, name="QR Cart Customer")
        self.client.force_login(customer)
        detail_url = reverse(
            "shop_product_qr_detail",
            kwargs={
                "shop_id": self.shop.pk,
                "product_shop_id": self.assignment.pk,
            },
        )

        page = self.client.get(detail_url)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Add to cart")
        self.assertContains(page, "Quantity in cart")
        self.assertContains(page, reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}))

        add_response = self.client.post(
            reverse("cart_update", kwargs={"product_shop_id": self.assignment.pk}),
            {"quantity": "2", "offer": "retail"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(add_response.status_code, 200)
        self.assertTrue(add_response.json()["success"])
        self.assertEqual(add_response.json()["quantity"], 2)
        self.assertEqual(self.client.session["cart"][str(self.assignment.pk)], 2)

        updated_page = self.client.get(detail_url)
        self.assertEqual(updated_page.context["cart_quantity"], 2)
        self.assertContains(updated_page, "Update cart")
        self.assertContains(updated_page, "2 in cart")

    def test_pos_camera_scan_accepts_the_public_product_qr_url(self):
        self.client.force_login(self.staff_user)
        product_qr_url = self.client.get(
            reverse(
                "shop_product_qr_label",
                kwargs={
                    "shop_id": self.shop.pk,
                    "product_shop_id": self.assignment.pk,
                },
            )
        ).context["qr_payload"]

        response = self.client.post(
            self.pos_url,
            {"action": "scan_item", "scan_code": product_qr_url},
        )

        self.assertRedirects(response, self.pos_url)
        self.assertEqual(
            self.client.session[f"shop_pos_cart_{self.shop.pk}"],
            {str(self.assignment.pk): 1},
        )

    def test_authorized_staff_can_record_paid_counter_sale_and_reduce_stock(self):
        self.client.force_login(self.staff_user)
        self._save_cart(2)

        response = self.client.post(
            self.pos_url,
            {
                "action": "place_sale",
                "fulfillment_type": ShopOrder.FULFILLMENT_COUNTER,
                "payment_type": "CASH",
                "customer_name": "Walk-in Buyer",
                "customer_phone": "1234567890",
            },
        )

        shop_order = ShopOrder.objects.get(shop=self.shop)
        order = shop_order.order
        self.assertRedirects(
            response,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(shop_order.fulfillment_type, ShopOrder.FULFILLMENT_COUNTER)
        self.assertEqual(shop_order.status, "DELIVERED")
        self.assertEqual(order.status, "DELIVERED")
        self.assertEqual(order.payment_status, "PAID")
        self.assertTrue(order.is_payment_done)
        self.assertEqual(order.payment_type, "CASH")
        self.assertEqual(order.customer_name, "Walk-in Buyer")
        self.assertEqual(order.created_by, self.staff_user)
        self.assertEqual(
            OrderItem.objects.get(shop_order=shop_order).quantity,
            2,
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, Decimal("3.0000"))
        self.assertEqual(
            StockMovement.objects.get(reference=order.order_id).quantity,
            Decimal("-2"),
        )
        self.assertEqual(
            OrderStatusHistory.objects.get(shop_order=shop_order).status,
            "DELIVERED",
        )
        order_detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertContains(order_detail, "Walk-in Buyer")
        self.assertContains(order_detail, "Customer carried from shop")
        order_list = self.client.get(
            reverse("shop_order_list"),
            {"shop": self.shop.pk},
        )
        self.assertEqual(order_list.status_code, 200)
        self.assertContains(order_list, "Counter sale")
        self.assertContains(order_list, "Walk-in Buyer")

    def test_home_delivery_captures_customer_location_and_enters_rider_flow(self):
        self.shop.delivery_fee = Decimal("20.00")
        self.shop.save(update_fields=["delivery_fee"])
        self.client.force_login(self.staff_user)
        self._save_cart(1)
        cart_page = self.client.get(self.pos_url)
        self.assertContains(cart_page, "₹32.00")

        response = self.client.post(
            self.pos_url,
            {
                "action": "place_sale",
                "fulfillment_type": ShopOrder.FULFILLMENT_DELIVERY,
                "payment_type": "UPI",
                "customer_name": "Delivery Buyer",
                "customer_phone": "1234567890",
                "delivery_address": "1 Market Road",
                "delivery_latitude": "23.0001",
                "delivery_longitude": "72.0001",
            },
        )

        shop_order = ShopOrder.objects.get(shop=self.shop)
        self.assertRedirects(
            response,
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk}),
        )
        self.assertEqual(shop_order.fulfillment_type, ShopOrder.FULFILLMENT_DELIVERY)
        self.assertEqual(shop_order.status, "PLACED")
        self.assertEqual(shop_order.delivery_fee, Decimal("20.00"))
        self.assertEqual(shop_order.order.status, "PLACED")
        self.assertEqual(shop_order.order.payment_status, "PAID")
        self.assertEqual(shop_order.order.payment_type, "UPI")
        self.assertEqual(shop_order.order.order_source, Order.SOURCE_POS)
        self.assertEqual(shop_order.order.delivery_address, "1 Market Road")
        self.assertEqual(shop_order.order.delivery_contact_no, "1234567890")
        self.assertEqual(shop_order.order.delivery_location, "23.0001,72.0001")
        self.assertTrue(
            Notification.objects.filter(
                user=self.owner,
                related_url=reverse(
                    "shop_order_detail",
                    kwargs={"shop_order_id": shop_order.pk},
                ),
            ).exists(),
        )
        shop_order.status = "DELIVERED"
        shop_order.delivery_completed_at = timezone.now()
        shop_order.save(update_fields=["status", "delivery_completed_at"])
        seller_views._credit_shop_wallet_for_delivery(shop_order)
        self.assertFalse(
            SiteWalletTransaction.objects.filter(shop_order=shop_order).exists(),
        )
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists(),
        )

    def test_unprivileged_staff_cannot_use_pos_and_out_of_stock_sale_is_rejected(self):
        ShopStaff.objects.create(
            shop=self.shop,
            user=self.no_order_access_user,
            role="PACKER",
            can_view_orders=False,
        )
        self.client.force_login(self.no_order_access_user)
        self.assertEqual(self.client.get(self.pos_url).status_code, 403)

        self.client.force_login(self.staff_user)
        self._save_cart(6)
        response = self.client.post(
            self.pos_url,
            {
                "action": "place_sale",
                "fulfillment_type": ShopOrder.FULFILLMENT_COUNTER,
                "payment_type": "CARD",
            },
        )
        self.assertRedirects(response, self.pos_url)
        self.assertFalse(ShopOrder.objects.filter(shop=self.shop).exists())
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.stock, Decimal("5.0000"))

    def test_delivery_respects_shop_pincode_coverage(self):
        self.shop.delivery_pincode = "123456"
        self.shop.save(update_fields=["delivery_pincode"])
        self.client.force_login(self.staff_user)
        self._save_cart(1)
        payload = {
            "action": "place_sale",
            "fulfillment_type": ShopOrder.FULFILLMENT_DELIVERY,
            "payment_type": "CASH",
            "customer_name": "Delivery Buyer",
            "customer_phone": "1234567890",
            "delivery_address": "1 Market Road",
            "delivery_latitude": "23.0001",
            "delivery_longitude": "72.0001",
            "delivery_pincode": "999999",
        }

        response = self.client.post(self.pos_url, payload)

        self.assertRedirects(response, self.pos_url)
        self.assertFalse(ShopOrder.objects.filter(shop=self.shop).exists())
        payload["delivery_pincode"] = "123456"
        accepted = self.client.post(self.pos_url, payload)
        self.assertEqual(accepted.status_code, 302)
        shop_order = ShopOrder.objects.get(shop=self.shop)
        self.assertEqual(shop_order.order.delivery_pincode, "123456")
