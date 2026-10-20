from datetime import datetime, time, timedelta
from decimal import Decimal

import json
import hashlib
import hmac
import tempfile
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase, override_settings
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
    FavoriteShop,
    FavoriteProduct,
    MarketplaceLayoutSettings,
    SiteBrandingSettings,
    LegalAcceptance,
    Shop,
    ShopStaff,
    JobApplication,
    JobOpening,
    ClientProfile,
    ClientDeliveryAddress,
    ClientWalletTransaction,
    Product,
    ProductCategory,
    ProductShop,
    Order,
    OrderItem,
    OrderStatusHistory,
    ShopOrder,
    Notification,
    NotificationDeviceToken,
    StockMovement,
    DeliverySharingSettings,
    SharedRiderGroup,
    SharedRiderGroupShop,
    ShopProductForecast,
    ShopWallet,
    SiteWallet,
    SiteWalletTransaction,
    ShopPayout,
    ShopWalletTransaction,
    RiderCashLedgerEntry,
    SharedRiderDeliveryPayment,
    CODBalancePayment,
    ShopStaffWallet,
    ShopStaffWalletTransaction,
    Coupon,
    SavedBasket,
    SavedBasketItem,
)
from . import views as seller_views

User = get_user_model()


class SellerProjectConfigTests(TestCase):
    def test_secret_key_is_not_default_placeholder(self):
        self.assertNotIn("django-insecure", settings.SECRET_KEY.lower())

    def test_default_auto_field_is_configured(self):
        self.assertEqual(settings.DEFAULT_AUTO_FIELD, "django.db.models.BigAutoField")

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
        with patch("seller.live_updates._send_user_event") as send_event:
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

        self.client.force_login(user)
        for query in (
            "spinach",
            "SPINACH-250",
            "250 g",
            "SABJI",
            "Green Basket",
            "garbanzo",
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

    def test_checkout_uses_wallet_and_notifies_shop_staff(self):
        user = User.objects.create_user(username="wallet_customer", password="pass1234")
        client_profile = ClientProfile.objects.create(user=user, name="Wallet Customer", wallet_balance="100.00")
        address = ClientDeliveryAddress.objects.create(client=client_profile, label="Home", address="Wallet Road", location={"lat": 1, "lng": 2}, is_default=True)
        owner = User.objects.create_user(username="wallet_owner", password="pass1234")
        staff_user = User.objects.create_user(username="wallet_staff", password="pass1234")
        shop = Shop.objects.create(name="Wallet Shop", owner=owner, location={"lat": 1, "lng": 2})
        ShopStaff.objects.create(shop=shop, user=staff_user, role="PACKER", is_active=True)
        product = Product.objects.create(created_by=owner, product_name="Wallet Item", price="40.00", unit="piece", product_category="OTHER")
        assignment = ProductShop.objects.create(shop=shop, product=product, price="40.00", stock=5, is_active=True)

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

        self.client.force_login(staff_user)
        response = self.client.get(reverse("staff_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned Shop")
        self.assertContains(response, "data-sellerhelper-scan-order")

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
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ShopStaff.objects.filter(shop=shop, user__username="new_rider").exists())

    def test_shop_owner_can_edit_gst_registration_details(self):
        owner = User.objects.create_user(username="shop_gst_settings_owner", password="test-password")
        shop = Shop.objects.create(name="Non GST Shop", owner=owner)
        self.client.force_login(owner)

        edit_response = self.client.get(
            reverse("shop_edit", kwargs={"pk": shop.pk})
        )
        self.assertEqual(edit_response.status_code, 200)
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

    def test_owner_can_assign_specific_rider_to_order(self):
        owner = User.objects.create_user(username="owner_rider_assign", password="pass1234")
        rider_user = User.objects.create_user(username="rider_assignee", password="pass1234")
        shop = Shop.objects.create(name="Rider Shop", owner=owner)
        rider_staff = ShopStaff.objects.create(shop=shop, user=rider_user, role="RIDER", is_active=True)
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

    def test_rider_can_accept_unassigned_order_when_no_order_accepted_yet(self):
        owner = User.objects.create_user(username="owner_accept_order", password="pass1234")
        rider_user = User.objects.create_user(username="rider_acceptor", password="pass1234")
        shop = Shop.objects.create(name="Accept Shop", owner=owner)
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
        heading_position = response_html.index("<h2 class=\"fw-bold mb-1\">Rider dashboard")
        self.assertLess(tabs_position, heading_position)
        self.assertContains(response, ">Available</span>")
        self.assertContains(response, ">Active</span>")

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
        self.client.force_login(rider_user)

        response = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 × 2pcs = 6pcs")
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
            max_load_weight_kg=Decimal("0.75"),
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
        self.assertContains(dashboard_response, "Current estimated load")

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
        self.assertContains(rider_dashboard, "pickup OTP to staff")
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
        self.assertContains(rider_dashboard, "data:image/png;base64,")

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
        shop_order_list = self.client.get(reverse("shop_order_list"))
        self.assertContains(shop_order_list, "Verify rider and confirm pickup")
        self.assertContains(shop_order_list, "shopPickupOtp")
        self.assertContains(shop_order_list, "Scan rider OTP QR")
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
            {"pickup_otp": f"SH-PICKUP:{renewed_otp}"},
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
        for parcel in same_order_parcels:
            parcel.refresh_from_db()
            self.assertEqual(parcel.status, "PICKED_UP")
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
        self.assertContains(blocked_dashboard, "Start delivery")
        self.assertContains(blocked_dashboard, "Parcels awaiting pickup stay unchanged.")
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
        self.assertContains(response, "Navigate all active orders")
        self.assertContains(response, "Navigate shop-to-shop")
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
        self.assertContains(response, "Navigate all accepted deliveries")
        self.assertContains(response, "Opening Google Maps for the first")

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
        ProductShop.objects.create(
            shop=shop,
            product=product,
            price="20.00",
            bulk_price="15.00",
            bulk_unit="box",
            stock=5,
            is_active=True,
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
            "const blockIndex = Math.floor(index / blockSize);",
        )
        self.assertContains(
            configured_response,
            "const indexInBlock = index % blockSize;",
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
            "const rowIndex = rowInBlock;",
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
        self.assertContains(response, reverse("delivery_address_default", kwargs={"pk": alternate_address.id}))

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

        all_response = self.client.get(reverse("notifications"), {"view": "all"})
        self.assertContains(all_response, "Earlier update")

        api_response = self.client.get(reverse("notifications_api"))
        self.assertEqual(api_response.status_code, 200)
        payload = json.loads(api_response.content)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertGreaterEqual(len(payload["items"]), 1)

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
        with patch(
            "seller.push_notifications.requests.post",
            return_value=push_response,
        ) as send_push:
            with self.captureOnCommitCallbacks(execute=True):
                Notification.objects.create(
                    user=user,
                    title="New shop order",
                    message="An order needs your attention.",
                    notification_type="ORDER",
                    related_url="/shop/orders/42/",
                )

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
        address = ClientDeliveryAddress.objects.create(
            client=client_profile,
            label="Home",
            address="Main Road, Ahmedabad",
            location={"lat": 23.0225, "lng": 72.5714},
            is_default=True,
            is_active=True,
        )

        session = self.client.session
        session["cart"] = {str(product_shop.id): 2}
        session.save()

        self.client.force_login(user)
        response = self.client.post(reverse("checkout"), {"address": address.id})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Order.objects.filter(user=user.username).exists())

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

        response = self.client.post(
            reverse("checkout"),
            {"address": address.id, "payment_method": "COD"},
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(user=buyer.username)
        shop_order = order.shop_orders.get()
        order_item = shop_order.order_items.get()
        self.assertEqual(order.gst_amount, Decimal("18.00"))
        self.assertEqual(order.final_billing_amount, Decimal("128.10"))
        self.assertEqual(shop_order.taxable_amount, Decimal("100.00"))
        self.assertEqual(shop_order.cgst_amount, Decimal("9.00"))
        self.assertEqual(shop_order.sgst_amount, Decimal("9.00"))
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
        self.assertEqual(shop_order.igst_amount, Decimal("18.00"))
        self.assertEqual(order.gst_amount, Decimal("18.00"))

        self.client.force_login(owner)
        invoice_response = self.client.get(
            reverse("packing_slip", kwargs={"shop_order_id": shop_order.id})
        )
        self.assertEqual(invoice_response.status_code, 200)
        self.assertContains(invoice_response, "Tax Invoice")
        self.assertContains(invoice_response, "24ABCDE1234F1Z5")
        self.assertContains(invoice_response, "₹118.00")
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
            is_default=True,
            is_active=True,
        )
        session = self.client.session
        session["cart"] = {str(assignment.id): 1}
        session.save()
        self.client.force_login(user)

        checkout_page = self.client.get(reverse("checkout"))

        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(checkout_page, 'value="RAZORPAY"')
        self.assertNotContains(checkout_page, 'value="RAZORPAY" disabled')
        self.assertRegex(
            checkout_page.content.decode(),
            r'id="razorpayFeeRow"[^>]*\bhidden\b',
        )
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
        self.assertContains(tracking_response, f"Tracking #{order.tracking_code}")
        self.assertContains(tracking_response, "Scheduled delivery")
        self.assertContains(
            tracking_response,
            "The shop will confirm the delivery timing.",
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
        packer_dashboard_response = self.client.get(
            reverse("packer_dashboard"),
            {"view": "active"},
        )
        self.assertContains(packer_dashboard_response, shop_order.order.order_id)
        self.assertContains(packer_dashboard_response, "Packer Customer Name")
        self.assertContains(packer_dashboard_response, "Mark ready for pickup")
        self.assertContains(packer_dashboard_response, "My active packing")

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
            order_id=f"PARTIAL-RETURN-{payment_type}",
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
            {"view": "completed"},
        )
        self.assertContains(rider_dashboard, "Total cash COD awaiting shop handover")
        self.assertContains(rider_dashboard, f"₹{cash_collection.amount:.2f}")

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

        self.assertContains(response, 'data-cod-qr="SHOP_UPI"')
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

        rider_dashboard = self.client.get(reverse("rider_dashboard"))
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
        self.assertContains(delivery_dashboard, "Paid to shop UPI")
        self.assertContains(delivery_dashboard, "Paid to my personal UPI")
        self.assertTrue(any(
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
        rider_dashboard = self.client.get(reverse("rider_dashboard"))
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

        response = self.client.post(
            reverse("complete_delivery", kwargs={"shop_order_id": shop_order.pk}),
            {
                "received_amount": str(order.payment_amount),
                "cod_payment_method": "SHOP_UPI",
            },
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertTrue(order.is_payment_done)
        self.assertFalse(
            RiderCashLedgerEntry.objects.filter(shop_order=shop_order).exists()
        )
        self.assertFalse(ShopStaffWallet.objects.filter(staff=self.rider).exists())
        payment = CODBalancePayment.objects.get(shop_order=shop_order)
        self.assertEqual(payment.amount, order.final_billing_amount)
        self.assertEqual(payment.received_by, self.rider_user)

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
            f"{reverse('rider_dashboard')}?view=active#order-{shop_order.pk}",
        )
        shop_order.refresh_from_db()
        self.assertFalse(shop_order.customer_otp_verified)
        self.assertFalse(
            ShopWalletTransaction.objects.filter(shop_order=shop_order).exists()
        )
        unverified_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertContains(unverified_dashboard, "Customer not verified")
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
        self.assertContains(payment_history, "Later COD payments recorded")
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
        shop_orders_response = self.client.get(reverse("shop_order_list"))
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

        regular_user = User.objects.create_user(
            username="catalog_workspace_user",
            password="test-password",
        )
        self.client.force_login(regular_user)
        response = self.client.get(reverse("admin_catalog_dashboard"))
        self.assertEqual(response.status_code, 403)

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
            {"name": "ORGANIC FOODS", "sort_order": "2"},
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
                "image_url": "",
                "hsn_code": "080510",
                "gst_rate": "5.00",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        product = AdminProductCatalog.objects.get(product_name="Workspace Orange")

        response = self.client.post(
            reverse(
                "admin_catalog_product_edit",
                kwargs={"pk": product.pk},
            ),
            {
                "product_category": "FRUITS",
                "product_name": "Edited Workspace Orange",
                "image_url": "",
                "hsn_code": "080510",
                "gst_rate": "12.00",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        product.refresh_from_db()
        self.assertEqual(product.product_name, "Edited Workspace Orange")
        self.assertEqual(product.gst_rate, Decimal("12.00"))

        response = self.client.post(
            reverse(
                "admin_catalog_product_toggle",
                kwargs={"pk": product.pk},
            ),
        )
        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        product.refresh_from_db()
        self.assertFalse(product.is_active)

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
            {"status": "active"},
        )
        inactive_response = self.client.get(
            reverse("admin_catalog_dashboard"),
            {"status": "inactive"},
        )

        self.assertEqual(active_response.status_code, 200)
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
            {"name": "LOCAL FRUIT", "sort_order": "3"},
        )

        self.assertRedirects(response, reverse("admin_catalog_dashboard"))
        category.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(category.name, "LOCAL FRUIT")
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
            b2b_stock=2,
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
        session.save()
        b2b_marketplace_response = self.client.get(reverse("marketplace"))
        self.assertTrue(b2b_marketplace_response.context["is_b2b_customer"])
        b2b_assignment = next(
            item for item in b2b_marketplace_response.context["products"]
            if item.pk == assignment.pk
        )
        self.assertEqual(b2b_assignment.marketplace_price, Decimal("12.50"))
        self.assertEqual(b2b_assignment.marketplace_stock, 2)
        self.assertEqual(b2b_assignment.marketplace_unit, "bag")
        self.assertTrue(b2b_assignment.has_b2b_price)
        self.assertEqual(b2b_assignment.bulk_marketplace_price, Decimal("70.00"))
        self.assertEqual(b2b_assignment.bulk_marketplace_unit, "carton")
        self.assertTrue(b2b_assignment.has_bulk_offer)
        self.assertContains(b2b_marketplace_response, "B2B price")
        self.assertContains(b2b_marketplace_response, "B2B bulk offer")
        self.assertContains(b2b_marketplace_response, "₹70.00 / carton")
        self.assertContains(b2b_marketplace_response, "Min 2 units")
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
                "shop_image_url": "https://example.com/shop-apples.png",
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
            self.assignment.display_image_url,
            "https://example.com/shop-apples.png",
        )
        self.shop.allow_product_image_overrides = False
        self.shop.save(update_fields=["allow_product_image_overrides"])
        self.assignment.refresh_from_db()
        self.assertEqual(
            self.assignment.display_image_url,
            "https://example.com/catalog-apples.png",
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
        with patch("seller.live_updates._send_user_event") as send_event:
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

    def test_owner_dashboard_summarizes_recent_orders_wallet_and_low_stock(self):
        shop_order = self.create_shop_order("ORD-OPERATIONS-DASHBOARD")
        ShopWallet.objects.create(shop=self.shop, balance=Decimal("75.25"))
        product = Product.objects.create(
            created_by=self.owner,
            product_name="Dashboard Low Stock",
            price=Decimal("9.00"),
        )
        ProductShop.objects.create(
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
