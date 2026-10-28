import hashlib
import hmac
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    ClientDeliveryAddress,
    ClientProfile,
    Order,
    OrderVoiceInstruction,
    Product,
    ProductShop,
    Shop,
    ShopStaff,
    ShopOrder,
)

User = get_user_model()


@override_settings(
    PLATFORM_CHARGE_FIXED="10.00",
    PLATFORM_CHARGE_PERCENT="0.1",
    RAZORPAY_CUSTOMER_FEE_PERCENT="2",
)
class OrderVoiceInstructionTests(TestCase):
    def setUp(self):
        self.audio_directory = tempfile.TemporaryDirectory()
        self.audio_storage = OrderVoiceInstruction._meta.get_field("audio_file").storage
        self.original_storage_location = self.audio_storage.base_location
        self.audio_storage.base_location = self.audio_directory.name
        self.audio_storage.__dict__.pop("location", None)
        self.addCleanup(self._restore_audio_storage)

        self.customer = User.objects.create_user(
            username="voice_instruction_customer",
            password="test-password",
        )
        self.owner = User.objects.create_user(
            username="voice_instruction_owner",
            password="test-password",
        )
        self.profile = ClientProfile.objects.create(
            user=self.customer,
            name="Voice Customer",
        )
        self.shop = Shop.objects.create(
            name="Voice Shop",
            owner=self.owner,
        )
        self.product = Product.objects.create(
            created_by=self.owner,
            product_name="Voice Item",
            price=Decimal("20.00"),
            unit="piece",
            product_category="OTHER",
        )
        self.assignment = ProductShop.objects.create(
            shop=self.shop,
            product=self.product,
            price=Decimal("20.00"),
            stock=5,
            is_active=True,
        )
        self.address = ClientDeliveryAddress.objects.create(
            client=self.profile,
            label="Home",
            address="Voice Road",
            is_default=True,
            is_active=True,
        )
        self._set_cart({str(self.assignment.pk): 1})
        self.client.force_login(self.customer)

    def _restore_audio_storage(self):
        self.audio_storage.base_location = self.original_storage_location
        self.audio_storage.__dict__.pop("location", None)
        self.audio_directory.cleanup()

    def _set_cart(self, cart):
        session = self.client.session
        session["cart"] = cart
        session.save()

    def _audio_upload(self):
        return SimpleUploadedFile(
            "customer-instruction.webm",
            b"sample voice data",
            content_type="audio/webm",
        )

    def test_checkout_attaches_private_voice_instruction_to_selected_shop(self):
        response = self.client.post(
            reverse("checkout"),
            {
                "address": self.address.pk,
                "payment_method": "COD",
                "order_instruction_shop": self.shop.pk,
                "order_instruction_text": "Please call when you arrive.",
                "order_instruction_audio": self._audio_upload(),
            },
        )

        self.assertEqual(response.status_code, 302)
        shop_order = ShopOrder.objects.get(order__user=self.customer.username)
        instruction = OrderVoiceInstruction.objects.get(shop_order=shop_order)
        self.assertEqual(instruction.shop_id, self.shop.pk)
        self.assertEqual(instruction.recipient, OrderVoiceInstruction.RECIPIENT_SHOP)
        self.assertEqual(instruction.instruction_text, "Please call when you arrive.")
        self.assertTrue(instruction.audio_file)
        self.assertFalse(
            str(instruction.audio_file.path).startswith(str(settings.MEDIA_ROOT))
        )

        self.client.force_login(self.owner)
        detail_response = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Customer instruction for shop team")
        self.assertContains(detail_response, "Please call when you arrive.")
        audio_response = self.client.get(
            reverse(
                "shop_order_instruction_audio",
                kwargs={"instruction_id": instruction.pk},
            )
        )
        self.assertEqual(audio_response.status_code, 200)
        self.assertEqual(b"".join(audio_response.streaming_content), b"sample voice data")
        self.assertEqual(audio_response["Cache-Control"], "private, no-store")

    def test_checkout_saves_independent_text_and_voice_instructions_per_shop(self):
        second_owner = User.objects.create_user(
            username="voice_instruction_second_owner",
        )
        second_shop = Shop.objects.create(
            name="Second Voice Shop",
            owner=second_owner,
        )
        second_product = Product.objects.create(
            created_by=second_owner,
            product_name="Second Voice Item",
            price=Decimal("30.00"),
            unit="piece",
            product_category="OTHER",
        )
        second_assignment = ProductShop.objects.create(
            shop=second_shop,
            product=second_product,
            price=Decimal("30.00"),
            stock=5,
            is_active=True,
        )
        self._set_cart({
            str(self.assignment.pk): 1,
            str(second_assignment.pk): 1,
        })

        checkout_page = self.client.get(reverse("checkout"))
        self.assertEqual(checkout_page.status_code, 200)
        self.assertContains(
            checkout_page,
            f'name="order_instruction_text_shop_{self.shop.pk}"',
        )
        self.assertContains(
            checkout_page,
            f'name="order_instruction_text_shop_{second_shop.pk}"',
        )
        response = self.client.post(
            reverse("checkout"),
            {
                "address": self.address.pk,
                "payment_method": "COD",
                f"order_instruction_text_shop_{self.shop.pk}": "Pack this shop order separately.",
                f"order_instruction_text_rider_{self.shop.pk}": "Call at the first entrance.",
                f"order_instruction_audio_shop_{self.shop.pk}": self._audio_upload(),
                f"order_instruction_text_shop_{second_shop.pk}": "Keep chilled products cold.",
                f"order_instruction_text_rider_{second_shop.pk}": "Use the side gate.",
                f"order_instruction_audio_rider_{second_shop.pk}": self._audio_upload(),
            },
        )

        self.assertEqual(response.status_code, 302)
        shop_orders = {
            shop_order.shop_id: shop_order
            for shop_order in ShopOrder.objects.filter(
                order__user=self.customer.username,
            )
        }
        self.assertEqual(set(shop_orders), {self.shop.pk, second_shop.pk})
        instructions_by_shop = {
            shop_id: {
                instruction.recipient: instruction
                for instruction in OrderVoiceInstruction.objects.filter(
                    shop_order=shop_order,
                )
            }
            for shop_id, shop_order in shop_orders.items()
        }
        self.assertEqual(
            instructions_by_shop[self.shop.pk][
                OrderVoiceInstruction.RECIPIENT_SHOP
            ].instruction_text,
            "Pack this shop order separately.",
        )
        self.assertEqual(
            instructions_by_shop[self.shop.pk][
                OrderVoiceInstruction.RECIPIENT_RIDER
            ].instruction_text,
            "Call at the first entrance.",
        )
        self.assertTrue(
            instructions_by_shop[self.shop.pk][
                OrderVoiceInstruction.RECIPIENT_SHOP
            ].audio_file
        )
        self.assertEqual(
            instructions_by_shop[second_shop.pk][
                OrderVoiceInstruction.RECIPIENT_SHOP
            ].instruction_text,
            "Keep chilled products cold.",
        )
        self.assertEqual(
            instructions_by_shop[second_shop.pk][
                OrderVoiceInstruction.RECIPIENT_RIDER
            ].instruction_text,
            "Use the side gate.",
        )
        self.assertTrue(
            instructions_by_shop[second_shop.pk][
                OrderVoiceInstruction.RECIPIENT_RIDER
            ].audio_file
        )
        for instructions in instructions_by_shop.values():
            for instruction in instructions.values():
                if instruction.audio_file:
                    self.addCleanup(
                        instruction.audio_file.storage.delete,
                        instruction.audio_file.name,
                    )

    def test_rider_instruction_is_only_shown_and_playable_by_assigned_rider(self):
        rider_user = User.objects.create_user(
            username="voice_instruction_rider",
        )
        rider = ShopStaff.objects.create(
            shop=self.shop,
            user=rider_user,
            role="RIDER",
            is_active=True,
        )
        response = self.client.post(
            reverse("checkout"),
            {
                "address": self.address.pk,
                "payment_method": "COD",
                "order_instruction_recipient": OrderVoiceInstruction.RECIPIENT_RIDER,
                "order_instruction_shop": self.shop.pk,
                "order_instruction_text": "Call me when you reach the gate.",
                "order_instruction_audio": self._audio_upload(),
            },
        )

        self.assertEqual(response.status_code, 302)
        shop_order = ShopOrder.objects.get(order__user=self.customer.username)
        shop_order.assigned_rider = rider
        shop_order.status = "READY"
        shop_order.save(update_fields=["assigned_rider", "status"])
        instruction = OrderVoiceInstruction.objects.get(shop_order=shop_order)
        self.assertEqual(instruction.recipient, OrderVoiceInstruction.RECIPIENT_RIDER)

        self.client.force_login(self.owner)
        shop_detail = self.client.get(
            reverse("shop_order_detail", kwargs={"shop_order_id": shop_order.pk})
        )
        self.assertEqual(shop_detail.status_code, 200)
        self.assertNotContains(shop_detail, "Call me when you reach the gate.")
        forbidden_audio = self.client.get(
            reverse(
                "shop_order_instruction_audio",
                kwargs={"instruction_id": instruction.pk},
            )
        )
        self.assertEqual(forbidden_audio.status_code, 403)

        self.client.force_login(rider_user)
        rider_dashboard = self.client.get(
            reverse("rider_dashboard"),
            {"view": "active"},
        )
        self.assertEqual(rider_dashboard.status_code, 200)
        self.assertContains(rider_dashboard, "Call me when you reach the gate.")
        self.assertContains(rider_dashboard, "Customer instruction for rider")
        rider_audio = self.client.get(
            reverse(
                "shop_order_instruction_audio",
                kwargs={"instruction_id": instruction.pk},
            )
        )
        self.assertEqual(rider_audio.status_code, 200)
        self.assertEqual(b"".join(rider_audio.streaming_content), b"sample voice data")

    def test_instruction_is_shop_scoped_and_other_shop_staff_cannot_listen(self):
        other_owner = User.objects.create_user(
            username="voice_instruction_other_owner",
            password="test-password",
        )
        other_shop = Shop.objects.create(
            name="Other Voice Shop",
            owner=other_owner,
        )
        other_product = Product.objects.create(
            created_by=other_owner,
            product_name="Other Voice Item",
            price=Decimal("15.00"),
            unit="piece",
            product_category="OTHER",
        )
        other_assignment = ProductShop.objects.create(
            shop=other_shop,
            product=other_product,
            price=Decimal("15.00"),
            stock=5,
            is_active=True,
        )
        self._set_cart({
            str(self.assignment.pk): 1,
            str(other_assignment.pk): 1,
        })

        response = self.client.post(
            reverse("checkout"),
            {
                "address": self.address.pk,
                "payment_method": "COD",
                "order_instruction_shop": other_shop.pk,
                "order_instruction_text": "Leave at the side door.",
                "order_instruction_audio": self._audio_upload(),
            },
        )
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(user=self.customer.username)
        selected_shop_order = ShopOrder.objects.get(order=order, shop=other_shop)
        unselected_shop_order = ShopOrder.objects.get(order=order, shop=self.shop)
        instruction = OrderVoiceInstruction.objects.get(shop_order=selected_shop_order)
        self.assertFalse(
            OrderVoiceInstruction.objects.filter(shop_order=unselected_shop_order).exists()
        )

        outsider = User.objects.create_user(
            username="voice_instruction_outsider",
            password="test-password",
        )
        self.client.force_login(outsider)
        forbidden_response = self.client.get(
            reverse(
                "shop_order_instruction_audio",
                kwargs={"instruction_id": instruction.pk},
            )
        )
        self.assertEqual(forbidden_response.status_code, 403)

    def test_audio_is_deleted_and_playback_expires_one_hour_after_delivery(self):
        self.client.post(
            reverse("checkout"),
            {
                "address": self.address.pk,
                "payment_method": "COD",
                "order_instruction_shop": self.shop.pk,
                "order_instruction_text": "Ring the bell.",
                "order_instruction_audio": self._audio_upload(),
            },
        )
        shop_order = ShopOrder.objects.get(order__user=self.customer.username)
        instruction = OrderVoiceInstruction.objects.get(shop_order=shop_order)
        file_name = instruction.audio_file.name
        shop_order.delivery_completed_at = timezone.now() - timedelta(
            hours=1,
            seconds=1,
        )
        shop_order.save(update_fields=["delivery_completed_at"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse(
                "shop_order_instruction_audio",
                kwargs={"instruction_id": instruction.pk},
            )
        )

        self.assertEqual(response.status_code, 404)
        instruction.refresh_from_db()
        self.assertFalse(instruction.audio_file)
        self.assertFalse(self.audio_storage.exists(file_name))
        self.assertEqual(instruction.instruction_text, "Ring the bell.")

    def test_cleanup_command_removes_expired_audio_files(self):
        order = Order.objects.create(
            order_id="VOICE-CLEANUP-ORDER",
            user=self.customer.username,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=self.shop,
            delivery_completed_at=timezone.now() - timedelta(hours=1, seconds=1),
        )
        instruction = OrderVoiceInstruction.objects.create(
            customer=self.customer,
            shop=self.shop,
            shop_order=shop_order,
            instruction_text="Keep the written note.",
        )
        instruction.audio_file.save(
            "cleanup-test.webm",
            ContentFile(b"old audio"),
            save=True,
        )
        file_name = instruction.audio_file.name

        call_command("cleanup_order_voice_instructions", stdout=Mock())

        instruction.refresh_from_db()
        self.assertFalse(instruction.audio_file)
        self.assertEqual(instruction.instruction_text, "Keep the written note.")
        self.assertFalse(self.audio_storage.exists(file_name))

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_voice",
        RAZORPAY_KEY_SECRET="voice-test-secret",
    )
    def test_razorpay_verification_keeps_the_uploaded_instruction(self):
        payment_order_response = Mock()
        payment_order_response.raise_for_status.return_value = None

        def create_payment_order(*args, **kwargs):
            payment_order_response.json.return_value = {
                "id": "order_voice_instruction",
                "amount": kwargs["json"]["amount"],
            }
            return payment_order_response

        with patch(
            "seller.views.requests.post",
            side_effect=create_payment_order,
        ):
            checkout_response = self.client.post(
                reverse("checkout"),
                {
                    "address": self.address.pk,
                    "payment_method": "RAZORPAY",
                    "use_wallet_balance": "0",
                    "order_instruction_shop": self.shop.pk,
                    "order_instruction_text": "Call before delivery.",
                    "order_instruction_audio": self._audio_upload(),
                },
            )
        self.assertEqual(checkout_response.status_code, 200)
        intent = self.client.session["razorpay_checkout_intent"]

        payment_id = "pay_voiceinstruction123"
        signature = hmac.new(
            b"voice-test-secret",
            f"order_voice_instruction|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        payment_response = Mock()
        payment_response.json.return_value = {
            "order_id": "order_voice_instruction",
            "amount": intent["amount_paise"],
            "currency": "INR",
            "status": "captured",
        }
        payment_response.raise_for_status.return_value = None
        with patch("seller.views.requests.get", return_value=payment_response):
            verify_response = self.client.post(
                reverse("verify_razorpay_payment"),
                {
                    "razorpay_order_id": "order_voice_instruction",
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature,
                },
            )

        self.assertEqual(verify_response.status_code, 302)
        shop_order = ShopOrder.objects.get(order__user=self.customer.username)
        instruction = OrderVoiceInstruction.objects.get(shop_order=shop_order)
        self.assertEqual(instruction.instruction_text, "Call before delivery.")
        self.assertTrue(instruction.audio_file)
