from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import ClientProfile, Order, OrderItem, Shop, ShopOrder


User = get_user_model()


class ShopCustomerAccountTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="customer_data_owner")
        self.other_owner = User.objects.create_user(username="different_shop_owner")
        self.customer_user = User.objects.create_user(
            username="repeat_customer",
            email="account@example.com",
        )
        ClientProfile.objects.create(
            user=self.customer_user,
            name="Asha Patel",
            contact_no="9876543210",
            email="asha@example.com",
            date_of_birth=date(1990, 1, 1),
            gender="Female",
        )
        self.shop = Shop.objects.create(name="Owner Shop One", owner=self.owner)
        self.second_shop = Shop.objects.create(name="Owner Shop Two", owner=self.owner)
        self.other_shop = Shop.objects.create(
            name="Other Owner Shop",
            owner=self.other_owner,
        )

    def create_purchase(self, shop, order_id, product_name, amount):
        order = Order.objects.create(
            order_id=order_id,
            user=self.customer_user.username,
            delivery_contact_no="1111111111",
            status="PLACED",
            shop=shop,
        )
        shop_order = ShopOrder.objects.create(
            order=order,
            shop=shop,
            total_amount=amount,
            status="PLACED",
        )
        OrderItem.objects.create(
            shop_order=shop_order,
            product_name=product_name,
            quantity=1,
            price=amount,
            total=amount,
        )
        return shop_order

    def test_owner_account_shows_unique_customer_across_owned_shops_only(self):
        self.create_purchase(
            self.shop,
            "CUSTOMER-ORDER-1",
            "Alphonso Mango",
            "120.00",
        )
        self.create_purchase(
            self.second_shop,
            "CUSTOMER-ORDER-2",
            "Fresh Milk",
            "80.00",
        )
        self.create_purchase(
            self.other_shop,
            "CUSTOMER-ORDER-3",
            "Other shop item",
            "50.00",
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("shop_customers"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["customer_count"], 1)
        customer = response.context["page"].object_list[0]
        self.assertEqual(customer["name"], "Asha Patel")
        self.assertEqual(customer["mobile"], "9876543210")
        self.assertEqual(customer["email"], "asha@example.com")
        self.assertEqual(customer["age"], timezone.localdate().year - 1990)
        self.assertEqual(customer["gender"], "Female")
        self.assertEqual(customer["order_count"], 2)
        self.assertEqual(customer["total_spent"], Decimal("200.00"))
        self.assertEqual(customer["product_names"], "Alphonso Mango, Fresh Milk")
        self.assertEqual(customer["shop_names"], "Owner Shop One, Owner Shop Two")
        self.assertNotIn("Other shop item", customer["product_names"])

    def test_customer_export_is_excel_compatible_csv_for_owned_customers(self):
        self.create_purchase(
            self.shop,
            "CUSTOMER-EXPORT-1",
            "Alphonso Mango",
            "120.00",
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("shop_customers_export"))
        content = response.content.decode("utf-8-sig")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("Customer name,Mobile,Email,Age,Gender", content)
        self.assertIn("Asha Patel,9876543210,asha@example.com", content)
        self.assertIn("Alphonso Mango", content)

    def test_customer_data_endpoints_are_limited_to_shop_owners(self):
        outsider = User.objects.create_user(username="customer_data_outsider")
        self.client.force_login(outsider)

        page_response = self.client.get(reverse("shop_customers"))
        export_response = self.client.get(reverse("shop_customers_export"))

        self.assertEqual(page_response.status_code, 404)
        self.assertEqual(export_response.status_code, 404)
