from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from sellerhelper.settings import _validated_session_cookie_domain

User = get_user_model()


class LoginSessionCookieTests(TestCase):
    def test_unmatched_cookie_domain_falls_back_to_host_only(self):
        self.assertIsNone(
            _validated_session_cookie_domain(
                ".stale-example.invalid",
                ["localhost", "127.0.0.1", "192.168.1.12"],
            )
        )

    def test_matching_parent_domain_is_preserved_for_storefront_sessions(self):
        self.assertEqual(
            _validated_session_cookie_domain(
                ".sellerhelper.example",
                ["sellerhelper.example", ".sellerhelper.example"],
            ),
            ".sellerhelper.example",
        )

    def test_successful_login_persists_session_for_next_request(self):
        user = User.objects.create_user(
            username="session_cookie_customer",
            password="session-cookie-password",
        )

        response = self.client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "session-cookie-password",
                "login_as": "CUSTOMER",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.cookies[settings.SESSION_COOKIE_NAME]["domain"],
            settings.SESSION_COOKIE_DOMAIN or "",
        )
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        next_response = self.client.get(reverse("home"))
        self.assertEqual(next_response.status_code, 200)
        self.assertTrue(self.client.session.get("_auth_user_id"))
